"""The bot: one process, one trading loop (decisions + paper broker), worker threads for everything slow.

Threads: trading loop (this module) | ws feed | health worker (clock + REST price fallback) |
sync worker (leader reconcile + meta/funding) | scorer (leaderboard, fills, candles, scoring) |
telegram poll + outbox. Workers only talk to the trading loop through one queue.
"""
from __future__ import annotations

import argparse
import re
import datetime as dt
import os
import queue
import shutil
import statistics
import sys
import threading
import time
import traceback
from pathlib import Path

from copybot import config, hl, log, tgfmt
from copybot.broker import PaperBroker
from copybot.detector import Detector, Move
from copybot.invo import PREFIX as INVO, InvoClient, Watcher as InvoWatcher
from copybot.feed import Clock, Feed
from copybot.ledger import Ledger, now_ms
from copybot.positions import PositionManager
from copybot.risk import Health, RiskGate
from copybot.selection import Plan, Scorer, rebalance, select
from copybot.discord import DiscordUI, MultiUI
from copybot.sol.fmt import FOMO_HELP
from copybot.sol.runner import SolBot
from copybot.tg import HELP, TelegramUI
from copybot.wallets import SideWallet, boot_repair


def day_key(ms: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def week_key(ms: float) -> str:
    y, w, _ = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def hour_key(ms: float) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ms / 1000))


class SingleInstance:
    """Two bots on one ledger would double-open. Hold an OS lock on data/bot.lock for the process life."""

    def __init__(self, path: Path):
        self.f = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another copybot is already running on {path.parent}") from None


