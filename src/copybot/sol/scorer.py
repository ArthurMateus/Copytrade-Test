"""Background scorer for Solana wallets (own thread, own disk cache in data/sol/cache).

review (daily): FOMO leaderboards 30d/7d/24h -> pre-screen -> swap history of each candidate -> full score.
cycle (hourly): rescore the followed wallets and the top of the list with fresh history, publish the ranking.
Pushes to `out`: ("sol_ranking", ranking, n_scored, scores) | ("sol_review", n_pre, n_scored, n_eligible) |
("sol_alert", text) | ("sol_auth", bool).  History is cached on disk and updated incrementally, so a restart
resumes instead of downloading everything again.
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from copybot import log
from copybot.config import Config
from copybot.selection import Cache
from copybot.sol import scoring
from copybot.sol.fomo import AuthError, FomoClient, FomoError, Leg, fetch_history

DAY = 86_400_000
HOUR = 3_600_000


class SolScorer:
    def __init__(self, cfg: Config, client: FomoClient, out: queue.Queue, cache_dir: str | Path,
                 now=lambda: int(time.time() * 1000)):
        self.cfg, self.c, self.client, self.out, self.now = cfg, cfg.sol, client, out, now
        self.cache = Cache(cache_dir)
        (Path(cache_dir) / "swaps").mkdir(parents=True, exist_ok=True)
        self.meta = self.cache.get("meta.json", {"last_review": 0, "last_cycle": 0})
        self.scores: dict = self.cache.get("scores.json", {})
        self.users: dict = self.cache.get("users.json", {})     # address -> {"uid":..., "handle":...}
        self.stop = threading.Event()
        self.focus: set[str] = set()
        self.auth_ok = True

    def uid(self, address: str) -> str | None:
        return (self.users.get(address) or {}).get("uid")

    def handle(self, address: str) -> str:
        return (self.users.get(address) or {}).get("handle", "")

    # ---- thread -----------------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self.run, name="sol-scorer", daemon=True).start()

    def run(self) -> None:
        if self.scores:
            self.maybe_cycle(force=True)
        while not self.stop.is_set():
            try:
                if self.now() - self.meta["last_review"] >= DAY:
                    self.review()
                self.maybe_cycle()
            except AuthError as e:
                self.on_auth_error(str(e))
                self.stop.wait(300)
            except Exception as e:
                log.exception("sol_scorer_error")
                self.out.put(("sol_alert", f"Solana scorer error: {type(e).__name__}"))
                self.stop.wait(60)
            self.stop.wait(5)

    def on_auth_error(self, why: str) -> None:
        if self.auth_ok:
            self.out.put(("sol_alert", f"FOMO session rejected ({why}). Refresh FOMO_COOKIE. "
                                       "No new Solana wallets are added until then; open copies keep their exits."))
        self.auth_ok = False
        self.out.put(("sol_auth", False))

    def _save(self) -> None:
        self.cache.put("meta.json", self.meta)
        self.cache.put("scores.json", self.scores)
        self.cache.put("users.json", self.users)

    # ---- history (cached, incremental) --------------------------------------------------------------
    def history(self, address: str, uid: str) -> list[Leg]:
        now = self.now()
        since = now - self.c.history_days * DAY
        rel = f"swaps/{address}.json"
        cached = self.cache.get(rel)
        legs: list[Leg] = [Leg(*x) for x in cached["legs"]] if cached else []
        merged = None
        if legs:
            page, _ = self.client.swaps(uid, 100)
            newest = max(g.ts for g in legs)
            if page and page[0].ts <= newest:       # the new page overlaps what we hold: merge
                byid = {g.id: g for g in legs}
                byid.update({g.id: g for g in page})
                merged = list(byid.values())
        if merged is None:
            merged = fetch_history(self.client, uid, since).legs
        merged = sorted((g for g in merged if g.ts >= since), key=lambda g: (g.ts, g.id))
        self.cache.put(rel, {"legs": [[g.id, g.ts, g.token, g.side, g.amount, g.usd] for g in merged], "ts": now})
        return merged

    def score(self, address: str) -> None:
        uid = self.uid(address)
        if not uid:
            return
        legs = self.history(address, uid)
        s = scoring.full_score(address, legs, self.now(), self.c)
        self.scores[address] = s.to_dict()
        log.info("sol_scored", addr=address, eligible=s.eligible, score=round(s.score, 4), trades=s.trades,
                 win=round(s.win_rate, 3), pf=round(s.profit_factor, 2), edge_pct=round(s.copy_edge_pct, 2),
                 why=",".join(s.reasons))

    # ---- daily review ---------------------------------------------------------------------------------
    def review(self) -> None:
        log.info("sol_review_start")
        rows30 = self.client.leaderboard("30d")
        rows7 = self.client.leaderboard("7d")
        rows24 = self.client.leaderboard("24h")
        self.auth_ok = True
        self.out.put(("sol_auth", True))
        for r in rows30 + rows7 + rows24:
            self.users[r.address] = {"uid": r.uid, "handle": r.handle}
        ranked = scoring.rank_prescreened(rows30, rows7, rows24, self.c)
        log.info("sol_prescreen_done", rows=len(rows30), passed=len(ranked))
        for r, _ in ranked[: self.c.max_candidates]:
            if self.stop.is_set():
                return
            done = self.scores.get(r.address)
            if done and self.now() - done.get("scored_ms", 0) < 6 * HOUR and r.address not in self.focus:
                continue
            try:
                self.score(r.address)
            except FomoError as e:
                log.warn("sol_score_failed", addr=r.address, err=str(e))
                continue
            self._save()
            self.maybe_cycle()
        self.meta["last_review"] = self.now()
        self._save()
        el = sum(1 for s in self.scores.values() if s["eligible"])
        log.info("sol_review_done", scored=len(self.scores), eligible=el)
        self.out.put(("sol_review", len(ranked), len(self.scores), el))

    # ---- hourly cycle -----------------------------------------------------------------------------------
    def ranking(self) -> list[str]:
        return scoring.ranking([scoring.Score(**d) for d in self.scores.values()])

    def maybe_cycle(self, force: bool = False) -> None:
        now = self.now()
        if len(self.scores) < self.c.min_scored_to_start:
            return
        if not force and now - self.meta["last_cycle"] < self.c.rescore_minutes * 60_000:
            return
        top = self.ranking()[: self.c.drop_rank]
        for a in sorted(set(top) | (self.focus & set(self.scores))):
            if self.stop.is_set():
                return
            try:
                self.score(a)
            except AuthError:
                raise
            except FomoError as e:
                log.warn("sol_rescore_failed", addr=a, err=str(e))
        self.meta["last_cycle"] = now
        self._save()
        self.out.put(("sol_ranking", self.ranking(), len(self.scores), dict(self.scores)))
