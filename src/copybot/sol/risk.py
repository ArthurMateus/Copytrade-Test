"""THE Solana risk gate. Every Solana paper order passes `SolGate.check`; nothing else sizes or approves one.

Exits (reduce/close) are never refused. Entries fail closed on any stale or doubtful input. The book is spot
only (no leverage): an entry can never use more cash than the book has.
"""
from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field

from copybot.config import Sol
from copybot.ledger import State

ENTRY = ("open", "add")
EXIT = ("reduce", "close")


@dataclass
class Order:
    kind: str                # open | add | reduce | close
    token: str
    size: float              # tokens (requested for entries, wanted for exits)
    px: float                # mark
    liq_usd: float = 0.0
    leader: str = ""
    swap_ts_ms: int = 0      # the leader's swap time, for entry freshness


@dataclass
class Health:
    now_ms: int = 0
    price_age_s: float = 1e9
    leader_feed_age_s: float = 1e9
    auth_ok: bool = False


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    size: float = 0.0
    stop_px: float = 0.0

    def __bool__(self):
        return self.ok


@dataclass
class SolGate:
    cfg: Sol
    clock: callable = time.time
    order_times: collections.deque = field(default_factory=collections.deque)

    def stop_for(self, entry: float) -> float:
        return entry * (1 - self.cfg.stop_pct / 100)

    def entry_size(self, equity: float, px: float) -> float:
        c = self.cfg
        return (equity * c.risk_per_trade_pct / 100) / (px * c.stop_pct / 100)

    def _rate_count(self) -> int:
        now = self.clock()
        while self.order_times and now - self.order_times[0] > 60:
            self.order_times.popleft()
        return len(self.order_times)

    def record_order(self) -> None:
        self.order_times.append(self.clock())

    def check(self, o: Order, st: State, h: Health, mids: dict[str, float]) -> Decision:
        if o.kind in EXIT:
            pos = st.positions.get(o.token)
            if pos is None:
                return Decision(False, "no_position")
            if o.kind == "close":
                return Decision(True, "exit", size=pos.size)
            size = min(o.size, pos.size)
            m = self.cfg.min_notional_usd
            if (pos.size - size) * o.px < m:
                return Decision(True, "exit_upgraded_to_close", size=pos.size)
            if size * o.px < m:
                return Decision(False, f"below_min_notional:${size * o.px:.2f}")
            return Decision(True, "exit", size=size)
        if o.kind not in ENTRY:
            return Decision(False, f"unknown_kind:{o.kind}")
        return self._entry(o, st, h, mids)

    def _entry(self, o: Order, st: State, h: Health, mids: dict[str, float]) -> Decision:
        c = self.cfg
        if st.entries_paused:
            return Decision(False, f"entries_paused:{st.pause_reason}")
        if st.uncertain:
            return Decision(False, "uncertain_state")
        if o.leader not in st.followed:
            return Decision(False, "leader_not_followed")
        if o.leader in st.paused_leaders:
            return Decision(False, "leader_paused")
        if not h.auth_ok:
            return Decision(False, "fomo_session_in_doubt")
        if h.price_age_s > c.max_price_age_s:
            return Decision(False, f"stale_price:{h.price_age_s:.1f}s")
        if h.leader_feed_age_s > c.max_leader_feed_age_s:
            return Decision(False, "leader_feed_in_doubt")
        if o.swap_ts_ms:
            age_s = (h.now_ms - o.swap_ts_ms) / 1000
            if age_s > c.max_entry_age_s:
                return Decision(False, f"leader_swap_too_old:{age_s:.0f}s")
        if not (o.px > 0):
            return Decision(False, "no_price")
        if o.liq_usd <= 0:
            return Decision(False, "liquidity_unknown")
        if o.liq_usd < c.min_liquidity_usd:
            return Decision(False, f"illiquid:${o.liq_usd:,.0f}")
        eq = st.equity(mids)
        for kind, lim in (("day", c.daily_loss_pct), ("week", c.weekly_loss_pct)):
            m = st.marks.get(kind)
            if m is None:
                return Decision(False, f"no_{kind}_mark")
            if (m["equity"] - eq) / m["equity"] * 100 >= lim:
                return Decision(False, f"{kind}_loss_limit")
        if self._rate_count() >= c.max_orders_per_min:
            return Decision(False, "order_rate_limit")
        pos = st.positions.get(o.token)
        if o.kind == "open":
            if pos is not None:
                return Decision(False, "token_held_by_other_leader" if pos.leader != o.leader else "already_open")
            if len(st.positions) >= c.max_positions:
                return Decision(False, "max_positions")
            stop = self.stop_for(o.px)
        else:
            if pos is None or pos.leader != o.leader:
                return Decision(False, "add_without_position")
            stop = pos.stop_px
            if o.px <= stop:
                return Decision(False, "price_beyond_stop")
        # budget: clamp the request to what fits. The 5% margin is slippage between the mark and the real fill.
        per_unit = abs(o.px - stop) * 1.05
        caps = [o.size,
                (eq * c.max_total_risk_pct / 100 - st.total_risk()) / per_unit,
                (eq * c.max_position_pct / 100 - (pos.size * pos.entry_px if pos else 0.0)) / o.px,
                c.max_impact_pct / 100 * o.liq_usd / 2 / o.px]   # price impact of the trade stays under the cap
        if o.kind == "open":
            caps.append(eq * c.risk_per_trade_pct / 100 / per_unit)
        invested = sum(p.size * p.entry_px for p in st.positions.values())
        caps.append((eq - invested) / (o.px * (1 + c.max_impact_pct / 100 + c.swap_fee_pct / 100)))
        size = max(0.0, min(caps))
        if size <= 0:
            return Decision(False, "risk_budget_full")
        if size * o.px < c.min_notional_usd:
            return Decision(False, f"below_min_notional:${size * o.px:.2f}")
        return Decision(True, "entry", size=size, stop_px=stop)
