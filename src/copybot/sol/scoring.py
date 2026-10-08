"""Deterministic scoring of Solana wallets from their on-chain swaps (sol/chain.py).

Round trips use average cost per token: buys build a position, sells reduce it, flat = a closed trip. Anything
not closed is an open position (a "bag"). The gates are deliberately strict: a wallet must have a real history,
be profitable now (not only long ago), not be sitting on a large open bag, not live off one lucky trade or one
token, not be a sniper (very short holds), and its trades must still pay after OUR costs and lag.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

from copybot.config import Sol
from copybot.sol.chain import Leg

DAY = 86_400_000
EPS = 1e-9


# ---- 1. round trips -------------------------------------------------------------------------------
@dataclass
class Trip:
    token: str
    open_ms: int
    close_ms: int
    cost: float
    proceeds: float

    @property
    def net(self) -> float:
        return self.proceeds - self.cost

    @property
    def ret(self) -> float:
        return self.net / self.cost if self.cost > 0 else 0.0

    @property
    def hold_s(self) -> float:
        return (self.close_ms - self.open_ms) / 1000


@dataclass
class Book:
    trips: list[Trip] = field(default_factory=list)
    open_cost: float = 0.0                  # cost basis still held
    open_tokens: dict = field(default_factory=dict)      # token -> remaining cost basis
    orphan_sells: int = 0                   # sells of tokens whose purchase we never saw


def build(legs: list[Leg]) -> Book:
    book = Book()
    pos: dict[str, dict] = {}
    for g in sorted(legs, key=lambda x: (x.ts, x.id)):
        p = pos.get(g.token)
        if g.side == "buy":
            if p is None:
                p = pos[g.token] = {"amt": 0.0, "peak": 0.0, "cost": 0.0, "tot": 0.0, "proceeds": 0.0, "open": g.ts}
            p["amt"] += g.amount
            p["peak"] = max(p["peak"], p["amt"])
            p["cost"] += g.usd
            p["tot"] += g.usd
            continue
        if p is None or p["amt"] <= EPS:
            book.orphan_sells += 1
            continue
        frac = min(1.0, g.amount / p["amt"])
        p["cost"] -= p["cost"] * frac
        p["amt"] -= min(g.amount, p["amt"])
        p["proceeds"] += g.usd
        if p["amt"] <= p["peak"] * 0.01:            # a dust remainder counts as closed (its basis is a loss)
            book.trips.append(Trip(g.token, p["open"], g.ts, p["tot"], p["proceeds"]))
            del pos[g.token]
    for t, p in pos.items():
        book.open_tokens[t] = p["cost"]
    book.open_cost = sum(book.open_tokens.values())
    return book


# ---- 2. the score ----------------------------------------------------------------------------------
@dataclass
class Score:
    address: str
    eligible: bool
    reasons: list = field(default_factory=list)
    score: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    pnl: float = 0.0
    pnl_7d: float = 0.0
    pnl_24h: float = 0.0
    positive_weeks: int = 0
    max_dd: float = 0.0
    cur_dd: float = 0.0
    best_trade_share: float = 0.0
    top3_share: float = 0.0
    token_share: float = 0.0
    median_hold_s: float = 0.0
    open_cost: float = 0.0
    open_buy_share: float = 0.0
    active_days: int = 0
    history_days: float = 0.0
    copy_edge_pct: float = 0.0
    shrink: float = 0.0
    scored_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def cost_rt_pct(c: Sol) -> float:
    """Our round-trip cost as a fraction: two swaps, each fee + extra slippage (price impact is gated at entry)."""
    return 2 * (c.swap_fee_pct + c.extra_slippage_pct) / 100


def lag_penalty(hold_s: float) -> float:
    """Return lost because we enter several seconds after the leader: far worse on very short holds."""
    return 0.06 if hold_s < 120 else (0.03 if hold_s < 600 else 0.015)


def copy_return(t: Trip, c: Sol) -> float:
    stop = c.stop_pct / 100
    return max(t.ret, -stop) - cost_rt_pct(c) - lag_penalty(t.hold_s)


def drawdowns(nets_in_time_order: list[float], capital: float) -> tuple[float, float]:
    """Drawdown of cumulative realised pnl as a fraction of (capital + peak profit)."""
    cum = peak = mdd = 0.0
    for x in nets_in_time_order:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, (peak - cum) / (capital + peak))
    return mdd, (peak - cum) / (capital + peak)


def full_score(address: str, legs: list[Leg], now_ms: int, c: Sol) -> Score:
    s = Score(address, False, scored_ms=now_ms)
    if not legs:
        s.reasons = ["no_swaps"]
        return s
    start = now_ms - c.history_days * DAY
    legs = [g for g in legs if g.ts >= start]
    s.history_days = (now_ms - min(g.ts for g in legs)) / DAY if legs else 0.0
    book = build(legs)
    trips = [t for t in book.trips if t.close_ms >= start]
    s.trades = n = len(trips)
    s.open_cost = book.open_cost
    s.active_days = len({g.ts // DAY for g in legs})
    buys30 = sum(g.usd for g in legs if g.side == "buy" and g.ts >= now_ms - 30 * DAY)
    s.open_buy_share = book.open_cost / buys30 if buys30 > 0 else 1.0
    if n == 0:
        s.reasons = ["no_round_trips"]
        return s
    nets = [t.net for t in sorted(trips, key=lambda t: t.close_ms)]
    s.pnl = sum(nets)
    s.pnl_7d = sum(t.net for t in trips if t.close_ms >= now_ms - 7 * DAY)
    s.pnl_24h = sum(t.net for t in trips if t.close_ms >= now_ms - DAY)
    s.win_rate = sum(1 for x in nets if x > 0) / n
    gp, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    s.profit_factor = gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0)
    weeks = [0.0] * 4
    for t in trips:
        age_w = int((now_ms - t.close_ms) // (7 * DAY))
        if age_w < 4:
            weeks[age_w] += t.net
    s.positive_weeks = sum(1 for w in weeks if w > 0)
    sizes = sorted(t.cost for t in trips)
    capital = max(sizes[int(0.9 * (n - 1))] * 5, 1.0)       # a proxy: 5 of its big trades' worth
    s.max_dd, s.cur_dd = drawdowns(nets, capital)
    ranked = sorted(nets, reverse=True)
    s.best_trade_share = ranked[0] / s.pnl if s.pnl > 0 else 1.0
    s.top3_share = sum(ranked[:3]) / s.pnl if s.pnl > 0 else 1.0
    per_token: dict[str, float] = {}
    for t in trips:
        per_token[t.token] = per_token.get(t.token, 0.0) + t.net
    s.token_share = max(per_token.values()) / s.pnl if s.pnl > 0 else 1.0
    s.median_hold_s = statistics.median(t.hold_s for t in trips)
    s.copy_edge_pct = sum(copy_return(t, c) for t in trips) / n * 100
    s.shrink = n / (n + 40.0)
    checks = [
        (n >= c.min_trades, f"trades<{c.min_trades}"),
        (s.active_days >= c.min_active_days, f"active_days<{c.min_active_days}"),
        (s.history_days >= c.min_history_days, f"history<{c.min_history_days}d"),
        (s.pnl > 0, "pnl<=0"),
        (s.pnl_7d > 0, "not_profitable_7d"),
        (s.pnl_24h >= -abs(s.pnl) * c.max_recent_loss_pct / 100, "losing_today"),
        (s.win_rate >= c.min_win_rate, f"win_rate<{c.min_win_rate:.0%}"),
        (s.profit_factor >= c.min_profit_factor, f"profit_factor<{c.min_profit_factor}"),
        (s.positive_weeks >= c.min_positive_weeks, f"positive_weeks<{c.min_positive_weeks}/4"),
        (s.best_trade_share <= c.max_best_trade_share, "one_trade_too_big"),
        (s.top3_share <= c.max_top3_share, "top3_trades_too_big"),
        (s.token_share <= c.max_token_share, "one_token_too_big"),
        (s.median_hold_s >= c.min_median_hold_s, "sniper_holds_too_short"),
        (s.open_buy_share <= c.max_open_buy_share, "holding_a_lot"),
        (book.open_cost <= max(s.pnl, 0.0) * c.max_open_vs_pnl, "open_bag_vs_pnl"),
        (s.max_dd <= c.max_drawdown, "max_drawdown"),
        (s.cur_dd <= c.max_current_drawdown, "current_drawdown"),
        (s.copy_edge_pct >= c.min_copy_edge_pct, "copy_edge_too_low"),
    ]
    s.reasons = [why for ok, why in checks if not ok]
    s.eligible = not s.reasons
    s.score = (s.shrink * min(max(s.copy_edge_pct, 0.0), 20.0) / 20.0 * min(s.profit_factor, 3.0) / 3.0
               * s.positive_weeks / 4 * (1 - s.max_dd) * (1 - s.open_buy_share))
    return s


def ranking(scores: list[Score]) -> list[str]:
    el = [s for s in scores if s.eligible]
    return [s.address for s in sorted(el, key=lambda s: (-round(s.score, 9), s.address))]
