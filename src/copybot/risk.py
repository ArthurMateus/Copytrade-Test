"""THE risk gate. Every order (entry or exit) goes through `RiskGate.check`. Nothing else may size or
approve an order.

Exits (reduce/close) are never refused: they are counted against the order rate but always pass.
Entries (open/add/boost) fail closed: any stale or doubtful input refuses them. Entries are only allowed on
the configured main coins. A boost is the extra size added when a second followed leader opens the same side
of a coin we already hold (consensus).
"""
from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass, field
from typing import Callable

from copybot.config import Config
from copybot.hl import Asset, is_core_perp, round_size
from copybot.ledger import State

ENTRY = ("open", "add", "boost")
EXIT = ("reduce", "close")


@dataclass
class Order:
    kind: str            # open | add | boost | reduce | close
    coin: str
    side: int            # +1 / -1 of the POSITION (not of the trade)
    size: float          # coins; for open/add a request (may be clamped), for exits what we want to remove
    px: float            # expected fill price (best bid/ask or mark)
    leader: str = ""
    stop_px: float = 0.0
    leverage: float = 0.0
    fill_time_ms: int = 0   # leader fill time, for entry freshness


@dataclass
class Health:
    """Freshness of inputs, filled by the runner every tick."""
    now_ms: int = 0
    mids_age_s: float = 1e9
    clock_ok: bool = False
    clock_offset_ms: float = 0.0
    clock_why: str = "no clock estimate"
    feed_age_s: float = 1e9
    feed_connected: bool = False

    def exchange_now_ms(self) -> float:
        return self.now_ms + self.clock_offset_ms


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    size: float = 0.0
    leverage: float = 0.0
    stop_px: float = 0.0

    def __bool__(self):
        return self.ok


def liq_distance(leverage: float, asset_max_lev: float) -> float:
    """Isolated-margin liquidation distance as a fraction of entry (maintenance margin = 1 / (2 x max lev))."""
    mmr = 1.0 / (2.0 * asset_max_lev)
    return 1.0 / leverage - mmr