class Bot:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        rt = cfg.runtime
        self.data = Path(rt.data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock = SingleInstance(self.data / "bot.lock")
        self.q: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.budget = hl.RateBudget(rt.weight_per_min, rt.critical_reserve_weight)
        self.info = hl.Info(rt.info_url, self.budget)
        self.ledger = Ledger(self.data / "ledger.jsonl")
        self.st = self.ledger.replay()
        self.mids: dict[str, float] = {}
        self.assets: dict[str, hl.Asset] = {}
        self.feed = Feed(rt.ws_url, self.q)
        self.clock = Clock()
        self.gate = RiskGate(cfg, alts_ok=lambda a: bool(self.score_dict(a).get("diversified")))
        self.books: dict[str, tuple[float, object]] = {}   # coin -> (fetched at, book), shared with side wallets
        self.broker = PaperBroker(cfg.broker, self.fresh_book)
        self.pm = PositionManager(cfg, self.st, self.ledger, self.gate, self.broker, self.health, self.mids,
                                  self.assets, notify=self.on_notify, score_of=self.score_of)
        side_broker = PaperBroker(cfg.broker, self.recent_book)
        self.sides = [SideWallet(cfg, r, self.data, side_broker, self.health, self.mids, self.assets,
                                 alts_ok=self.gate.alts_ok, score_of=self.score_of)
                      for r in cfg.risk.side_wallets_risk_pct]
        self.leader_value: dict[str, float] = {}   # followed leader -> perp account value (refreshed every 5 min)
        if cfg.risk.mirror_wallet:
            m = cfg.risk.mirror_mult
            self.sides.append(SideWallet(cfg, cfg.risk.mirror_limits_pct, self.data, side_broker, self.health,
                                         self.mids, self.assets, alts_ok=self.gate.alts_ok, score_of=self.score_of,
                                         name=f"mirror_x{m:g}", label=f"mirror x{m:g} (their % of account)",
                                         sizer=self.mirror_size))
        self.invo: SideWallet | None = None          # the Invo calls wallet (needs INVO_TOKEN_FILE)
        self.invo_watch: InvoWatcher | None = None
        self.invo_calls: dict[str, str] = {}         # Invo call id -> the coin we hold for it
        self._invo_call = None                       # the call being opened (read by invo_size)
        self.invo_info: dict[str, str] = {}          # our position id -> what the trader called (for its card)
        self._invo_close_why: str | None = None      # the trader's close reason while it is being mirrored
        if cfg.invo.enabled and cfg.invo_token_file:
            w = SideWallet(cfg, cfg.invo.limits_pct, self.data, side_broker, self.health, self.mids, self.assets,
                           alts_ok=lambda leader: True, score_of=self.score_of, name="invo_calls",
                           label="invo calls (posted trades)", sizer=self.invo_size, own_leaders=True,
                           notify=self.on_invo_notify)
            # a call is first seen up to poll_s after it was posted: entries may be that old (still fail closed)
            w.cfg.risk.max_entry_age_s = cfg.invo.max_call_age_s + cfg.invo.poll_s
            self.invo = w
            self.sides.append(w)
            self.invo_watch = InvoWatcher(InvoClient(cfg.invo.api_base, cfg.invo_token_file), self.q,
                                          lambda: {a[len(INVO):] for a in w.st.followed}, cfg.invo.poll_s,
                                          cfg.invo.max_call_age_s, self.stop)
        elif cfg.invo.enabled:
            log.info("invo_off", why="INVO_TOKEN_FILE not set")
        self.det = Detector()
        on_cmd = lambda c: self.q.put(("cmd", c))
        self.ui = MultiUI(TelegramUI(cfg, on_cmd, lambda k, m: self.q.put(("card", k, m))),
                          DiscordUI(cfg, on_cmd, lambda k, m: self.q.put(("card", "dc:" + k, m))))
        tgfmt.set_utc_offset(cfg.telegram.utc_offset_hours)
        self.sol: SolBot | None = None
        if cfg.sol.enabled:
            self.sol = SolBot(cfg, self.ui, self.alert, restart=lambda: setattr(self, "restart_at", time.time() + 3))
        self.scorer = Scorer(cfg, self.info, self.q, self.data / "cache")
        self.ranks: dict[str, int] = {}
        self.ranking: list[str] = []
        self.scores: dict = {}
        self.search_pending = False
        self.restart_at = 0.0        # /restart and /reset: stop the loop at this time (the outbox gets to flush)
        self.reconcile_now = threading.Event()
        self.last_body: dict[str, str] = {}
        self.last_alert: dict[str, float] = {}
        self.last_funding_meta_ms = 0
        self.live_cards: set[str] = set()
        self.leaders_with_positions: set[str] = set()
        self.ws_down_since = 0.0     # websocket down since (0 = up); alerted only if it stays down
        self.ws_alerted = False

    # ---- health ------------------------------------------------------------------------------------
    def health(self) -> Health:
        now = time.time()
        _, mts = self.feed.mids()
        ok, off, why = self.clock.status(self.cfg.risk.clock_tolerance_ms, self.cfg.risk.max_clock_age_s)
        return Health(now_ms=int(now * 1000), mids_age_s=now - mts if mts else 1e9, clock_ok=ok, clock_offset_ms=off,
                      clock_why=why, feed_age_s=now - self.feed.last_msg if self.feed.last_msg else 1e9,
                      feed_connected=self.feed.connected)

    def rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    # ---- books: the main wallet always reads a fresh one; side wallets reuse it for a second -------------
    def fresh_book(self, coin: str):
        b = self.info.book(coin, self.cfg.runtime.trading_timeout_s)
        self.books[coin] = (time.time(), b)
        return b

    def recent_book(self, coin: str):
        hit = self.books.get(coin)
        if hit and time.time() - hit[0] < 1.0:
            return hit[1]
        return self.fresh_book(coin)

    def on_sides(self, what: str, fn) -> None:
        """Run fn(side) for every side wallet; a failure in one is logged and never reaches the main wallet."""
        for w in self.sides:
            if w.own_leaders and what in ("sync", "move", "reconcile"):
                continue
            try:
                fn(w)
            except Exception as e:
                log.exception("side_wallet_error", wallet=w.name, what=what)
                self.alert(f"side wallet {w.name}: {what} failed ({type(e).__name__})", key=f"side:{w.name}:{what}")

    def sync_sides(self) -> None:
        self.on_sides("sync", lambda w: w.sync_leaders(self.st))

    def alert(self, text: str, key: str | None = None, every_s: float = 600) -> None:
        k = key or text
        if time.time() - self.last_alert.get(k, 0) < every_s:
            return
        self.last_alert[k] = time.time()
        log.warn("alert", text=text)
        self.ui.send(f"⚠️ {tgfmt.esc(text)}")

    # ---- startup -----------------------------------------------------------------------------------
    def boot(self) -> None:
        cfg = self.cfg
        log.info("boot", paper=True, cfg=str(config.public_dict(cfg)).replace(" ", ""))
        if not self.st.genesis_ms:
            btc = 0.0
            try:
                btc = float(self.info.post({"type": "allMids"}, hl.CRITICAL, 5)["BTC"])
            except Exception as e:
                log.warn("genesis_no_btc_price", err=str(e))
            self.rec({"ev": "genesis", "equity0": cfg.risk.start_equity, "btc_px0": btc})
        self.rec({"ev": "boot", "positions": len(self.st.positions), "followed": len(self.st.followed)})
        try:
            self.assets.update(self.info.meta(timeout=10))
        except Exception as e:
            log.warn("boot_meta_failed", err=str(e))
        problems = boot_repair(self.st, self.rec, self.gate, "main")
        if problems:
            self.ui.send("⚠️ <b>Restart with uncertainty</b> · ⏸️ entries paused, exits and stops keep running\n"
                         + "\n".join(f"• {tgfmt.esc(x)}" for x in problems[:10])
                         + "\nCheck, then /resume.")

        def boot_side(w):
            if not w.st.genesis_ms:
                w.rec({"ev": "genesis", "equity0": w.cfg.risk.start_equity, "btc_px0": self.st.btc_px0})
            w.rec({"ev": "boot", "positions": len(w.st.positions), "followed": len(w.st.followed)})
            side_problems = boot_repair(w.st, w.rec, w.gate, w.name)
            if side_problems:
                self.ui.send(f"⚠️ <b>Side wallet {tgfmt.esc(w.label)} restarted with uncertainty</b> · ⏸️ its entries "
                             f"paused\n" + "\n".join(f"• {tgfmt.esc(x)}" for x in side_problems[:5])
                             + "\n/resume resumes every wallet.")
            if self.st.entries_paused and not w.st.entries_paused:
                w.rec({"ev": "pause", "reason": self.st.pause_reason or "main wallet paused"})
            log.info("side_wallet", wallet=w.name, equity=round(w.st.equity(), 2), positions=len(w.st.positions),
                     trades=len(w.st.closed), paused=w.st.entries_paused)
        self.on_sides("boot", boot_side)
        self.sync_sides()
        for key, mid in self.st.cards.items():
            self.ui.restore_card(key, mid)
        log.info("state", equity=round(self.st.equity(), 2), positions=len(self.st.positions),
                 followed=len(self.st.followed), paused=self.st.entries_paused, trades=len(self.st.closed))
        for p in self.st.positions.values():
            log.info("position_restored", coin=p.coin, side=p.side, size=p.size, entry=p.entry_px, stop=p.stop_px,
                     leader=p.leader)
        self.ui.send(f"🤖 <b>Copybot started</b> (paper) · {len(self.st.positions)} open · "
                     f"{len(self.st.followed)} leaders" + (" · ⏸️ paused" if self.st.entries_paused else ""))

    def start_threads(self) -> None:
        self.feed.set_users(self.wanted_users())
        self.feed.start()
        self.ui.start()
        if self.sol:
            threading.Thread(target=self.sol.run, name="sol", daemon=True).start()
        threading.Thread(target=self.health_worker, name="health", daemon=True).start()
        threading.Thread(target=self.sync_worker, name="sync", daemon=True).start()
        if self.invo_watch:
            self.invo_watch.start()
        self.scorer.focus = set(self.st.followed)
        self.scorer.start()

    def wanted_users(self) -> set[str]:
        return set(self.st.followed) | self.position_leaders()

    def position_leaders(self) -> set[str]:
        """Owners and backers of open positions in any wallet: their fills and positions must keep being watched."""
        return {a for st in (self.st, *(w.st for w in self.sides)) for p in st.positions.values()
                for a in (p.leader, *p.backers) if not a.startswith(INVO)}       # Invo traders: no HL address

    def mirror_size(self, m, px: float, equity: float) -> float:
        """Mirror wallet: the leader's new position as a share of its account value (leverage included), times
        mirror_mult, of our equity; at least the minimum notional. Unknown account value: the fixed 1% risk size.
        The wallet's RiskGate then clamps it like any other copy."""
        r = self.cfg.risk
        v = self.leader_value.get(m.leader, 0.0)
        if v <= 0 or px <= 0:
            log.info("mirror_size_fallback", leader=m.leader, coin=m.coin, why="account value unknown")
            return equity * r.risk_per_trade_pct / 100 / (px * r.stop_pct / 100)
        share = abs(m.end_pos) * (m.px or px) / v
        notional = max(share * r.mirror_mult * equity, r.min_notional_usd * 1.05)
        log.info("mirror_size", leader=m.leader, coin=m.coin, leader_pct=round(share * 100, 3),
                 want_usd=round(notional, 2))
        return notional / px

    def invo_record_rows(self) -> dict:
        """Invo's own numbers for each followed trader (from the watcher's last read)."""
        out = {}
        for name, ports in (self.invo_watch.stats if self.invo_watch else {}).items():
            rows = [(f"Invo: {p.title.strip()[:24] or 'portfolio'}",
                     f"{p.win_rate:.0f}% win · {p.closed} calls · {p.open_count} open") for p in ports[:3]]
            out[INVO + name] = rows
        return out

    def invo_command(self, c) -> None:
        w = self.invo
        if w is None:
            return self.ui.send("🧾 Invo is off: save the bot's Invo login in a file, set INVO_TOKEN_FILE to it and "
                                "restart (see the README, Invo section).")
        if c.name in ("/invo", "/invotrades", "/invotraders"):
            key = {"/invo": "invo:status", "/invotrades": "invo:trades", "/invotraders": "invo:traders"}[c.name]
            self.live_cards.add(key)
            self.last_body.pop(key, None)
            return self.ui.set_card(key, self.render_card(key, self.health(), now_ms()), new=True)
        name = c.arg.strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_.]{2,40}", name):
            return self.ui.send(f"Usage: {c.name} &lt;Invo username&gt; (as in app.invoapp.com/&lt;username&gt;)")
        leader = INVO + name.lower()
        if c.name == "/invounfollow":
            if leader not in w.st.followed:
                return self.ui.send(f"🧾 @{tgfmt.esc(name)} is not followed.")
            held = sum(1 for p in w.st.positions.values() if p.leader == leader)
            w.rec({"ev": "unfollow", "leader": leader, "reason": "/invounfollow"})
            self.invo_watch.forget(name.lower())
            return self.ui.send(f"➖ 🧾 <b>Unfollowed</b> @{tgfmt.esc(name)}"
                                + (f" · {held} open copy still managed until its stop or its close" if held else ""))
        if leader in w.st.followed:
            return self.ui.send(f"✅ 🧾 @{tgfmt.esc(name)} is already followed.")
        if len(w.st.followed) >= self.cfg.invo.max_traders:
            return self.ui.send(f"⛔ 🧾 Already following {len(w.st.followed)} Invo traders (the maximum): "
                                "/invounfollow one first.")
        w.rec({"ev": "follow", "leader": leader})
        self.ui.send(f"➕ 🧾 <b>Following</b> @{tgfmt.esc(name)} on Invo · calls they open from now on are copied in "
                     "the 'invo calls' wallet (/hyperwallet compares it). Calls already open are not copied.")

    def invo_size(self, m, px: float, equity: float) -> float:
        """Invo calls wallet: the trader's exposure (portfolio share x leverage) x invo.size_mult of our equity,
        at least the minimum notional; the wallet's RiskGate clamps it."""
        c = self._invo_call
        share = c.exposure if c is not None else 0.0
        notional = max(share * self.cfg.invo.size_mult * equity, self.cfg.risk.min_notional_usd * 1.05)
        return notional / px if px > 0 else 0.0

    def invo_coin(self, ticker: str) -> str | None:
        """Invo ticker -> the Hyperliquid perp name (case can differ, e.g. kPEPE)."""
        if ticker in self.assets:
            return ticker
        return next((a for a in self.assets if a.upper() == ticker.upper()), None)

    def on_invo(self, kind: str, trader: str, call) -> None:
        w, leader = self.invo, INVO + trader
        if w is None or leader not in w.st.followed:
            return
        coin = self.invo_coin(call.ticker)
        side = 1 if call.long else -1
        if kind == "invo_open":
            if coin is None:
                return self.ui.send(f"🧾 Invo · @{tgfmt.esc(trader)} called {tgfmt.esc(call.ticker)}: not on "
                                    "Hyperliquid, not copied.")
            if coin in w.st.positions:
                log.info("invo_skip", trader=trader, coin=coin, why="coin already held")
                return
            m = Move(leader, coin, 0.0, float(side), call.created_ms, call.created_ms, abs(hash(call.id)) % 10**12,
                     (), call.entry, 1)
            self._invo_call = call
            try:
                w.pm.on_move(m)
            finally:
                self._invo_call = None
            p = w.st.positions.get(coin)
            if p is not None and p.leader == leader:
                self.invo_calls[call.id] = coin
                self.invo_info[p.pos_id] = (
                    f"Invo call by @{tgfmt.esc(trader)}: {call.size * 100:.1f}% x {call.leverage:g}x of their paper "
                    f"portfolio · their entry {tgfmt.fpx(call.entry)} · target "
                    f"{tgfmt.fpx(call.target) if call.target else '-'} · stop {tgfmt.fpx(call.stop) if call.stop else '-'}")
                self.invo_card(p, force=True)
            return
        held = self.invo_calls.pop(call.id, None) or coin
        p = w.st.positions.get(held) if held else None
        if p is None or p.leader != leader:
            return
        self._invo_close_why = call.reason_closed or "closed"
        try:
            w.pm.on_move(Move(leader, held, float(p.side), 0.0, int(time.time() * 1000), call.created_ms,
                              abs(hash(call.id)) % 10**12, (), call.closing_price or 0.0, 1))
        finally:
            self._invo_close_why = None

    def on_invo_notify(self, kind: str, **kw) -> None:
        """The Invo wallet's position manager: every close (the trader's, or our stop) finalizes the copy's card."""
        if kind == "closed":
            self.invo_final(kw["trade"], self._invo_close_why)

    def invo_final(self, t: dict, why: str | None = None) -> None:
        """An Invo copy closed (the trader's close, or our stop): its live card becomes the final summary."""
        key = f"invo:pos:{t['pos_id']}"
        self.last_body.pop(key, None)
        info = self.invo_info.pop(t["pos_id"], "")
        self.ui.final_card(key, tgfmt.closed_card(t) + "\n🧾 Invo" + (f" · closed by the trader ({tgfmt.esc(why)})"
                                                                     if why else "") + (f"\n{info}" if info else ""))

    def score_dict(self, leader: str) -> dict:
        return self.scores.get(leader) or self.scorer.scores.get(leader) or {}

    def score_of(self, leader: str) -> float:
        return float(self.score_dict(leader).get("score", 0.0))

    # ---- workers (never touch state: they only enqueue) ---------------------------------------------
    def health_worker(self) -> None:
        rt = self.cfg.runtime
        last_clock = 0.0
        while not self.stop.is_set():
            try:
                if time.time() - last_clock >= rt.clock_refresh_s:
                    self.clock.sample(self.info, rt.trading_timeout_s)
                    last_clock = time.time()
                _, mts = self.feed.mids()
                if time.time() - mts > 3:   # websocket prices late: REST fallback so stops keep working
                    mids = self.info.post({"type": "allMids"}, hl.CRITICAL, rt.trading_timeout_s)
                    self.feed.set_mids({k: float(v) for k, v in mids.items()}, "rest")
            except Exception as e:
                log.warn("health_worker", err=f"{type(e).__name__}: {e}"[:200])
                last_clock = min(last_clock, time.time() - rt.clock_refresh_s + 5)
            self.stop.wait(1.0)

    def sync_worker(self) -> None:
        rt = self.cfg.runtime
        last_rec, last_meta, last_values = 0.0, 0.0, 0.0
        while not self.stop.is_set():
            try:
                hour_start = (time.time() // 3600) * 3600
                if last_meta < hour_start + 5 and time.time() >= hour_start + 5 or time.time() - last_meta > 3600:
                    self.q.put(("meta", self.info.meta(timeout=10), int(time.time() * 1000)))
                    last_meta = time.time()
                if self.reconcile_now.is_set() or time.time() - last_rec >= rt.reconcile_s:
                    self.reconcile_now.clear()
                    last_rec = time.time()
                    for leader in sorted(self.leaders_with_positions):
                        self.q.put(("leader_pos", leader, self.info.positions(leader, rt.trading_timeout_s)))
                if self.cfg.risk.mirror_wallet and time.time() - last_values >= 300:
                    last_values = time.time()
                    for leader in sorted(self.st.followed):        # BULK class: never competes with exits
                        try:
                            self.leader_value[leader] = self.info.account(leader, timeout=10).value
                        except Exception as e:
                            log.warn("leader_value_failed", leader=leader, err=f"{type(e).__name__}"[:60])
            except Exception as e:
                log.warn("sync_worker", err=f"{type(e).__name__}: {e}"[:200])
            self.stop.wait(1.0)

    # ---- trading loop -------------------------------------------------------------------------------
    def run(self) -> None:
        self.boot()
        self.start_threads()
        last_ui = 0.0
        last_lag_log = time.time()
        last_beat = 0.0
        while not self.stop.is_set():
            t0 = time.time()
            if self.restart_at and t0 >= self.restart_at:
                log.info("restart", why="command")
                self.stop.set()
                break
            try:
                self.tick()
                if time.time() - last_ui >= 1.0:
                    self.refresh_ui()
                    last_ui = time.time()
                if time.time() - last_beat >= 60:
                    self.heartbeat()
                    last_beat = time.time()
                if time.time() - last_lag_log >= 3600:
                    self.log_lag()
                    last_lag_log = time.time()
            except Exception as e:
                log.exception("loop_error")
                self.alert(f"trading loop error: {type(e).__name__}: {e}"[:300], key=f"loop:{type(e).__name__}")
            self.stop.wait(max(0.0, self.cfg.runtime.tick_s - (time.time() - t0)))

    def tick(self) -> None:
        mids, _ = self.feed.mids()
        self.mids.update(mids)
        # 1. exits first: stops on the latest prices
        self.pm.check_stops()
        self.on_sides("stops", lambda w: w.pm.check_stops())
        # 2. events from workers
        deadline = time.time() + 0.2
        while time.time() < deadline and not self.restart_at:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            self.handle(item)
            self.mids.update(self.feed.mids()[0])
        # 3. calendar marks for the loss limits, funding
        now = now_ms()
        eq = self.st.equity(self.mids)
        for kind, key in (("day", day_key(now)), ("week", week_key(now))):
            if self.st.marks.get(kind, {}).get("key") != key:
                if kind == "day" and self.st.marks.get("day"):
                    self.ui.send(tgfmt.progress_text(self.st, self.mids, self.mids.get("BTC"), now))
                self.rec({"ev": "mark", "kind": kind, "key": key, "equity": eq})

        def marks(w):
            for kind, key in (("day", day_key(now)), ("week", week_key(now))):
                if w.st.marks.get(kind, {}).get("key") != key:
                    w.rec({"ev": "mark", "kind": kind, "key": key, "equity": w.st.equity(self.mids)})
        self.on_sides("marks", marks)
        self.leaders_with_positions = self.position_leaders()
        self.feed.set_users(self.wanted_users())
        if self.ws_down_since and not self.ws_alerted and \
                time.time() - self.ws_down_since >= self.cfg.runtime.ws_alert_after_s:
            self.ws_alerted = True
            self.alert(f"websocket down for {time.time() - self.ws_down_since:.0f}s: new copies refused until it is "
                       f"back (exits and stops keep working)", key="ws_down", every_s=1800)

    def handle(self, item) -> None:
        kind = item[0]
        if kind == "fills":
            ev, recv = item[1], item[2]
            if ev.user not in self.wanted_users():
                return
            for m in self.det.on_fills(ev.user, ev.fills, snapshot=ev.snapshot):
                self.pm.on_move(m)                                   # the main wallet always first
                self.on_sides("move", lambda w: w.pm.on_move(m))
        elif kind == "leader_pos":
            self.pm.reconcile(item[1], item[2])
            self.on_sides("reconcile", lambda w: w.pm.reconcile(item[1], item[2]))
        elif kind == "meta":
            assets, at = item[1], item[2]
            self.assets.update(assets)
            hk = hour_key(at)
            if self.st.marks.get("funding", {}).get("key") != hk and at % 3_600_000 < 600_000:
                self.pm.apply_funding(self.assets, hk)
                self.rec({"ev": "mark", "kind": "funding", "key": hk, "equity": self.st.equity(self.mids)})

            def funding(w):
                if w.st.marks.get("funding", {}).get("key") != hk:
                    w.pm.apply_funding(self.assets, hk)
                    w.rec({"ev": "mark", "kind": "funding", "key": hk, "equity": w.st.equity(self.mids)})
            if at % 3_600_000 < 600_000:
                self.on_sides("funding", funding)
        elif kind == "ws_up":
            self.reconcile_now.set()   # anything missed while disconnected is caught by reconcile
            if item[1] > 1:
                down = time.time() - self.ws_down_since if self.ws_down_since else 0.0
                log.warn("ws_reconnected", n=item[1], down_s=round(down, 1))
                if self.ws_alerted:
                    self.ui.send(f"✅ Websocket back after {down:.0f}s · copying again")
            self.ws_down_since, self.ws_alerted = 0.0, False
        elif kind == "ws_down":
            # Hyperliquid closes it about every 3 h ("Expired") and we reconnect in seconds: no alert for that
            log.warn("ws_down")
            self.ws_down_since = self.ws_down_since or time.time()
        elif kind == "cmd":
            self.command(item[1])
        elif kind == "card":
            key, mid = item[1], item[2]
            if mid is None:
                if key in self.st.cards:
                    self.rec({"ev": "card_drop", "key": key})
            elif self.st.cards.get(key) != mid:
                self.rec({"ev": "card", "key": key, "msg_id": mid})
        elif kind == "ranking":
            self.on_ranking(item[1], item[2], item[3])
        elif kind == "searched":
            self.on_searched(item[1], item[3])
        elif kind == "added":
            self.on_added(*item[1:])
        elif kind in ("invo_open", "invo_close"):
            try:
                self.on_invo(kind, item[1], item[2])
            except Exception:
                log.exception("invo_error", kind=kind)
                self.alert("Invo calls wallet error (logged); the other wallets are not affected", key="invo_err")
        elif kind == "invo_unknown":
            self.ui.send(f"⚠️ 🧾 No Invo user called @{tgfmt.esc(item[1])}: /invounfollow {tgfmt.esc(item[1])} and "
                         "check the name (as in app.invoapp.com/&lt;username&gt;).")
        elif kind == "invo_auth":
            if item[1]:
                self.ui.send("✅ 🧾 Invo login OK again.")
            else:
                self.alert(f"Invo login refused ({item[2]}): no new Invo copies until a fresh token is saved in "
                           "INVO_TOKEN_FILE (open copies keep their stops)", key="invo_auth", every_s=3600)
        elif kind == "review":
            weekly, n_pre, n_scored, n_el = item[1:]
            self.ui.send(f"🔎 <b>{'Weekly' if weekly else 'Daily'} review</b> · {n_pre} passed the pre-screen · "
                         f"{n_scored} fully scored · {n_el} eligible")
            if weekly:
                self.ui.send(tgfmt.progress_text(self.st, self.mids, self.mids.get("BTC"), now_ms()))
        elif kind == "alert":
            self.alert(item[1])

    # ---- selection ----------------------------------------------------------------------------------
    def on_ranking(self, ranking: list[str], n_scored: int, scores: dict) -> None:
        self.ranking = list(ranking)
        self.ranks = {a: i + 1 for i, a in enumerate(ranking)}
        self.scores = scores
        now = now_ms()
        if now - int(self.st.sel.get("at", 0)) < self.cfg.selection.rescore_minutes * 60_000 * 0.9:
            return   # a ranking re-published right after a restart is not a new cycle
        plan = select(self.st.sel, ranking, self.st.followed, set(self.st.paused_leaders), self.st.dropped, now, self.cfg)
        self.apply_plan(plan)
        self.rec({"ev": "sel", "state": plan.state})
        if not ranking:
            self.alert("no eligible wallet this cycle: following nobody new", key="no_eligible", every_s=6 * 3600)

    def on_searched(self, ranking: list[str], scores: dict) -> None:
        """The /search review finished: re-pick the best traders with what it found."""
        self.ranking, self.scores = list(ranking), scores
        self.ranks = {a: i + 1 for i, a in enumerate(ranking)}
        if not self.search_pending:
            return
        self.search_pending = False
        if not ranking:
            self.ui.send("🔎 <b>Search finished</b> · no eligible wallet found: keeping your current traders.")
            return
        self.repick("🔎 <b>Search finished</b>")

    def on_added(self, a: str, screened: dict | None, score: dict | None, ranking: list[str], scores: dict) -> None:
        """/hyperadd result: follow at once if it passes every rule and a slot is free, else say why not."""
        self.ranking, self.scores = list(ranking), scores
        self.ranks = {x: i + 1 for i, x in enumerate(ranking)}
        who = f"<code>{tgfmt.short(a)}</code>"
        if screened is None:
            self.ui.send(f"⚠️ /hyperadd {who}: could not read its trades from Hyperliquid. Try again in a minute.")
        elif not screened.get("ok"):
            self.ui.send(f"❌ /hyperadd {who} did not pass the first check: {tgfmt.esc(screened.get('reason', '?'))}. "
                         "Not followed.")
        elif not score or not score.get("eligible"):
            why = ", ".join((score or {}).get("reasons") or ["not scored"])
            self.ui.send(f"❌ /hyperadd {who} fails the strict rules: {tgfmt.esc(why)}. Not followed.")
        elif a in self.st.followed:
            self.ui.send(f"✅ /hyperadd {who} passes ({score.get('score', 0):.0f}/100) and is already followed.")
        elif a in self.st.paused_leaders:
            self.ui.send(f"⏸️ /hyperadd {who} passes but is paused after a bad streak of ours. Not followed.")
        elif len(self.st.followed) >= self.cfg.risk.max_leaders:
            self.ui.send(f"✅ /hyperadd {who} passes ({score.get('score', 0):.0f}/100), but you already follow "
                         f"{len(self.st.followed)}. It is in the ranking now: /hypersearch re-picks the best.")
        else:
            self.apply_plan(Plan([a], [], {}))

    def repick(self, title: str) -> None:
        plan = rebalance(self.ranking, self.st.followed, set(self.st.paused_leaders), self.st.dropped, now_ms(),
                         self.cfg)
        if not plan.joins and not plan.drops:
            self.ui.send(f"{title} · no change: you already follow the best {len(self.st.followed)} "
                         f"({len(self.ranking)} eligible).")
            return
        self.ui.send(f"{title} · following {len(plan.joins)} new, dropping {len(plan.drops)}.")
        self.apply_plan(plan)

    def apply_plan(self, plan: Plan) -> None:
        scores = self.scores
        for a, why in plan.drops:
            self.rec({"ev": "unfollow", "leader": a, "reason": why})
            held = sum(1 for p in self.st.positions.values() if p.leader == a)
            log.info("leader_dropped", leader=a, reason=why, open_copies=held)
            self.ui.send(f"➖ <b>Dropped</b> <code>{tgfmt.short(a)}</code> · {tgfmt.esc(why)}"
                         + (f" · {held} copy still managed until exit" if held else ""))
        for a in plan.joins:
            self.rec({"ev": "follow", "leader": a, "rank": self.ranks.get(a)})
            s = scores.get(a, {})
            log.info("leader_followed", leader=a, rank=self.ranks.get(a), score=s.get("score"))
            self.ui.send(f"➕ <b>Following</b> <code>{tgfmt.short(a)}</code> · rank #{self.ranks.get(a)}\n" + tgfmt.pre([
                ("Score", f"{s.get('score', 0):.0f}/100"),
                ("Coins", "all perps 🎲 (diversified)" if s.get("diversified") else "main coins"),
                ("Trades", str(s.get("trades", "-"))),
                ("Win", f"{s.get('win_rate', 0) * 100:.0f}%"),
                ("PF", tgfmt.pf_text(s.get("profit_factor", 0))),
                ("Avg per trade", f"{s.get('copy_edge_bps', 0) / 100:+.2f}% after our costs"),
                ("Biggest drop", f"{s.get('max_dd', 0) * 100:.0f}%"),
            ]))
        self.sync_sides()
        self.scorer.focus = set(self.st.followed)

    # ---- notifications from the position manager -----------------------------------------------------
    def on_notify(self, kind: str, **kw) -> None:
        if kind in ("opened", "updated"):
            p = self.st.positions.get(kw["coin"])
            if p:
                self.card_for(p, force=True)
        elif kind == "closed":
            t = kw["trade"]
            key = f"pos:{t['pos_id']}"
            self.last_body.pop(key, None)
            self.ui.final_card(key, tgfmt.closed_card(t))
        elif kind == "consensus":
            p = self.st.positions.get(kw["coin"])
            if p:
                self.card_for(p, force=True)
                extra = f"+{tgfmt.fusd(kw['size'] * p.entry_px, sign=False)} size" if kw["size"] else "no extra size"
                self.ui.send(f"🤝 <b>{tgfmt.esc(p.coin)}</b> {tgfmt.side_tag(p.side)} · "
                             f"<code>{tgfmt.short(kw['leader'])}</code> agrees with <code>{tgfmt.short(p.leader)}</code>"
                             f" · {extra}")
        elif kind == "handover":
            p = self.st.positions.get(kw["coin"])
            if p:
                self.card_for(p, force=True)
            self.ui.send(f"🔁 <b>{tgfmt.esc(kw['coin'])}</b> kept: <code>{tgfmt.short(kw['prev'])}</code> exited, "
                         f"<code>{tgfmt.short(kw['leader'])}</code> still holds and now leads the copy")
        elif kind == "conflict":
            self.ui.send(f"⚔️ <b>{tgfmt.esc(kw['coin'])}</b> conflict: switching to <code>{tgfmt.short(kw['leader'])}</code>"
                         f" (score {kw['score']:.0f}) over <code>{tgfmt.short(kw['holder'])}</code>"
                         f" (score {kw['holder_score']:.0f})")
        elif kind == "leader_paused":
            self.sync_sides()
            self.ui.send(f"⏸️ <b>Leader paused</b> <code>{tgfmt.short(kw['leader'])}</code> · "
                         f"{tgfmt.esc(kw['reason'])}\nNo new copies from it; open copies keep mirroring exits.")

    def card_for(self, p, force: bool = False, prefix: str = "pos:", extra: str = "") -> None:
        key = f"{prefix}{p.pos_id}"
        mark = self.mids.get(p.coin)
        body = tgfmt.trade_card(p, mark, 0) + extra
        if not force and self.last_body.get(key) == body:
            return   # nothing changed: no edit (the time stamp alone is not a change)
        self.last_body[key] = body
        self.ui.set_card(key, tgfmt.trade_card(p, mark, now_ms()) + extra)

    def invo_card(self, p, force: bool = False) -> None:
        info = self.invo_info.get(p.pos_id)
        self.card_for(p, force, prefix="invo:pos:", extra=f"\n🧾 {info}" if info else "\n🧾 Invo call")

    def refresh_ui(self) -> None:
        for p in list(self.st.positions.values()):
            self.card_for(p)
        if self.invo is not None:
            for p in list(self.invo.st.positions.values()):
                self.invo_card(p)
        h = self.health()
        for key in list(self.live_cards):
            # rendered at time 0 to see whether anything but the clock changed (no edit for a time stamp alone)
            body = self.render_card(key, h, 0)
            if self.last_body.get(key) != body:
                self.last_body[key] = body
                self.ui.set_card(key, self.render_card(key, h, now_ms()))

    def render_card(self, key: str, h, now: float) -> str:
        if key.startswith("invo:") and self.invo is not None:
            w = self.invo
            if key == "invo:trades":
                return tgfmt.trades_card(w.st, self.mids, now, title="🧾 <b>Invo trades</b>")
            if key == "invo:traders":
                return tgfmt.traders_card(w.st, self.mids, {}, {}, now, title="🧾 <b>Invo traders</b>",
                                          extra=self.invo_record_rows())
            return tgfmt.invo_text(w.st, self.mids, self.invo_watch, now)
        if key == "status":
            return tgfmt.status_card(self.st, self.mids, h, now, self.mids.get("BTC"))
        if key == "trades":
            return tgfmt.trades_card(self.st, self.mids, now)
        if key == "traders":
            return tgfmt.traders_card(self.st, self.mids, self.ranks, self.scores, now)
        if key == "wallets":
            return tgfmt.wallets_card([(self.cfg.risk.risk_per_trade_pct, self.st, True)]
                                      + [(w.risk_pct, w.st, False, w.label) for w in self.sides], self.mids, now)
        return tgfmt.leaders_card(self.st, self.ranks, now, self.scores)

    # ---- commands ------------------------------------------------------------------------------------
    def command(self, c) -> None:
        now = now_ms()
        if c.name.startswith("/fomo"):
            if self.sol:
                self.sol.q.put(("cmd", c))
            else:
                self.ui.send("🪙 FOMO is off: set enabled = true in config/sol.toml, then restart.")
            return
        if c.name == "/help":
            self.ui.send(HELP + "\n\n" + (FOMO_HELP if self.sol else
                                          "🪙 <b>FOMO</b> is off (enabled = true in config/sol.toml, then restart)"))
        elif c.name in ("/status", "/leaders", "/trades", "/traders", "/wallets"):
            key = c.name[1:]
            self.live_cards.add(key)
            self.last_body.pop(key, None)
            self.ui.set_card(key, self.render_card(key, self.health(), now), new=True)
        elif c.name == "/positions":
            self.ui.send(tgfmt.positions_text(self.st, self.mids))
        elif c.name == "/progress":
            self.ui.send(tgfmt.progress_text(self.st, self.mids, self.mids.get("BTC"), now))
        elif c.name == "/pause":
            self.rec({"ev": "pause", "reason": "/pause"})
            self.on_sides("pause", lambda w: w.rec({"ev": "pause", "reason": "/pause"}))
            self.ui.send("⏸️ <b>Entries paused.</b> Exits and stops keep running. /resume to continue.")
        elif c.name == "/resume":
            if self.st.uncertain:
                self.rec({"ev": "ack", "items": list(self.st.uncertain)})
            self.rec({"ev": "resume"})

            def resume(w):
                if w.st.uncertain:
                    w.rec({"ev": "ack", "items": list(w.st.uncertain)})
                w.rec({"ev": "resume"})
            self.on_sides("resume", resume)
            self.ui.send("▶️ <b>Entries resumed</b> (all wallets).")
        elif c.name == "/search":
            if not self.ranking:
                self.ui.send("🔎 The ranking is not ready yet (the bot just started). Try again in a minute.")
                return
            self.repick("🔎 <b>Search</b> · re-picked from the wallets checked so far")
            self.search_pending = True
            self.scorer.search_req.set()
            self.ui.send("🔎 Checking for new wallets in the background; I will re-pick again when it finishes.")
        elif c.name in ("/invo", "/invotrades", "/invotraders", "/invofollow", "/invounfollow"):
            self.invo_command(c)
        elif c.name == "/add":
            a = c.arg.strip()
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", a):
                self.ui.send("Usage: /hyperadd 0x… (a Hyperliquid wallet address, 0x and 40 hex characters)")
                return
            self.scorer.add_q.put(a.lower())
            self.ui.send(f"🔎 Checking <code>{tgfmt.short(a.lower())}</code> with the strict rules now (about a "
                         "minute); I will follow it if it passes and a slot is free.")
        elif c.name == "/restart":
            self.ui.send("🔄 <b>Restarting</b>… back in about 15 seconds.")
            self.restart_at = time.time() + 3
        elif c.name == "/reset":
            if not self.ui.check_pin(c.arg):
                log.warn("reset_bad_pin")
                self.ui.send("⛔ Wrong or missing PIN. Usage: /reset &lt;PIN&gt;")
                return
            wallets = [self.st, *(w.st for w in self.sides)]
            n_open = sum(len(st.positions) for st in wallets)
            if n_open:
                self.ui.send(f"⛔ <b>Reset refused</b>: {n_open} open trade(s). Wait for them to close, or "
                             f"/flatten &lt;PIN&gt; first.")
                return
            where = self.reset_wallets()
            self.ui.send(f"♻️ <b>Reset done</b> · every wallet is back to "
                         f"{tgfmt.fusd(self.cfg.risk.start_equity, sign=False)}, traders kept. Old history saved in "
                         f"<code>{tgfmt.esc(where)}</code>.\n🔄 Restarting… back in about 15 seconds.")
            self.restart_at = time.time() + 3
        elif c.name == "/flatten":
            if not self.ui.check_pin(c.arg):
                log.warn("flatten_bad_pin")
                self.ui.send("⛔ Wrong or missing PIN. Usage: /flatten &lt;PIN&gt;")
                return
            n = len(self.st.positions)
            self.rec({"ev": "pause", "reason": "/flatten"})
            self.pm.flatten("flatten")

            def flatten(w):
                w.rec({"ev": "pause", "reason": "/flatten"})
                w.pm.flatten("flatten")
            self.on_sides("flatten", flatten)
            self.ui.send(f"🛑 <b>Flattened</b> {n} position(s) (and every side wallet). ⏸️ Entries paused · "
                         f"/resume to continue.")

    def reset_wallets(self) -> str:
        """/reset: archive every ledger and start fresh ones that keep the followed leaders (with their 'since'),
        pauses, drop cooldowns and selection streaks. Nothing is deleted. The caller restarts the process."""
        st = self.st
        seed = [{"ev": "genesis", "equity0": self.cfg.risk.start_equity,
                 "btc_px0": self.mids.get("BTC") or st.btc_px0}]
        seed += [{"ev": "unfollow", "leader": a, "reason": "kept across /reset", "ts": t} for a, t in st.dropped.items()]
        seed += [{"ev": "follow", "leader": a, "ts": t} for a, t in st.followed.items()]
        seed += [{"ev": "leader_pause", "leader": a, "reason": why} for a, why in st.paused_leaders.items()]
        seed.append({"ev": "sel", "state": st.sel})
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        arch = self.data / "archive" / f"reset-{stamp}"
        arch.mkdir(parents=True, exist_ok=True)
        seed.append({"ev": "note", "text": f"reset; previous history in {arch.as_posix()}"})
        for lg in (self.ledger, *(w.ledger for w in self.sides)):
            lg.frozen = True       # nothing more is written to the old files before the restart
            lg.close()
        shutil.move(str(self.data / "ledger.jsonl"), str(arch / "ledger.jsonl"))
        if (self.data / "wallets").exists():
            shutil.move(str(self.data / "wallets"), str(arch / "wallets"))
        fresh = Ledger(self.data / "ledger.jsonl")
        for ev in seed:
            fresh.append(ev)
        fresh.close()
        if self.invo is not None and self.invo.st.followed:
            d = self.data / "wallets" / self.invo.name
            d.mkdir(parents=True, exist_ok=True)
            lg = Ledger(d / "ledger.jsonl")
            for a, t in self.invo.st.followed.items():
                lg.append({"ev": "follow", "leader": a, "ts": t})
            lg.close()
        log.info("reset", archive=arch.as_posix(), followed=len(st.followed))
        return arch.as_posix()

    def heartbeat(self) -> None:
        h = self.health()
        log.info("heartbeat", equity=round(self.st.equity(self.mids), 2), positions=len(self.st.positions),
                 followed=len(self.st.followed), paused=self.st.entries_paused, uncertain=len(self.st.uncertain),
                 trades=len(self.st.closed), mids_age_s=round(h.mids_age_s, 1), feed=h.feed_connected,
                 feed_age_s=round(h.feed_age_s, 1), clock_ok=h.clock_ok, clock_offset_ms=round(h.clock_offset_ms),
                 clock_why=h.clock_why, budget=round(self.budget.tokens), scored=len(self.scorer.scores),
                 screened=len(self.scorer.screened))

    def log_lag(self) -> None:
        lags = self.st.lags_ms
        if lags:
            log.info("lag_stats", n=len(lags), p50_ms=round(statistics.median(lags)),
                     p95_ms=round(tgfmt.pctl(lags, 0.95)))

    def shutdown(self) -> None:
        self.stop.set()
        self.feed.stop.set()
        self.ui.shutdown()
        self.scorer.stop.set()
        if self.sol:
            self.sol.shutdown()
        self.ledger.close()
        for w in self.sides:
            w.ledger.close()


DEFAULT_INVO_TOKEN = os.path.join("secrets", "invo.token")   # used when INVO_TOKEN_FILE is not set


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Hyperliquid copy-trading bot - PAPER MODE ONLY")
    ap.add_argument("--config", default="config")
    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2)
    for s in (cfg.tg_token, cfg.pin, cfg.dc_token, cfg.helius_key):
        log.add_secret(s)
    log.setup(cfg.runtime.log_dir)
    if not cfg.invo_token_file and os.path.exists(DEFAULT_INVO_TOKEN):
        # same default as tools/invo_check.py: a terminal opened before `setx INVO_TOKEN_FILE` still finds it
        cfg.invo_token_file = os.path.abspath(DEFAULT_INVO_TOKEN)
        log.info("invo_token_file", source="default secrets/invo.token")
    bot = Bot(cfg)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.info("shutdown", why="ctrl-c")
    except Exception:
        log.error("fatal", tb=traceback.format_exc()[-1500:])
        raise
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()
