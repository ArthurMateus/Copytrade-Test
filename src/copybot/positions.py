"""Position manager: mirrors leader moves on our paper wallet, places the stop at entry, runs stops,
funding, reconciliation and leader pause rules. Every order goes through the RiskGate, every state change
is written to the ledger (write-ahead intent, then result) before it is acted upon in memory.

Copy model: when we open we record k = our_size / |leader_position|. Afterwards our target size is
k x |leader_position|: leader adds -> we add (risk-capped), leader reduces -> we reduce, leader closes ->
we close, leader flips -> we close and open a fresh, risk-sized copy on the other side. Further fills of the
SAME leader order that opened the position only rebase k (one order split into many fills is one trade).
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Callable

from copybot import log
from copybot.broker import NoBook, PaperBroker, funding_payment
from copybot.config import Config
from copybot.detector import Move
from copybot.hl import Asset
from copybot.ledger import Ledger, Position, State
from copybot.risk import Health, Order, RiskGate


class PositionManager:
    def __init__(self, cfg: Config, st: State, ledger: Ledger, gate: RiskGate, broker: PaperBroker,
                 health: Callable[[], Health], mids: dict[str, float], assets: dict[str, Asset],
                 notify: Callable[..., None] = lambda *a, **k: None):
        self.cfg, self.st, self.ledger, self.gate, self.broker = cfg, st, ledger, gate, broker
        self.health, self.mids, self.assets, self.notify = health, mids, assets, notify

    # ---- ledger helpers -----------------------------------------------------------------------
    def _rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    def _skip(self, why: str, **kw) -> None:
        log.info("copy_skip", reason=why, **kw)
        if why.startswith("below_min_notional"):
            self._rec({"ev": "count", "name": "skipped_min_notional"})
        # `kind` is notify()'s own first argument: pass the order kind under another name
        self.notify("skip", reason=why, **{("order" if k == "kind" else k): v for k, v in kw.items()})

    def _lag(self, h: Health, move: Move | None) -> float | None:
        return None if move is None else h.exchange_now_ms() - move.time_ms

    # ---- leader moves -------------------------------------------------------------------------
    def on_move(self, m: Move) -> None:
        pos = self.st.positions.get(m.coin)
        log.info("leader_move", leader=m.leader, coin=m.coin, kind=m.kind, start=m.start_pos, end=m.end_pos,
                 px=m.px, fills=m.n_fills, oid=m.oid)
        if pos is not None and pos.leader != m.leader:
            return self._skip("symbol_held_for_other_leader", coin=m.coin, leader=m.leader, holder=pos.leader)
        kind = m.kind
        if kind == "open" or (kind == "flip" and pos is None):
            if pos is not None:  # we still hold a copy the leader no longer had: stale, close it first
                self.close(m.coin, "leader_reopened", move=m)
            return self.open(m)
        if pos is None:
            return self._skip(f"no_copy_for_{kind}", coin=m.coin, leader=m.leader)
        if kind == "close":
            return self.close(m.coin, "leader_close", move=m)
        if kind == "flip":
            self.close(m.coin, "leader_flip", move=m)
            return self.open(m)
        target = pos.k * abs(m.end_pos)
        if kind == "add":
            if m.oid == pos.open_oid:  # more fills of the order that opened: rebase, do not add
                k = pos.size / abs(m.end_pos)
                self._rec({"ev": "rebase", "coin": m.coin, "pos_id": pos.pos_id, "k": k})
                return
            return self.add(m, target - pos.size)
        if kind == "reduce":
            return self.reduce(m.coin, pos.size - target, "leader_reduce", move=m)

    # ---- entries ------------------------------------------------------------------------------
    def open(self, m: Move) -> None:
        h = self.health()
        side = m.new_side
        asset = self.assets.get(m.coin)
        px = self.mids.get(m.coin) or m.px
        want = self.gate.entry_size(self.st.equity(self.mids), px)
        d = self.gate.check(Order("open", m.coin, side, want, px, leader=m.leader, fill_time_ms=m.time_ms),
                            self.st, h, self.mids, asset)
        if not d:
            return self._skip(d.reason, coin=m.coin, leader=m.leader, side=side)
        iid = uuid.uuid4().hex[:12]
        self._rec({"ev": "intent", "intent": iid, "action": "open", "coin": m.coin})
        try:
            f = self.broker.market(m.coin, side > 0, d.size)
        except NoBook as e:
            self._rec({"ev": "intent_abort", "intent": iid})
            return self._skip("no_book", coin=m.coin, err=str(e))
        lag = self._lag(self.health(), m)
        # the stop is placed at entry, from the actual fill price; it is part of the same durable event
        stop = self.gate.stop_for(side, f.px)
        pos = Position(pos_id=f"{m.coin}-{iid}", coin=m.coin, side=side, size=d.size, entry_px=f.px,
                       stop_px=stop, leverage=d.leverage, leader=m.leader, k=d.size / abs(m.end_pos),
                       open_oid=m.oid, opened_ms=h.now_ms, open_lag_ms=lag)
        self._rec({"ev": "open", "intent": iid, "pos": asdict(pos), "fee": f.fee, "lag_ms": lag,
                   "slip_bps": f.slippage_bps, "leader_px": m.px})
        self.gate.record_order()
        log.info("copy_open", coin=m.coin, side="long" if side > 0 else "short", size=d.size, px=f.px, stop=stop,
                 lev=d.leverage, notional=round(d.size * f.px, 2), fee=f.fee, lag_ms=lag, leader=m.leader,
                 slip_bps=round(f.slippage_bps, 2))
        self.notify("opened", coin=m.coin)

    def add(self, m: Move, size: float) -> None:
        pos = self.st.positions[m.coin]
        h = self.health()
        px = self.mids.get(m.coin) or m.px
        d = self.gate.check(Order("add", m.coin, pos.side, size, px, leader=m.leader, fill_time_ms=m.time_ms),
                            self.st, h, self.mids, self.assets.get(m.coin))
        if not d:
            return self._skip(d.reason, coin=m.coin, leader=m.leader, kind="add")
        iid = uuid.uuid4().hex[:12]
        self._rec({"ev": "intent", "intent": iid, "action": "add", "coin": m.coin})
        try:
            f = self.broker.market(m.coin, pos.side > 0, d.size)
        except NoBook as e:
            self._rec({"ev": "intent_abort", "intent": iid})
            return self._skip("no_book", coin=m.coin, err=str(e))
        lag = self._lag(self.health(), m)
        # k follows what we actually hold, so a clamped add does not make us chase the leader forever
        k = (pos.size + d.size) / abs(m.end_pos)
        self._rec({"ev": "add", "intent": iid, "coin": m.coin, "pos_id": pos.pos_id, "sz": d.size, "px": f.px,
                   "fee": f.fee, "k": k, "lag_ms": lag})
        self.gate.record_order()
        log.info("copy_add", coin=m.coin, size=d.size, px=f.px, new_size=pos.size, lag_ms=lag)
        self.notify("updated", coin=m.coin)

    # ---- exits (never blocked by staleness, pauses or rate limits) ------------------------------
    def reduce(self, coin: str, size: float, reason: str, move: Move | None = None) -> None:
        pos = self.st.positions.get(coin)
        if pos is None or size <= 0:
            return
        mid = self.mids.get(coin) or pos.entry_px
        d = self.gate.check(Order("reduce", coin, pos.side, size, mid), self.st, self.health(), self.mids, None)
        if not d:
            return self._skip(d.reason, coin=coin, kind="reduce")
        if d.size >= pos.size:
            return self.close(coin, reason, move=move)
        h = self.health()
        iid = uuid.uuid4().hex[:12]
        self._rec({"ev": "intent", "intent": iid, "action": "reduce", "coin": coin})
        f = self.broker.market(coin, pos.side < 0, d.size, exit=True, mid=mid)
        lag = self._lag(self.health(), move)
        self._rec({"ev": "reduce", "intent": iid, "coin": coin, "pos_id": pos.pos_id, "sz": d.size, "px": f.px,
                   "fee": f.fee, "reason": reason, "lag_ms": lag})
        self.gate.record_order()
        log.info("copy_reduce", coin=coin, size=d.size, px=f.px, left=pos.size, reason=reason, lag_ms=lag,
                 src=f.source)
        self.notify("updated", coin=coin)

    def close(self, coin: str, reason: str, move: Move | None = None) -> None:
        pos = self.st.positions.get(coin)
        if pos is None:
            return
        mid = self.mids.get(coin) or pos.entry_px
        d = self.gate.check(Order("close", coin, pos.side, pos.size, mid), self.st, self.health(), self.mids, None)
        assert d.ok, d.reason  # exits are never refused
        h = self.health()
        iid = uuid.uuid4().hex[:12]
        self._rec({"ev": "intent", "intent": iid, "action": "close", "coin": coin})
        f = self.broker.market(coin, pos.side < 0, pos.size, exit=True, mid=mid)
        lag = self._lag(self.health(), move)
        leader, pos_id = pos.leader, pos.pos_id
        self._rec({"ev": "close", "intent": iid, "coin": coin, "pos_id": pos_id, "px": f.px, "fee": f.fee,
                   "reason": reason, "lag_ms": lag, "src": f.source})
        self.gate.record_order()
        trade = self.st.closed[-1]
        log.info("copy_close", coin=coin, px=f.px, pnl=round(trade["pnl"], 4), reason=reason, lag_ms=lag,
                 src=f.source, leader=leader)
        self.notify("closed", coin=coin, trade=trade)
        self._check_leader(leader)

    # ---- periodic duties ----------------------------------------------------------------------
    def check_stops(self) -> None:
        for p in list(self.st.positions.values()):
            m = self.mids.get(p.coin)
            if m and p.stop_hit(m):
                log.warn("stop_hit", coin=p.coin, mark=m, stop=p.stop_px)
                self.close(p.coin, "stop")

    def apply_funding(self, assets: dict[str, Asset], hour_key: str) -> None:
        for p in list(self.st.positions.values()):
            a = assets.get(p.coin)
            mark = self.mids.get(p.coin) or (a.mark if a else 0)
            if not a or not mark:
                log.warn("funding_missing", coin=p.coin)
                continue
            amt = funding_payment(p.side, p.size, mark, a.funding)
            self._rec({"ev": "funding", "coin": p.coin, "pos_id": p.pos_id, "amount": amt, "rate": a.funding,
                       "hour": hour_key})

    def reconcile(self, leader: str, leader_pos: dict[str, float]) -> None:
        """Safety net for missed websocket fills: compare with the leader's real positions."""
        for p in list(self.st.positions.values()):
            if p.leader != leader:
                continue
            szi = leader_pos.get(p.coin, 0.0)
            if szi == 0 or (szi > 0) != (p.side > 0):
                log.warn("reconcile_exit", coin=p.coin, leader=leader, leader_pos=szi)
                self._rec({"ev": "count", "name": "reconcile_exits"})
                self.close(p.coin, "reconcile_leader_flat")
            elif p.k * abs(szi) < p.size * 0.98:
                log.warn("reconcile_reduce", coin=p.coin, leader=leader, leader_pos=szi, ours=p.size)
                self._rec({"ev": "count", "name": "reconcile_reduces"})
                self.reduce(p.coin, p.size - p.k * abs(szi), "reconcile_leader_reduced")

    def flatten(self, reason: str) -> None:
        for coin in list(self.st.positions):
            self.close(coin, reason)

    def _check_leader(self, leader: str) -> None:
        st = self.st.leader_stats.get(leader)
        if st is None or leader in self.st.paused_leaders or leader not in self.st.followed:
            return
        r = self.cfg.risk
        alloc = self.st.equity(self.mids) / max(1, r.max_leaders)
        why = ""
        if st.drawdown > alloc * r.leader_pause_dd_pct / 100:
            why = f"copy drawdown ${st.drawdown:.2f} > {r.leader_pause_dd_pct}% of ${alloc:.0f}"
        elif st.consec_losses >= r.leader_pause_losses:
            why = f"{st.consec_losses} consecutive losses"
        if why:
            self._rec({"ev": "leader_pause", "leader": leader, "reason": why})
            log.warn("leader_paused", leader=leader, reason=why)
            self.notify("leader_paused", leader=leader, reason=why)
