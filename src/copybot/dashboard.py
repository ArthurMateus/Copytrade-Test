"""Read-only web dashboard (docs/PRODUCTION_PLAN.md Phase 3): `uv run copybot-dashboard`, then http://127.0.0.1:8787.

A SEPARATE process from the bot, so a bug here can never touch trading. It only READS the wallets' ledgers
(`data/wallets/<name>/ledger.jsonl`), folding their lines with the same `State.apply` the bot uses. It never opens them
through `Ledger` (whose `replay` cuts a torn last line and creates folders) and never writes anything under data/.
A last line without its newline (the bot mid-write) is ignored. Each ledger is re-read only when its size or mtime
changes. Prices for open trades come from Hyperliquid's public `allMids` (cached 5 s; the live wallet on testnet uses
the testnet's); without them open trades count at their entry price and the page says prices are stale.

Bound to 127.0.0.1 only; no login in the app: remote access goes through Cloudflare Tunnel + Cloudflare Access
(README "Web dashboard"). GET/HEAD only, every other method is 405; every text is HTML-escaped; no environment
variable is read (the config is loaded with an empty environment) so no secret can reach a page.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from copybot import config
from copybot.ledger import State
from copybot.wallets import max_drop_pct

DEFAULT_PORT = 8787
INFO_URLS = {"mainnet": "https://api.hyperliquid.xyz/info", "testnet": "https://api.hyperliquid-testnet.xyz/info"}
PRICE_TTL_S = 5.0
REFRESH_S = 15
LIVE = "live"
MAIN = "invo_calls"
N_CLOSED = 30


# ---- reading the ledgers (never writing) --------------------------------------------------------------------------
@dataclass
class Folded:
    st: State
    torn: bool = False       # the last line had no newline yet (the bot was writing it): ignored
    bad_lines: int = 0       # lines that were not JSON or did not fit the state (also counted in st.uncertain)


def fold_bytes(raw: bytes) -> Folded:
    """The bot's own fold over a ledger's bytes, without its side effects (no truncation, no file opened)."""
    st, bad = State(), 0
    lines = raw.split(b"\n")
    tail = lines.pop()
    for line in lines:
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            st.apply(ev)
        except (ValueError, KeyError, TypeError, AttributeError):
            bad += 1
    return Folded(st, torn=bool(tail.strip()), bad_lines=bad)


class LedgerCache:
    """path -> Folded, folded again only when the file's size or mtime changed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[tuple, Folded]] = {}

    def get(self, path: Path) -> Folded | None:
        try:
            s = path.stat()
        except OSError:
            return None
        key = (s.st_size, s.st_mtime_ns)
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == key:
                return hit[1]
        try:
            with open(path, "rb") as f:          # read-only
                raw = f.read()
        except OSError:
            return None
        folded = fold_bytes(raw)
        with self._lock:
            self._cache[path] = (key, folded)
        return folded


# ---- prices ------------------------------------------------------------------------------------------------------
def fetch_all_mids(url: str, timeout: float = 4.0) -> dict[str, float]:
    """Hyperliquid's public mid prices (POST {"type": "allMids"}); perps only."""
    req = urllib.request.Request(url, data=b'{"type":"allMids"}', method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return {k: float(v) for k, v in data.items() if not k.startswith(("@", "#")) and ":" not in k}


class Prices:
    """url -> (mids, ok, fetched_ms), cached `ttl` seconds; a failure gives {} (open trades at entry) and ok False."""

    def __init__(self, fetch=fetch_all_mids, ttl: float = PRICE_TTL_S):
        self.fetch, self.ttl = fetch, ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict, bool]] = {}

    def get(self, url: str) -> tuple[dict, bool]:
        with self._lock:
            hit = self._cache.get(url)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1], hit[2]
        try:
            mids, ok = self.fetch(url), True
        except Exception:                        # network, HTTP, JSON: show entry prices instead
            mids, ok = {}, False
        with self._lock:
            self._cache[url] = (time.monotonic(), mids, ok)
        return mids, ok


