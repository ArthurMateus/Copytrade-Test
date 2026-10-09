"""Leader selection: pure hysteresis rules + the background scorer (downloads, disk cache, scoring).

Hysteresis (all per selection cycle):
  bad leaders leave at once: paused (losing copies), or no longer eligible for `confirm_cycles` cycles
  join  when rank <= join_rank for `confirm_cycles` consecutive cycles, but only once `change_cooldown_hours`
        have passed since the last join (then any free slots fill together)
  drop  an eligible leader at rank > drop_rank for `confirm_cycles` cycles, followed >= min_follow_hours,
        only when joins are allowed: ONE such swap per cycle
  a dropped leader cannot rejoin during the cooldown.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from copybot import hl, log, scoring
from copybot.config import Config
from copybot.hl import Fill

HOUR = 3_600_000
DAY = 86_400_000


# ---- pure hysteresis -------------------------------------------------------------------------------
@dataclass
class Plan:
    joins: list
    drops: list          # [(address, reason)]
    state: dict


def select(sel: dict, ranking: list[str], followed: dict[str, int], paused: set[str], dropped: dict[str, int],
           now_ms: int, cfg: Config) -> Plan:
    s = cfg.selection
    streaks = dict(sel.get("streaks", {}))
    rank = {a: i + 1 for i, a in enumerate(ranking)}
    new: dict[str, dict] = {}
    for a in set(streaks) | set(followed) | set(ranking[: s.drop_rank + 5]):
        r = rank.get(a)
        old = streaks.get(a, {"join": 0, "drop": 0})
        j = old["join"] + 1 if r is not None and r <= s.join_rank else 0
        d = old["drop"] + 1 if r is None or r > s.drop_rank else 0
        if j or d or a in followed:
            new[a] = {"join": j, "drop": d}
    drops: list[tuple[str, str]] = []
    swaps = []
    for a, since in followed.items():
        d = new.get(a, {}).get("drop", 0)
        if a in paused:
            drops.append((a, "paused after a bad streak"))
        elif a not in rank and d >= s.confirm_cycles:
            drops.append((a, f"no longer passes the rules for {s.confirm_cycles} cycles"))
        elif d >= s.confirm_cycles and now_ms - since >= s.min_follow_hours * HOUR:
            swaps.append((-rank.get(a, 10**6), a, f"rank > {s.drop_rank} for {s.confirm_cycles} cycles"))
    swaps.sort()
    cooldown = s.dropped_cooldown_days * DAY
    joinable = [a for a in ranking if a not in followed and new.get(a, {}).get("join", 0) >= s.confirm_cycles
                and now_ms - dropped.get(a, -10**15) >= cooldown]
    joins: list[str] = []
    # the followed set only grows once per `change_cooldown_hours` (counted from the newest leader)
    if now_ms - max(followed.values(), default=-10**15) >= s.change_cooldown_hours * HOUR:
        slots = cfg.risk.max_leaders - (len(followed) - len(drops))
        joins = joinable[:max(0, slots)]
        rest = joinable[len(joins):]
        if swaps and s.swaps_per_cycle > 0:
            _, a, why = swaps[0]
            drops.append((a, why))
            if rest:
                joins.append(rest[0])
    return Plan(joins, drops, {"streaks": new, "cycles": int(sel.get("cycles", 0)) + 1, "at": now_ms})


def rebalance(ranking: list[str], followed: dict[str, int], paused: set[str], dropped: dict[str, int],
              now_ms: int, cfg: Config) -> Plan:
    """/search (owner request): follow the best `max_leaders` of the ranking right now, without the daily window
    or the confirmation cycles. Followed leaders outside them are dropped (their open copies keep being managed
    until they close; no new copies). Paused leaders and leaders in their drop cooldown are not picked."""
    cooldown = cfg.selection.dropped_cooldown_days * DAY
    target = [a for a in ranking if a not in paused
              and (a in followed or now_ms - dropped.get(a, -10**15) >= cooldown)][: cfg.risk.max_leaders]
    rank = set(ranking)
    drops = [(a, "paused after a bad streak" if a in paused else
              ("replaced by a better trader (/search)" if a in rank else "no longer passes the rules"))
             for a in followed if a not in target]
    joins = [a for a in target if a not in followed]
    return Plan(joins, drops, {})


# ---- disk cache --------------------------------------------------------------------------------------
class Cache:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        for sub in ("fills", "candles"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def get(self, rel: str, default=None):
        p = self.root / rel
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def put(self, rel: str, obj) -> None:
        p = self.root / rel
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)


def _fill_from(d: dict) -> Fill:
    return Fill(**d)


# ---- background scorer -------------------------------------------------------------------------------
class Scorer:
    """Runs on its own thread. Pushes ("ranking", ranking, n_scored, scores) to `out` once at least
    `min_scored_to_start` wallets are fully scored OR a first review has finished (whatever it found), then
    every `rescore_minutes`. Pushes ("alert", text)
    for problems. All downloads use the BULK budget class (exits can never be starved)."""

    def __init__(self, cfg: Config, info: hl.Info, out: queue.Queue, cache_dir: str | os.PathLike,
                 now=lambda: int(time.time() * 1000)):
        self.cfg, self.info, self.out, self.now = cfg, info, out, now
        self.cache = Cache(cache_dir)
        self.meta = self.cache.get("meta.json", {"last_daily": 0, "last_weekly": 0, "last_cycle": 0})
        self.coins = tuple(cfg.selection.main_coins)
        self.params = scoring.ScoreParams(stop_pct=cfg.risk.stop_pct,
                                          cost_bps=2 * (cfg.broker.taker_fee_pct * 100 + cfg.broker.extra_slippage_bps + 1),
                                          coins=self.coins, alt_min_coins=cfg.selection.alt_min_coins,
                                          alt_max_share=cfg.selection.alt_max_coin_share,
                                          min_win_rate=cfg.selection.min_win_rate, min_score=cfg.selection.min_score,
                                          min_profit_factor=cfg.selection.min_profit_factor,
                                          max_dd_cap=cfg.selection.max_drawdown,
                                          max_open_loss=cfg.selection.max_open_loss,
                                          max_loss_7d=cfg.selection.max_loss_7d,
                                          max_loss_24h=cfg.selection.max_loss_24h,
                                          max_loss_streak=cfg.selection.max_loss_streak,
                                          min_recent_win_rate=cfg.selection.min_recent_win_rate,
                                          max_dd_7d=cfg.selection.max_dd_7d)
        # results of an older screen/score version, or scored under other eligibility floors, are redone
        # (screened wallets are rescored from the disk cache at startup: see rescore_missing)
        self.screened: dict = {a: d for a, d in self.cache.get("screened.json", {}).items()
                               if d.get("v") == scoring.SCREEN_VERSION}
        self.scores: dict = {a: d for a, d in self.cache.get("scores.json", {}).items()
                             if d.get("v") == scoring.VERSION and d.get("rules") == self.params.rules()}
        self.stop = threading.Event()
        self.search_req = threading.Event()   # /search: run a review now, then publish ("searched", ...)
        self.add_q: queue.Queue = queue.Queue()   # /hyperadd: wallets the owner wants checked now
        self.focus: set[str] = set()   # followed leaders: always rescored (set by the trading loop)
        self.our_notional = cfg.risk.start_equity * cfg.risk.risk_per_trade_pct / cfg.risk.stop_pct

    # ---- thread ---------------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self.run, name="scorer", daemon=True).start()

    def run(self) -> None:
        self.rescore_missing()
        if self.scores:   # restart: resume from cache, do not repeat hours of downloads
            self.maybe_cycle(force=True)
        while not self.stop.is_set():
            try:
                self.handle_adds()
                now = self.now()
                weekly = now - self.meta["last_weekly"] >= 7 * DAY
                if weekly or now - self.meta["last_daily"] >= DAY:
                    self.review(weekly)
                if self.search_req.is_set():
                    self.search_req.clear()
                    self.review(False)   # wallets checked in the last 7 days come from the cache
                    if not self.stop.is_set():
                        self.out.put(("searched", self.ranking(), len(self.scores), dict(self.scores)))
                self.maybe_cycle()
            except Exception as e:
                log.exception("scorer_error")
                self.out.put(("alert", f"scorer error: {type(e).__name__}"))
                self.stop.wait(60)
            self.stop.wait(5)

    def handle_adds(self) -> None:
        """/hyperadd: screen and score each requested wallet now (a fresh check, even if screened this week) and
        publish ("added", address, screen result or None, score or None, ranking, scores)."""
        while not self.stop.is_set():
            try:
                a = self.add_q.get_nowait()
            except queue.Empty:
                return
            try:
                av = self.info.account(a).value
            except Exception as e:
                log.warn("add_account_failed", addr=a, err=str(e))
                av = 0.0
            self.screened.pop(a, None)
            self.screen_and_score(a, av)
            log.info("added_checked", addr=a, screened=a in self.screened, scored=a in self.scores)
            self.out.put(("added", a, self.screened.get(a), self.scores.get(a), self.ranking(), dict(self.scores)))

    def rescore_missing(self) -> None:
        """Wallets that passed the screen but have no score of the current version (the scoring rules changed):
        score them again from the cached fills and candles instead of waiting for the next review."""
        missing = sorted(a for a, d in self.screened.items() if d.get("ok") and a not in self.scores)
        for a in missing:
            if self.stop.is_set():
                return
            try:
                self.score(a)
            except Exception as e:
                log.warn("rescore_failed", addr=a, err=str(e))
        if missing:
            self._save()

    def _save(self) -> None:
        self.cache.put("meta.json", self.meta)
        self.cache.put("screened.json", self.screened)
        self.cache.put("scores.json", self.scores)

    # ---- review: leaderboard -> prescreen -> screen -> full score -----------------------------------
    def review(self, weekly: bool) -> None:
        log.info("review_start", weekly=weekly)
        lb = hl.parse_leaderboard(hl.get_json(self.cfg.runtime.leaderboard_url, timeout=180))
        ranked = scoring.rank_prescreened(lb)
        log.info("prescreen_done", rows=len(lb), passed=len(ranked))
        cands = ranked[: self.cfg.selection.max_candidates]
        pool = 0   # wallets of this review that are fully scored: stop at the top `pool_size`
        for r, pre in cands:
            if self.stop.is_set():
                return
            self.handle_adds()                # an owner request does not wait hours for the review to end
            if pool >= self.cfg.selection.pool_size:
                break
            a = r.address
            done = self.screened.get(a)
            # a screen result is kept for a week (rejected wallets then get a new chance); a restart in the
            # middle of a review therefore resumes where it stopped instead of downloading everything again
            if done and self.now() - done.get("ts", 0) < 7 * DAY:
                done["account_value"] = r.account_value
                if done.get("ok") and a not in self.scores:
                    self.score(a)
                    self._save()
                    self.maybe_cycle()
                pool += a in self.scores
                continue
            self.screen_and_score(a, r.account_value)
            pool += a in self.scores
            self.maybe_cycle()
        self.meta["last_daily"] = self.now()
        if weekly:
            self.meta["last_weekly"] = self.now()
        self._save()
        log.info("review_done", pool=pool, screened=len(self.screened), scored=len(self.scores),
                 eligible=sum(1 for s in self.scores.values() if s["eligible"]))
        self.out.put(("review", weekly, len(ranked), len(self.scores),
                      sum(1 for s in self.scores.values() if s["eligible"])))

    def screen_and_score(self, a: str, account_value: float) -> None:
        now = self.now()
        start = now - self.cfg.selection.history_days * DAY
        try:
            page = self.info.fills_page(a, start, timeout=30)
        except Exception as e:
            log.warn("screen_fetch_failed", addr=a, err=str(e))
            return
        sc = scoring.fill_screen(page, now, self.our_notional, self.cfg.risk.min_notional_usd, coins=self.coins,
                                 min_trips=self.params.min_trades, alt_min_coins=self.params.alt_min_coins,
                                 alt_max_share=self.params.alt_max_share)
        self.screened[a] = {"ok": sc.ok, "reason": sc.reason, "metrics": sc.metrics, "ts": now,
                            "account_value": account_value, "v": scoring.SCREEN_VERSION}
        log.info("screen", addr=a, ok=sc.ok, reason=sc.reason, **{k: v for k, v in sc.metrics.items()})
        if sc.ok:
            self.score(a, prior_page=page)
        self._save()

    def fills(self, a: str, prior_page: list[Fill] | None = None) -> list[Fill]:
        now = self.now()
        start = now - self.cfg.selection.history_days * DAY
        c = self.cache.get(f"fills/{a}.json")
        prior = [_fill_from(d) for d in c["fills"]] if c else list(prior_page or [])
        prior = [f for f in prior if f.time >= start]
        fills, complete = hl.fetch_fills_history(self.info, a, start, now, prior=prior or None)
        fills = [f for f in fills if f.time >= start]
        self.cache.put(f"fills/{a}.json", {"fills": [asdict(f) for f in fills], "until": now, "complete": complete})
        return fills

    def candles(self, coin: str, start: int, end: int) -> list[hl.Candle]:
        out: list[hl.Candle] = []
        chunk = 30 * DAY
        for s, e in hl.chunk_ranges(start, end, chunk):
            rel = f"candles/{coin}_{s}.json"
            c = self.cache.get(rel)
            closed = e < self.now() - HOUR
            if c is None or (not c.get("closed") and self.now() - c.get("ts", 0) > HOUR):
                try:
                    cs = self.info.candles(coin, s, e, timeout=30)
                except hl.HttpError as err:
                    log.warn("candles_failed", coin=coin, status=err.status)
                    cs = []
                c = {"candles": [asdict(x) for x in cs], "closed": closed, "ts": self.now()}
                self.cache.put(rel, c)
            out.extend(hl.Candle(**x) for x in c["candles"])
        return sorted(out, key=lambda x: x.t)

    def score(self, a: str, prior_page: list[Fill] | None = None) -> None:
        now = self.now()
        fills = self.fills(a, prior_page)
        start = now - self.cfg.selection.history_days * DAY
        # every core perp: whether the wallet is diversified (and so scored on all of them) is decided by the score
        coins = sorted({f.coin for f in fills if hl.is_core_perp(f.coin)})
        cs = {c: self.candles(c, start, now) for c in coins}
        av = self.screened.get(a, {}).get("account_value", 0.0)
        try:   # live positions: losers it keeps open count against it (followed leaders are rescored every cycle)
            live = self.info.account(a)
        except Exception as e:
            log.warn("account_fetch_failed", addr=a, err=str(e))
            live = None
        s = scoring.full_score(a, fills, cs, av, now, self.params, live)
        self.scores[a] = s.to_dict()
        log.info("scored", addr=a, eligible=s.eligible, score=round(s.score, 1), trades=s.trades,
                 win=round(s.win_rate, 3), pf=round(s.profit_factor, 2), edge_bps=round(s.copy_edge_bps, 1),
                 mdd=round(s.max_dd, 3), open_loss=round(s.open_loss_pct, 3), open_losers=s.open_losers,
                 diversified=s.diversified, why=",".join(s.reasons))

    # ---- hourly cycle ------------------------------------------------------------------------------
    def ranking(self) -> list[str]:
        return scoring.ranking([scoring.Score(**d) for d in self.scores.values()])

    def ready(self) -> bool:
        """Enough to pick leaders: `min_scored_to_start` wallets scored, or a full review done. Most real
        wallets are rejected before the full score, so a review can end with fewer than that."""
        return len(self.scores) >= self.cfg.selection.min_scored_to_start or self.meta.get("last_daily", 0) > 0

    def maybe_cycle(self, force: bool = False) -> None:
        now = self.now()
        if not self.ready():
            return
        if not force and now - self.meta["last_cycle"] < self.cfg.selection.rescore_minutes * 60_000:
            return
        # rescore the leaders we follow and the current top of the list with fresh fills
        top = self.ranking()[: self.cfg.selection.drop_rank]
        for a in sorted(set(top) | (self.focus & set(self.scores))):
            if self.stop.is_set():
                return
            try:
                self.score(a)
            except Exception as e:
                log.warn("rescore_failed", addr=a, err=str(e))
        # a forced cycle (restart) that comes early is not a new hourly cycle: keep the schedule, so restarts
        # do not push the next selection cycle back
        if not force or now - self.meta["last_cycle"] >= self.cfg.selection.rescore_minutes * 60_000:
            self.meta["last_cycle"] = now
        self._save()
        self.out.put(("ranking", self.ranking(), len(self.scores), dict(self.scores)))
