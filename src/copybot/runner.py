"""The bot: one process, one trading loop (decisions + paper broker), worker threads for everything slow.

Only the Invo book runs (owner request 2026-10-26: the Hyperliquid and FOMO books, the arena and the bot's own Invo
search were removed). Hyperliquid still gives every price, order book and paper fill of the Invo copies.

Threads: trading loop (this module) | ws feed (prices) | health worker (clock + REST price fallback) |
sync worker (coin list + funding) | Invo watcher | discord gateway + outbox. Workers only talk to the trading loop
through one queue.
"""
from __future__ import annotations

import argparse
import re
import datetime as dt
import math
import os
import queue
import shutil
import statistics
import sys
import threading
import time
import traceback
from pathlib import Path

from copybot import config, hl, log, cardfmt
from copybot.broker import PaperBroker
from copybot.invo import (DELETED as INVO_DELETED, PREFIX as INVO, REPLACED_S as INVO_REPLACED_S,
                          RESIZE_MIN as INVO_RESIZE_MIN, InvoClient, Watcher as InvoWatcher)

EMERGENCY_STOP_PCT = 30.0   # "trader's exit" Invo wallets: the farthest stop, only a guard (a normal trade never gets
                            # there), lowered for big copies to just before their liquidation (invo_emergency_stop)


def invo_emergency_stop(mult: float) -> float:
    """The stop (%) of a "trader's exit" Invo wallet whose copies are `mult` x its equity: a real position that big
    needs ceil(mult) leverage and is liquidated about 1/leverage away (maintenance margin of a 20x coin, 5% margin)."""
    lev = max(1, math.ceil(mult - 1e-9))
    return min(EMERGENCY_STOP_PCT, (1.0 / lev - 0.025) * 0.95 * 100)
from copybot.feed import Clock, Feed
from copybot.ledger import Ledger, now_ms
from copybot.positions import Move
from copybot.risk import Health
from copybot.discord import DiscordUI
from copybot.chat import HELP
from copybot.wallets import SideWallet, boot_repair