# ---- the numbers ---------------------------------------------------------------------------------------------------
def paper_wallets(cfg: config.Config) -> list[tuple[str, str, str]]:
    """(name, size label, stop label) of the 18 paper wallets, named exactly as runner.Bot.invo_set names them."""
    iv = cfg.invo
    main = iv.main_stop_pct
    sizes = [(MAIN, iv.limits_pct, True)] + [(f"invo_risk_{r:g}pct", r, False) for r in iv.risk_wallets_pct]
    out = []
    for s in [main, *[s for s in iv.stops_pct if s != main]]:
        suffix = "" if s == main else f"_stop{s:g}" if s > 0 else "_trader"
        stop_label = f"{s:g}% stop" if s > 0 else "trader's exit"
        for name, pct, is_main in sizes:
            if iv.margin_allocations:
                size_label = f"{pct:g}% margin" + (" (main)" if is_main else "")
            else:
                size_label = "trader size" if is_main else f"{pct:g}% risk"
            out.append((name + suffix, size_label, stop_label))
    return out


def trader_name(leader: str) -> str:
    return "@" + leader[5:] if leader.startswith("invo:") else leader


def win_rate(trades: list[dict]) -> float | None:
    return sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100 if trades else None


def position_rows(wallet: str, st: State, mids: dict) -> list[dict]:
    rows = []
    for p in sorted(st.positions.values(), key=lambda p: p.opened_ms):
        mark = mids.get(p.coin)
        pnl = (p.upnl(mark) if mark else 0.0) + p.realized       # after fees and funding (cardfmt.net_pnl)
        notional = p.size * p.entry_px
        rows.append(dict(wallet=wallet, coin=p.coin, side="long" if p.side > 0 else "short", size=p.size,
                         entry=p.entry_px, mark=mark, stop=p.stop_px, leverage=p.leverage, pnl=pnl,
                         pnl_pct=pnl / notional * 100 if notional else 0.0, notional=notional,
                         traders=[trader_name(a) for a in (p.shares or {p.leader: 0})],
                         opened_ms=p.opened_ms))
    return rows


def closed_rows(wallet: str, st: State, n: int | None = None) -> list[dict]:
    out = [dict(wallet=wallet, coin=t["coin"], side="long" if t["side"] > 0 else "short", entry=t["entry"],
                exit=t["exit"], pnl=t["pnl"], pnl_pct=t["pnl"] / t["notional"] * 100 if t.get("notional") else 0.0,
                reason=t.get("reason", ""), opened_ms=t["opened_ms"], closed_ms=t["closed_ms"],
                traders=[trader_name(a) for a in (t.get("allocation_traders") or [t["leader"]])])
           for t in reversed(st.closed)]
    return out[:n] if n else out


def curve(st: State, mids: dict, now_ms: int, since_ms: int = 0, start: float | None = None) -> list[list]:
    """[ms, equity] at the start, after every closed trade, and now (open trades at the current price)."""
    eq = st.equity0 if start is None else start
    pts = [[max(st.genesis_ms, since_ms), eq]]
    for t in sorted(st.closed, key=lambda t: t["closed_ms"]):
        if t["closed_ms"] < since_ms:
            continue
        eq += t["pnl"]
        pts.append([t["closed_ms"], eq])
    pts.append([now_ms, st.equity(mids)])
    return pts


def wallet_numbers(st: State, mids: dict) -> dict:
    eq = st.equity(mids)
    pnl = eq - st.equity0
    return dict(start=st.equity0, equity=eq, pnl=pnl, return_pct=pnl / st.equity0 * 100 if st.equity0 else 0.0,
                trades=len(st.closed), win_rate=win_rate(st.closed), open=len(st.positions),
                max_drop_pct=max_drop_pct(st, mids), paused=st.entries_paused or bool(st.uncertain),
                pause_reason=st.pause_reason or ("uncertain restart" if st.uncertain else ""))


