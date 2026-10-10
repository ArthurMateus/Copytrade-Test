"""The daily Invo search (owner request 2026-10-09): find Invo traders worth copying with the SAME scoring as the
Hyperliquid wallets.

Candidates: the usernames on Invo's Discover rankings (trending / this month / all time portfolios, trending users),
plus the followed traders (always re-checked: a followed trader failing the rules `drop_after_fails` searches in a row
is dropped). For each one: its active portfolios -> its closed calls of the last `history_days` (and its open ones).

Scoring: every closed call becomes one round trip of synthetic fills (`calls_to_fills`): size = the committed share of
the portfolio x leverage of a notional `BASE` portfolio, P&L = what that call made. Open calls become open positions at
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

REASON_TEXT = {"not_on_hyperliquid": "most calls are on coins Hyperliquid does not list",
               "too_fast": "calls too short to copy", "inactive": "no new call lately",
               "no_calls": "no closed calls in a portfolio good enough to copy"}


def key_coin(key: str) -> str:
    return key.split("|", 1)[0]


def calls_to_fills(calls: list[tuple[str, Call]]) -> list[Fill]:
    """[(Hyperliquid coin, closed call)] -> two synthetic fills per call (open, close), each call its own coin key."""
    out: list[Fill] = []
    for n, (coin, c) in enumerate(sorted(calls, key=lambda x: (x[1].created_ms, x[1].id))):
        if not c.closed_ms or not c.closing_price or c.closing_price <= 0 or c.entry <= 0:
            continue
        side = 1 if c.long else -1
        sz = c.exposure * BASE / c.entry
        if sz <= 0:
            continue
        key = f"{coin}|{n}"
        pnl = side * sz * (c.closing_price - c.entry)
        close_ms = max(c.closed_ms, c.created_ms + 1)
        out.append(Fill(key, c.entry, sz, "B" if side > 0 else "A", c.created_ms, 0.0, "Open", 0.0, 0.0, True,
                        2 * n, 2 * n, ""))
        out.append(Fill(key, c.closing_price, sz, "A" if side > 0 else "B", close_ms, side * sz, "Close", pnl, 0.0,
                        True, 2 * n + 1, 2 * n + 1, ""))
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


def score_trader(name: str, closed: list[Call], opened: list[Call], coin_of, candles_of, mids: dict, now_ms: int,
                 params: scoring.ScoreParams, min_hl_share: float, min_hold_min: float,
                 max_idle_days: float) -> dict:
    """Pure (given its inputs): the trader's Score dict (+ Invo fields), eligible only if every rule passes."""
    hl_closed = [(coin_of(c.ticker), c) for c in closed]
    on_hl = [(k, c) for k, c in hl_closed if k]
    fills = calls_to_fills(on_hl)
    ordered = sorted(on_hl, key=lambda x: (x[1].created_ms, x[1].id))     # the order calls_to_fills numbers them
    cs: dict[str, list[Candle]] = {}
    full: dict[str, list[Candle]] = {}
    start = now_ms - params.blocks * params.block_days * DAY
    for f in fills:
        if f.coin in cs:
            continue
        coin = key_coin(f.coin)
        if coin not in full:
            full[coin] = candles_of(coin, start, now_ms)
        c = ordered[int(f.coin.split("|", 1)[1])][1]
        cs[f.coin] = slice_candles(full[coin], c.created_ms, c.closed_ms or now_ms)
    live = open_account([(k, c) for k, c in ((coin_of(c.ticker), c) for c in opened) if k], mids)
    s = scoring.full_score(name, fills, cs, BASE, now_ms, params, live).to_dict()
    holds = [(c.closed_ms - c.created_ms) / 60_000 for _, c in on_hl if c.closed_ms]
    newest = max([c.created_ms for c in closed] + [c.created_ms for c in opened], default=0)
    s.update(calls=len(closed), hl_share=len(on_hl) / len(closed) if closed else 0.0,
             median_hold_min=statistics.median(holds) if holds else 0.0, open_calls=len(opened),
             idle_days=(now_ms - newest) / DAY if newest else 999.0,
             exposure=statistics.median([c.exposure for _, c in on_hl]) if on_hl else 0.0, coin_pnl={})
    extra = []
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
                 now=lambda: int(time.time() * 1000), skip=lambda name, portfolio: None):
        """`skip(name, Portfolio)`: portfolios whose calls would not be copied are not scored either."""
        self.cfg, self.c, self.client = cfg, cfg.invo, client
        self.params = replace(params, coins=None)      # every Hyperliquid perp (the Invo wallets copy alts too)
        self.coin_of, self.candles_of, self.mids, self.followed = coin_of, candles_of, mids, followed
        self.out, self.stop, self.now, self.skip = out, stop, now, skip
        self.dir = Path(cache_dir) / "invo"
        self.dir.mkdir(parents=True, exist_ok=True)
        st = self._load()
        self.scores: dict = {k: v for k, v in st.get("scores", {}).items()
                             if v.get("v") == scoring.VERSION and v.get("rules") == self.params.rules()}
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
        """/invoadd: score each requested trader now and publish ("invo_added", name, score or None)."""
        while not self.stop.is_set():
            try:
                name = self.add_q.get_nowait()
            except queue.Empty:
                return
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
            self.out.put(("invo_added", name, s))

    def _gap(self) -> None:
        self.stop.wait(REQ_GAP_S)

    def discover(self) -> list[str]:
        found: list[str] = []
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
        closed: list[Call] = []
        opened: list[Call] = []
        for p in self.client.portfolios(u[0]):
            self._gap()
            if not p.active or self.skip(name, p):
                continue
            if p.open_count:
                opened += [c for c in self.client.open_calls(p.id) if c.is_open]
                self._gap()
            page = 1
            while len(closed) < self.c.max_calls and not self.stop.is_set():
                batch = self.client.closed_calls(p.id, page)
                self._gap()
                closed += [c for c in batch if c.closed_ms and c.closed_ms >= since]
                if len(batch) < 50 or min(c.created_ms for c in batch) < since:
                    break
                page += 1
        closed = closed[: self.c.max_calls]
        return score_trader(name, closed, opened, self.coin_of, self.candles_of, self.mids, now, self.params,
                            self.c.min_hl_share, self.c.min_hold_min, self.c.max_idle_days)
