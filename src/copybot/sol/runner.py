"""SolBot: the Solana paper book, running as threads inside the main process.

Threads: poller (leader swaps, on-chain; woken by the websocket) | websocket (sol-ws) | prices (DexScreener) |
seeder (history -> leader balances) | scorer |
this module's loop (decisions, paper broker, UI). Workers only talk to the loop through one queue, the loop is the
only writer of state (ledger.append then State.apply, like the Hyperliquid side).
It owns data/sol/ledger.jsonl and data/sol/cache/. Telegram/Discord go through the shared UI group.
"""
from __future__ import annotations

import datetime as dt
import queue
import re
import threading
import time
import shutil
from pathlib import Path

from copybot import log, tgfmt
from copybot.tgfmt import esc, fusd
from copybot.config import Config
from copybot.ledger import Ledger, now_ms
from copybot.sol import fmt
from copybot.sol.chain import AuthError, ChainClient, ChainError, FallbackRpc, LogWatch, Rpc
from copybot.sol.hysteresis import Plan, rebalance, select
from copybot.sol.market import PaperBroker, Prices
from copybot.sol.risk import Health, SolGate
from copybot.sol.scorer import SolScorer
from copybot.sol.trader import Detector, Trader


def day_key(ms: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def week_key(ms: float) -> str:
    y, w, _ = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


class SolBot:
    def __init__(self, cfg: Config, ui, alert, restart=lambda: None):
        self.cfg, self.c, self.ui, self.alert_fn, self.restart = cfg, cfg.sol, ui, alert, restart
        self.search_pending = False
        self.data = Path(cfg.runtime.data_dir) / "sol"
        self.data.mkdir(parents=True, exist_ok=True)
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.ledger = Ledger(self.data / "ledger.jsonl")
        self.st = self.ledger.replay()
        c, key = self.c, cfg.helius_key
        self.live_source = "Helius" if key else "public Solana endpoint (no HELIUS_API_KEY: searching is very slow)"
        public = Rpc(c.rpc_url, c.rpc_interval_s, name="public rpc")
        if key:
            # one rate limiter for everything on Helius; the search spends at most helius_daily_credits a day there,
            # then continues on the free public endpoint
            helius = Rpc(f"{c.live_rpc_url}/?api-key={key}", c.live_rpc_interval_s, name="helius")
            self.client = ChainClient(helius)
            self.hist = ChainClient(FallbackRpc(helius, public, c.helius_daily_credits),
                                    parallel=c.history_parallel, bulk=True)
        else:
            # the public endpoint refuses parallel reads (HTTP 429): one at a time
            self.client = self.hist = ChainClient(public)
        self.wakes: set[str] = set()
        self.wake_lock = threading.Lock()
        self.wake_ev = threading.Event()
        self.watch = LogWatch(f"{c.live_ws_url}/?api-key={key}" if key else c.public_ws_url,
                              lambda: self.wanted() & self.ready, self.on_ws_tx, self.stop)
        self.prices = Prices(self.c.dex_url)
        self.scorer = SolScorer(cfg, self.hist, self.q, self.data / "cache")
        self.det = Detector()
        self.gate = SolGate(self.c)
        self.broker = PaperBroker(self.c)
        self.trader = Trader(self.c, self.st, self.ledger, self.gate, self.broker, self.prices, self.health, self.det,
                             notify=self.on_notify)
        self.started = time.time()
        self.auth_ok = True
        self.last_poll_ok = 0.0
        self.ready: set[str] = set()          # leaders whose balances are seeded (polling starts after this)
        self.seeding: set[str] = set()
        self.seed_q: queue.Queue = queue.Queue()
        self.ranks: dict[str, int] = {}
        self.scores: dict = {}
        self.last_body: dict[str, str] = {}
        self.live_cards: set[str] = set()
        self.last_alert: dict[str, float] = {}

    # ---- helpers ---------------------------------------------------------------------------------
    def health(self) -> Health:
        now = time.time()
        return Health(now_ms=now_ms(), price_age_s=now - self.prices.last_ok if self.prices.last_ok else 1e9,
                      leader_feed_age_s=now - self.last_poll_ok if self.last_poll_ok else 1e9, auth_ok=self.auth_ok)

    def rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    def alert(self, text: str, key: str | None = None, every_s: float = 600) -> None:
        k = key or text
        if time.time() - self.last_alert.get(k, 0) < every_s:
            return
        self.last_alert[k] = time.time()
        log.warn("sol_alert", text=text)
        self.ui.send(f"⚠️ 🪙 {tgfmt.esc(text)}")

    def wanted(self) -> set[str]:
        return set(self.st.followed) | {p.leader for p in self.st.positions.values()}

    def handle_of(self, a: str) -> str:
        return self.scorer.handle(a)

    # ---- boot -----------------------------------------------------------------------------------------
    def boot(self) -> None:
        log.info("sol_boot", paper=True)
        if not self.st.genesis_ms:
            self.rec({"ev": "genesis", "equity0": self.c.start_equity, "btc_px0": 0.0})
        self.rec({"ev": "boot", "positions": len(self.st.positions), "followed": len(self.st.followed)})
        problems = list(self.st.uncertain)
        for iid in list(self.st.open_intents):
            self.rec({"ev": "intent_abort", "intent": iid})
        for p in list(self.st.positions.values()):
            bad = p.stop_px <= 0 or p.stop_px >= p.entry_px
            if bad:
                stop = self.gate.stop_for(p.entry_px)
                self.rec({"ev": "stop_set", "coin": p.coin, "pos_id": p.pos_id, "stop_px": stop})
                log.error("sol_stop_repaired", token=p.coin, stop=stop)
        if problems:
            self.st.uncertain = problems
            self.rec({"ev": "pause", "reason": "uncertain restart"})
            self.ui.send("⚠️ 🪙 <b>FOMO restart with uncertainty</b> · ⏸️ entries paused, exits keep running\n"
                         + "\n".join(f"• {tgfmt.esc(x)}" for x in problems[:10]) + "\nCheck, then /fomoresume.")
        log.info("sol_source", live=self.live_source)
        log.info("sol_state", equity=round(self.st.equity(), 2), positions=len(self.st.positions),
                 followed=len(self.st.followed), paused=self.st.entries_paused, trades=len(self.st.closed))
        self.ui.send(f"🪙 <b>FOMO book started</b> (paper) · {len(self.st.positions)} open · "
                     f"{len(self.st.followed)} leaders" + (" · ⏸️ paused" if self.st.entries_paused else "")
                     + f"\nLive trades from: {esc(self.live_source)}")

    def start_threads(self) -> None:
        for name, fn in (("sol-poll", self.poll_worker), ("sol-price", self.price_worker),
                         ("sol-seed", self.seed_worker)):
            threading.Thread(target=fn, name=name, daemon=True).start()
        self.watch.start()
        for a in self.wanted():
            self.seed_q.put(a)
        self.scorer.focus = set(self.st.followed)
        self.scorer.start()

    # ---- workers (never touch state: they only enqueue) -------------------------------------------------
    def set_auth(self, ok: bool, why: str = "") -> None:
        if ok != self.auth_ok:
            self.auth_ok = ok
            self.q.put(("auth", ok, why))

    def seed_worker(self) -> None:
        while not self.stop.is_set():
            try:
                a = self.seed_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if a in self.ready:
                continue
            try:
                legs = self.scorer.history(a) or []      # None: too busy to read (sells then close our copy)
                cursor = self.st.cursors.get(a)
                if cursor is None:
                    cursor = now_ms()
                self.q.put(("seeded", a, self.det.seed(a, legs, cursor), cursor))
                self.set_auth(True)
            except AuthError as e:
                self.set_auth(False, str(e))
                self.stop.wait(30)
                self.seed_q.put(a)
            except Exception as e:
                log.warn("sol_seed_failed", leader=a, err=f"{type(e).__name__}: {e}"[:200])
                self.stop.wait(15)
                self.seed_q.put(a)

    def on_ws_tx(self, wallet: str) -> None:
        """Websocket thread: a followed wallet just made a transaction -> read its swaps now."""
        with self.wake_lock:
            self.wakes.add(wallet)
        self.wake_ev.set()

    def poll_one(self, a: str) -> bool:
        try:
            legs, more = self.client.swaps(a, 25)
            cur = self.st.cursors.get(a, 0)
            if legs and more and legs[0].ts > cur:        # the whole page is new: there may be a gap
                legs, _ = self.client.swaps(a, 200)
            self.q.put(("legs", a, legs))
            self.set_auth(True)
            return True
        except AuthError as e:
            self.set_auth(False, str(e))
        except ChainError as e:
            log.warn("sol_poll_failed", leader=a, err=str(e))
        except Exception as e:
            log.warn("sol_poll_error", leader=a, err=f"{type(e).__name__}: {e}"[:200])
        return False

    def poll_worker(self) -> None:
        """Every followed wallet every `poll_leader_s` (safety net), and at once when the websocket says it traded.
        A failed read of a woken wallet is retried a second later (the node may not have the transaction yet)."""
        next_full = 0.0
        while not self.stop.is_set():
            live = self.wanted() & self.ready
            if time.time() >= next_full:
                ok = all([self.poll_one(a) for a in sorted(live)])
                if ok:
                    self.last_poll_ok = time.time()
                next_full = time.time() + self.c.poll_leader_s
            else:
                with self.wake_lock:
                    woken, self.wakes = self.wakes, set()
                retry = {a for a in sorted(woken & live) if not self.poll_one(a)}
                if retry:
                    with self.wake_lock:
                        self.wakes |= retry
                    self.stop.wait(1.0)
            self.wake_ev.wait(max(0.0, min(next_full - time.time(), self.c.poll_leader_s)))
            self.wake_ev.clear()
            if self.wakes:
                self.wake_ev.set()

    def price_worker(self) -> None:
        while not self.stop.is_set():
            toks = list(self.st.positions)
            if toks:
                self.prices.fetch(toks, timeout=5.0)
            else:
                self.prices.last_ok = time.time()      # nothing to price: not stale
            self.stop.wait(self.c.price_poll_s)

    # ---- trading loop -----------------------------------------------------------------------------------
    def run(self) -> None:
        self.boot()
        self.start_threads()
        last_ui = last_beat = 0.0
        while not self.stop.is_set():
            t0 = time.time()
            try:
                self.tick()
                if time.time() - last_ui >= 1.0:
                    self.refresh_ui()
                    last_ui = time.time()
                if time.time() - last_beat >= 60:
                    self.heartbeat()
                    last_beat = time.time()
            except Exception as e:
                log.exception("sol_loop_error")
                self.alert(f"Solana loop error: {type(e).__name__}: {e}"[:300], key=f"loop:{type(e).__name__}")
            self.stop.wait(max(0.0, 0.25 - (time.time() - t0)))

    def tick(self) -> None:
        self.trader.check_stops()                      # exits first
        deadline = time.time() + 0.2
        while time.time() < deadline:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            self.handle(item)
        now = now_ms()
        eq = self.st.equity(self.trader.marks())
        for kind, key in (("day", day_key(now)), ("week", week_key(now))):
            if self.st.marks.get(kind, {}).get("key") != key:
                self.rec({"ev": "mark", "kind": kind, "key": key, "equity": eq})
        stale = self.trader.stale_marks(120) if time.time() - self.started > 120 else []   # not before the first fetches
        if stale:
            self.alert(f"no price for {', '.join(stale[:5])} for 2+ min: stops cannot run on them", key="stale",
                       every_s=900)

    def handle(self, item) -> None:
        kind = item[0]
        if kind == "legs":
            a, legs = item[1], item[2]
            if a in self.ready and a in self.wanted():
                self.process(a, self.det.on_legs(a, legs), legs)
        elif kind == "seeded":
            a, later, cursor = item[1], item[2], item[3]
            self.ready.add(a)
            self.on_ws_tx(a)                               # read it live now, not at the next safety poll
            if a not in self.st.cursors:
                self.rec({"ev": "cursor", "leader": a, "t": cursor})
            self.process(a, self.det.on_legs(a, later), later)
        elif kind == "auth":
            ok, why = item[1], item[2]
            if ok:
                self.ui.send("✅ 🪙 Solana data OK again.")
            else:
                self.alert(f"Solana data refused ({why}). Check HELIUS_API_KEY. No new copies until then; "
                           "exits and stops keep running.", key="auth", every_s=3600)
        elif kind == "sol_auth":
            self.auth_ok = item[1]
        elif kind == "sol_ranking":
            self.on_ranking(item[1], item[2], item[3])
        elif kind == "sol_found":
            traders, n_cands, n_todo = item[1:]
            self.ui.send(f"🔎 🪙 <b>FOMO search</b> · {traders} FOMO traders seen in the last minutes of FOMO trades · "
                         f"scoring the {n_cands} biggest ({n_todo} need fresh data). Progress: /fomoleaders")
        elif kind == "sol_review":
            self.on_review(*item[1:])
        elif kind == "sol_added":
            self.on_added(*item[1:])
        elif kind == "sol_alert":
            self.alert(item[1])
        elif kind == "cmd":
            self.command(item[1])
        elif kind == "card":
            key, mid = item[1], item[2]
            if mid is None:
                if key in self.st.cards:
                    self.rec({"ev": "card_drop", "key": key})
            elif self.st.cards.get(key) != mid:
                self.rec({"ev": "card", "key": key, "msg_id": mid})

    def process(self, leader: str, moves, legs) -> None:
        for m in moves:
            self.trader.on_move(m)
        if legs:
            newest = max(g.ts for g in legs)
            if newest > self.st.cursors.get(leader, 0):
                self.rec({"ev": "cursor", "leader": leader, "t": newest})
        self.trader.reconcile(leader)

    # ---- selection --------------------------------------------------------------------------------------
    def on_ranking(self, ranking: list[str], n_scored: int, scores: dict) -> None:
        """Hourly cycle (and partial rankings during a review): the normal hysteresis, at most once per cycle."""
        c = self.c
        self.ranks = {a: i + 1 for i, a in enumerate(ranking)}
        self.scores = scores
        now = now_ms()
        if n_scored < c.min_scored_to_start or not self.auth_ok:
            return
        if now - int(self.st.sel.get("at", 0)) < c.rescore_minutes * 60_000 * 0.9:
            return
        self.apply_plan(select(self.st.sel, ranking, self.st.followed, set(self.st.paused_leaders), self.st.dropped,
                               now, c), scores)
        if not ranking and not self.scorer.busy:      # mid-search rankings are partial: the search end reports it
            self.alert("no Solana wallet passed the strict scoring: following nobody new", key="no_eligible",
                       every_s=6 * 3600)

    def on_review(self, n_cands: int, n_scored: int, n_el: int, ranking: list[str], scores: dict) -> None:
        """A review finished. After /fomosearch, or when nobody is followed yet, the best `max_leaders` are followed
        at once (no confirmation cycles); otherwise the hourly hysteresis decides."""
        self.ranks = {a: i + 1 for i, a in enumerate(ranking)}
        self.scores = scores
        searched, self.search_pending = self.search_pending, False
        plan = None
        if (searched or not self.st.followed) and ranking and self.auth_ok:
            plan = rebalance(self.st.sel, ranking, self.st.followed, set(self.st.paused_leaders), self.st.dropped,
                             now_ms(), self.c)
            self.apply_plan(plan, scores)
        top = ", ".join(f"{tgfmt.short(a)} {scores.get(a, {}).get('score', 0) * 100:.0f} pts" for a in ranking[:3])
        self.ui.send(f"🔎 🪙 <b>FOMO search done</b> · {n_cands} FOMO traders checked · {n_el} pass the strict rules"
                     f" · {len(self.st.followed)} followed"
                     + (f" · +{len(plan.joins)} new" if plan and plan.joins else "")
                     + (f" · −{len(plan.drops)} dropped" if plan and plan.drops else "")
                     + (f"\nBest: {esc(top)}" if top else
                        "\nNobody passed the strict rules this time; the next search runs in 24 h (or /fomosearch).")
                     + (f"\n{why}" if (why := fmt.reject_summary(scores)) else ""))

    def on_added(self, a: str, score: dict | None, err: str, ranking: list[str], scores: dict) -> None:
        """/fomoadd result: follow at once if it passes every rule and a slot is free, else say why not."""
        self.ranks = {x: i + 1 for i, x in enumerate(ranking)}
        self.scores = scores
        who = f"<code>{tgfmt.short(a)}</code>"
        if score is None:
            self.ui.send(f"⚠️ 🪙 /fomoadd {who}: could not read its trades ({esc(err[:120])}). Try again later.")
        elif not score.get("eligible"):
            why = ", ".join(fmt.reject_text(r) for r in score.get("reasons") or []) or "not scored"
            self.ui.send(f"❌ 🪙 /fomoadd {who} fails the strict rules: {esc(why)}.\n"
                         f"{score.get('trades', 0)} trades in {self.c.history_days} days · "
                         f"{score.get('win_rate', 0) * 100:.0f}% win · PF {score.get('profit_factor', 0):.2f} · "
                         f"{fusd(score.get('pnl', 0))}. Not followed; it is re-checked at every search.")
        elif a in self.st.followed:
            self.ui.send(f"✅ 🪙 /fomoadd {who} passes ({score.get('score', 0) * 100:.0f} pts) and is already followed.")
        elif a in self.st.paused_leaders:
            self.ui.send(f"⏸️ 🪙 /fomoadd {who} passes but is paused after a bad streak of ours. Not followed.")
        elif len(self.st.followed) >= self.c.max_leaders:
            self.ui.send(f"✅ 🪙 /fomoadd {who} passes ({score.get('score', 0) * 100:.0f} pts), but you already follow "
                         f"{len(self.st.followed)}. It competes at the next search (/fomosearch).")
        else:
            self.apply_plan(Plan([a], [], dict(self.st.sel)), scores)

    def apply_plan(self, plan, scores: dict) -> None:
        now = now_ms()
        for a, why in plan.drops:
            self.rec({"ev": "unfollow", "leader": a, "reason": why})
            held = sum(1 for p in self.st.positions.values() if p.leader == a)
            log.info("sol_leader_dropped", leader=a, reason=why, open_copies=held)
            self.ui.send(f"➖ 🪙 <b>Dropped</b> <code>{tgfmt.short(a)}</code> · {tgfmt.esc(why)}"
                         + (f" · {held} copy still managed until exit" if held else ""))
        for a in plan.joins:
            self.rec({"ev": "follow", "leader": a, "rank": self.ranks.get(a)})
            self.rec({"ev": "cursor", "leader": a, "t": now})     # follow from now on: never copy older swaps
            self.seed_q.put(a)
            s = scores.get(a, {})
            log.info("sol_leader_followed", leader=a, rank=self.ranks.get(a), score=s.get("score"))
            self.ui.send(f"➕ 🪙 <b>Following</b> {tgfmt.esc(fmt.who(a, self.handle_of(a)))} · rank #{self.ranks.get(a)}\n"
                         + tgfmt.pre([
                             ("Trades", str(s.get("trades", "-"))),
                             ("Win", f"{s.get('win_rate', 0) * 100:.0f}%"),
                             ("PF", f"{s.get('profit_factor', 0):.2f}"),
                             ("Edge", f"{s.get('copy_edge_pct', 0):.1f}% / trade after costs"),
                             ("Median hold", f"{s.get('median_hold_s', 0) / 60:.0f} min"),
                             ("Open bag", f"{s.get('open_buy_share', 0) * 100:.0f}% of buys"),
                         ]))
        self.rec({"ev": "sel", "state": plan.state})
        self.scorer.focus = set(self.st.followed)

    # ---- notifications from the trader ----------------------------------------------------------------------
    def on_notify(self, kind: str, **kw) -> None:
        if kind in ("opened", "updated"):
            p = self.st.positions.get(kw["token"])
            if p:
                self.card_for(p, force=True)
        elif kind == "closed":
            t = kw["trade"]
            key = f"sol:pos:{t['pos_id']}"
            self.last_body.pop(key, None)
            self.ui.final_card(key, fmt.closed_card(t, self.handle_of(t["leader"])))
        elif kind == "leader_paused":
            self.ui.send(f"⏸️ 🪙 <b>Leader paused</b> <code>{tgfmt.short(kw['leader'])}</code> · "
                         f"{tgfmt.esc(kw['reason'])}\nNo new copies from it; open copies keep mirroring exits.")

    def card_for(self, p, force: bool = False) -> None:
        key = f"sol:pos:{p.pos_id}"
        mark = self.prices.marks().get(p.coin)
        h = self.handle_of(p.leader)
        body = fmt.trade_card(p, mark, 0, h)
        if not force and self.last_body.get(key) == body:
            return
        self.last_body[key] = body
        self.ui.set_card(key, fmt.trade_card(p, mark, now_ms(), h))

    def render(self, key: str, h, marks: dict, now: float) -> str:
        handles = {a: self.handle_of(a) for a in (*self.st.followed, *(p.leader for p in self.st.positions.values()))}
        if key == "sol:status":
            return fmt.status_card(self.st, marks, h, now, self.auth_ok, self.scorer.progress)
        if key == "sol:trades":
            return fmt.trades_card(self.st, marks, now, handles)
        if key == "sol:traders":
            return fmt.traders_card(self.st, marks, self.ranks, self.scores, handles, now)
        if key == "sol:wallet":
            return fmt.wallet_card(self.st, marks, self.c, now)
        return fmt.leaders_card(self.st, self.ranks, handles, now, self.scores, self.scorer.progress)

    def refresh_ui(self) -> None:
        for p in list(self.st.positions.values()):
            self.card_for(p)
        marks, h = self.prices.marks(), self.health()
        for key in list(self.live_cards):
            body = self.render(key, h, marks, 0)           # time 0: no edit for a time stamp alone
            if self.last_body.get(key) != body:
                self.last_body[key] = body
                self.ui.set_card(key, self.render(key, h, marks, now_ms()))

    # ---- commands -----------------------------------------------------------------------------------------------
    LIVE = {"/fomo": "sol:status", "/fomotrades": "sol:trades", "/fomotraders": "sol:traders",
            "/fomowallet": "sol:wallet", "/fomoleaders": "sol:leaders"}

    def command(self, c) -> None:
        now, marks = now_ms(), self.prices.marks()
        if c.name in self.LIVE:
            key = self.LIVE[c.name]
            self.live_cards.add(key)
            self.last_body.pop(key, None)
            self.ui.set_card(key, self.render(key, self.health(), marks, now), new=True)
        elif c.name == "/fomopositions":
            self.ui.send(fmt.positions_text(self.st, marks))
        elif c.name == "/fomoprogress":
            self.ui.send(fmt.progress_text(self.st, marks, now))
        elif c.name == "/fomosearch":
            self.search_pending = True
            if self.scorer.busy:
                self.ui.send("🔎 🪙 A search is already running: " + esc(fmt.search_line(self.scorer.progress, now))
                             + ". I will follow the best when it ends.")
                return
            self.scorer.search_req.set()
            self.ui.send("🔎 🪙 Looking for active FOMO traders on-chain and scoring them now; I will follow the best "
                         f"{self.c.max_leaders} when it ends. Progress: /fomoleaders")
        elif c.name == "/fomoadd":
            a = c.arg.strip()
            if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", a) or a == self.c.fomo_fee_payer:
                self.ui.send("Usage: /fomoadd &lt;Solana wallet address&gt; (32 to 44 letters and digits, no 0x)")
                return
            self.scorer.add_q.put(a)
            self.ui.send(f"🔎 🪙 Checking <code>{tgfmt.short(a)}</code> with the strict rules now (a minute or two); "
                         "I will follow it if it passes and a slot is free.")
        elif c.name == "/fomopause":
            self.rec({"ev": "pause", "reason": "/fomopause"})
            self.ui.send("⏸️ 🪙 <b>FOMO entries paused.</b> Exits and stops keep running. /fomoresume to continue.")
        elif c.name == "/fomoresume":
            if self.st.uncertain:
                self.rec({"ev": "ack", "items": list(self.st.uncertain)})
            self.rec({"ev": "resume"})
            self.ui.send("▶️ 🪙 <b>FOMO entries resumed.</b>")
        elif c.name == "/fomoflatten":
            if not self.ui.check_pin(c.arg):
                log.warn("sol_flatten_bad_pin")
                self.ui.send("⛔ Wrong or missing PIN. Usage: /fomoflatten &lt;PIN&gt;")
                return
            n = len(self.st.positions)
            self.rec({"ev": "pause", "reason": "/fomoflatten"})
            self.trader.flatten("flatten")
            self.ui.send(f"🛑 🪙 <b>Flattened</b> {n} FOMO trade(s). ⏸️ Entries paused · /fomoresume to continue.")
        elif c.name == "/fomoreset":
            if not self.ui.check_pin(c.arg):
                log.warn("sol_reset_bad_pin")
                self.ui.send("⛔ Wrong or missing PIN. Usage: /fomoreset &lt;PIN&gt;")
                return
            if self.st.positions:
                self.ui.send(f"⛔ <b>FOMO reset refused</b>: {len(self.st.positions)} open trade(s). Wait for them to "
                             f"close, or /fomoflatten &lt;PIN&gt; first.")
                return
            where = self.reset_book()
            self.ui.send(f"♻️ 🪙 <b>FOMO reset done</b> · the wallet is back to "
                         f"{fusd(self.c.start_equity, sign=False)}, traders kept. Old history saved in "
                         f"<code>{esc(where)}</code>.\n🔄 Restarting… back in about 15 seconds.")
            self.restart()

    def reset_book(self) -> str:
        """Archive the Solana ledger and start a fresh one that keeps the followed traders (with their 'since'), pauses,
        drop cooldowns, selection streaks and swap cursors (so old swaps are never copied). Nothing is deleted.
        The caller restarts the process."""
        st = self.st
        seed = [{"ev": "genesis", "equity0": self.c.start_equity, "btc_px0": 0.0}]
        seed += [{"ev": "unfollow", "leader": a, "reason": "kept across /fomoreset", "ts": t} for a, t in st.dropped.items()]
        seed += [{"ev": "follow", "leader": a, "ts": t} for a, t in st.followed.items()]
        seed += [{"ev": "leader_pause", "leader": a, "reason": why} for a, why in st.paused_leaders.items()]
        seed += [{"ev": "cursor", "leader": a, "t": t} for a, t in st.cursors.items()]
        seed.append({"ev": "sel", "state": st.sel})
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        arch = self.data / "archive" / f"reset-{stamp}"
        arch.mkdir(parents=True, exist_ok=True)
        seed.append({"ev": "note", "text": f"reset; previous history in {arch.as_posix()}"})
        self.ledger.frozen = True       # nothing more is written to the old file before the restart
        self.ledger.close()
        shutil.move(str(self.data / "ledger.jsonl"), str(arch / "ledger.jsonl"))
        fresh = Ledger(self.data / "ledger.jsonl")
        for ev in seed:
            fresh.append(ev)
        fresh.close()
        log.info("sol_reset", archive=arch.as_posix(), followed=len(st.followed))
        return arch.as_posix()

    def heartbeat(self) -> None:
        h = self.health()
        log.info("sol_heartbeat", equity=round(self.st.equity(self.prices.marks()), 2), positions=len(self.st.positions),
                 followed=len(self.st.followed), paused=self.st.entries_paused, trades=len(self.st.closed),
                 auth=self.auth_ok, price_age_s=round(h.price_age_s, 1), leader_feed_age_s=round(h.leader_feed_age_s, 1),
                 scored=len(self.scorer.scores), ready=len(self.ready))

    def shutdown(self) -> None:
        self.stop.set()
        self.scorer.stop.set()
        self.ledger.close()