def traders(st: State, mids: dict, now_ms: int) -> list[dict]:
    """What copying each trader made in the main wallet (cardfmt.traders_card, plus traders no longer followed)."""
    from copybot.cardfmt import trader_closed, trader_live
    seen = list(dict.fromkeys([*st.followed, *[a for p in st.positions.values() for a in (p.leader, *p.shares)],
                               *[t["leader"] for t in st.closed if not t.get("allocated")],
                               *[t["leader"] for t in st.allocation_closed]]))
    out = []
    for a in seen:
        mine = trader_closed(st, a)
        held = [p for p in st.positions.values() if a in p.shares or (not p.shares and p.leader == a)]
        realized = sum(t["pnl"] for t in mine)
        live = sum(trader_live(p, a, mids.get(p.coin)) for p in held)
        out.append(dict(trader=trader_name(a), followed=a in st.followed, paused=st.paused_leaders.get(a),
                        made=realized + live, realized=realized, open_pnl=live, trades=len(mine),
                        win_rate=win_rate(mine), open=len(held),
                        best=max((t["pnl"] for t in mine), default=None),
                        worst=min((t["pnl"] for t in mine), default=None),
                        following_s=(now_ms - st.followed[a]) / 1000 if a in st.followed else None))
    out.sort(key=lambda r: -r["made"])
    return out


def twin_at(st: State, since_ms: int) -> float:
    """The paper wallet's value when the live wallet started (closed trades up to then)."""
    return st.equity0 + sum(t["pnl"] for t in st.closed if t["closed_ms"] <= since_ms)


class Dashboard:
    """Builds the summary from the files under `root` (the repo folder: config/ and data/)."""

    def __init__(self, root: str | Path = ".", prices: Prices | None = None, clock=None):
        self.root = Path(root)
        self.prices = prices or Prices()
        self.cache = LedgerCache()
        self.clock = clock or (lambda: int(time.time() * 1000))

    def config(self) -> tuple[config.Config, list[str]]:
        try:
            return config.load(self.root / "config", env={}), []     # env={}: no secret is ever read
        except (config.ConfigError, OSError, ValueError) as e:
            return config.Config(), [f"config not readable, defaults used: {e}"]

    def summary(self) -> dict:
        cfg, warnings = self.config()
        now = self.clock()
        data = self.root / cfg.runtime.data_dir / "wallets"
        mids, ok = self.prices.get(cfg.runtime.info_url or INFO_URLS["mainnet"])
        net = cfg.live.network
        live_mids, live_ok = (mids, ok) if net == "mainnet" else self.prices.get(INFO_URLS["testnet"])
        torn = []

        def read(name):
            f = self.cache.get(data / name / "ledger.jsonl")
            if f is not None and f.torn:
                torn.append(name)
            return f

        paper = []
        for name, size, stop in paper_wallets(cfg):
            f = read(name)
            if f is None:
                continue
            paper.append(dict(name=name, size=size, stop=stop, twin=name == cfg.live.copy_wallet,
                              **wallet_numbers(f.st, mids)))
        paper.sort(key=lambda r: (-r["return_pct"], r["name"]))
        for i, r in enumerate(paper):
            r["rank"] = i + 1

        open_trades, closed = [], []
        states = {}
        for r in paper:
            st = self.cache.get(data / r["name"] / "ledger.jsonl").st
            states[r["name"]] = st
            open_trades += position_rows(r["name"], st, mids)
            closed += closed_rows(r["name"], st, N_CLOSED)

        live = twin = None
        lf = read(LIVE)
        if lf is not None:
            st, lc = lf.st, cfg.live
            n = wallet_numbers(st, live_mids)
            day = st.marks.get("day")
            today = n["equity"] - day["equity"] if day and day["equity"] > 0 else None
            live = dict(network=net, enabled=lc.enabled, copies=lc.copy_wallet, **n,
                        today=today, day_start=day["equity"] if day else None,
                        today_pct=today / day["equity"] * 100 if today is not None else None,
                        daily_stop_pct=lc.daily_loss_pct, floor=lc.floor_usd,
                        below_floor=n["equity"] <= lc.floor_usd,
                        started_ms=st.genesis_ms, positions=position_rows(LIVE, st, live_mids),
                        closed=closed_rows(LIVE, st), curve=curve(st, live_mids, now),
                        uncertain=len(st.uncertain), prices_ok=live_ok)
            open_trades += live["positions"]
            closed += closed_rows(LIVE, st, N_CLOSED)
            tst = states.get(lc.copy_wallet)
            if tst is not None and st.equity0 > 0:
                at = twin_at(tst, st.genesis_ms)
                t_eq = tst.equity(mids)
                t_pct = (t_eq - at) / at * 100 if at > 0 else 0.0
                lpts = [[t, (v / st.equity0 - 1) * 100] for t, v in curve(st, live_mids, now)]
                tpts = [[t, (v / at - 1) * 100] for t, v in curve(tst, mids, now, since_ms=st.genesis_ms, start=at)]
                twin = dict(name=lc.copy_wallet, since_ms=st.genesis_ms, live_pct=n["return_pct"], paper_pct=t_pct,
                            gap_pct=n["return_pct"] - t_pct, live_start=st.equity0, paper_start=at,
                            live_equity=n["equity"], paper_equity=t_eq, live_curve=lpts, paper_curve=tpts)

        main = states.get(MAIN)
        open_trades.sort(key=lambda r: (r["coin"], r["wallet"] != LIVE, r["wallet"]))
        closed.sort(key=lambda r: -r["closed_ms"])
        return dict(generated_ms=now, utc_offset_hours=cfg.discord.utc_offset_hours, refresh_s=REFRESH_S,
                    prices=dict(ok=ok, live_ok=live_ok, live_network=net), live=live, paper=paper, twin=twin,
                    traders=traders(main, mids, now) if main is not None else [],
                    open_trades=open_trades, closed_trades=closed[:N_CLOSED], torn=torn, warnings=warnings)


