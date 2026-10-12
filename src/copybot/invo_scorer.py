"""The daily Invo search (owner request 2026-10-09): find Invo traders worth copying with the SAME scoring as the
Hyperliquid wallets.

Candidates: Invo's score leaderboard (/analytics/score/user/top, 2026-10-12) and Discover rankings (trending / this
month / all time portfolios, trending users), plus the followed traders (always re-checked: a followed trader failing
the rules `drop_after_fails` searches in a row is dropped).

Per portfolio (owner request 2026-10-12): EACH portfolio of a trader (paper, or "live" = real money on Hyperliquid,
read from Invo's read-only /dex/ views) is scored on its own calls of the last `history_days`, and Invo's own score of
it is kept next to ours (shown only: on the top 100 of 2026-10-12 it had no link with what a copy earned). Only
portfolios passing OUR rules are copied (`by_portfolio[id]["copy"]`; the owner's paper floors still apply to paper
ones). The trader passes when at least one portfolio is copyable AND all its portfolios together (active or not) did
not lose: a good-looking portfolio among losing ones is the bait pattern (audit 2026-10-11, `loses_overall`).

Scoring: every closed call becomes one round trip of synthetic fills (`calls_to_fills`): size = the committed share of
the portfolio x leverage of a notional `BASE` portfolio, P&L = what that call made minus OUR round-trip cost (a +0.01%
"win" on Invo is a loss for a copy). Coins Hyperliquid lists per 1,000 (PEPE -> kPEPE) are scored on kPEPE's prices.
Open calls become open positions at
live Hyperliquid prices (losing ones count as lost trades, as for wallets). `scoring.full_score` then applies exactly
the Hyperliquid rules and points (win rate, profit factor, no single trade > 30% of the profit, drawdowns, this
week/today, losing streak, recent win rate, copy edge after our costs with our 3% stop checked on 1h candles, score
>= 70). Invo-only gates on top: most calls on coins Hyperliquid lists, calls not too short to copy by polling, active
lately. Each call is its own pseudo-coin ("ETH|7") so overlapping calls never merge; its candles are only the slice
of the coin's candles while it was open.

Runs on its own thread with the bot's own Invo login (shared with the watcher; the client spaces every request).
Publishes ("invo_review", n_candidates, scores, fails) when a search ends. Results are cached in data/cache/invo/.
"""
from __future__ import annotations

import bisect
import json
import os
import queue
import statistics
import threading
import time
from dataclasses import replace
from pathlib import Path

from copybot import log, scoring
from copybot.hl import Account, Candle, Fill, OpenPos
from copybot.invo import Call, InvoAuthError

DAY = 86_400_000
HOUR = 3_600_000
BASE = 10_000.0          # notional portfolio the calls' shares are applied to (only ratios matter)
FILTERS = ("trending", "month", "all_time")
REQ_GAP_S = 1.0          # extra spacing of the search's own requests (the watcher's polls go first)
VERSION = 4              # Invo-only scoring changes (2: every portfolio, our cost per call, k-coins; 3: per
                         # portfolio, live portfolios, Invo's score; 4: funding paid while held): rescore

REASON_TEXT = {"not_on_hyperliquid": "most calls are on coins Hyperliquid does not list",
               "too_fast": "calls too short to copy", "inactive": "no new call lately",
               "no_calls": "no closed calls lately",
               "no_copyable_portfolio": "no portfolio passes the rules (and the copy floors)",
               "loses_overall": "its portfolios together lose money (only some look good)"}


def key_coin(key: str) -> str:
    return key.split("|", 1)[0]


def funding_paid(rates: list[tuple[int, float]], side: int, start_ms: int, end_ms: int) -> float:
    """Funding a position of notional 1 paid (+) or received (-) while held: positive rates make longs pay."""
    if not rates:
        return 0.0
    ts = [t for t, _ in rates]
    i, j = bisect.bisect_left(ts, start_ms), bisect.bisect_right(ts, end_ms)
    return side * sum(r for _, r in rates[i:j])


