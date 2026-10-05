"""Position manager: mirrors leader moves on our paper wallet, places the stop at entry, runs stops,
funding, reconciliation and leader pause rules. Every order goes through the RiskGate, every state change
is written to the ledger (write-ahead intent, then result) before it is acted upon in memory.

Copy model: when we open we record k = our_size / |leader_position|. Afterwards our target size is
k x |leader_position|: leader adds -> we add (risk-capped), leader reduces -> we reduce, leader closes ->
we close, leader flips -> we close and open a fresh, risk-sized copy on the other side. Further fills of the
SAME leader order that opened the position only rebase k (one order split into many fills is one trade).

Several leaders on one coin (we still hold ONE net position per coin, owned by one leader):
  agreement  another followed leader opens the SAME side -> it becomes a backer and we add a boost
             (consensus_risk_pct, within the per-symbol cap). Its later adds/reduces only refresh its size;
             when it closes or flips, its share of the boost is trimmed off.
             When the OWNER exits while a backer still holds, the position is handed over to the best-scored
             backer and trimmed back to a normal-size copy instead of being closed.
  conflict   another followed leader opens the OPPOSITE side -> the leader with the higher wallet score wins:
             if the newcomer scores higher than every leader on our side we close and copy the newcomer,
             otherwise its trade is skipped. Ties keep the position we hold.
Only the configured main coins are copied, plus any perp for leaders whose wallet is diversified.
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
                 notify: Callable[..., None] = lambda *a, **k: None,
                 score_of: Callable[[str], float] = lambda leader: 0.0, pause_leaders: bool = True):
        self.cfg, self.st, self.ledger, self.gate, self.broker = cfg, st, ledger, gate, broker
        self.health, self.mids, self.assets, self.notify = health, mids, assets, notify
        self.score_of = score_of   # wallet score 0-100 of a leader (0 when unknown)
        self.pause_leaders = pause_leaders   # False for side wallets: they follow the main wallet's pauses

    # ---- ledger helpers -----------------------------------------------------------------------
    def _rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    def _skip(self, why: str, **kw) -> None:
        log.info("copy_skip", reason=why, **kw)
        if why.startswith("below_min_notional"):
            self._rec({"ev": "count", "name": "skipped_min_notional"})
        self.notify("skip", reason=why, **kw)

    def _lag(self, h: Health, move: Move | None) -> float | None:
        return None if move is None else h.exchange_now_ms() - move.time_ms

    # ---- leader moves -------------------------------------------------------------------------
    def on_move(self, m: Move) -> None:
        pos = self.st.positions.get(m.coin)
        log.info("leader_move", leader=m.leader, coin=m.coin, kind=m.kind, start=m.start_pos, end=m.end_pos,
                 px=m.px, fills=m.n_fills, oid=m.oid)
        if pos is None and not self.gate.coin_allowed(m.leader, m.coin):
            return self._skip("not_main_coin", coin=m.coin, leader=m.leader)
        if pos is not None and pos.leader != m.leader:
            return self.on_other_leader(pos, m)
        kind = m.kind
        if kind == "open" or (kind == "flip" and pos is None):
            if pos is not None:  # we still hold a copy the leader no longer had: stale, close it first
                self.close(m.coin, "leader_reopened", move=m)
            return self.open(m)
        if pos is None:
            return self._skip(f"no_copy_for_{kind}", coin=m.coin, leader=m.leader)
        if kind == "close":
            return self.owner_exit(m.coin, "leader_close", move=m)
        if kind == "flip":
            self.owner_exit(m.coin, "leader_flip", move=m)
            pos = self.st.positions.get(m.coin)
            if pos is not None:   # handed over to a backer: the leader's new side is now a conflict
                return self.on_other_leader(pos, m)
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

    # ---- several leaders on one coin ------------------------------------------------------------
    def on_other_leader(self, pos: Position, m: Move) -> None:
        """A move by a leader that does not own our position on this coin."""
        if m.leader in pos.backers:
            if m.new_side == pos.side:   # the backer adds or reduces: only follow its size
                self._rec({"ev": "back", "coin": m.coin, "pos_id": pos.pos_id, "leader": m.leader,
                           "lpos": abs(m.end_pos)})
                return
            self.unback(m.coin, m.leader, "backer_close" if m.new_side == 0 else "backer_flip", move=m)
            if m.new_side == 0:
                return
            pos = self.st.positions.get(m.coin)
            if pos is None:
                return self.open(m)
            return self.conflict(pos, m)
        if m.kind not in ("open", "flip"):
            return self._skip("symbol_held_for_other_leader", coin=m.coin, leader=m.leader, holder=pos.leader)
        if m.new_side == pos.side:
            return self.boost(pos, m)
        return self.conflict(pos, m)

    def boost(self, pos: Position, m: Move) -> None:
        """Agreement: a second followed leader opens the side we hold. Record it as a backer and add size."""
        h = self.health()
        px = self.mids.get(m.coin) or m.px
        eq = self.st.equity(self.mids)
        want = eq * self.cfg.risk.consensus_risk_pct / 100 / max(abs(px - pos.stop_px), 1e-12)
        d = self.gate.check(Order("boost", m.coin, pos.side, want, px, leader=m.leader, fill_time_ms=m.time_ms),
                            self.st, h, self.mids, self.assets.get(m.coin))
        back = {"ev": "back", "coin": m.coin, "pos_id": pos.pos_id, "leader": m.leader, "lpos": abs(m.end_pos),
                "oid": m.oid}
        if not d:
            if d.reason in ("leader_not_followed", "leader_paused", "uncertain_state", "not_main_coin") or \
                    d.reason.startswith("entries_paused"):
                return self._skip(d.reason, coin=m.coin, leader=m.leader, action="boost")
            # no extra size (risk caps, stale data...) but the agreement still counts: this leader can take over
            log.info("consensus", coin=m.coin, leader=m.leader, holder=pos.leader, boost=0, why=d.reason)
            self._rec(back)
            self.notify("consensus", coin=m.coin, leader=m.leader, size=0.0)
            return
        iid = uuid.uuid4().hex[:12]
        self._rec({"ev": "intent", "intent": iid, "action": "boost", "coin": m.coin})
        try:
            f = self.broker.market(m.coin, pos.side > 0, d.size)
        except NoBook as e:
            self._rec({"ev": "intent_abort", "intent": iid})
            self._rec(back)
            return self._skip("no_book", coin=m.coin, err=str(e))
        lag = self._lag(self.health(), m)
        k = pos.k * (pos.size + d.size) / pos.size   # the owner's later moves scale the boosted size
        self._rec({**back, "intent": iid, "sz": d.size, "px": f.px, "fee": f.fee, "k": k, "lag_ms": lag})
        self.gate.record_order()
        log.info("consensus", coin=m.coin, leader=m.leader, holder=pos.leader, boost=d.size, px=f.px,
                 new_size=pos.size, lag_ms=lag)
        self.notify("consensus", coin=m.coin, leader=m.leader, size=d.size)

    def conflict(self, pos: Position, m: Move) -> None:
        """A followed leader opens the side opposite to ours: keep the position with the better leader."""
        ours = max(self.score_of(a) for a in (pos.leader, *pos.backers))
        theirs = self.score_of(m.leader)
        kw = dict(coin=m.coin, leader=m.leader, holder=pos.leader, score=round(theirs, 1), holder_score=round(ours, 1))
        if theirs <= ours:
            return self._skip("conflict_kept_better_leader", **kw)
        st = self.st
        if m.leader not in st.followed or m.leader in st.paused_leaders or st.entries_paused or st.uncertain:
            return self._skip("conflict_entry_not_allowed", **kw)   # do not close for a copy we cannot open
        log.info("conflict_switch", **kw)
        self.close(m.coin, "conflict_better_leader", move=m)
        self.notify("conflict", **kw)
        return self.open(m)

    def owner_exit(self, coin: str, reason: str, move: Move | None = None) -> None:
        """The owning leader closed: hand the position to a backer if one still holds, else close it."""
        pos = self.st.positions.get(coin)
        if pos is None:
            return
        if not pos.backers:
            return self.close(coin, reason, move=move)
        new = max(pos.backers, key=lambda a: (self.score_of(a), a))
        prev = pos.leader
        # the boost came from the agreement that just ended: trim back to a normal 1%-risk copy
        base = self.st.equity(self.mids) * self.cfg.risk.risk_per_trade_pct / 100 / max(pos.risk_usd() / pos.size, 1e-12)
        if pos.size > base * 1.001:
            self.reduce(coin, pos.size - base, f"{reason}_handover", move=move)
        pos = self.st.positions.get(coin)
        if pos is None or new not in pos.backers:
            return
        lpos = pos.backers[new]["lpos"]
        k = pos.size / lpos if lpos > 0 else pos.k
        self._rec({"ev": "handover", "coin": coin, "pos_id": pos.pos_id, "leader": new, "k": k, "from": prev,
                   "reason": reason})
        log.info("copy_handover", coin=coin, prev=prev, leader=new, size=pos.size, reason=reason)
        self.notify("handover", coin=coin, leader=new, prev=prev, reason=reason)

    def unback(self, coin: str, leader: str, reason: str, move: Move | None = None) -> None:
        """A backer left the agreement: drop it and trim the extra size it brought."""
        pos = self.st.positions.get(coin)
        b = pos.backers.get(leader) if pos else None
        if b is None:
            return
        trim = pos.size * b["frac"]
        self._rec({"ev": "unback", "coin": coin, "pos_id": pos.pos_id, "leader": leader, "reason": reason})
        log.info("consensus_end", coin=coin, leader=leader, holder=pos.leader, trim=trim, reason=reason)
        if trim > 0:
            self.reduce(coin, trim, reason, move=move)
        else:
            self.notify("updated", coin=coin)

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
            if not d.reason.startswith(("leader_", "not_main_coin", "below_min_notional")):
                self._rec({"ev": "count", "name": "opens_refused"})   # a copy our limits did not allow
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
            return self._skip(d.reason, coin=m.coin, leader=m.leader, action="add")
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
            return self._skip(d.reason, coin=coin, action="reduce")
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
            szi = leader_pos.get(p.coin, 0.0)
            if leader in p.backers:
                if szi == 0 or (szi > 0) != (p.side > 0):
                    log.warn("reconcile_backer_gone", coin=p.coin, leader=leader, leader_pos=szi)
                    self.unback(p.coin, leader, "reconcile_backer_flat")
                continue
            if p.leader != leader:
                continue
            if szi == 0 or (szi > 0) != (p.side > 0):
                log.warn("reconcile_exit", coin=p.coin, leader=leader, leader_pos=szi)
                self._rec({"ev": "count", "name": "reconcile_exits"})
                self.owner_exit(p.coin, "reconcile_leader_flat")
            elif p.k * abs(szi) < p.size * 0.98:
                log.warn("reconcile_reduce", coin=p.coin, leader=leader, leader_pos=szi, ours=p.size)
                self._rec({"ev": "count", "name": "reconcile_reduces"})
                self.reduce(p.coin, p.size - p.k * abs(szi), "reconcile_leader_reduced")

    def flatten(self, reason: str) -> None:
        for coin in list(self.st.positions):
            self.close(coin, reason)

    def _check_leader(self, leader: str) -> None:
        st = self.st.leader_stats.get(leader)
        if not self.pause_leaders or st is None or leader in self.st.paused_leaders or leader not in self.st.followed:
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