# ---- HTML --------------------------------------------------------------------------------------------------------
def e(s) -> str:
    return html.escape(str(s), quote=True)


def usd(x, sign=True) -> str:
    if x is None:
        return "–"
    s = f"{abs(x):,.2f}$"
    return (("+" if x >= 0 else "−") + s) if sign else ("−" + s if x < 0 else s)


def pct(x, sign=True) -> str:
    if x is None:
        return "–"
    return (("+" if x >= 0 else "−") if sign else ("−" if x < 0 else "")) + f"{abs(x):.2f}%"


def px(x) -> str:
    if x is None:
        return "–"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.1f}"
    if a >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.6g}"


def drop(x) -> str:
    return f"−{x:.1f}%" if x >= 0.05 else "0.0%"


def cls(x) -> str:
    return "" if x is None or abs(x) < 0.005 else ("up" if x > 0 else "down")


def money(x, base=None) -> str:
    extra = f" <small>({pct(x / base * 100)})</small>" if base else ""
    return f'<span class="{cls(x)}">{usd(x)}</span>{extra}'


def when(ms, off: float, secs: bool = False) -> str:
    if not ms:
        return "–"
    return time.strftime("%d/%m %H:%M" + (":%S" if secs else ""), time.gmtime(ms / 1000 + off * 3600))