def calls_to_fills(calls: list[tuple[str, Call]], cost_bps: float = 0.0, funding_of=None) -> list[Fill]:
    """[(Hyperliquid coin, closed call)] -> two synthetic fills per call (open, close), each call its own coin key.
    Each fill pays half of `cost_bps` of its notional as a fee, so a call's net is what a copy would keep; with
    `funding_of(coin) -> [(hour ms, rate)]` the funding paid while it was held comes off its result too (owner request
    2026-10-12: calls held for weeks pay real money in funding)."""
    out: list[Fill] = []
    for n, (coin, c) in enumerate(sorted(calls, key=lambda x: (x[1].created_ms, x[1].id))):
        if not c.closed_ms or not c.closing_price or c.closing_price <= 0 or c.entry <= 0:
            continue
        side = 1 if c.long else -1
        sz = c.exposure * BASE / c.entry
        if sz <= 0:
            continue
        key = f"{coin}|{n}"
        close_ms = max(c.closed_ms, c.created_ms + 1)
        paid = funding_paid(funding_of(coin), side, c.created_ms, close_ms) if funding_of else 0.0
        pnl = side * sz * (c.closing_price - c.entry) - paid * sz * c.entry
        fee = cost_bps / 2 / 1e4
        out.append(Fill(key, c.entry, sz, "B" if side > 0 else "A", c.created_ms, 0.0, "Open", 0.0,
                        sz * c.entry * fee, True, 2 * n, 2 * n, ""))
        out.append(Fill(key, c.closing_price, sz, "A" if side > 0 else "B", close_ms, side * sz, "Close", pnl,
                        sz * c.closing_price * fee, True, 2 * n + 1, 2 * n + 1, ""))
    return out


def open_account(calls: list[tuple[str, Call]], mids: dict) -> Account:
    """Open calls as open positions at live prices (unknown price: no unrealized result)."""
    ps = []
    for n, (coin, c) in enumerate(calls):
        side = 1 if c.long else -1
        sz = c.exposure * BASE / c.entry if c.entry > 0 else 0.0
        px = mids.get(coin) or c.entry
        if sz > 0:
            ps.append(OpenPos(f"{coin}|open{n}", side * sz, sz * px, side * sz * (px - c.entry)))
    return Account(BASE, tuple(ps))


def slice_candles(cs: list[Candle], start: int, end: int) -> list[Candle]:
    ts = [c.t for c in cs]
    i = max(0, bisect.bisect_right(ts, start) - 1)
    j = bisect.bisect_right(ts, end + HOUR)
    return cs[i:j]


def to_hl(coin_of, c: Call) -> tuple[str | None, Call]:
    """(Hyperliquid coin, the call in that coin's price unit). `coin_of(ticker)` gives a coin or (coin, price factor)."""
    r = coin_of(c.ticker)
    coin, fx = r if isinstance(r, tuple) else (r, 1.0)
    if coin and fx != 1.0:
        x = lambda v: v * fx if v else v
        c = replace(c, entry=c.entry * fx, closing_price=x(c.closing_price), target=x(c.target), stop=x(c.stop))
    return coin, c


def score_trader(name: str, closed: list[Call], opened: list[Call], coin_of, candles_of, mids: dict, now_ms: int,
                 params: scoring.ScoreParams, min_hl_share: float, min_hold_min: float,
                 max_idle_days: float, portfolios: tuple[int, int, int] | None = None, funding_of=None) -> dict:
    """Pure (given its inputs): the trader's Score dict (+ Invo fields), eligible only if every rule passes.
    `portfolios` = (read, copyable, losing): no copyable one fails the trader (None = not checked)."""
    hl_closed = [to_hl(coin_of, c) for c in closed]
    on_hl = [(k, c) for k, c in hl_closed if k]
    start = now_ms - params.blocks * params.block_days * DAY
    rates: dict[str, list] = {}
    fund = None
    if funding_of is not None:
        def fund(coin: str) -> list:
            if coin not in rates:
                rates[coin] = funding_of(coin, start, now_ms)
            return rates[coin]
    fills = calls_to_fills(on_hl, params.cost_bps, fund)
    ordered = sorted(on_hl, key=lambda x: (x[1].created_ms, x[1].id))     # the order calls_to_fills numbers them
    cs: dict[str, list[Candle]] = {}
    full: dict[str, list[Candle]] = {}
    for f in fills:
        if f.coin in cs:
            continue
        coin = key_coin(f.coin)
        if coin not in full:
            full[coin] = candles_of(coin, start, now_ms)
        c = ordered[int(f.coin.split("|", 1)[1])][1]
        cs[f.coin] = slice_candles(full[coin], c.created_ms, c.closed_ms or now_ms)
    live = open_account([(k, c) for k, c in (to_hl(coin_of, c) for c in opened) if k], mids)
    s = scoring.full_score(name, fills, cs, BASE, now_ms, params, live).to_dict()
    holds = [(c.closed_ms - c.created_ms) / 60_000 for _, c in on_hl if c.closed_ms]
    newest = max([c.created_ms for c in closed] + [c.created_ms for c in opened], default=0)
    s.update(calls=len(closed), hl_share=len(on_hl) / len(closed) if closed else 0.0,
             median_hold_min=statistics.median(holds) if holds else 0.0, open_calls=len(opened),
             idle_days=(now_ms - newest) / DAY if newest else 999.0,
             exposure=statistics.median([c.exposure for _, c in on_hl]) if on_hl else 0.0, coin_pnl={})
    s["iv"] = VERSION
    if portfolios is not None:
        s.update(portfolios=portfolios[0], copyable_portfolios=portfolios[1], losing_portfolios=portfolios[2])
    extra = []
    if portfolios is not None and portfolios[1] == 0:
        extra.append("no_copyable_portfolio")
    if not closed:
        extra.append("no_calls")
    elif s["hl_share"] < min_hl_share:
        extra.append("not_on_hyperliquid")
    if holds and s["median_hold_min"] < min_hold_min:
        extra.append("too_fast")
    if s["idle_days"] > max_idle_days:
        extra.append("inactive")
    if extra:
        s["reasons"] = list(s["reasons"]) + extra
        s["eligible"], s["score"] = False, 0.0
    return s


