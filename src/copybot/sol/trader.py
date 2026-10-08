"""Leader swap detection and the Solana position manager.

Detector: turns a leader's new swap legs into Moves (open/add/reduce/close) using a running token balance
that is folded from the swap history. A sell of a token whose purchase we never saw (balance unknown) is
treated as a full exit of any copy we hold.

Trader: mirrors moves on the paper book. Copy model (spot, long only): at open we record
k = our size / leader balance after its buy. A leader add buys k x amount; a leader sell leaves our target at
k x its remaining balance (we sell down to it); a close closes. The stop sits 30% (configurable) under our
fill price, from the actual fill, and is part of the same durable ledger event as the entry.
Every order goes through SolGate; every state change through ledger.append then State.apply.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable

from copybot import log
from copybot.config import Sol
from copybot.ledger import Ledger, Position, State
from copybot.sol.fomo import Leg
from copybot.sol.market import NoQuote, PaperBroker, Prices
from copybot.sol.risk import Health, Order, SolGate

EPS = 1e-9


@dataclass
class Move:
    leader: str
    token: str
    kind: str            # open | add | reduce | close
    ts_ms: int
    px: float
    amount: float        # tokens bought / sold by the leader in this swap
    start: float         # leader balance before
    end: float           # leader balance after
    leg_id: str = ""
    unknown_balance: bool = False


class Detector:
    def __init__(self):
        self.bal: dict[tuple[str, str], float] = {}
        self.seen: dict[str, set[str]] = {}

    def balance(self, leader: str, token: str) -> float:
        return self.bal.get((leader, token), 0.0)

    def _fold(self, leader: str, g: Leg) -> tuple[float, float]:
        k = (leader, g.token)
        start = self.bal.get(k, 0.0)
        end = start + g.amount if g.side == "buy" else max(0.0, start - g.amount)
        self.bal[k] = end
        return start, end

    def seed(self, leader: str, legs: list[Leg], cursor_ms: int) -> list[Leg]:
        """Fold history up to `cursor_ms` (already handled) and return the legs after it (still to handle)."""
        seen = self.seen.setdefault(leader, set())
        later = []
        for g in sorted(legs, key=lambda x: (x.ts, x.id)):
            if g.ts <= cursor_ms:
                self._fold(leader, g)
                seen.add(g.id)
            else:
                later.append(g)
        return later

    def on_legs(self, leader: str, legs: list[Leg]) -> list[Move]:
        seen = self.seen.setdefault(leader, set())
        out = []
        for g in sorted(legs, key=lambda x: (x.ts, x.id)):
            if g.id in seen:
                continue
            seen.add(g.id)
            start, end = self._fold(leader, g)
            if g.side == "buy":
                kind = "open" if start <= EPS else "add"
                out.append(Move(leader, g.token, kind, g.ts, g.px, g.amount, start, end, g.id))
            elif start <= EPS:
                out.append(Move(leader, g.token, "close", g.ts, g.px, g.amount, 0.0, 0.0, g.id, True))
            elif end <= start * 0.02:
                out.append(Move(leader, g.token, "close", g.ts, g.px, g.amount, start, 0.0, g.id))
            else:
                out.append(Move(leader, g.token, "reduce", g.ts, g.px, g.amount, start, end, g.id))
        if len(seen) > 20_000:       # bound memory: ids older than the newest 10k are never fetched again
            for i in list(seen)[:10_000]:
                seen.discard(i)
        return out


class Trader:
    def __init__(self, cfg: Sol, st: State, ledger: Ledger, gate: SolGate, broker: PaperBroker, prices: Prices,
                 health: Callable[[], Health], det: Detector, notify: Callable[..., None] = lambda *a, **k: None):
        self.cfg, self.st, self.ledger, self.gate, self.broker = cfg, st, ledger, gate, broker
        self.prices, self.health, self.det, self.notify = prices, health, det, notify
        self.last_px: dict[str, float] = {}      # last mark we ever saw per held token (exit fallback)

    def _rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    def _skip(self, why: str, **kw) -> None:
        log.info("sol_copy_skip", reason=why, **kw)
        if why.startswith("below_min_notional"):
            self._rec({"ev": "count", "name": "skipped_min_notional"})
        # `kind` is notify()'s own first argument: pass the order kind under another name
        self.notify("skip", reason=why, **{("order" if k == "kind" else k): v for k, v in kw.items()})

    def marks(self) -> dict[str, float]:
        m = self.prices.marks()
        self.last_px.update({t: p for t, p in m.items() if t in self.st.positions})
        return m

    # ---- leader moves ---------------------------------------------------------------------------
    def on_move(self, m: Move) -> None:
        pos = self.st.positions.get(m.token)
        log.info("sol_leader_move", leader=m.leader, token=m.token, kind=m.kind, amount=m.amount, px=m.px,
                 start=m.start, end=m.end)
        if pos is not None and pos.leader != m.leader:
            return self._skip("token_held_for_other_leader", token=m.token, leader=m.leader, holder=pos.leader)
        if m.kind == "open":
            if pos is not None:    # we still hold a copy the leader no longer had: stale, close it first
                self.close(m.token, "leader_reopened", move=m)
            return self.enter("open", m, None)
        if pos is None:
            return self._skip(f"no_copy_for_{m.kind}", token=m.token, leader=m.leader)
        if m.kind == "add":
            return self.enter("add", m, pos)
        if m.kind == "close":
            return self.close(m.token, "leader_close", move=m)
        # target-based like Hyperliquid: our size follows k x the leader's balance, so a trim too small to
        # trade ($ minimum) is not lost, it is caught up by the next one
        self.reduce(m.token, pos.size - pos.k * m.end, "leader_reduce", move=m)

    # ---- entries ---------------------------------------------------------------------------------
    def enter(self, kind: str, m: Move, pos: Position | None) -> None:
        q = self.prices.fetch([m.token], timeout=2.0).get(m.token) or self.prices.get(m.token)
        if q is None:
            return self._skip("no_price", token=m.token, leader=m.leader)
        h = self.health()
        h.price_age_s = time.time() - q.ts
        mids = self.marks()
        eq = self.st.equity(mids)
        want = self.gate.entry_size(eq, q.px) if kind == "open" else (pos.k * m.amount)
        d = self.gate.check(Order(kind, m.token, want, q.px, q.liq_usd, m.leader, m.ts_ms), self.st, h, mids)
        if not d:
            return self._skip(d.reason, token=m.token, leader=m.leader, kind=kind, sym=q.symbol)
        try:
            f = self.broker.market(True, d.size, q.px, q.liq_usd)
        except NoQuote:
            return self._skip("no_price", token=m.token, leader=m.leader)
        lag = h.now_ms - m.ts_ms
        iid = uuid.uuid4().hex[:12]
        if kind == "open":
            stop = self.gate.stop_for(f.px)
            pos = Position(pos_id=f"{q.symbol}-{iid}", coin=m.token, side=1, size=d.size, entry_px=f.px, stop_px=stop,
                           leverage=1.0, leader=m.leader, k=d.size / m.end, open_oid=0, opened_ms=h.now_ms,
                           open_lag_ms=lag, sym=q.symbol)
            self._rec({"ev": "open", "intent": iid, "pos": asdict(pos), "fee": f.fee, "lag_ms": lag,
                       "impact_pct": f.impact_pct, "leader_px": m.px})
            log.info("sol_copy_open", sym=q.symbol, token=m.token, size=d.size, px=f.px, stop=stop,
                     notional=round(d.size * f.px, 2), fee=round(f.fee, 4), lag_ms=lag, leader=m.leader,
                     impact_pct=round(f.impact_pct, 2))
            self.gate.record_order()
            self.notify("opened", token=m.token)
        else:
            k = (pos.size + d.size) / m.end
            self._rec({"ev": "add", "intent": iid, "coin": m.token, "pos_id": pos.pos_id, "sz": d.size, "px": f.px,
                       "fee": f.fee, "k": k, "lag_ms": lag})
            log.info("sol_copy_add", sym=pos.sym, token=m.token, size=d.size, px=f.px, lag_ms=lag)
            self.gate.record_order()
            self.notify("updated", token=m.token)

    # ---- exits (never blocked) -----------------------------------------------------------------------
    def _exit_fill(self, token: str, size: float):
        q = self.prices.get(token)
        last = self.last_px.get(token) or (self.st.positions[token].entry_px)
        mark = q.px if q else None
        return self.broker.market(False, size, mark, q.liq_usd if q else 0.0, exit=True, last_px=last)

    def reduce(self, token: str, size: float, reason: str, move: Move | None = None) -> None:
        pos = self.st.positions.get(token)
        if pos is None or size <= 0:
            return
        mark = (self.prices.get(token) or None)
        px = mark.px if mark else (self.last_px.get(token) or pos.entry_px)
        d = self.gate.check(Order("reduce", token, size, px), self.st, self.health(), {})
        if not d:
            return self._skip(d.reason, token=token, kind="reduce")
        if d.size >= pos.size:
            return self.close(token, reason, move=move)
        f = self._exit_fill(token, d.size)
        lag = (self.health().now_ms - move.ts_ms) if move else None
        iid = uuid.uuid4().hex[:12]
        self._rec({"ev": "reduce", "intent": iid, "coin": token, "pos_id": pos.pos_id, "sz": d.size, "px": f.px,
                   "fee": f.fee, "reason": reason, "lag_ms": lag})
        self.gate.record_order()
        log.info("sol_copy_reduce", sym=pos.sym, token=token, size=d.size, px=f.px, left=pos.size, reason=reason,
                 lag_ms=lag, src=f.source)
        self.notify("updated", token=token)

    def close(self, token: str, reason: str, move: Move | None = None) -> None:
        pos = self.st.positions.get(token)
        if pos is None:
            return
        d = self.gate.check(Order("close", token, pos.size, pos.entry_px), self.st, self.health(), {})
        assert d.ok, d.reason
        f = self._exit_fill(token, pos.size)
        lag = (self.health().now_ms - move.ts_ms) if move else None
        leader, iid = pos.leader, uuid.uuid4().hex[:12]
        self._rec({"ev": "close", "intent": iid, "coin": token, "pos_id": pos.pos_id, "px": f.px, "fee": f.fee,
                   "reason": reason, "lag_ms": lag, "src": f.source})
        self.gate.record_order()
        trade = self.st.closed[-1]
        log.info("sol_copy_close", sym=trade.get("sym"), token=token, px=f.px, pnl=round(trade["pnl"], 4),
                 reason=reason, lag_ms=lag, src=f.source, leader=leader)
        self.notify("closed", token=token, trade=trade)
        self._check_leader(leader)

    # ---- periodic duties ---------------------------------------------------------------------------------
    def check_stops(self) -> None:
        marks = self.marks()
        for p in list(self.st.positions.values()):
            m = marks.get(p.coin)
            if m and p.stop_hit(m):
                log.warn("sol_stop_hit", sym=p.sym, token=p.coin, mark=m, stop=p.stop_px)
                self.close(p.coin, "stop")

    def stale_marks(self, older_than_s: float) -> list[str]:
        return [p.sym or p.coin[:6] for p in self.st.positions.values() if self.prices.age_s(p.coin) > older_than_s]

    def reconcile(self, leader: str) -> None:
        """Safety net: we hold a copy but the leader's computed balance of that token is gone."""
        for p in list(self.st.positions.values()):
            if p.leader == leader and self.det.balance(leader, p.coin) <= EPS:
                log.warn("sol_reconcile_exit", token=p.coin, leader=leader)
                self._rec({"ev": "count", "name": "reconcile_exits"})
                self.close(p.coin, "reconcile_leader_flat")

    def flatten(self, reason: str) -> None:
        for token in list(self.st.positions):
            self.close(token, reason)

    def _check_leader(self, leader: str) -> None:
        st = self.st.leader_stats.get(leader)
        if st is None or leader in self.st.paused_leaders or leader not in self.st.followed:
            return
        c = self.cfg
        alloc = self.st.equity(self.marks()) / max(1, c.max_leaders)
        why = ""
        if st.drawdown > alloc * c.leader_pause_dd_pct / 100:
            why = f"copy drawdown ${st.drawdown:.2f} > {c.leader_pause_dd_pct}% of ${alloc:.0f}"
        elif st.consec_losses >= c.leader_pause_losses:
            why = f"{st.consec_losses} consecutive losses"
        if why:
            self._rec({"ev": "leader_pause", "leader": leader, "reason": why})
            log.warn("sol_leader_paused", leader=leader, reason=why)
            self.notify("leader_paused", leader=leader, reason=why)