@dataclass
class RiskGate:
    cfg: Config
    clock: callable = time.time
    order_times: collections.deque = field(default_factory=collections.deque)
    # leader -> diversified wallet? (then every core perp may be copied from it, not only the main coins)
    alts_ok: Callable[[str], bool] = field(default_factory=lambda: (lambda leader: False))

    def coin_allowed(self, leader: str, coin: str) -> bool:
        return coin in self.cfg.selection.main_coins or (is_core_perp(coin) and self.alts_ok(leader))

    # ---- sizing (from OUR risk limits, never from the leader's size) ---------------------------
    def stop_for(self, side: int, entry: float) -> float:
        d = self.cfg.risk.stop_pct / 100
        return entry * (1 - d) if side > 0 else entry * (1 + d)

    def leverage_for(self, asset: Asset) -> float:
        r = self.cfg.risk
        stop = r.stop_pct / 100
        mmr = 1.0 / (2.0 * asset.max_leverage)
        lev = math.floor(1.0 / (r.liq_buffer_mult * stop + mmr))
        return float(max(0, min(r.max_leverage, asset.max_leverage, lev)))

    def entry_size(self, equity: float, px: float) -> float:
        r = self.cfg.risk
        return (equity * r.risk_per_trade_pct / 100) / (px * r.stop_pct / 100)

    # ---- the gate -----------------------------------------------------------------------------
    def _rate_count(self) -> int:
        now = self.clock()
        while self.order_times and now - self.order_times[0] > 60:
            self.order_times.popleft()
        return len(self.order_times)

    def record_order(self) -> None:
        self.order_times.append(self.clock())

    def check(self, o: Order, st: State, h: Health, mids: dict[str, float], asset: Asset | None) -> Decision:
        if o.kind in EXIT:
            pos = st.positions.get(o.coin)
            if pos is None:
                return Decision(False, "no_position")
            # the orders/min ceiling is never applied to exits
            if o.kind == "close":
                return Decision(True, "exit", size=pos.size)
            size = min(o.size, pos.size)
            min_n = self.cfg.risk.min_notional_usd
            if (pos.size - size) * o.px < min_n:
                return Decision(True, "exit_upgraded_to_close", size=pos.size)   # never leave a dust position
            if size * o.px < min_n:
                return Decision(False, f"below_min_notional:${size * o.px:.2f}")  # partial mirror too small
            return Decision(True, "exit", size=size)
        if o.kind not in ENTRY:
            return Decision(False, f"unknown_kind:{o.kind}")
        return self._entry(o, st, h, mids, asset)

    def _entry(self, o: Order, st: State, h: Health, mids: dict, asset: Asset | None) -> Decision:
        r = self.cfg.risk
        if st.entries_paused:
            return Decision(False, f"entries_paused:{st.pause_reason}")
        if st.uncertain:
            return Decision(False, "uncertain_state")
        if o.leader not in st.followed:
            return Decision(False, "leader_not_followed")
        if o.leader in st.paused_leaders:
            return Decision(False, "leader_paused")
        if not self.coin_allowed(o.leader, o.coin):
            return Decision(False, "not_main_coin")
        # ---- fail closed on stale/doubtful inputs
        if h.mids_age_s > r.max_mids_age_s:
            return Decision(False, f"stale_prices:{h.mids_age_s:.1f}s")
        if not h.clock_ok:
            return Decision(False, f"clock_in_doubt:{h.clock_why}")
        if not h.feed_connected or h.feed_age_s > r.max_leader_feed_age_s:
            return Decision(False, "leader_feed_in_doubt")
        if o.fill_time_ms:
            age_s = (h.exchange_now_ms() - o.fill_time_ms - r.clock_tolerance_ms) / 1000
            if age_s > r.max_entry_age_s:
                return Decision(False, f"leader_fill_too_old:{age_s:.1f}s")
        if asset is None or asset.delisted:
            return Decision(False, "unknown_or_delisted_asset")
        if not (o.px > 0):
            return Decision(False, "no_price")
        # ---- loss limits
        eq = st.equity(mids)
        for kind, lim in (("day", r.daily_loss_pct), ("week", r.weekly_loss_pct)):
            m = st.marks.get(kind)
            if m is None:
                return Decision(False, f"no_{kind}_mark")
            if m["equity"] <= 0 or eq <= 0:
                return Decision(False, "wallet_empty")
            if (m["equity"] - eq) / m["equity"] * 100 >= lim:
                return Decision(False, f"{kind}_loss_limit")
        if self._rate_count() >= r.max_orders_per_min:
            return Decision(False, "order_rate_limit")
        # ---- position count / symbol ownership
        pos = st.positions.get(o.coin)
        if o.kind == "open":
            if pos is not None:
                return Decision(False, "symbol_taken" if pos.leader != o.leader else "already_open")
            if len(st.positions) >= r.max_positions:
                return Decision(False, "max_positions")
            lev = self.leverage_for(asset)
            stop = self.stop_for(o.side, o.px)
        elif o.kind == "boost":
            if pos is None or pos.leader == o.leader or pos.side != o.side:
                return Decision(False, "boost_without_position")
            lev, stop = pos.leverage, pos.stop_px
        else:
            if pos is None or pos.leader != o.leader or pos.side != o.side:
                return Decision(False, "add_without_position")
            lev, stop = pos.leverage, pos.stop_px
        if lev < 1:
            return Decision(False, "no_safe_leverage")
        stop_dist = abs(o.px - stop) / o.px
        if o.kind != "open" and (o.side > 0 and o.px <= stop or o.side < 0 and o.px >= stop):
            return Decision(False, "price_beyond_stop")
        if liq_distance(lev, asset.max_leverage) < r.liq_buffer_mult * stop_dist - 1e-12:
            return Decision(False, "stop_too_close_to_liquidation")
        # ---- risk budget: clamp the request to what fits
        # 1% margin: slippage moves the real entry away from the fixed stop, the caps must hold after the fill
        per_unit = abs(o.px - stop) * 1.01
        caps = [o.size]
        if o.kind == "open":
            caps.append(eq * r.risk_per_trade_pct / 100 / per_unit)
        elif o.kind == "boost":
            caps.append(eq * r.consensus_risk_pct / 100 / per_unit)
        sym_now = pos.risk_usd() if pos else 0.0
        caps.append((eq * r.max_symbol_risk_pct / 100 - sym_now) / per_unit)
        caps.append((eq * r.max_total_risk_pct / 100 - st.total_risk()) / per_unit)
        used_margin = sum(p.size * p.entry_px / p.leverage for p in st.positions.values())
        caps.append((eq - used_margin) * lev / o.px)
        size = round_size(max(0.0, min(caps)), asset.sz_decimals)
        if size <= 0:
            return Decision(False, "risk_budget_full")
        if size * o.px < r.min_notional_usd:
            return Decision(False, f"below_min_notional:${size * o.px:.2f}")
        return Decision(True, "entry", size=size, leverage=lev, stop_px=stop)