def dur(s) -> str:
    m = int(s // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h < 48 else f"{h // 24}d{h % 24}h"


def svg_chart(series: list[tuple[str, str, list]], unit: str = "$", zero: float | None = None) -> str:
    """A small line chart: series = [(label, css class, [[ms, y], ...])]."""
    pts = [p for _, _, ps in series for p in ps]
    if len(pts) < 2:
        return '<p class="muted">Not enough data for a chart yet.</p>'
    w, h, pl, pr, pt, pb = 900, 240, 62, 10, 10, 10
    xs, ys = [p[0] for p in pts], [p[1] for p in pts] + ([zero] if zero is not None else [])
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if y1 - y0 < 1e-9:
        y0, y1 = y0 - 1, y1 + 1
    pad = (y1 - y0) * 0.08
    y0, y1 = y0 - pad, y1 + pad
    fx = lambda x: pl + (x - x0) / (x1 - x0) * (w - pl - pr)
    fy = lambda y: pt + (y1 - y) / (y1 - y0) * (h - pt - pb)
    fmt = (lambda v: f"{v:,.0f}$" if abs(y1 - y0) > 20 else f"{v:,.2f}$") if unit == "$" else (lambda v: f"{v:+.1f}%")
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">']
    for i in range(5):
        v = y0 + (y1 - y0) * i / 4
        y = fy(v)
        out.append(f'<line x1="{pl}" x2="{w - pr}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{pl - 6}" y="{y + 4:.1f}" class="axis" text-anchor="end">{e(fmt(v))}</text>')
    if zero is not None:
        out.append(f'<line x1="{pl}" x2="{w - pr}" y1="{fy(zero):.1f}" y2="{fy(zero):.1f}" class="zero"/>')
    for label, css, ps in series:
        if len(ps) < 2:
            continue
        d = " ".join(f"{fx(x):.1f},{fy(y):.1f}" for x, y in ps)
        out.append(f'<polyline points="{d}" class="line {e(css)}"><title>{e(label)}</title></polyline>')
    out.append("</svg>")
    legend = "".join(f'<span class="key {e(css)}">{e(label)}</span>' for label, css, _ in series if len(series) > 1)
    return "".join(out) + (f'<div class="legend">{legend}</div>' if legend else "")


def table(head: list[str], rows: list[list[str]], cls_rows: list[str] | None = None, empty: str = "None.") -> str:
    """`rows` cells are already-escaped HTML."""
    if not rows:
        return f'<p class="muted">{e(empty)}</p>'
    th = "".join(f"<th>{e(x)}</th>" for x in head)
    body = []
    for i, r in enumerate(rows):
        c = f' class="{e(cls_rows[i])}"' if cls_rows and cls_rows[i] else ""
        body.append(f"<tr{c}>" + "".join(f'<td data-l="{e(head[j])}"><span>{v}</span></td>' for j, v in enumerate(r)) + "</tr>")
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def side(s: str) -> str:
    return f'<span class="side {e(s)}">{"▲ LONG" if s == "long" else "▼ SHORT"}</span>'


def open_table(rows: list[dict], off: float, with_wallet: bool) -> str:
    head = (["Wallet"] if with_wallet else []) + ["Coin", "Side", "Size", "Entry", "Now", "Stop", "P&L", "Traders",
                                                   "Opened"]
    out = []
    for r in rows:
        cells = ([e(r["wallet"])] if with_wallet else []) + [
            f"<b>{e(r['coin'])}</b>", side(r["side"]), e(f"{r['size']:.6g}"), e(px(r["entry"])),
            e(px(r["mark"])) if r["mark"] else '<span class="muted">entry</span>', e(px(r["stop"])),
            f'{money(r["pnl"])} <small>({pct(r["pnl_pct"])})</small>', e(", ".join(r["traders"])),
            e(when(r["opened_ms"], off))]
        out.append(cells)
    return table(head, out, [("live" if r["wallet"] == LIVE else "") for r in rows], "No open trades.")


def closed_table(rows: list[dict], off: float, with_wallet: bool) -> str:
    head = (["Wallet"] if with_wallet else []) + ["Closed", "Coin", "Side", "Entry → exit", "Result", "Why",
                                                   "Traders"]
    from copybot.cardfmt import REASONS
    out = [([e(r["wallet"])] if with_wallet else []) + [
        e(when(r["closed_ms"], off)), f"<b>{e(r['coin'])}</b>", side(r["side"]),
        e(f"{px(r['entry'])} → {px(r['exit'])}"), f'{money(r["pnl"])} <small>({pct(r["pnl_pct"])})</small>',
        e(REASONS.get(r["reason"], r["reason"])), e(", ".join(r["traders"]))] for r in rows]
    return table(head, out, [("live" if r["wallet"] == LIVE else "") for r in rows], "No closed trades yet.")


def tile(label: str, value_html: str, sub_html: str = "") -> str:
    return (f'<div class="tile"><div class="lbl">{e(label)}</div><div class="val">{value_html}</div>'
            f'<div class="sub">{sub_html}</div></div>')


def render_main(s: dict) -> str:
    off = s["utc_offset_hours"]
    zone = f"UTC{off:+g}" if off else "UTC"
    parts = [f'<p class="muted stamp">Updated {e(when(s["generated_ms"], off, secs=True))} ({e(zone)}) · refreshes every '
             f'{s["refresh_s"]} s · read-only</p>']
    if not s["prices"]["ok"] or (s["live"] and not s["prices"]["live_ok"]):
        parts.append('<div class="warn">⚠️ Prices are stale (Hyperliquid did not answer): open trades are counted at '
                     'their entry price.</div>')
    for w in s["warnings"]:
        parts.append(f'<div class="warn">⚠️ {e(w)}</div>')

    lv = s["live"]
    parts.append('<section id="live"><h2>💵 Live wallet</h2>')
    if lv is None:
        parts.append('<p class="muted">No live wallet yet (data/wallets/live does not exist).</p>')
    else:
        state = (f'<span class="down">⏸️ Paused</span>' if lv["paused"] else '<span class="up">▶️ Running</span>')
        reason = e(lv["pause_reason"]) if lv["paused"] else ("copies " + e(lv["copies"]))
        today = (money(lv["today"]) + f' <small>({pct(lv["today_pct"])})</small>') if lv["today"] is not None \
            else '<span class="muted">–</span>'
        parts.append('<div class="tiles">' + "".join([
            tile("Balance", f'{usd(lv["equity"], sign=False)}',
                 f'{money(lv["pnl"], lv["start"])} since the {usd(lv["start"], sign=False)} start'),
            tile("Today", today, f'day start {usd(lv["day_start"], sign=False)} · daily stop −{lv["daily_stop_pct"]:g}%'),
            tile("Floor", usd(lv["floor"], sign=False),
                 '<span class="down">at or below the floor</span>' if lv["below_floor"] else
                 f'{usd(lv["equity"] - lv["floor"], sign=False)} above it'),
            tile("Entries", state, reason),
            tile("Trades", f'{lv["trades"]}', (f'{lv["win_rate"]:.0f}% won · ' if lv["win_rate"] is not None else "")
                 + f'{lv["open"]} open · biggest drop {drop(lv["max_drop_pct"])}'),
            tile("Network", e(lv["network"]), "REAL money" if lv["network"] == "mainnet" else "test money"),
        ]) + "</div>")
        parts.append("<h3>Balance over closed trades</h3>" + svg_chart([("Live", "c1", lv["curve"])]))
        parts.append("<h3>Open</h3>" + open_table(lv["positions"], off, False))
        parts.append("<h3>Closed</h3>" + closed_table(lv["closed"][:N_CLOSED], off, False))
    parts.append("</section>")

    tw = s["twin"]
    if tw is not None:
        parts.append('<section id="twin"><h2>⚖️ Live vs its paper twin</h2>')
        parts.append(f'<p class="muted">Since the live start ({e(when(tw["since_ms"], off))}), in % of each wallet: '
                     f'live {usd(tw["live_start"], sign=False)}, paper {e(tw["name"])} '
                     f'{usd(tw["paper_start"], sign=False)} at that time.</p>')
        parts.append('<div class="tiles">' + "".join([
            tile("Live", f'<span class="{cls(tw["live_pct"])}">{pct(tw["live_pct"])}</span>',
                 usd(tw["live_equity"], sign=False)),
            tile("Paper twin", f'<span class="{cls(tw["paper_pct"])}">{pct(tw["paper_pct"])}</span>',
                 usd(tw["paper_equity"], sign=False)),
            tile("Gap", f'<span class="{cls(tw["gap_pct"])}">{pct(tw["gap_pct"])}</span>',
                 "live minus paper (delay, slippage, failed orders)"),
        ]) + "</div>")
        parts.append(svg_chart([("Live", "c1", tw["live_curve"]), ("Paper twin", "c2", tw["paper_curve"])],
                               unit="%", zero=0.0))
        parts.append("</section>")

    parts.append('<section id="paper"><h2>🧾 Paper wallets</h2>')
    rows = [[f'{r["rank"]}', f'<b>{e(r["size"])}</b> · {e(r["stop"])}' + (' <span class="tag">live twin</span>'
                                                                       if r["twin"] else "")
             + (' <span class="tag warnt">⏸️</span>' if r["paused"] else ""),
             f'<span class="{cls(r["return_pct"])}">{pct(r["return_pct"])}</span>', money(r["pnl"]),
             str(r["trades"]), f'{r["win_rate"]:.0f}%' if r["win_rate"] is not None else "–", str(r["open"]),
             drop(r["max_drop_pct"])] for r in s["paper"]]
    parts.append(table(["#", "Wallet (size · stop)", "Return", "P&L", "Trades", "Won", "Open", "Biggest drop"], rows,
                       ["twin" if r["twin"] else "" for r in s["paper"]], "No paper wallets found."))
    parts.append("</section>")

    parts.append(f'<section id="traders"><h2>👥 Traders</h2><p class="muted">What copying each trader made in the main '
                 f'wallet ({e(MAIN)}), closed trades plus open ones at the current price.</p>')
    rows = [[f'<b>{e(t["trader"])}</b>' + ("" if t["followed"] else ' <span class="tag">not followed</span>')
             + (f' <span class="tag warnt">⏸️ {e(t["paused"])}</span>' if t["paused"] else ""),
             money(t["made"]), str(t["trades"]), f'{t["win_rate"]:.0f}%' if t["win_rate"] is not None else "–",
             f'{t["open"]} · {usd(t["open_pnl"])}', usd(t["best"]) if t["best"] is not None else "–",
             usd(t["worst"]) if t["worst"] is not None else "–",
             e(dur(t["following_s"])) if t["following_s"] is not None else "–"] for t in s["traders"]]
    parts.append(table(["Trader", "Made", "Trades", "Won", "Open", "Best", "Worst", "Following for"], rows,
                       empty="No traders yet."))
    parts.append("</section>")

    parts.append('<section id="open"><h2>📂 Open trades (every wallet)</h2>'
                 + open_table(s["open_trades"], off, True) + "</section>")
    parts.append('<section id="closed"><h2>✅ Last closed trades</h2>'
                 + closed_table(s["closed_trades"], off, True) + "</section>")
    return "\n".join(parts)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#16181d;--muted:#6b7280;--line:#e5e7eb;--up:#0a8a4a;--down:#c62828;
--c1:#2563eb;--c2:#d97706;--hl:#eef4ff;--warn:#fff4e0;--tag:#e8ebf0}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e8eaee;--muted:#9aa3af;--line:#2a2f38;
--up:#34d399;--down:#f87171;--c1:#60a5fa;--c2:#fbbf24;--hl:#1b2536;--warn:#3a2c12;--tag:#262b34}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;z-index:2}
header h1{font-size:17px;margin:0 0 4px}nav a{color:var(--muted);margin-right:12px;text-decoration:none;font-size:14px;
white-space:nowrap}nav{overflow-x:auto;white-space:nowrap}
main{max-width:1100px;margin:0 auto;padding:12px 16px 40px}
section{scroll-margin-top:84px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:14px 0}
h2{font-size:17px;margin:2px 0 10px}h3{font-size:14px;margin:16px 0 6px;color:var(--muted)}
.muted{color:var(--muted)}small{color:var(--muted)}.up{color:var(--up)}.down{color:var(--down)}
.stamp{font-size:13px;margin:4px 0}.warn{background:var(--warn);border-radius:8px;padding:8px 12px;margin:8px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.tile{border:1px solid var(--line);border-radius:8px;padding:8px 10px}.lbl{font-size:12px;color:var(--muted)}
.val{font-size:20px;font-weight:600;margin:2px 0}.sub{font-size:12px;color:var(--muted)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600}tr.twin td,tr.live td{background:var(--hl)}
.tag{background:var(--tag);border-radius:6px;padding:1px 6px;font-size:11px;color:var(--fg)}
.side.long{color:var(--up)}.side.short{color:var(--down)}
.chart{width:100%;height:auto;max-height:300px;display:block}.grid{stroke:var(--line);stroke-width:1}
.zero{stroke:var(--muted);stroke-dasharray:4 4}.axis{fill:var(--muted);font-size:11px}
.line{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}.line.c1{stroke:var(--c1)}.line.c2{stroke:var(--c2)}
.legend{font-size:12px;margin-top:4px}.key{margin-right:14px}.key:before{content:"";display:inline-block;width:12px;
height:3px;margin-right:5px;vertical-align:middle}.key.c1:before{background:var(--c1)}.key.c2:before{background:var(--c2)}
@media (max-width:640px){
 table,thead,tbody,tr,td{display:block}thead{display:none}tr{border-bottom:1px solid var(--line);padding:6px 0}
 td{border:0;padding:2px 4px;white-space:normal;display:flex;justify-content:space-between;gap:12px}
 td:before{content:attr(data-l);color:var(--muted);font-size:12px;flex:none}td>span{text-align:right}.val{font-size:18px}
}
"""

JS = """(function(){var n=%d;setInterval(function(){fetch('/?fragment=1',{cache:'no-store'}).then(function(r){
if(!r.ok)throw 0;return r.text()}).then(function(t){document.getElementById('main').innerHTML=t}).catch(function(){
var s=document.querySelector('.stamp');if(s)s.textContent='Could not refresh (dashboard not reachable); retrying.'});
},n*1000)})();""" % REFRESH_S


def render_page(s: dict) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex\"><title>copybot dashboard</title>"
            f"<style>{CSS}</style></head><body><header><h1>📊 copybot</h1><nav>"
            "<a href=\"#live\">Live</a><a href=\"#twin\">Live vs paper</a><a href=\"#paper\">Paper wallets</a>"
            "<a href=\"#traders\">Traders</a><a href=\"#open\">Open</a><a href=\"#closed\">Closed</a></nav></header>"
            f"<main id=\"main\">{render_main(s)}</main><script src=\"/app.js\"></script></body></html>")


# ---- HTTP (GET only) -----------------------------------------------------------------------------------------------
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self';"
                               " img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "copybot-dashboard"
    sys_version = ""
    dash: Dashboard = None   # set by make_server

    def _send(self, code: int, body: bytes, ctype: str, head_only: bool = False, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _get(self, head_only: bool) -> None:
        u = urlsplit(self.path)
        try:
            if u.path == "/app.js":
                return self._send(200, JS.encode(), "text/javascript; charset=utf-8", head_only)
            if u.path == "/api/summary":
                body = json.dumps(self.dash.summary(), ensure_ascii=False, allow_nan=False).encode()
                return self._send(200, body, "application/json; charset=utf-8", head_only)
            if u.path in ("/", "/index.html"):
                s = self.dash.summary()
                frag = parse_qs(u.query).get("fragment") == ["1"]
                return self._send(200, (render_main(s) if frag else render_page(s)).encode(),
                                  "text/html; charset=utf-8", head_only)
            return self._send(404, b"not found", "text/plain; charset=utf-8", head_only)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._send(500, b"dashboard error (see its window)", "text/plain; charset=utf-8", head_only)

    def do_GET(self):
        self._get(False)

    def do_HEAD(self):
        self._get(True)

    def _not_allowed(self):
        self.close_connection = True         # its body (if any) is never read
        self._send(405, b"read-only dashboard: GET only", "text/plain; charset=utf-8", extra={"Allow": "GET, HEAD"})

    def __getattr__(self, name):
        if name.startswith("do_"):           # POST, PUT, DELETE, PATCH, OPTIONS, anything else: 405
            return self._not_allowed
        raise AttributeError(name)

    def log_message(self, fmt, *args):       # quiet: only errors (printed above)
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # on Windows SO_REUSEADDR lets a second program bind the same port: a second start must fail, not share it
    allow_reuse_address = sys.platform != "win32"


def make_server(dash: Dashboard, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> Server:
    handler = type("BoundHandler", (Handler,), {"dash": dash})
    return Server((host, port), handler)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="copybot-dashboard", description="Read-only web dashboard of the copybot ledgers")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    ap.add_argument("--root", default=".", help="the bot's folder (with config/ and data/); default: this folder")
    a = ap.parse_args(argv)
    srv = make_server(Dashboard(a.root), a.port)
    print(f"copybot dashboard (read-only) on http://127.0.0.1:{srv.server_address[1]}  (Ctrl+C stops it)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
