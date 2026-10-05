"""The bot: one process, one trading loop (decisions + paper broker), worker threads for everything slow.

Threads: trading loop (this module) | ws feed | health worker (clock + REST price fallback) |
sync worker (leader reconcile + meta/funding) | scorer (leaderboard, fills, candles, scoring) |
telegram poll + outbox. Workers only talk to the trading loop through one queue.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import queue
import statistics
import sys
import threading
import time
import traceback
from pathlib import Path

from copybot import config, hl, log, tgfmt
from copybot.broker import PaperBroker
from copybot.detector import Detector
from copybot.feed import Clock, Feed
from copybot.ledger import Ledger, now_ms
from copybot.positions import PositionManager
from copybot.risk import Health, RiskGate
from copybot.selection import Scorer, select
from copybot.tg import HELP, TelegramUI


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
        self.broker = PaperBroker(cfg.broker, lambda c: self.info.book(c, rt.trading_timeout_s))
        self.pm = PositionManager(cfg, self.st, self.ledger, self.gate, self.broker, self.health, self.mids,
                                  self.assets, notify=self.on_notify, score_of=self.score_of)
        self.det = Detector()
        self.ui = TelegramUI(cfg, lambda c: self.q.put(("cmd", c)), lambda k, m: self.q.put(("card", k, m)))
        self.scorer = Scorer(cfg, self.info, self.q, self.data / "cache")
        self.ranks: dict[str, int] = {}
        self.scores: dict = {}
        self.reconcile_now = threading.Event()
        self.last_body: dict[str, str] = {}
        self.last_alert: dict[str, float] = {}
        self.last_funding_meta_ms = 0
        self.live_cards: set[str] = set()
        self.leaders_with_positions: set[str] = set()

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
        problems = list(self.st.uncertain)
        for iid in list(self.st.open_intents):
            self.rec({"ev": "intent_abort", "intent": iid})
        for p in list(self.st.positions.values()):  # never a stop-less position
            bad = p.stop_px <= 0 or (p.side > 0 and p.stop_px >= p.entry_px) or (p.side < 0 and p.stop_px <= p.entry_px)
            if bad:
                stop = self.gate.stop_for(p.side, p.entry_px)
                self.rec({"ev": "stop_set", "coin": p.coin, "pos_id": p.pos_id, "stop_px": stop})
                log.error("stop_repaired", coin=p.coin, stop=stop)
        if problems:
            self.st.uncertain = problems
            self.rec({"ev": "pause", "reason": "uncertain restart"})
            self.ui.send("⚠️ <b>Restart with uncertainty</b> · ⏸️ entries paused, exits and stops keep running\n"
                         + "\n".join(f"• {tgfmt.esc(x)}" for x in problems[:10])
                         + "\nCheck, then /resume.")
            log.error("uncertain_restart", problems=" | ".join(problems))
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
        threading.Thread(target=self.health_worker, name="health", daemon=True).start()
        threading.Thread(target=self.sync_worker, name="sync", daemon=True).start()
        self.scorer.focus = set(self.st.followed)
        self.scorer.start()

    def wanted_users(self) -> set[str]:
        return set(self.st.followed) | self.position_leaders()

    def position_leaders(self) -> set[str]:
        """Owners and backers of our open positions: their fills and positions must keep being watched."""
        return {a for p in self.st.positions.values() for a in (p.leader, *p.backers)}

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
        last_rec, last_meta = 0.0, 0.0
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
        # 2. events from workers
        deadline = time.time() + 0.2
        while time.time() < deadline:
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
        self.leaders_with_positions = self.position_leaders()
        self.feed.set_users(self.wanted_users())

    def handle(self, item) -> None:
        kind = item[0]
        if kind == "fills":
            ev, recv = item[1], item[2]
            if ev.user not in self.wanted_users():
                return
            for m in self.det.on_fills(ev.user, ev.fills, snapshot=ev.snapshot):
                self.pm.on_move(m)
        elif kind == "leader_pos":
            self.pm.reconcile(item[1], item[2])
        elif kind == "meta":
            assets, at = item[1], item[2]
            self.assets.update(assets)
            hk = hour_key(at)
            if self.st.marks.get("funding", {}).get("key") != hk and at % 3_600_000 < 600_000:
                self.pm.apply_funding(self.assets, hk)
                self.rec({"ev": "mark", "kind": "funding", "key": hk, "equity": self.st.equity(self.mids)})
        elif kind == "ws_up":
            self.reconcile_now.set()   # anything missed while disconnected is caught by reconcile
            if item[1] > 1:
                log.warn("ws_reconnected", n=item[1])
        elif kind == "ws_down":
            self.alert("websocket disconnected: entries refused until it is back", key="ws_down", every_s=1800)
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
        self.ranks = {a: i + 1 for i, a in enumerate(ranking)}
        self.scores = scores
        now = now_ms()
        if now - int(self.st.sel.get("at", 0)) < self.cfg.selection.rescore_minutes * 60_000 * 0.9:
            return   # a ranking re-published right after a restart is not a new cycle
        plan = select(self.st.sel, ranking, self.st.followed, set(self.st.paused_leaders), self.st.dropped, now, self.cfg)
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
                ("PF", f"{s.get('profit_factor', 0):.2f}"),
                ("Edge", f"{s.get('copy_edge_bps', 0):.1f} bps after costs"),
                ("Max DD", f"{s.get('max_dd', 0) * 100:.0f}%"),
            ]))
        self.rec({"ev": "sel", "state": plan.state})
        self.scorer.focus = set(self.st.followed)
        if not ranking:
            self.alert("no eligible wallet this cycle: following nobody new", key="no_eligible", every_s=6 * 3600)

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
            self.ui.send(f"⏸️ <b>Leader paused</b> <code>{tgfmt.short(kw['leader'])}</code> · "
                         f"{tgfmt.esc(kw['reason'])}\nNo new copies from it; open copies keep mirroring exits.")

    def card_for(self, p, force: bool = False) -> None:
        key = f"pos:{p.pos_id}"
        mark = self.mids.get(p.coin)
        body = tgfmt.trade_card(p, mark, 0)
        if not force and self.last_body.get(key) == body:
            return   # nothing changed: no edit (the time stamp alone is not a change)
        self.last_body[key] = body
        self.ui.set_card(key, tgfmt.trade_card(p, mark, now_ms()))

    def refresh_ui(self) -> None:
        for p in list(self.st.positions.values()):
            self.card_for(p)
        h = self.health()
        for key in list(self.live_cards):
            # rendered at time 0 to see whether anything but the clock changed (no edit for a time stamp alone)
            body = self.render_card(key, h, 0)
            if self.last_body.get(key) != body:
                self.last_body[key] = body
                self.ui.set_card(key, self.render_card(key, h, now_ms()))

    def render_card(self, key: str, h, now: float) -> str:
        if key == "status":
            return tgfmt.status_card(self.st, self.mids, h, now, self.mids.get("BTC"))
        if key == "trades":
            return tgfmt.trades_card(self.st, self.mids, now)
        if key == "traders":
            return tgfmt.traders_card(self.st, self.mids, self.ranks, self.scores, now)
        return tgfmt.leaders_card(self.st, self.ranks, now, self.scores)

    # ---- commands ------------------------------------------------------------------------------------
    def command(self, c) -> None:
        now = now_ms()
        if c.name == "/help":
            self.ui.send(HELP)
        elif c.name in ("/status", "/leaders", "/trades", "/traders"):
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
            self.ui.send("⏸️ <b>Entries paused.</b> Exits and stops keep running. /resume to continue.")
        elif c.name == "/resume":
            if self.st.uncertain:
                self.rec({"ev": "ack", "items": list(self.st.uncertain)})
            self.rec({"ev": "resume"})
            self.ui.send("▶️ <b>Entries resumed.</b>")
        elif c.name == "/flatten":
            if not self.ui.check_pin(c.arg):
                log.warn("flatten_bad_pin")
                self.ui.send("⛔ Wrong or missing PIN. Usage: /flatten &lt;PIN&gt;")
                return
            n = len(self.st.positions)
            self.rec({"ev": "pause", "reason": "/flatten"})
            self.pm.flatten("flatten")
            self.ui.send(f"🛑 <b>Flattened</b> {n} position(s). ⏸️ Entries paused · /resume to continue.")

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
        self.ui.stop.set()
        self.scorer.stop.set()
        self.ledger.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Hyperliquid copy-trading bot - PAPER MODE ONLY")
    ap.add_argument("--config", default="config")
    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2)
    for s in (cfg.tg_token, cfg.pin):
        log.add_secret(s)
    log.setup(cfg.runtime.log_dir)
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