def reason_text(r: str) -> str:
    return REASON_TEXT.get(r, r.replace("_", " "))


class InvoScorer:
    """Thread: one search per `review_hours` (the first at start when none is cached)."""

    def __init__(self, cfg, client, params: scoring.ScoreParams, coin_of, candles_of, mids: dict, followed,
                 out: queue.Queue, cache_dir: str | os.PathLike, stop: threading.Event,
                 now=lambda: int(time.time() * 1000), skip=lambda name, portfolio: None, funding_of=None):
        """`skip(name, Portfolio)`: why a portfolio's calls would not be copied (a trader needs one that would)."""
        self.cfg, self.c, self.client = cfg, cfg.invo, client
        self.params = replace(params, coins=None)      # every Hyperliquid perp (the Invo wallets copy alts too)
        self.coin_of, self.candles_of, self.mids, self.followed = coin_of, candles_of, mids, followed
        self.out, self.stop, self.now, self.skip = out, stop, now, skip
        self.funding_of = funding_of           # (coin, start, end) -> hourly funding rates (None: not charged)
        self.dir = Path(cache_dir) / "invo"
        self.dir.mkdir(parents=True, exist_ok=True)
        st = self._load()
        self.scores: dict = {k: v for k, v in st.get("scores", {}).items()
                             if v.get("v") == scoring.VERSION and v.get("rules") == self.params.rules()
                             and v.get("iv") == VERSION}
        self.fails: dict = st.get("fails", {})
        self.last = int(st.get("last", 0)) if self.scores else 0     # rules changed (or first start): search now
        self.progress: dict = {"phase": "idle", "todo": 0, "done": 0}
        self.req = threading.Event()
        self.add_q: queue.Queue = queue.Queue()    # /invoadd: traders the owner wants checked now

    def _load(self) -> dict:
        try:
            return json.loads((self.dir / "scores.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        p = self.dir / "scores.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"scores": self.scores, "fails": self.fails, "last": self.last},
                                  separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)

    def busy(self) -> bool:
        return self.progress["phase"] != "idle"

    def ranking(self) -> list[str]:
        el = [(v["score"], k) for k, v in self.scores.items() if v.get("eligible")]
        return [k for _, k in sorted(el, key=lambda x: (-round(x[0], 9), x[1]))]

    def start(self) -> None:
        threading.Thread(target=self.run, name="invo-scorer", daemon=True).start()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.handle_adds()
                if self.req.is_set() or self.now() - self.last >= self.c.review_hours * HOUR:
                    self.req.clear()
                    self.review()
            except InvoAuthError as e:
                log.warn("invo_search_auth", err=str(e))
                self.progress = {"phase": "idle", "todo": 0, "done": 0}
                self.stop.wait(1800)
            except Exception:
                log.exception("invo_search_error")
                self.progress = {"phase": "idle", "todo": 0, "done": 0}
                self.stop.wait(600)
            self.stop.wait(30)

    def handle_adds(self) -> None:
        """/invoadd: score each requested trader now and publish ("invo_added", name, score or None, quiet). An item is
        a name (/invoadd) or (name, True) (/invofollow: report quietly)."""
        while not self.stop.is_set():
            try:
                item = self.add_q.get_nowait()
            except queue.Empty:
                return
            name, quiet = item if isinstance(item, tuple) else (item, False)
            try:
                s = self.score(name)
            except InvoAuthError:
                raise
            except Exception as e:
                log.warn("invo_add_failed", trader=name, err=f"{type(e).__name__}: {e}"[:160])
                s = None
            if s is not None:
                self.scores[name] = s
                self._save()
            self.out.put(("invo_added", name, s, quiet))

    def _gap(self) -> None:
        self.stop.wait(REQ_GAP_S)

    def discover(self) -> list[str]:
        found: list[str] = []
        try:                                    # Invo's score leaderboard (one portfolio per trader, top 100)
            found += [x["user"] for x in self.client.score_top() if x["user"] not in found]
        except InvoAuthError:
            raise
        except Exception as e:
            log.warn("invo_discover_failed", filter="score", err=str(e)[:120])
        self._gap()
        for flt in FILTERS:
            for page in range(1, self.c.discover_pages + 1):
                try:
                    names = self.client.ranking(flt, page)
                except InvoAuthError:
                    raise
                except Exception as e:
                    log.warn("invo_discover_failed", filter=flt, page=page, err=str(e)[:120])
                    break
                self._gap()
                found += [n for n in names if n not in found]
                if not names:
                    break
        for page in range(1, self.c.discover_pages + 1):
            try:
                names = self.client.trending_users(page)
            except InvoAuthError:
                raise
            except Exception as e:
                log.warn("invo_discover_failed", filter="users", page=page, err=str(e)[:120])
                break
            self._gap()
            found += [n for n in names if n not in found]
            if not names:
                break
        log.info("invo_discovered", traders=len(found))
        return found

    def review(self) -> None:
        self.progress = {"phase": "discovering", "todo": 0, "done": 0}
        followed = sorted(self.followed())
        found = self.discover() if self.c.search else []
        cands = followed + [n for n in found if n not in followed][: self.c.max_candidates]
        self.progress = {"phase": "scoring", "todo": len(cands), "done": 0}
        for i, name in enumerate(cands):
            if self.stop.is_set():
                return
            self.handle_adds()                  # an owner request does not wait for the whole search
            try:
                s = self.score(name)
            except InvoAuthError:
                raise
            except Exception as e:
                log.warn("invo_score_failed", trader=name, err=f"{type(e).__name__}: {e}"[:160])
                s = None
            if s is not None:
                self.scores[name] = s
                self.fails[name] = 0 if s["eligible"] else self.fails.get(name, 0) + 1
                log.info("invo_scored", trader=name, eligible=s["eligible"], score=round(s["score"], 1),
                         trades=s["trades"], win=round(s["win_rate"], 3), pf=round(s["profit_factor"], 2),
                         why=",".join(s["reasons"]))
            self.progress = {"phase": "scoring", "todo": len(cands), "done": i + 1}
            if (i + 1) % 10 == 0:
                self._save()
        self.last = self.now()
        self._save()
        self.progress = {"phase": "idle", "todo": 0, "done": 0}
        n_el = sum(1 for n in cands if self.scores.get(n, {}).get("eligible"))
        log.info("invo_review_done", candidates=len(cands), eligible=n_el)
        self.out.put(("invo_review", len(cands), dict(self.scores), dict(self.fails)))

    def score(self, name: str) -> dict | None:
        """None when the trader does not exist or could not be read (its old result is kept)."""
        u = self.client.user(name)
        self._gap()
        if not u:
            return None
        now = self.now()
        since = now - self.c.history_days * DAY
        ports = self.client.portfolios(u[0])
        self._gap()
        args = (self.coin_of, self.candles_of, self.mids, now, self.params, self.c.min_hl_share, self.c.min_hold_min,
                self.c.max_idle_days)
        fkw = {"funding_of": self.funding_of}
        full: dict[str, dict] = {}
        per: dict[str, dict] = {}
        all_closed: list[Call] = []
        all_open: list[Call] = []
        for p in ports:                         # every portfolio counts, not only the ones that look good
            p, closed, opened = self.read_portfolio(p, since)
            all_closed += closed
            all_open += opened
            if not closed and not opened:
                continue
            s = full[p.id] = score_trader(f"{name}/{p.title.strip()[:24]}", closed, opened, *args, **fkw)
            inv = self.invo_score(p) if p.active else {}
            per[p.id] = {"title": p.title.strip(), "kind": p.kind, "active": p.active, "eligible": s["eligible"],
                         "score": s["score"], "reasons": list(s["reasons"]), "trades": s["trades"],
                         "win_rate": s["win_rate"], "profit_factor": s["profit_factor"],
                         "copy_edge_bps": s.get("copy_edge_bps", 0.0), "pnl": s["pnl"],
                         "invo_score": inv.get("score"), "invo_capped": inv.get("capped", False),
                         "invo_caps": inv.get("caps", []),
                         "copy": bool(p.active and s["eligible"] and not self.skip(name, p))}
        overall = score_trader(name, all_closed, all_open, *args,
                               (len(ports), sum(1 for x in per.values() if x["copy"]),
                                sum(1 for p in ports if p.pnl_pct < 0)), **fkw)
        return trader_result(overall, full, per)

    def read_portfolio(self, p, since: int):
        """(portfolio with its real numbers, closed calls since `since`, open calls) of one portfolio."""
        closed: list[Call] = []
        opened: list[Call] = []
        if p.kind == "live":                    # real money: Invo's read-only position views
            try:
                p = self.client.live_overview(p)
            except InvoAuthError:
                raise
            except Exception as e:
                log.warn("invo_live_overview_failed", portfolio=p.id, err=f"{type(e).__name__}: {e}"[:120])
            self._gap()
            opened = [c for c in self.client.live_calls(p.id, True)[0] if c.is_open]
            self._gap()
            cursor = None
            while len(closed) < self.c.max_calls and not self.stop.is_set():
                batch, cursor = self.client.live_calls(p.id, False, 100, cursor)
                self._gap()
                closed += [c for c in batch if c.closed_ms and c.closed_ms >= since]
                if not cursor or not batch or min(c.closed_ms or 0 for c in batch) < since:
                    break
            return p, closed[: self.c.max_calls], opened
        if p.open_count:
            opened = [c for c in self.client.open_calls(p.id) if c.is_open]
            self._gap()
        page = 1
        while len(closed) < self.c.max_calls and not self.stop.is_set():
            batch = self.client.closed_calls(p.id, page)
            self._gap()
            closed += [c for c in batch if c.closed_ms and c.closed_ms >= since]
            if len(batch) < 50 or min(c.created_ms for c in batch) < since:
                break
            page += 1
        return p, closed[: self.c.max_calls], opened

    def invo_score(self, p) -> dict:
        """Invo's own score of the portfolio ({} when unknown); kept for the reports, never decides."""
        try:
            return self.client.portfolio_score(p.id)
        except InvoAuthError:
            raise
        except Exception as e:
            log.warn("invo_score_read_failed", portfolio=p.id, err=f"{type(e).__name__}: {e}"[:120])
            return {}
        finally:
            self._gap()