def _traders(n: int) -> str:
    return f"{n} trader" + ("" if n == 1 else "s")


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
        # the bot's own ledger: start (genesis, BTC price then), restarts and the Discord card ids; every wallet
        # has its own ledger in data/wallets/<name>/
        self.ledger = Ledger(self.data / "ledger.jsonl")
        self.st = self.ledger.replay()
        self.mids: dict[str, float] = {}
        self.assets: dict[str, hl.Asset] = {}
        self.feed = Feed(rt.ws_url, self.q)
        self.clock = Clock()
        self.books: dict[str, tuple[float, object]] = {}   # coin -> (fetched at, book), shared by the wallets
        side_broker = PaperBroker(cfg.broker, self.recent_book)
        self.sides: list[SideWallet] = []                  # every Invo wallet
        self.invo: SideWallet | None = None          # the Invo calls wallet (needs INVO_TOKEN_FILE)
        self.invo_watch: InvoWatcher | None = None
        self.invo_calls: dict[tuple, str] = {}       # (wallet name, Invo call id) -> the coin we hold for it
        # (deciding wallet name, Invo call id) it would have copied but had no margin left for (audit 2026-10-24):
        # its 3%/8% versions still copy the call with their own margin
        self.invo_decided: set[tuple] = set()
        self.invo_extra: list[SideWallet] = []       # the fixed-risk Invo wallets (invo.risk_wallets_pct)
        self._invo_call = None                       # the call being opened (read by invo_size)
        self.invo_info: dict[str, str] = {}          # our position id -> what the trader called (for its card)
        self._invo_close_why: str | None = None      # the trader's close reason while it is being mirrored
        self.invo_baseline_wait: dict[str, list] = {}  # trader -> its first read, held until Hyperliquid's coins load
        self.side_broker = side_broker
        if cfg.invo.enabled and cfg.invo_token_file:
            self.invo, self.invo_extra = self.invo_set(dict(notify=self.on_invo_notify, pause_leaders=True))
            self.sides += [self.invo, *self.invo_extra]
            # watched: the followed traders AND the unfollowed ones we still hold a copy of (their exits are copied);
            # rebuilt on the trading thread (invo_refresh_watched), read by the watcher
            self.invo_watched: frozenset[str] = frozenset()
            self.invo_watch = InvoWatcher(InvoClient(cfg.invo.api_base, cfg.invo_token_file,
                                                     min_interval_s=cfg.invo.request_gap_s), self.q,
                                          lambda: self.invo_watched, cfg.invo.poll_s,
                                          cfg.invo.max_call_age_s, self.stop, skip=self.invo_skip)
            self.invo_refresh_watched()
            for x in self.invo_wallets():            # the call each open copy belongs to (ledger, audit 2026-10-11)
                for p in x.st.positions.values():
                    link = x.st.invo_links.get(p.pos_id)
                    if link and p.leader.startswith(INVO):
                        self.invo_calls[(x.name, link["call"])] = p.coin
                        if x is self.invo and link.get("info"):
                            self.invo_info[p.pos_id] = link["info"]
        elif cfg.invo.enabled:
            log.info("invo_off", why="INVO_TOKEN_FILE not set")
        on_cmd = lambda c: self.q.put(("cmd", c))
        # Discord card ids are kept in the ledger as "dc:<key>" (since Telegram ran next to it until 2026-10-10)
        self.ui = DiscordUI(cfg, on_cmd, lambda k, m: self.q.put(("card", "dc:" + k, m)))
        cardfmt.set_utc_offset(cfg.discord.utc_offset_hours)
        self.restart_at = 0.0        # /restart and /reset: stop the loop at this time (the outbox gets to flush)
        self.last_body: dict[str, str] = {}
        self.last_alert: dict[str, float] = {}
        self.live_cards: set[str] = set()
        self.ws_down_since = 0.0     # websocket down since (0 = up); alerted only if it stays down
        self.ws_alerted = False

    def invo_set(self, primary_kw: dict) -> tuple:
        """Every Invo size x every stop (owner request 2026-10-12): the trader-size wallet and the 1-20% risk wallets,
        each once per stop in invo.stops_pct (0 = the trader's exit). Returns (the trader-size wallet at the main stop,
        the others); `primary_kw` goes to the first one.
        A risk wallet risks its r% at ITS OWN stop (owner request 2026-10-13, "A": copies of r% x equity / stop; the
        trader's exit at its 30% guard), so a "20% risk" wallet never loses more than ~20% on one copy at any stop; it
        used to copy the 3%-stop notional at every stop (8% stop = 53% of the wallet). The trader-size versions keep
        one notional. The versions at invo.main_stop_pct keep the plain names (invo_calls, invo_risk_<r>pct), decide
        which calls are copied, and invo_calls posts the cards and owns the followed list; the others are named
        <name>_stop<s> / <name>_trader (owner request 2026-10-12: "keep the trades open until the traders close them",
        main_stop_pct 0; it was our 3% stop)."""
        cfg = self.cfg
        base, main = cfg.risk.stop_pct, cfg.invo.main_stop_pct
        stop_word = lambda s: f"{s:g}% stop" if s > 0 else "trader's exit"
        suffix = lambda s: "" if s == main else f"_stop{s:g}" if s > 0 else "_trader"
        sizes = [("invo_calls", "trader size", cfg.invo.limits_pct, cfg.invo.max_risk_pct / base, self.invo_size,
                  self.invo_cap)]
        sizes += [(f"invo_risk_{r:g}pct", f"{r:g}% risk", r, None, None, None) for r in cfg.invo.risk_wallets_pct]
        variants = []
        first, others = None, []
        for s in [main, *[s for s in cfg.invo.stops_pct if s != main]]:      # the deciding versions first
            for name, size_label, risk, mult, sizer, cap in sizes:
                gate_box: list = []              # this wallet's gate, once built (the sizer below needs its stop)
                if sizer is None:                # a risk wallet: r% of equity lost if its own stop is hit
                    stop = s if s > 0 else invo_emergency_stop(risk / EMERGENCY_STOP_PCT)

                    def stop_of(m, st=stop, box=gate_box, s=s):
                        """The stop distance (%) this copy will get. The trader's exit: the guard before liquidation
                        at the trader's leverage (often 5-8%, not the 30% cap: audit 2026-10-24, F1/F2)."""
                        if s > 0 or not box:
                            return st
                        g, asset = box[0], self.assets.get(m.coin)
                        if asset is None:
                            return st
                        return (1 - g.stop_for(1, 1.0, g.leverage_for(asset), asset)) * 100

                    # (lifted to the minimum notional: 1% at the 30% guard is ~10$, a smaller copy would be skipped)
                    sizer = lambda m, px, eq, r=risk, f=stop_of: max(
                        eq * r / f(m), cfg.risk.min_notional_usd * 1.05) / px if px > 0 else 0.0
                    # its limits: r% (at least room for a minimum copy at this stop: 1% at the 30% guard is ~10$)
                    level = max(risk, 120 * cfg.risk.min_notional_usd * stop / 100 / cfg.risk.start_equity)
                else:                            # trader size: the same notional at every stop, and the limits
                    stop = s if s > 0 else invo_emergency_stop(mult)   # of a wallet risking it at this stop
                    level = risk * stop / base
                primary = s == main and name == "invo_calls"
                x = SideWallet(cfg, level, self.data, self.side_broker, self.health, self.mids,
                               self.assets, name=name + suffix(s), label=f"{size_label} · {stop_word(s)}",
                               sizer=sizer, cap=cap, **(primary_kw if primary else {}))
                x.size_label, x.stop_label = size_label, stop_word(s)
                gate_box.append(x.gate)
                if s == 0:
                    # the trader's exit: the copy takes the trader's leverage (at most 10x) and the guard stop
                    # sits just before that leverage's liquidation (owner request 2026-10-14: a 40x call -> 10x)
                    x.gate.follow_leverage = True
                    x.gate.call_leverage = lambda: self._invo_call.leverage if self._invo_call else None
                if s != main:
                    x.base_name = name           # its main-stop version: the calls it copies (see invo_one)
                variants.append((x, s, stop))
                if primary:
                    first = x
                else:
                    others.append(x)
        for x, s, stop in variants:
            # a call is first seen up to a round (poll_s + the reads) after it was posted: entries may be that
            # old (still fail closed)
            x.cfg.risk.max_entry_age_s = cfg.invo.max_call_age_s + cfg.invo.poll_s + 10
            x.cfg.risk.trail_after_pct, x.cfg.risk.trail_pct = cfg.invo.trail_after_pct, cfg.invo.trail_pct
            # the leader pause compares a trader's copy drawdown with leader_pause_dd_pct of (equity /
            # max_leaders): set so the limit is invo.pause_dd_pct of the whole wallet
            x.cfg.risk.leader_pause_dd_pct = cfg.invo.pause_dd_pct * max(1, x.cfg.risk.max_leaders)
            # a second trader on the coin and side we hold adds size (owner request 2026-10-13, "B"), but the
            # first trader's exit still closes the whole copy (no hand-over to the second one)
            x.pm.handover = False
            if s != base:
                # its stop; the stop only has to sit before liquidation (a wider stop with the 3x buffer of the
                # usual one would leave no leverage for the same copy size)
                x.cfg.risk.stop_pct = stop
                x.cfg.risk.liq_buffer_mult = 1.0
            if s != main:
                # same calls as its main-stop version (owner request 2026-10-12: the 8% and trader-exit versions
                # of the bigger sizes copied 3 of 13 calls, their budget still full of copies the usual stop had
                # closed): that version decides which calls are copied, so the wallet-wide budgets here only stop
                # runaways. Per-trade and per-coin limits stay (same copy size); margin still limits what it holds.
                r = x.cfg.risk
                r.max_total_risk_pct, r.max_positions = 1000.0, 100
                r.daily_loss_pct = r.weekly_loss_pct = 100.0
        return first, others

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

    # ---- books: a fresh one, reused by the other wallets for a second ------------------------------------
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
        """Run fn(wallet) for every wallet; a failure in one is logged and never reaches the others."""
        for w in self.sides:
            try:
                fn(w)
            except Exception as e:
                log.exception("side_wallet_error", wallet=w.name, what=what)
                self.alert(f"wallet {w.name}: {what} failed ({type(e).__name__})", key=f"side:{w.name}:{what}")

    def alert(self, text: str, key: str | None = None, every_s: float = 600) -> None:
        k = key or text
        if time.time() - self.last_alert.get(k, 0) < every_s:
            return
        self.last_alert[k] = time.time()
        log.warn("alert", text=text)
        self.ui.send(f"⚠️ {cardfmt.esc(text)}")

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
        self.on_sides("boot", self.boot_side)
        for key, mid in self.st.cards.items():
            if key.startswith("dc:"):                 # (keys without the prefix were Telegram messages)
                self.ui.restore_card(key[3:], mid)
        lines = []
        if self.invo is not None:
            lines.append(f"🧾 Invo: {sum(len(w.st.positions) for w in self.invo_wallets())} open · "
                         f"{_traders(len(self.invo.st.followed))}" + (" · ⏸️ paused" if self.invo.st.entries_paused else ""))
        else:
            lines.append("🧾 Invo is off: save the bot's Invo login in a file and set INVO_TOKEN_FILE to it")
        self.ui.send("🤖 <b>Copybot started</b> (paper)\n" + "\n".join(lines))

    def boot_side(self, w) -> None:
        """Genesis, boot and restart repair of one wallet."""
        if not w.st.genesis_ms:
            w.rec({"ev": "genesis", "equity0": w.cfg.risk.start_equity, "btc_px0": self.st.btc_px0})
        w.rec({"ev": "boot", "positions": len(w.st.positions), "followed": len(w.st.followed)})
        side_problems = boot_repair(w.st, w.rec, w.gate, w.name, self.assets)
        if side_problems:
            self.ui.send(f"⚠️ <b>Wallet {cardfmt.esc(w.label)} restarted with uncertainty</b> · ⏸️ its entries "
                         f"paused\n" + "\n".join(f"• {cardfmt.esc(x)}" for x in side_problems[:5])
                         + "\n/invoresume resumes the Invo wallets.")
        log.info("side_wallet", wallet=w.name, equity=round(w.st.equity(), 2), positions=len(w.st.positions),
                 trades=len(w.st.closed), paused=w.st.entries_paused)

    def start_threads(self) -> None:
        self.feed.start()
        self.ui.start()
        threading.Thread(target=self.health_worker, name="health", daemon=True).start()
        threading.Thread(target=self.sync_worker, name="sync", daemon=True).start()
        if self.invo_watch:
            self.sync_invo_extras()
            self.invo_watch.start()

    def invo_record_rows(self) -> dict:
        """Invo's own numbers for the copied portfolios of each followed trader (from the watcher's last read)."""
        out = {}
        for name, ports in list((self.invo_watch.stats if self.invo_watch else {}).items()):
            ok = [p for p in ports if not self.invo_skip(name, p)]
            rows = [(f"{'💵' if p.kind == 'live' else '✅'} {p.title.strip()[:24] or 'portfolio'}",
                     f"{p.win_rate:.0f}% win · {p.pnl_pct:+,.0f}% · {p.closed} calls · {p.open_count} open"
                     + (" · real money" if p.kind == "live" else "")) for p in ok[:4]]
            if not ok and ports:
                rows = [("Portfolios", f"🚫 all {len(ports)} blocked by you")]
            out[INVO + name] = rows
        return out

    def invo_skip(self, name: str, p) -> str | None:
        """Why calls in this portfolio of `name` are not copied (None = they are). Every portfolio of a followed
        trader is copied (owner request 2026-10-14, the only way since the search was removed 2026-10-26); only
        /invoblock stops one."""
        if self.invo is not None and p.id in self.invo.st.blocked.get(INVO + name.lower(), {}):
            return "blocked by you"
        return None

    def invo_command(self, c) -> None:
        w = self.invo
        if w is None:
            return self.ui.send("🧾 Invo is off: save the bot's Invo login in a file, set INVO_TOKEN_FILE to it and "
                                "restart (see the README, Invo section).")
        if c.name == "/invoprogress":
            return self.ui.send("🧾 " + cardfmt.progress_text(w.st, self.mids, self.mids.get("BTC"), now_ms()))
        if c.name == "/invopause":
            for x in self.invo_wallets():
                x.rec({"ev": "pause", "reason": "/invopause"})
            return self.ui.send("⏸️ 🧾 <b>Invo copies paused</b> (every Invo wallet). Exits and stops keep running. "
                                "/invoresume to continue.")
        if c.name == "/invoresume":
            for x in self.invo_wallets():
                if x.st.uncertain:
                    x.rec({"ev": "ack", "items": list(x.st.uncertain)})
                x.rec({"ev": "resume"})
            return self.ui.send("▶️ 🧾 <b>Invo copies resumed</b> (every Invo wallet).")
        if c.name in ("/invoflatten", "/invoreset"):
            if not self.ui.check_pin(c.arg):
                log.warn("invo_bad_pin", cmd=c.name)
                return self.ui.send(f"⛔ Wrong or missing PIN. Usage: {c.name} &lt;PIN&gt;")
            if c.name == "/invoflatten":
                n = 0
                for x in self.invo_wallets():
                    n += len(x.st.positions)
                    x.rec({"ev": "pause", "reason": "/invoflatten"})
                    x.pm.flatten("flatten")
                return self.ui.send(f"🛑 🧾 <b>Invo flattened</b>: {n} trade(s) closed in the Invo wallets. ⏸️ Invo "
                                    "copies paused · /invoresume to continue.")
            n_open = sum(len(x.st.positions) for x in self.invo_wallets())
            if n_open:
                return self.ui.send(f"⛔ 🧾 <b>Invo reset refused</b>: {n_open} open trade(s). Wait for them to close, "
                                    "or /invoflatten &lt;PIN&gt; first.")
            where = self.invo_reset()
            self.ui.send(f"♻️ 🧾 <b>Invo reset done</b> · every Invo wallet is back to "
                         f"{cardfmt.fusd(self.cfg.risk.start_equity, sign=False)}, traders kept. Old history saved in "
                         f"<code>{cardfmt.esc(where)}</code>.\n🔄 Restarting… back in about 15 seconds.")
            self.restart_at = time.time() + 3
            return
        if c.name in ("/invostatus", "/invotrades", "/invotraders", "/invowallets"):
            key = {"/invostatus": "invo:status", "/invotrades": "invo:trades", "/invotraders": "invo:traders",
                   "/invowallets": "invo:wallets"}[c.name]
            self.live_cards.add(key)
            self.last_body.pop(key, None)
            return self.ui.set_card(key, self.render_card(key, self.health(), now_ms()), new=True)
        if c.name in ("/invoblock", "/invounblock"):
            return self.invo_block(c)
        name = c.arg.strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_.]{2,40}", name):
            return self.ui.send(f"Usage: {c.name} &lt;Invo username&gt; (as in app.invoapp.com/&lt;username&gt;)")
        leader = INVO + name.lower()
        if c.name == "/invounfollow":
            if leader not in w.st.followed:
                return self.ui.send(f"🧾 @{cardfmt.esc(name)} is not followed.")
            held = sum(1 for p in w.st.positions.values() if p.leader == leader)
            w.rec({"ev": "unfollow", "leader": leader, "reason": "/invounfollow"})
            self.sync_invo_extras()
            self.invo_watch.forget(name.lower())
            return self.ui.send(f"➖ 🧾 <b>Unfollowed</b> @{cardfmt.esc(name)}"
                                + (f" · {held} open copy still managed until its stop or its close" if held else ""))
        if leader in w.st.followed:
            return self.ui.send(f"✅ 🧾 @{cardfmt.esc(name)} is already followed (every portfolio is copied unless you "
                                "/invoblock it).")
        if len(w.st.followed) >= self.cfg.invo.max_traders:
            return self.ui.send(f"⛔ 🧾 Already following {len(w.st.followed)} Invo traders (the maximum): "
                                "/invounfollow one first.")
        w.rec({"ev": "follow", "leader": leader, "picked": True})
        self.sync_invo_extras()
        self.ui.send(f"➕ 🧾 <b>Following</b> @{cardfmt.esc(name)} on Invo · calls they open from now on are copied in "
                     "every Invo wallet (/invowallets compares them). Calls already open are not copied. Every "
                     f"portfolio of it is copied; /invoblock {cardfmt.esc(name)} &lt;portfolio&gt; stops one.")

    def invo_block(self, c) -> None:
        """/invoblock <user> <portfolio>: stop copying that portfolio of a followed trader (its open copies still follow
        the trader's exits); /invounblock undoes it. The portfolio is matched by the start of its name."""
        w = self.invo
        user, _, title = c.arg.strip().lstrip("@").partition(" ")
        user, title = user.lower(), title.strip().lower()
        leader = INVO + user
        if not user or not title:
            return self.ui.send(f"Usage: {c.name} &lt;Invo username&gt; &lt;portfolio name&gt;, e.g. {c.name} nicush "
                                "Probably nothing")
        if leader not in w.st.followed:
            return self.ui.send(f"🧾 @{cardfmt.esc(user)} is not followed.")
        ports = (self.invo_watch.stats if self.invo_watch else {}).get(user) or []
        hit = [p for p in ports if p.title.strip().lower() == title] or \
              [p for p in ports if p.title.strip().lower().startswith(title)]
        if len(hit) != 1:
            names = ", ".join(p.title.strip() for p in ports) or "(not read yet: try again in a few seconds)"
            return self.ui.send(f"🧾 @{cardfmt.esc(user)} has {'no' if not hit else 'several'} portfolio(s) matching "
                                f"“{cardfmt.esc(title)}”. Its portfolios: {cardfmt.esc(names)}")
        p = hit[0]
        if c.name == "/invoblock":
            w.rec({"ev": "invo_block", "leader": leader, "portfolio": p.id, "title": p.title.strip()})
            return self.ui.send(f"🚫 🧾 <b>Blocked</b> @{cardfmt.esc(user)} · {cardfmt.esc(p.title.strip())}: its new "
                                "calls are not copied (open copies still follow the trader's exits). /invounblock "
                                "undoes it.")
        if p.id not in w.st.blocked.get(leader, {}):
            return self.ui.send(f"🧾 {cardfmt.esc(p.title.strip())} of @{cardfmt.esc(user)} is not blocked.")
        w.rec({"ev": "invo_unblock", "leader": leader, "portfolio": p.id})
        self.ui.send(f"✅ 🧾 <b>Unblocked</b> @{cardfmt.esc(user)} · {cardfmt.esc(p.title.strip())}")

    def invo_size(self, m, px: float, equity: float) -> float:
        """Invo calls wallet: the trader's exposure (portfolio share x leverage) x invo.size_mult of our equity,
        capped at invo.max_risk_pct of our equity at our stop, at least the minimum notional; the wallet's RiskGate
        clamps it too."""
        c = self._invo_call
        share = c.exposure if c is not None else 0.0
        # real-money traders put far less of their account in one trade than paper callers (often < 1%)
        mult = self.cfg.invo.live_size_mult if c is not None and c.live else self.cfg.invo.size_mult
        notional = max(min(share * mult * equity, self.invo_cap(equity)),
                       self.cfg.risk.min_notional_usd * 1.05)
        return notional / px if px > 0 else 0.0

    def invo_cap(self, equity: float) -> float:
        """The most notional one Invo calls copy may hold (also after the trader's adds): invo.max_risk_pct of our
        equity at our stop (owner request 2026-10-09: a 25% x 5x call had become a 375$ copy of a 300$ wallet)."""
        return equity * self.cfg.invo.max_risk_pct / self.cfg.risk.stop_pct

    def invo_coin(self, ticker: str) -> str | None:
        """Invo ticker -> the Hyperliquid perp name of the same unit (case can differ; invo.aliases for renamed
        coins). PEPE does not map to kPEPE here (see invo_trade_coin)."""
        alias = {k.upper(): v for k, v in self.cfg.invo.aliases.items()}.get(ticker.upper())
        for t in ((alias, ticker) if alias else (ticker,)):
            if t in self.assets:
                return t
            hit = next((a for a in self.assets if a.upper() == t.upper()), None)
            if hit:
                return hit
        return None

    def invo_trade_coin(self, ticker: str) -> tuple[str | None, float]:
        """Invo ticker -> (Hyperliquid perp name, price factor) for copying. Hyperliquid lists some memecoins per
        1,000 coins with a "k" prefix (kPEPE, kBONK, kSHIB, kFLOKI ...): a PEPE call trades kPEPE and the trader's
        prices are x1000 there (owner request 2026-10-11: those calls were reported as not on Hyperliquid)."""
        coin = self.invo_coin(ticker)
        if coin is not None:
            return coin, 1.0
        k = "K" + ticker.upper()
        return next(((a, 1000.0) for a in self.assets if a.upper() == k and a[:1] == "k"), (None, 1.0))

    def invo_refresh_watched(self) -> None:
        """The traders the Invo watcher reads: followed ones, and unfollowed ones we still hold a copy of in any
        Invo wallet (owner request 2026-10-11: a dropped trader's exits were no longer copied)."""
        if self.invo is None:
            return
        names = {a[len(INVO):] for a in self.invo.st.followed}
        for w in self.invo_wallets():
            names |= {a[len(INVO):] for p in w.st.positions.values() for a in (p.leader, *p.backers)
                      if a.startswith(INVO)}
        if names != self.invo_watched:
            self.invo_watched = frozenset(names)

    def invo_held(self, w, call, coin: str | None) -> str | None:
        """The coin of OUR copy of this call in wallet w, or None. A call we never copied (e.g. skipped because the
        coin was already held for another call) must not move that other call's copy; the coin alone is only used
        when no call is known to own it (code audit 2026-10-10 #4)."""
        hit = self.invo_calls.get((w.name, call.id))
        if hit is not None:
            return hit
        if coin is None or any(k[0] == w.name and v == coin for k, v in self.invo_calls.items()):
            return None
        return coin

    def on_invo_baseline(self, trader: str, calls: list) -> None:
        """First read of a trader (start, re-follow): match our open copies to its open calls (by the call id saved
        with the copy, else by coin and side), and close copies whose call is gone (it closed while we were not
        watching; code audit 2026-10-10 #5). Held until Hyperliquid's coin list is loaded: without it no call maps to
        a coin and every copy would look orphaned (audit 2026-10-11)."""
        if not self.assets:
            log.warn("invo_baseline_waits", trader=trader, why="Hyperliquid coin list not loaded yet")
            self.invo_baseline_wait[trader] = calls
            return
        leader = INVO + trader
        ids = {c.id for c in calls}
        open_by = {}
        for c in calls:
            coin, _ = self.invo_trade_coin(c.ticker)
            if coin is not None:
                open_by.setdefault((coin, 1 if c.long else -1), []).append(c)
        by_id = {c.id: c for c in calls}
        for w in self.invo_wallets():
            # copies this trader backs (agreement, "B"): find its call again, or take the extra size off if it closed
            for p in [p for p in w.st.positions.values() if leader in p.backers]:
                mine = open_by.get((p.coin, p.side), [])
                if mine:
                    self.invo_calls[(w.name, mine[0].id)] = p.coin
                    continue
                log.warn("invo_backer_closed_unseen", wallet=w.name, trader=trader, coin=p.coin)
                try:
                    w.pm.on_move(Move(leader, p.coin, p.side * 1.0, 0.0, int(time.time() * 1000),
                                      int(time.time() * 1000), 0, (), 0.0, 1))
                except Exception:
                    log.exception("invo_wallet_error", wallet=w.name, kind="invo_baseline")
            for p in [p for p in w.st.positions.values() if p.leader == leader]:
                link = w.st.invo_links.get(p.pos_id)
                if link and link["call"] not in ids:
                    # Invo re-ids an edited call (live 2026-10-13): the same coin and side, created when we copied it
                    twin = next((c for c in open_by.get((p.coin, p.side), []) if c.id not in self.invo_linked(w)
                                 and abs(c.created_ms - p.opened_ms) <= (self.cfg.invo.max_call_age_s
                                                                         + INVO_REPLACED_S) * 1000), None)
                    if twin is not None:
                        log.info("invo_relinked", wallet=w.name, trader=trader, coin=p.coin, old=link["call"],
                                 new=twin.id, at="baseline")
                        self.invo_calls.pop((w.name, link["call"]), None)
                        w.rec({"ev": "invo_link", "pos_id": p.pos_id, "call": twin.id})
                if link:
                    if link["call"] in ids:
                        self.invo_calls[(w.name, link["call"])] = p.coin
                        self.invo_catch_up(w, p, by_id[link["call"]])
                        continue
                    self.invo_calls.pop((w.name, link["call"]), None)
                else:                                          # copies opened before calls were saved with them
                    mine = open_by.get((p.coin, p.side), [])
                    if len(mine) == 1:
                        self.invo_calls[(w.name, mine[0].id)] = p.coin
                    if mine:
                        continue
                log.warn("invo_closed_unseen", wallet=w.name, trader=trader, coin=p.coin)
                self._invo_close_why = "closed while the bot was not watching"
                try:
                    w.pm.on_move(Move(leader, p.coin, p.side * 1.0, 0.0, int(time.time() * 1000),
                                      int(time.time() * 1000), 0, (), 0.0, 1))
                    self.invo_closed_by(w, p, link["call"] if link else "", "unseen")
                except Exception:
                    log.exception("invo_wallet_error", wallet=w.name, kind="invo_baseline")
                finally:
                    self._invo_close_why = None

    def invo_agree(self, w, leader: str, trader: str, call, coin: str, side: int, fx: float) -> None:
        """A second followed trader opens the coin and side a copy already holds (owner request 2026-10-13, "B"):
        it becomes a backer and the copy gets extra size (`risk.consensus_risk_pct`, scaled like the wallet's other
        limits: half a normal copy; within `invo_cap` in the trader-size wallet; the per-coin limit caps the total).
        Its close takes that size off again; the first trader's exit still closes everything."""
        p = w.st.positions[coin]
        before = p.size
        w.pm.on_move(Move(leader, coin, 0.0, side * call.exposure, call.created_ms, call.created_ms,
                          abs(hash(call.id)) % 10**12, (), call.entry * fx, 1))
        q = w.st.positions.get(coin)
        if q is None or leader not in q.backers:
            return
        self.invo_calls[(w.name, call.id)] = coin
        if w is self.invo:
            self.invo_note(q, f"@{cardfmt.esc(trader)} also called it: +{(q.size - before) * q.entry_px:.2f}$ "
                              f"(their entry {cardfmt.fpx(call.entry)})")

    def invo_note(self, p, text: str) -> None:
        """Appends a line to an Invo copy's card text (kept in its ledger link)."""
        if p.pos_id not in self.invo_info:
            return
        self.invo_info[p.pos_id] += f"\n🤝 {text}"
        self.invo.rec({"ev": "invo_link", "pos_id": p.pos_id, "call": self.invo.st.invo_links[p.pos_id]["call"],
                       "info": self.invo_info[p.pos_id]})
        self.invo_card(p, force=True)

    def invo_linked(self, w) -> set:
        """Ids of the calls wallet w's open copies follow."""
        return {w.st.invo_links[q.pos_id]["call"] for q in w.st.positions.values() if q.pos_id in w.st.invo_links}

    def invo_catch_up(self, w, p, call) -> None:
        """A copy found again at a restart takes the call's size now (owner request 2026-10-13: "if the trader
        resizes it, resize it, always"): an add or trim made while the bot was off is mirrored like any other.
        Paper calls only: a real-money call's size is a % of an equity that moves with the price."""
        if call.units > 0 or call.exposure <= 0 or p.k <= 0:
            return
        was = p.size / p.k                                  # the trader's exposure our copy last followed
        if abs(call.exposure - was) / was <= INVO_RESIZE_MIN:
            return
        if p.leader not in w.st.followed and call.exposure > was:
            return                                          # an unfollowed trader's adds are not copied
        log.info("invo_resize", wallet=w.name, trader=p.leader[len(INVO):], coin=p.coin, old=round(was, 4),
                 new=round(call.exposure, 4), at="baseline")
        _, fx = self.invo_trade_coin(call.ticker)
        try:
            w.pm.on_move(Move(p.leader, p.coin, p.side * was, p.side * call.exposure, int(time.time() * 1000),
                              call.created_ms, abs(hash(call.id)) % 10**12 + 2, (), call.entry * fx, 1))
        except Exception:
            log.exception("invo_wallet_error", wallet=w.name, kind="invo_catch_up")

    def invo_closed_by(self, w, p, call_id: str, how: str) -> None:
        """Records how a copy closed by the trader's exit was known (ledger `invo_link`, field closed_by)."""
        q = w.st.positions.get(p.coin)
        if q is not None and q.pos_id == p.pos_id:
            return                                     # still open: nothing to record
        w.rec({"ev": "invo_link", "pos_id": p.pos_id, "call": call_id, "closed_by": how})
        if w is self.invo:
            log.info("invo_copy_closed", trader=p.leader[len(INVO):], coin=p.coin, how=how)

    def invo_wallets(self) -> list:
        """The trader-size Invo wallet first (it owns the followed list, the cards and the notes), then the fixed-risk
        Invo wallets (`invo.risk_wallets_pct`), which follow the same traders and get the same calls."""
        return [self.invo, *self.invo_extra] if self.invo is not None else []

    def sync_invo_extras(self) -> None:
        """The fixed-risk Invo wallets follow exactly the trader-size wallet's Invo traders."""
        if self.invo is None:
            return
        for w in self.invo_extra:
            for a, t in self.invo.st.followed.items():
                if a not in w.st.followed:
                    w.rec({"ev": "follow", "leader": a, "ts": t})
            for a in list(w.st.followed):
                if a not in self.invo.st.followed:
                    w.rec({"ev": "unfollow", "leader": a, "reason": "unfollowed in the Invo calls wallet"})

    def on_invo(self, kind: str, trader: str, call, new=None) -> None:
        leader = INVO + trader
        if self.invo is None:
            return
        main = leader in self.invo.st.followed
        if not main and (kind == "invo_open" or not any(
                p.leader == leader or leader in p.backers for w in self.invo_wallets()
                for p in w.st.positions.values())):
            return       # an unfollowed trader: only the exits and resizes of copies we still hold
        coin, fx = self.invo_trade_coin(call.ticker)
        if kind == "invo_open":
            stop_pct = abs(call.stop / call.entry - 1) * 100 if call.stop and call.entry else None
            target_pct = abs(call.target / call.entry - 1) * 100 if call.target and call.entry else None
            # the trader's own stop and target (owner request 2026-10-11: measure them before using them)
            log.info("invo_call", trader=trader, ticker=call.ticker, coin=coin, long=call.long, lev=call.leverage,
                     their_stop_pct=round(stop_pct, 2) if stop_pct is not None else "none",
                     their_target_pct=round(target_pct, 2) if target_pct is not None else "none")
            if coin is None:
                return self.ui.send(f"🧾 Invo · @{cardfmt.esc(trader)} called {cardfmt.esc(call.ticker)}: not on "
                                    "Hyperliquid, not copied.")
        self.sync_invo_extras()
        wallets = self.invo_wallets()
        if kind == "invo_open":
            port = self.invo_watch.ports.get(call.portfolio_id) if self.invo_watch is not None else None
            why = self.invo_skip(trader, port) if port is not None else None
            if why:
                log.info("invo_skip", wallet=self.invo.name, trader=trader, coin=coin, why=why)
                return
        for w in wallets:
            try:
                self.invo_one(w, kind, leader, trader, call, new, coin, fx)
            except Exception as e:          # one Invo wallet's failure never reaches the others
                log.exception("invo_wallet_error", wallet=w.name, kind=kind)
                self.alert(f"Invo wallet {w.name}: {kind} failed ({type(e).__name__})", key=f"invo:{w.name}:{kind}")
        self.invo_refresh_watched()

    def invo_one(self, w, kind: str, leader: str, trader: str, call, new, coin, fx: float = 1.0) -> None:
        primary = w is self.invo
        side = 1 if call.long else -1
        if kind == "invo_open":
            if leader not in w.st.followed:
                return
            base = getattr(w, "base_name", None)
            if base is not None and (base, call.id) not in self.invo_calls and (base, call.id) not in self.invo_decided:
                log.info("invo_skip", wallet=w.name, trader=trader, coin=coin, why=f"not copied by {base}")
                return
            held_p = w.st.positions.get(coin)
            if held_p is not None:
                if held_p.leader == leader or leader in held_p.backers or held_p.side != side:
                    log.info("invo_skip", wallet=w.name, trader=trader, coin=coin, why="coin already held")
                    return
                return self.invo_agree(w, leader, trader, call, coin, side, fx)
            # leader "position" = its committed exposure, so k = our size / exposure and adds/trims scale with it
            m = Move(leader, coin, 0.0, side * call.exposure, call.created_ms, call.created_ms,
                     abs(hash(call.id)) % 10**12, (), call.entry * fx, 1)
            self._invo_call = call
            w.pm.last_refusal = None
            try:
                w.pm.on_move(m)
            finally:
                self._invo_call = None
            p = w.st.positions.get(coin)
            if base is None and w.pm.last_refusal == "margin_full":
                # a deciding wallet (trader's exit: the trader's leverage, often 1x) out of margin: the call is still
                # one it copies, so its 3%/8% versions take it with their own margin (audit 2026-10-24, F3)
                self.invo_decided.add((w.name, call.id))
                log.info("invo_decided_no_margin", wallet=w.name, trader=trader, coin=coin)
            if p is not None and p.leader == leader:
                self.invo_calls[(w.name, call.id)] = coin
                # how much worse than the trader we got in (+ = we paid more for a long / got less for a short), in
                # the trader's own price unit (kPEPE = PEPE x 1000): the first number to watch live (audit 2026-10-11)
                gap = side * (p.entry_px / (call.entry * fx) - 1) * 100 if call.entry > 0 else 0.0
                info = ""
                port = self.invo_watch.ports.get(call.portfolio_id) if self.invo_watch is not None else None
                title = port.title.strip() if port is not None else ""
                if primary:
                    log.info("invo_entry_gap", trader=trader, coin=coin, gap_pct=round(gap, 3),
                             their=call.entry * fx, ours=p.entry_px,
                             age_s=round((time.time() * 1000 - call.created_ms) / 1000, 1))
                    kind_word = "real-money" if call.live else "paper"
                    # what getting in after them cost: the price gap on our notional (+ = we paid more)
                    cost = gap / 100 * p.size * p.entry_px
                    late = (f"{p.open_lag_ms / 1000:.1f}s after them" if p.open_lag_ms is not None else "after them")
                    fill = ("same price as theirs" if abs(gap) < 0.005 else
                            f"{abs(gap):.2f}% {'worse' if gap > 0 else 'better'} than their {cardfmt.fpx(call.entry)}"
                            f" ({cardfmt.fusd(-cost)})")
                    tp_sl = " · ".join(f"{n} {cardfmt.fpx(v)}" for n, v in (("target", call.target), ("their stop", call.stop)) if v)
                    info = (f"🧾 <b>@{cardfmt.esc(trader)}</b> · {call.committed * 100:.1f}% x {call.leverage:g}x · "
                            f"{kind_word}{' ' + cardfmt.esc(title) if title else ''}\n"
                            f"⏱️ In {late} · {fill}" + (f"\n🎯 {tp_sl}" if tp_sl else ""))
                    self.invo_info[p.pos_id] = info
                w.rec({"ev": "invo_link", "pos_id": p.pos_id, "call": call.id, "info": info,
                       "portfolio": call.portfolio_id, "title": title, "live": call.live, "coin": coin,
                       "gap": round(gap, 4)})
                if primary:
                    self.invo_card(p, force=True)
            return
        if kind == "invo_replace":
            # the trader's edited call under a new id (live 2026-10-13): our copy now follows the new id, and a size
            # change in the edit is mirrored like any add or trim
            held = self.invo_held(w, call, coin)
            p = w.st.positions.get(held) if held else None
            if p is None or p.side != side or (p.leader != leader and leader not in p.backers):
                return
            self.invo_calls.pop((w.name, call.id), None)
            self.invo_calls[(w.name, new.id)] = held
            if p.leader != leader:
                return                       # a backer's call: its later resizes do not change the extra size
            w.rec({"ev": "invo_link", "pos_id": p.pos_id, "call": new.id})
            log.info("invo_relinked", wallet=w.name, trader=trader, coin=held, old=call.id, new=new.id)
            if call.exposure > 0 and abs(new.exposure - call.exposure) / call.exposure > INVO_RESIZE_MIN:
                kind = "invo_resize"
            else:
                return
        if kind == "invo_resize":
            held = self.invo_held(w, new, coin)
            p = w.st.positions.get(held) if held else None
            if p is None or p.leader != leader or p.side != side:
                return
            if leader not in w.st.followed and new.exposure > call.exposure:
                return       # an unfollowed trader's trims are copied, its adds are not
            before = p.size
            w.pm.on_move(Move(leader, held, side * call.exposure, side * new.exposure, int(time.time() * 1000),
                              new.created_ms, abs(hash(new.id)) % 10**12 + 1, (), new.entry * fx, 1))
            p = w.st.positions.get(held)
            if primary and p is not None and p.pos_id in self.invo_info:
                base_info = re.sub(r"\n↕️[^\n]*| · resized by the trader[^\n]*", "", self.invo_info[p.pos_id])
                self.invo_info[p.pos_id] = base_info + (
                    f"\n↕️ Trader resized {call.committed * 100:.1f}% → {new.committed * 100:.1f}%")
                w.rec({"ev": "invo_link", "pos_id": p.pos_id, "call": new.id, "info": self.invo_info[p.pos_id]})
            log.info("invo_resize", wallet=w.name, trader=trader, coin=held, old=round(call.committed, 4),
                     new=round(new.committed, 4), ours_before=before, ours_after=p.size if p else 0)
            return
        held = self.invo_held(w, call, coin)
        self.invo_calls.pop((w.name, call.id), None)
        p = w.st.positions.get(held) if held else None
        if p is not None and p.side == side and leader in p.backers:
            # the second trader closed: take off the size it added (the first trader's copy stays)
            w.pm.on_move(Move(leader, held, side * max(call.exposure, 1e-9), 0.0, int(time.time() * 1000),
                              call.created_ms, abs(hash(call.id)) % 10**12, (), (call.closing_price or 0.0) * fx, 1))
            if primary:
                self.invo_note(w.st.positions.get(held) or p, f"@{cardfmt.esc(trader)} closed: extra size taken off")
            return
        if p is None or p.leader != leader or p.side != side:
            return
        for k in [k for k, v in self.invo_calls.items() if k[0] == w.name and v == held]:
            self.invo_calls.pop(k, None)     # its backers' calls no longer move anything
        self._invo_close_why = call.reason_closed or "closed"
        try:
            w.pm.on_move(Move(leader, held, p.side * max(call.exposure, 1e-9), 0.0, int(time.time() * 1000),
                              call.created_ms, abs(hash(call.id)) % 10**12, (), (call.closing_price or 0.0) * fx, 1))
        finally:
            self._invo_close_why = None
        # how the trader's close was known: in its closed calls ("confirmed") or the call just disappeared
        # ("vanished"): a copy closed by a wrong read would only show up here (analysis 2026-10-12)
        self.invo_closed_by(w, p, call.id, "vanished" if call.reason_closed == INVO_DELETED else "confirmed")

    def on_invo_notify(self, kind: str, **kw) -> None:
        """The Invo wallet's position manager: every close (the trader's, or our stop) finalizes the copy's card."""
        if kind == "closed":
            self.invo_final(kw["trade"], self._invo_close_why)
        elif kind == "leader_paused":
            self.q.put(("invo_drop", kw["leader"], kw["reason"]))     # after the close that triggered it

    def invo_final(self, t: dict, why: str | None = None) -> None:
        """An Invo copy closed (the trader's close, or our stop): its live card becomes the final summary."""
        key = f"invo:pos:{t['pos_id']}"
        self.last_body.pop(key, None)
        info = self.invo_info.pop(t["pos_id"], "")
        if info and not info.startswith("🧾"):
            info = "🧾 " + info
        self.ui.final_card(key, cardfmt.closed_card(t) + (f"\n{info}" if info else "\n🧾 Invo")
                           + (f"\n🏁 closed by the trader ({cardfmt.esc(why)})" if why else ""))

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
        """Hyperliquid's coin list (and its funding rates) once an hour, just after the hour."""
        last_meta = 0.0
        while not self.stop.is_set():
            try:
                hour_start = (time.time() // 3600) * 3600
                if last_meta < hour_start + 5 and time.time() >= hour_start + 5 or time.time() - last_meta > 3600:
                    self.q.put(("meta", self.info.meta(timeout=10), int(time.time() * 1000)))
                    last_meta = time.time()
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
        self.invo_refresh_watched()     # follows, drops and closes change who the Invo watcher reads
        # 3. calendar marks for the loss limits
        now = now_ms()

        def marks(w):
            for kind, key in (("day", day_key(now)), ("week", week_key(now))):
                if w.st.marks.get(kind, {}).get("key") != key:
                    w.rec({"ev": "mark", "kind": kind, "key": key, "equity": w.st.equity(self.mids)})
        self.on_sides("marks", marks)
        if self.ws_down_since and not self.ws_alerted and \
                time.time() - self.ws_down_since >= self.cfg.runtime.ws_alert_after_s:
            self.ws_alerted = True
            self.alert(f"websocket down for {time.time() - self.ws_down_since:.0f}s: new copies refused until it is "
                       f"back (exits and stops keep working)", key="ws_down", every_s=1800)

    def handle(self, item) -> None:
        kind = item[0]
        if kind == "cmd":
            self.command(item[1])
        elif kind == "meta":
            assets, at = item[1], item[2]
            self.assets.update(assets)
            for trader, calls in list(self.invo_baseline_wait.items()) if self.assets else ():
                del self.invo_baseline_wait[trader]
                try:
                    self.on_invo_baseline(trader, calls)
                except Exception:
                    log.exception("invo_error", kind="invo_baseline")
            hk = hour_key(at)

            def funding(w):
                if w.st.marks.get("funding", {}).get("key") != hk:
                    w.pm.apply_funding(self.assets, hk)
                    w.rec({"ev": "mark", "kind": "funding", "key": hk, "equity": w.st.equity(self.mids)})
            if at % 3_600_000 < 600_000:
                self.on_sides("funding", funding)
        elif kind == "ws_up":
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
        elif kind == "card":
            key, mid = item[1], item[2]
            if mid is None:
                if key in self.st.cards:
                    self.rec({"ev": "card_drop", "key": key})
            elif self.st.cards.get(key) != mid:
                self.rec({"ev": "card", "key": key, "msg_id": mid})
        elif kind in ("invo_open", "invo_close", "invo_resize", "invo_replace"):
            try:
                self.on_invo(kind, item[1], item[2], *item[3:])
            except Exception:
                log.exception("invo_error", kind=kind)
                self.alert("Invo calls wallet error (logged); the other wallets are not affected", key="invo_err")
        elif kind == "invo_baseline":
            try:
                self.on_invo_baseline(item[1], item[2])
            except Exception:
                log.exception("invo_error", kind=kind)
            self.invo_refresh_watched()
        elif kind == "invo_drop":
            self.invo_drop(item[1], item[2])
        elif kind == "invo_unknown":
            self.ui.send(f"⚠️ 🧾 No Invo user called @{cardfmt.esc(item[1])}: /invounfollow {cardfmt.esc(item[1])} and "
                         "check the name (as in app.invoapp.com/&lt;username&gt;).")
        elif kind == "invo_live":
            name, ok, why = item[1], item[2], item[3]
            if ok:
                self.ui.send(f"✅ 🧾 Invo shows @{cardfmt.esc(name)}'s real-money portfolio again: its calls are "
                             "copied again.")
            else:
                self.alert(f"Invo refused to show @{name}'s real-money portfolio ({why}): its new calls are not copied "
                           "until it works again; open copies keep their stops", key=f"invo_live:{name}", every_s=3600)
        elif kind == "invo_auth":
            if item[1]:
                self.ui.send("✅ 🧾 Invo login OK again.")
            else:
                self.alert(f"Invo login refused ({item[2]}): no new Invo copies until a fresh token is saved in "
                           "INVO_TOKEN_FILE (open copies keep their stops)", key="invo_auth", every_s=3600)
        elif kind == "alert":
            self.alert(item[1])

    def invo_drop(self, leader: str, why: str) -> None:
        """The Invo wallet's copies of this trader hit the leader pause rules (copy drawdown or losing streak, as for
        wallets): drop it at once. Not a blacklist: /invofollow brings it back."""
        w = self.invo
        if w is None or leader not in w.st.followed:
            return
        name = leader[len(INVO):]
        held = sum(1 for p in w.st.positions.values() if p.leader == leader)
        w.rec({"ev": "unfollow", "leader": leader, "reason": f"paused: {why}"})
        self.sync_invo_extras()
        self.invo_watch.forget(name)
        self.ui.send(f"➖ 🧾 <b>Dropped</b> @{cardfmt.esc(name)} · copying it lost too much ({cardfmt.esc(why)})"
                     + (f" · {held} open copy still managed" if held else "")
                     + f" · /invofollow {cardfmt.esc(name)} brings it back")

    def card_for(self, p, force: bool, prefix: str, extra: str, fmt) -> None:
        key = f"{prefix}{p.pos_id}"
        mark = self.mids.get(p.coin)
        body = fmt(p, mark, 0) + extra
        if not force and self.last_body.get(key) == body:
            return   # nothing changed: no edit (the time stamp alone is not a change)
        self.last_body[key] = body
        self.ui.set_card(key, fmt(p, mark, now_ms()) + extra)

    def invo_card(self, p, force: bool = False) -> None:
        info = self.invo_info.get(p.pos_id)
        if info and not info.startswith("🧾"):
            info = "🧾 " + info                       # copies opened before the 2026-10-14 layout
        self.card_for(p, force, prefix="invo:pos:", extra=f"\n{info}" if info else "\n🧾 Invo call",
                      fmt=cardfmt.invo_trade_card)

    def refresh_ui(self) -> None:
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
        if self.invo is not None:
            w = self.invo
            if key == "invo:wallets":
                return cardfmt.invo_wallets_card([(x.size_label, x.stop_label, x.st) for x in self.invo_wallets()],
                                                 self.mids, now)
            if key == "invo:trades":
                return cardfmt.trades_card(w.st, self.mids, now, title="🧾 <b>Invo trades</b>", infos=self.invo_info)
            if key == "invo:traders":
                return cardfmt.traders_card(w.st, self.mids, now, extra=self.invo_record_rows())
            return cardfmt.invo_text(w.st, self.mids, self.invo_watch, now)
        return "🧾 Invo is off: set INVO_TOKEN_FILE to the bot's Invo login file, then restart."

    # ---- commands ------------------------------------------------------------------------------------
    def command(self, c) -> None:
        """Every accepted name is canonical here (chat.canon): /invo<verb>, /help or /restart."""
        if c.name.startswith("/invo"):
            return self.invo_command(c)
        if c.name == "/restart":
            self.ui.send("🔄 <b>Restarting</b>… back in about 15 seconds.")
            self.restart_at = time.time() + 3
            return
        self.ui.send(HELP)

    def invo_reset(self) -> str:
        """/invoreset: archive the Invo wallets' ledgers and start fresh ones that keep the followed traders. The caller
        restarts the process."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        arch = self.data / "archive" / f"invo-reset-{stamp}" / "wallets"
        arch.mkdir(parents=True, exist_ok=True)
        follows = dict(self.invo.st.followed)
        for w in self.invo_wallets():
            w.ledger.frozen = True
            w.ledger.close()
            d = self.data / "wallets" / w.name
            if d.exists():
                shutil.move(str(d), str(arch / w.name))
        for d in sorted((self.data / "wallets").glob("invo_*")):   # wallets of an earlier invo.main_stop_pct
            if d.is_dir():
                shutil.move(str(d), str(arch / d.name))
        d = self.data / "wallets" / self.invo.name
        d.mkdir(parents=True, exist_ok=True)
        lg = Ledger(d / "ledger.jsonl")
        for a, t in follows.items():
            lg.append({"ev": "follow", "leader": a, "ts": t, **({"picked": True} if a in self.invo.st.picked else {})})
        lg.close()
        log.info("invo_reset", archive=arch.parent.as_posix(), followed=len(follows))
        return arch.parent.as_posix()

    def heartbeat(self) -> None:
        h = self.health()
        w = self.invo.st if self.invo is not None else self.st
        log.info("heartbeat", equity=round(w.equity(self.mids), 2), positions=len(w.positions),
                 followed=len(w.followed), paused=w.entries_paused, uncertain=len(w.uncertain),
                 trades=len(w.closed), mids_age_s=round(h.mids_age_s, 1), feed=h.feed_connected,
                 feed_age_s=round(h.feed_age_s, 1), clock_ok=h.clock_ok, clock_offset_ms=round(h.clock_offset_ms),
                 clock_why=h.clock_why, budget=round(self.budget.tokens))

    def log_lag(self) -> None:
        lags = self.invo.st.lags_ms if self.invo is not None else []
        if lags:
            log.info("lag_stats", n=len(lags), p50_ms=round(statistics.median(lags)),
                     p95_ms=round(cardfmt.pctl(lags, 0.95)))

    def shutdown(self) -> None:
        self.stop.set()
        self.feed.stop.set()
        self.ui.shutdown()
        self.ledger.close()
        for w in self.sides:
            w.ledger.close()


DEFAULT_INVO_TOKEN = os.path.join("secrets", "invo.token")   # used when INVO_TOKEN_FILE is not set


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Invo calls copy-trading bot - PAPER MODE ONLY")
    ap.add_argument("--config", default="config")
    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(2)
    for s in (cfg.pin, cfg.dc_token):
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
