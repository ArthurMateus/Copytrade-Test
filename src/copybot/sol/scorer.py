"""Background scorer for FOMO traders, all from the Solana chain (own thread, own disk cache in data/sol/cache).

review (daily, or /fomosearch): sample FOMO's fee payer (every FOMO swap is co-signed by it) -> the real wallets
of active FOMO traders and the USD they moved -> the `max_candidates` biggest -> swap history of each -> full score.
cycle (hourly): rescore the followed wallets and the top of the list with fresh history, publish the ranking.
Pushes to `out`: ("sol_found", traders_seen, n_candidates, n_to_score) when discovery is done |
("sol_ranking", ranking, n_scored, scores) (also every PUBLISH_EVERY wallets during a review) |
("sol_review", n_candidates, n_scored, n_eligible, ranking, scores) at its end | ("sol_alert", text) | ("sol_auth", bool).
`progress` is read by the UI thread (plain values, replaced whole). Histories are cached on disk and updated incrementally (only transactions
newer than the last one read), so a restart resumes instead of downloading everything again.
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
from copybot.sol.chain import AuthError, ChainClient, ChainError, Leg, fetch_history, sample_fomo

DAY = 86_400_000
HOUR = 3_600_000
POOL_DAYS = 7            # a trader not seen in FOMO's flow for this long leaves the candidate pool
PUBLISH_EVERY = 10       # during a review, publish the ranking every this many scored wallets


class SolScorer:
    def __init__(self, cfg: Config, client: ChainClient, out: queue.Queue, cache_dir: str | Path,
                 now=lambda: int(time.time() * 1000)):
        self.cfg, self.c, self.client, self.out, self.now = cfg, cfg.sol, client, out, now
        self.cache = Cache(cache_dir)
        (Path(cache_dir) / "swaps").mkdir(parents=True, exist_ok=True)
        self.meta = self.cache.get("meta.json", {"last_review": 0, "last_cycle": 0})
        self.scores: dict = self.cache.get("scores.json", {})
        self.pool: dict = self.cache.get("pool.json", {})        # wallet -> {"n": seen, "usd": moved, "last": ms}
        self.stop = threading.Event()
        self.focus: set[str] = set()
        self.auth_ok = True
        self.search_req = threading.Event()      # /fomosearch: review now
        self.add_q: queue.Queue = queue.Queue()  # /fomoadd: wallets the owner wants checked now
        # the daily search needs Helius (the free endpoint reads ~1 transaction a second); /fomosearch always works
        self.auto_search = bool(cfg.helius_key) or self.c.search_without_helius
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self.progress: dict = {"phase": "idle", "found": 0, "todo": 0, "done": 0, "eligible": 0, "started": 0,
                               "finished": int(self.meta.get("last_review", 0))}

    @property
    def busy(self) -> bool:
        return self.progress["phase"] != "idle"

    def _progress(self, **kw) -> None:
        self.progress = {**self.progress, **kw}

    def handle(self, address: str) -> str:
        return ""                                # on-chain wallets have no FOMO name

    # ---- thread -----------------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self.run, name="sol-scorer", daemon=True).start()

    def run(self) -> None:
        if self.scores:
            self.maybe_cycle(force=True)
        while not self.stop.is_set():
            try:
                self.handle_adds()
                if self.search_req.is_set():
                    self.search_req.clear()
                    self.review()
                    self.maybe_cycle(force=True)
                elif self.now() - self.meta["last_review"] >= DAY and self.auto_search:
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
            self.out.put(("sol_alert", f"Solana data refused ({why}). No new wallets are added until it works again; "
                                       "open copies keep their exits."))
        self.auth_ok = False
        self.out.put(("sol_auth", False))

    def _save(self) -> None:
        self.cache.put("meta.json", self.meta)
        self.cache.put("scores.json", self.scores)
        self.cache.put("pool.json", self.pool)

    # ---- history (cached, incremental) --------------------------------------------------------------
    def history(self, address: str) -> list[Leg] | None:
        """Swaps of the last `history_days`, or None if the wallet trades too often to read its history.
        One wallet at a time: the seeder and the scorer can ask for the same wallet together (and Windows refuses two
        writers of one cache file)."""
        with self._locks_lock:
            lock = self._locks.setdefault(address, threading.Lock())
        with lock:
            return self._history(address)

    def _history(self, address: str) -> list[Leg] | None:
        now = self.now()
        since = now - self.c.history_days * DAY
        rel = f"swaps/{address}.json"
        cached = self.cache.get(rel)
        old: list[Leg] = []
        h = None
        if cached and cached.get("newest_sig"):         # (a cache without it is from the old FOMO API: ignored)
            old = [Leg(*x) for x in cached["legs"]]
            h = fetch_history(self.client, address, since, stop_sig=cached["newest_sig"],
                              max_sigs=self.c.max_history_txs)
            if not h.complete:                       # more new transactions than we read: start over
                h, old = None, []
        if h is None:
            h = fetch_history(self.client, address, since, max_sigs=self.c.max_history_txs,
                              min_span_ms=self.c.min_history_days * DAY)
            if not h.complete and not h.legs:
                log.info("sol_too_busy", addr=address, txs=h.n_sigs, days=round(h.span_ms / DAY, 1))
                return None
        byid = {g.id: g for g in old}
        byid.update({g.id: g for g in h.legs})
        merged = sorted((g for g in byid.values() if g.ts >= since), key=lambda g: (g.ts, g.id))
        self.cache.put(rel, {"legs": [[g.id, g.ts, g.token, g.side, g.amount, g.usd] for g in merged],
                             "newest_sig": h.newest_sig, "ts": now})
        return merged

    def recent(self, address: str) -> list[Leg]:
        """Swaps to seed a newly followed wallet: the cached history if there is one (cheap update), else only its
        newest `seed_txs` transactions (an owner pick should start copying in minutes, not after hours of reading)."""
        cached = self.cache.get(f"swaps/{address}.json")
        if cached and cached.get("newest_sig"):
            return self.history(address) or []
        since = self.now() - self.c.history_days * DAY
        return fetch_history(self.client, address, since, max_sigs=self.c.seed_txs).legs

    def score(self, address: str) -> None:
        legs = self.history(address)
        if legs is None:
            s = scoring.Score(address, False, reasons=[f"too_busy>{self.c.max_history_txs}tx"], scored_ms=self.now())
        else:
            s = scoring.full_score(address, legs, self.now(), self.c)
        self.scores[address] = s.to_dict()
        log.info("sol_scored", addr=address, eligible=s.eligible, score=round(s.score, 4), trades=s.trades,
                 win=round(s.win_rate, 3), pf=round(s.profit_factor, 2), edge_pct=round(s.copy_edge_pct, 2),
                 why=",".join(s.reasons))

    def handle_adds(self) -> None:
        """/fomoadd: score each requested wallet now, keep it in the pool for every later search, and publish
        ("sol_added", address, score or None, error text, ranking, scores)."""
        while not self.stop.is_set():
            try:
                a = self.add_q.get_nowait()
            except queue.Empty:
                return
            p = self.pool.get(a) or {"n": 0, "usd": 0.0}
            self.pool[a] = {**p, "last": self.now(), "manual": True}
            err = ""
            try:
                self.score(a)
            except (ChainError, AuthError) as e:
                err = str(e)
                log.warn("sol_add_failed", addr=a, err=err)
            self._save()
            self.out.put(("sol_added", a, None if err else self.scores.get(a), err, self.ranking(), dict(self.scores)))

    # ---- daily review ---------------------------------------------------------------------------------
    def discover(self) -> tuple[int, int]:
        """Sample FOMO's flow and add the traders seen to the pool -> (FOMO trades opened, distinct traders in them)."""
        seen = sample_fomo(self.client, self.c.fomo_fee_payer, self.c.discover_pages, self.c.discover_per_page,
                           stop=self.stop)
        now = self.now()
        for w, legs in seen:
            p = self.pool.setdefault(w, {"n": 0, "usd": 0.0, "last": 0})
            p["n"] += 1
            p["usd"] += sum(g.usd for g in legs)
            p["last"] = now
        for w in [w for w, p in self.pool.items() if now - p["last"] > POOL_DAYS * DAY and not p.get("manual")]:
            del self.pool[w]
        self.auth_ok = True
        self.out.put(("sol_auth", True))
        traders = len({w for w, _ in seen})
        log.info("sol_discovered", txs=len(seen), traders=traders, pool=len(self.pool))
        return len(seen), traders

    def candidates(self) -> list[str]:
        """Biggest traders by USD moved in the sampled FOMO flow (more sightings break ties)."""
        ranked = sorted(self.pool.items(), key=lambda kv: (-kv[1]["usd"], -kv[1]["n"], kv[0]))
        manual = [w for w, p in ranked if p.get("manual")]          # /fomoadd wallets are always re-checked
        return manual + [w for w, p in ranked if p["usd"] > 0 and not p.get("manual")][: self.c.max_candidates]

    def eligible(self, wallets) -> int:
        return sum(1 for a in wallets if (self.scores.get(a) or {}).get("eligible"))

    def review(self) -> None:
        log.info("sol_review_start")
        self._progress(phase="discovering", found=0, todo=0, done=0, eligible=0, started=self.now())
        try:
            self._review()
        finally:
            self._progress(phase="idle", finished=self.now())

    def _review(self) -> None:
        _, traders = self.discover()
        self._save()
        cands = self.candidates()
        fresh = lambda a: (self.now() - (self.scores.get(a) or {}).get("scored_ms", 0) < 6 * HOUR
                           and a not in self.focus)
        todo = [a for a in cands if not fresh(a)]
        self._progress(phase="scoring", found=len(self.pool), todo=len(todo), done=0,
                       eligible=self.eligible(cands))
        self.out.put(("sol_found", traders, len(cands), len(todo)))
        for i, a in enumerate(todo, 1):
            if self.stop.is_set():
                return
            self.handle_adds()                # an owner request does not wait for the search to end
            try:
                self.score(a)
            except ChainError as e:
                log.warn("sol_score_failed", addr=a, err=str(e))
            self._progress(done=i, eligible=self.eligible(cands))
            self._save()
            if i % PUBLISH_EVERY == 0:
                self.out.put(("sol_ranking", self.ranking(), len(self.scores), dict(self.scores)))
        self.meta["last_review"] = self.now()
        self._save()
        el = self.eligible(cands)
        log.info("sol_review_done", candidates=len(cands), scored=len(todo), eligible=el)
        self.out.put(("sol_review", len(cands), len(self.scores), el, self.ranking(), dict(self.scores)))

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
            except ChainError as e:
                log.warn("sol_rescore_failed", addr=a, err=str(e))
        self.meta["last_cycle"] = now
        self._save()
        self.out.put(("sol_ranking", self.ranking(), len(self.scores), dict(self.scores)))
