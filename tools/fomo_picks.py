"""Daily FOMO picks without Helius (owner request 2026-10-10): FOMO's own leaderboards + the bot's EXACT FOMO scoring.

Steps (the daily Claude task runs them; see tools/fomo_collect.js for step 1):
  1. in a signed-in fomo.family tab, tools/fomo_collect.js reads the 24h/7d/30d leaderboards and the last 100 swaps
     of every leaderboard trader (up to 400), and keeps the ones not clearly failing; each chunk is saved as
     reports/fomo/<day>/chunk_<n>.json
  2. uv run python tools/fomo_picks.py score  [day]  -> copybot.sol.scoring.full_score with config/sol.toml, unchanged
  3. uv run python tools/fomo_picks.py wallets [day] -> each pick's real Solana wallet (free public Solana endpoint)
  4. uv run python tools/fomo_picks.py report  [day] -> reports/fomo/<day>/report.md + reports/fomo/index.html,
     compared with the previous report (reports/fomo/latest.json)

FOMO shows only a trader's last 100 swaps. When a trader fails ONLY because that is too little history (fewer than
40 trades, under 14 active days or 21 days of history, under 3 winning weeks of 4), it is "promising": everything
else passes, and the bot's /fomoadd then checks its full 30 days on-chain with the same rules.
Read-only: no keys, no orders. Reports are git-ignored.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copybot import config
from copybot.sol import scoring
from copybot.sol.chain import Leg
from copybot.sol.fmt import reject_text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "fomo"
RPC = "https://api.mainnet-beta.solana.com"
FEE_PAYER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
HISTORY = ("trades<", "active_days<", "history<", "positive_weeks<")
TOP = 7


def today() -> str:
    cfg = config.load(ROOT / "config", env={})
    return (datetime.now(timezone.utc) + timedelta(hours=cfg.discord.utc_offset_hours)).strftime("%Y-%m-%d")


def day_dir(day: str) -> Path:
    d = OUT / day
    d.mkdir(parents=True, exist_ok=True)
    return d


def collected(day: str) -> dict:
    out: dict = {}
    for p in sorted(day_dir(day).glob("chunk_*.json")):
        out.update(json.loads(p.read_text(encoding="utf-8")))
    return out


def legs_of(t: dict) -> list[Leg]:
    toks = t["tokens"]
    return [Leg(f"{i}", int(ts) * 1000, toks[k], "buy" if buy else "sell", float(amt), float(usd))
            for i, (ts, k, buy, amt, usd) in enumerate(t["legs"])]


# ---- 2. score ------------------------------------------------------------------------------------------------
def score(day: str) -> dict:
    c = config.load(ROOT / "config", env={}).sol
    now = int(time.time() * 1000)
    res = {}
    for handle, t in collected(day).items():
        s = asdict(scoring.full_score(handle, legs_of(t), now, c))
        hist = [r for r in s["reasons"] if r.startswith(HISTORY)]
        s["status"] = "passes" if not s["reasons"] else ("promising" if len(hist) == len(s["reasons"]) else "fails")
        s.update(id=t["id"], pnl24h=t.get("pnl24h", 0), pnl7d=t.get("pnl7d", 0), pnl30d=t.get("pnl30d", 0))
        res[handle] = s
    (day_dir(day) / "scores.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    n = {k: sum(1 for s in res.values() if s["status"] == k) for k in ("passes", "promising", "fails")}
    print(f"scored {len(res)} traders: {n['passes']} pass, {n['promising']} promising (history too short on FOMO), "
          f"{n['fails']} fail")
    return res


def picks(res: dict) -> list[str]:
    order = {"passes": 0, "promising": 1}
    ok = [h for h, s in res.items() if s["status"] in order]
    return sorted(ok, key=lambda h: (order[res[h]["status"]], -res[h]["score"], h))[:TOP]


# ---- 3. wallets (free public Solana endpoint) -------------------------------------------------------------------
def rpc(method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(8):
        try:
            req = urllib.request.Request(RPC, body, {"Content-Type": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=40).read())
            if "result" in d:
                return d["result"]
        except Exception:
            pass
        time.sleep(2 + 2 * i)           # 429 / timeouts: back off
    return None


def owner_deltas(tx: dict, mint: str) -> dict:
    m = tx.get("meta") or {}
    ch: dict = {}
    for side, arr in (("pre", m.get("preTokenBalances") or []), ("post", m.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("mint") != mint:
                continue
            v = float((b.get("uiTokenAmount") or {}).get("uiAmountString") or 0)
            ch[b.get("owner")] = ch.get(b.get("owner"), 0.0) + (v if side == "post" else -v)
    return ch


def pages_needed(mint: str, ts: int) -> tuple[float, list]:
    """How many pages of 1000 signatures stand between now and `ts` for this token (busy memecoins: thousands), and
    the first page (reused by the search)."""
    res = rpc("getSignaturesForAddress", [mint, {"limit": 1000}]) or []
    if len(res) < 1000 or (res[-1].get("blockTime") or 0) <= ts - 600:
        return 1.0, res
    span = max((res[0].get("blockTime") or 0) - (res[-1].get("blockTime") or 0), 1)
    return 1 + ((res[-1]["blockTime"] - (ts - 600)) * 1000 / span) / 1000, res


def find_wallet(mint: str, amount: float, ts: int, buy: bool, first: list | None = None,
                max_pages: int = 12) -> str | None:
    """The signer (besides FOMO's fee payer) of a FOMO-co-signed transaction near `ts` whose balance of `mint` moved by
    exactly `amount`. FOMO's time can trail the chain by minutes, so the window is 10 min before to 1 min after."""
    lo, hi = ts - 600, ts + 60
    cands, before = [], None
    if first:
        cands += [x for x in first if x.get("blockTime") and lo <= x["blockTime"] <= hi and not x.get("err")]
        if len(first) < 1000 or (first[-1].get("blockTime") or 0) < lo:
            max_pages = 0
        before = first[-1]["signature"]
    for _ in range(max_pages):
        p = {"limit": 1000, **({"before": before} if before else {})}
        res = rpc("getSignaturesForAddress", [mint, p])
        if not res:
            break
        cands += [x for x in res if x.get("blockTime") and lo <= x["blockTime"] <= hi and not x.get("err")]
        before = res[-1]["signature"]
        if (res[-1].get("blockTime") or 0) < lo:
            break
        time.sleep(0.5)
    if not cands or len(cands) > 150:          # nothing seen, or a token too busy to search by hand
        return None
    for x in sorted(cands, key=lambda x: abs(x["blockTime"] - ts)):
        tx = rpc("getTransaction", [x["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
        time.sleep(0.6)
        if not tx:
            continue
        keys = tx["transaction"]["message"]["accountKeys"]
        signers = [k["pubkey"] for k in keys if k.get("signer")]
        if FEE_PAYER not in signers:
            continue
        d = owner_deltas(tx, mint)
        for s in signers:
            if s != FEE_PAYER and abs(d.get(s, 0.0) - (amount if buy else -amount)) <= max(abs(amount) * 1e-4, 1e-9):
                return s
    return None


def wallets(day: str) -> dict:
    res = json.loads((day_dir(day) / "scores.json").read_text(encoding="utf-8"))
    known_p = OUT / "wallets.json"
    known = json.loads(known_p.read_text(encoding="utf-8")) if known_p.exists() else {}
    data = collected(day)
    for h in picks(res):
        if known.get(h):
            print(f"{h}: {known[h]} (known)")
            continue
        t = data[h]
        seen, probes = set(), []
        for ts, k, buy, amt, usd in reversed(t["legs"]):        # newest swap of each token, up to 15 tokens
            if k not in seen:
                seen.add(k)
                probes.append((t["tokens"][k], amt, ts, bool(buy)))
            if len(probes) == 15:
                break
        sized = []
        for mint, amt, ts, buy in probes:                      # quietest tokens first (busy ones cost many pages)
            need, first = pages_needed(mint, ts)
            time.sleep(0.4)
            if need <= 12:
                sized.append((need, mint, amt, ts, buy, first))
        w = None
        for need, mint, amt, ts, buy, first in sorted(sized, key=lambda x: x[0])[:4]:
            w = find_wallet(mint, amt, ts, buy, first)
            if w:
                break
        known[h] = w
        known_p.write_text(json.dumps(known, indent=1), encoding="utf-8")
        print(f"{h}: {w or 'not found (tokens too busy to search on the free endpoint)'}")
    return known


# ---- 4. report ------------------------------------------------------------------------------------------------
def rules_rows(c) -> list[tuple[str, str]]:
    return [
        ("Finished trades", f"at least {c.min_trades} round trips (buy then sell) in {c.history_days} days"),
        ("Active", f"trades on at least {c.min_active_days} different days, {c.min_history_days}+ days of history"),
        ("Profitable", "made money overall, made money in the last 7 days, not losing more than "
                       f"{c.max_recent_loss_pct:g}% of its profit today"),
        ("Win rate", f"at least {c.min_win_rate * 100:.0f}% of trades won"),
        ("Profit factor", f"won at least {c.min_profit_factor:g}$ for every 1$ lost"),
        ("Consistent", f"profitable in at least {c.min_positive_weeks} of the last 4 weeks"),
        ("No luck", f"best trade at most {c.max_best_trade_share * 100:.0f}% of the profit, best 3 at most "
                    f"{c.max_top3_share * 100:.0f}%, one token at most {c.max_token_share * 100:.0f}%"),
        ("Copyable", f"median hold at least {c.min_median_hold_s:.0f} s (no snipers), still pays at least "
                     f"{c.min_copy_edge_pct:g}% per trade after OUR fees ({c.swap_fee_pct:g}% + {c.extra_slippage_pct:g}% "
                     f"per swap), our {c.stop_pct:g}% stop and our delay"),
        ("Not stuck", f"open bags at most {c.max_open_buy_share * 100:.0f}% of its 30-day buys and not bigger "
                      "than its profit"),
        ("Drawdown", f"biggest drop at most {c.max_drawdown * 100:.0f}%, now at most "
                     f"{c.max_current_drawdown * 100:.0f}% below its peak"),
    ]


SCORE_TEXT = ("Score (0-100) = sample size x copy edge (20%+ per trade = full) x profit factor (3+ = full) x winning "
              "weeks (4 of 4 = full) x (1 - biggest drop) x (1 - open bag share). Only traders that pass every rule "
              "are ranked by it; 'promising' ones pass everything except the amount of history FOMO shows.")


def report(day: str) -> None:
    c = config.load(ROOT / "config", env={}).sol
    res = json.loads((day_dir(day) / "scores.json").read_text(encoding="utf-8"))
    known_p = OUT / "wallets.json"
    known = json.loads(known_p.read_text(encoding="utf-8")) if known_p.exists() else {}
    latest_p = OUT / "latest.json"
    prev = json.loads(latest_p.read_text(encoding="utf-8")) if latest_p.exists() else {}
    if prev.get("day") == day:
        prev = prev.get("prev") or {}
    top = picks(res)
    before = prev.get("picks", [])
    rows = []
    for i, h in enumerate(top, 1):
        s, w = res[h], known.get(h)
        rows.append({
            "rank": i, "handle": h, "status": s["status"], "new": h not in before, "score": round(s["score"] * 100),
            "trades": s["trades"], "win": round(s["win_rate"] * 100), "pf": round(min(s["profit_factor"], 99), 2),
            "pnl": round(s["pnl"]), "pnl7d": round(s["pnl_7d"]), "edge": round(s["copy_edge_pct"], 1),
            "hold_min": round(s["median_hold_s"] / 60), "best": round(s["best_trade_share"] * 100),
            "weeks": s["positive_weeks"], "days": round(s["history_days"], 1), "dd": round(s["max_dd"] * 100),
            "wallet": w, "missing": [reject_text(r) for r in s["reasons"]],
            "cmd": (f"/fomoadd {w}" if w else None)})
    gone = [{"handle": h, "why": (", ".join(reject_text(r) for r in res[h]["reasons"][:3]) if h in res
                                  else "not in today's leaderboards")} for h in before if h not in top]
    from collections import Counter
    fails = Counter(reject_text(r) for s in res.values() if s["status"] == "fails" for r in s["reasons"]).most_common(6)
    meta_p = day_dir(day) / "collect.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    data = {"day": day, "checked": len(res), "leaderboard": meta.get("leaderboard"), "read": meta.get("checked"), "picks": top, "rows": rows, "gone": gone, "fails": fails,
            "prev_day": prev.get("day"), "rules": rules_rows(c), "score_text": SCORE_TEXT,
            "n_pass": sum(1 for s in res.values() if s["status"] == "passes"),
            "n_promising": sum(1 for s in res.values() if s["status"] == "promising")}
    (day_dir(day) / "report.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    (day_dir(day) / "report.md").write_text(markdown(data), encoding="utf-8")
    tmpl = (ROOT / "tools" / "fomo_picks.html").read_text(encoding="utf-8")
    (OUT / "index.html").write_text(tmpl.replace("/*DATA*/null", json.dumps(data)), encoding="utf-8")
    latest_p.write_text(json.dumps({"day": day, "picks": top, "prev": {"day": prev.get("day"), "picks": before}}),
                        encoding="utf-8")
    print(markdown(data))


def markdown(d: dict) -> str:
    L = [f"# FOMO picks · {d['day']}", "",
         (f"{d['leaderboard']} traders on FOMO's 24h/7d/30d leaderboards, the best {d['read']} read, "
          if d.get("leaderboard") else "")
         + f"{d['checked']} not clearly failing were scored with the bot's FOMO rules: {d['n_pass']} pass, "
         f"{d['n_promising']} promising (only FOMO's short history is missing)."
         + (f" Compared with {d['prev_day']}." if d["prev_day"] else ""), ""]
    if not d["rows"]:
        L.append("Nobody passes today.")
    for r in d["rows"]:
        tag = "🆕 " if r["new"] and d["prev_day"] else ""
        L.append(f"{r['rank']}. {tag}**{r['handle']}** · {r['status']} · score {r['score']}/100")
        L.append(f"   {r['trades']} trades · {r['win']}% win · PF {r['pf']} · {r['pnl']:+,}$ ({r['pnl7d']:+,}$ in 7 d) · "
                 f"{r['edge']:+}% per copy after costs · median hold {r['hold_min']} min · best trade {r['best']}% of profit")
        if r["missing"]:
            L.append(f"   only missing: {', '.join(r['missing'])} (the bot checks 30 days on-chain)")
        L.append(f"   `{r['cmd']}`" if r["cmd"] else "   wallet not found automatically (busy tokens)")
    if d["gone"]:
        L += ["", "Left since the last report:"] + [f"- {g['handle']}: {g['why']}" for g in d["gone"]]
    return "\n".join(L)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    day = sys.argv[2] if len(sys.argv) > 2 else today()
    if cmd == "score":
        score(day)
    elif cmd == "wallets":
        wallets(day)
    elif cmd == "report":
        report(day)
    elif cmd == "dir":
        print(day_dir(day))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