def trader_result(overall: dict, full: dict[str, dict], per: dict[str, dict]) -> dict:
    """A trader's result from its portfolios' scores: the best COPYABLE portfolio's score (else the best one's, failed),
    eligible only when one is copyable and its portfolios together did not lose. Pure."""
    copyable = [pid for pid, x in per.items() if x["copy"]]
    pool = copyable or list(per)
    best = max(pool, key=lambda pid: (full[pid]["score"], full[pid]["trades"], pid)) if pool else None
    res = dict(full[best]) if best else dict(overall)
    reasons = [] if copyable else list(res.get("reasons", []))
    if not copyable and "no_copyable_portfolio" not in reasons:
        reasons.append("no_copyable_portfolio")
    if overall["trades"] and overall["pnl"] <= 0:
        reasons.append("loses_overall")
    res.update(address=overall.get("address", res.get("address")), by_portfolio=per, best_portfolio=best,
               portfolios=overall.get("portfolios", len(per)), copyable_portfolios=len(copyable),
               losing_portfolios=overall.get("losing_portfolios", 0), reasons=reasons, iv=VERSION,
               overall={k: overall[k] for k in ("trades", "pnl", "win_rate", "profit_factor")},
               calls=overall.get("calls", res.get("calls", 0)), idle_days=overall.get("idle_days", 0.0),
               open_calls=overall.get("open_calls", 0))
    res["eligible"] = not reasons
    if reasons:
        res["score"] = 0.0
    return res
