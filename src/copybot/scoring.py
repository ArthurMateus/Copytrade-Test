"""Deterministic wallet scoring. Pure functions over leaderboard rows, VERIFIED fills and 1h candles.

Leaderboard numbers are only used for the cheap pre-screen; the final rank comes from fills.
"""
from __future__ import annotations

import bisect
import statistics
from dataclasses import asdict, dataclass, field

from copybot.hl import Candle, Fill, LbRow, is_core_perp

DAY = 86_400_000
EPS = 1e-12


# ---- 1. pre-screen from the leaderboard row alone -------------------------------------------------
@dataclass
class Pre:
    ok: bool
    reason: str = ""
    edge_bps: float = 0.0


def prescreen(r: LbRow) -> Pre:
    av = r.account_value
    if av < 10_000:
        return Pre(False, "account_value<10k")
    if r.week.vlm <= 0:
        return Pre(False, "inactive_this_week")
    if r.month.vlm < 2 * av:
        return Pre(False, "month_volume<2x")
    if r.month.vlm > 500 * av:
        return Pre(False, "month_volume>500x")
    if r.day.vlm > 50 * av:
        return Pre(False, "day_volume>50x")
    before_pnl = r.all_time.pnl - r.month.pnl
    before_vlm = r.all_time.vlm - r.month.vlm
    if r.month.pnl <= 0:
        return Pre(False, "month_pnl<=0")
    if before_pnl <= 0:
        return Pre(False, "pnl_before_month<=0")
    m_edge = r.month.pnl / r.month.vlm * 1e4
    b_edge = before_pnl / before_vlm * 1e4 if before_vlm > 0 else 0.0
    if m_edge < 10 or b_edge < 10:
        return Pre(False, "edge<10bps")
    if r.month.pnl > av:
        return Pre(False, "month_pnl>100%_of_account")
    return Pre(True, "", min(m_edge, b_edge))


def rank_prescreened(rows: list[LbRow]) -> list[tuple[LbRow, Pre]]:
    """Rank by pnl-per-dollar edge (capped at 50 bps, the weaker of the two periods), then all-time pnl."""
    ok = [(r, p) for r in rows for p in [prescreen(r)] if p.ok]
    return sorted(ok, key=lambda x: (-min(x[1].edge_bps, 50.0), -x[0].all_time.pnl, x[0].address))


# ---- round trips ---------------------------------------------------------------------------------
@dataclass
class Trip:
    coin: str
    side: int
    open_ms: int
    close_ms: int
    entry_px: float          # first opening price
    notional: float          # sum of opening/adding notional
    gross: float             # sum of closedPnl
    fees: float
    peak_size: float = 0.0

    @property
    def net(self) -> float:
        return self.gross - self.fees

    @property
    def ret(self) -> float:
        return self.gross / self.notional if self.notional > 0 else 0.0


def round_trips(fills: list[Fill]) -> list[Trip]:
    """Closed round trips (flat -> position -> flat, or -> flip) on core perps. Trips whose start we did
    not see (history begins mid-position) are skipped."""
    trips: list[Trip] = []
    cur: dict[str, Trip | None] = {}
    for f in sorted(fills, key=lambda x: (x.time, x.tid)):
        if not is_core_perp(f.coin):
            continue
        t = cur.get(f.coin)
        s, e = f.start_pos, f.end_pos
        if abs(s) < EPS:
            if abs(e) < EPS:
                continue
            t = cur[f.coin] = Trip(f.coin, 1 if e > 0 else -1, f.time, 0, f.px, 0.0, 0.0, 0.0)
            t.notional += f.notional
            t.fees += f.fee
            t.peak_size = abs(e)
            continue
        if t is None:  # mid-position without a seen start: wait until flat
            if abs(e) < EPS or (s > 0) != (e > 0):
                if abs(e) > EPS:
                    nt = cur[f.coin] = Trip(f.coin, 1 if e > 0 else -1, f.time, 0, f.px, abs(e) * f.px, 0.0, 0.0)
                    nt.peak_size = abs(e)
            continue
        flipped = abs(e) > EPS and (s > 0) != (e > 0)
        if abs(e) > abs(s) + EPS and not flipped:
            t.notional += f.notional
        t.fees += f.fee
        t.gross += f.closed_pnl
        t.peak_size = max(t.peak_size, abs(e))
        if abs(e) < EPS or flipped:
            t.close_ms = f.time
            trips.append(t)
            cur[f.coin] = None
            if flipped:
                nt = cur[f.coin] = Trip(f.coin, 1 if e > 0 else -1, f.time, 0, f.px, abs(e) * f.px, 0.0, 0.0)
                nt.peak_size = abs(e)
    return trips


# ---- 2. screen of the first page of fills ---------------------------------------------------------
@dataclass
class Screen:
    ok: bool
    reason: str = ""
    metrics: dict = field(default_factory=dict)


def fill_screen(page: list[Fill], now_ms: int, our_notional: float, min_notional: float = 10.0,
                page_size: int = 2000) -> Screen:
    if not page:
        return Screen(False, "no_fills")
    page = sorted(page, key=lambda f: (f.time, f.tid))
    span_days = max((page[-1].time - page[0].time) / DAY, 1e-9)
    full = len(page) >= page_size
    m: dict = {"fills": len(page), "span_days": round(span_days, 2)}
    if full and span_days < 1:
        return Screen(False, "high_frequency", m)
    if full:
        m["fills_per_day"] = round(len(page) / span_days, 1)
        if len(page) / span_days >= 56:
            return Screen(False, "too_fast", m)
    total = sum(f.notional for f in page)
    core = [f for f in page if is_core_perp(f.coin)]
    core_n = sum(f.notional for f in core)
    m["core_share"] = round(core_n / total, 3) if total else 0
    if not total or core_n / total < 0.5:
        return Screen(False, "core_perp_share<50%", m)
    maker = sum(f.notional for f in core if not f.crossed) / core_n
    m["maker_share"] = round(maker, 3)
    if maker > 0.7:
        return Screen(False, "maker_share>70%", m)
    hist = (now_ms - page[0].time) / DAY
    m["history_days"] = round(hist, 1)
    if hist < 60:
        return Screen(False, "history<60d", m)
    trips = round_trips(page)
    m["round_trips"] = len(trips)
    if len(trips) < 150:
        return Screen(False, "round_trips<150", m)
    med = statistics.median(t.close_ms - t.open_ms for t in trips) / 60_000
    m["median_hold_min"] = round(med, 1)
    if med < 15:
        return Screen(False, "median_hold<15min", m)
    frac = copyable_fraction(core, our_notional, min_notional)
    m["copyable"] = round(frac, 3)
    if frac < 0.5:
        return Screen(False, "too_small_to_copy", m)
    return Screen(True, "", m)


def copyable_fraction(fills: list[Fill], our_notional: float, min_notional: float) -> float:
    """Share of the leader's trades (one per order) whose copy order on our side would be >= $10.
    Opens are copied at our risk-sized notional; adds/reduces at the same fraction of our copy as of the
    leader's position."""
    ref: dict[str, float] = {}   # coin -> leader's |position| right after its opening order
    by_order: dict[tuple, list[Fill]] = {}
    for f in sorted(fills, key=lambda x: (x.time, x.tid)):
        by_order.setdefault((f.coin, f.oid), []).append(f)
    ok = n = 0
    for (coin, _), fs in sorted(by_order.items(), key=lambda kv: kv[1][0].time):
        s, e = fs[0].start_pos, fs[-1].end_pos
        if abs(s) < EPS or (abs(e) > EPS and (s > 0) != (e > 0)):   # open or flip: a full-size copy
            ref[coin] = abs(e)
            n += 1
            ok += our_notional >= min_notional
            continue
        if coin not in ref or ref[coin] < EPS:
            continue
        n += 1
        ok += our_notional * abs(e - s) / ref[coin] >= min_notional or abs(e) < EPS   # full closes always copy
    return ok / n if n else 0.0


# ---- 3. full-history score ------------------------------------------------------------------------
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
    positive_blocks: int = 0
    max_dd: float = 0.0
    cur_dd: float = 0.0
    concentration: float = 0.0
    copy_edge_bps: float = 0.0
    shrink: float = 0.0
    scored_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoreParams:
    stop_pct: float = 3.0
    cost_bps: float = 13.0         # our round trip: 2 x (taker 4.5 + slippage 1 + extra 1) bps
    shrink_n0: float = 50.0
    min_trades: int = 150
    blocks: int = 6
    block_days: int = 30
    min_positive_blocks: int = 4
    max_dd: float = 0.35
    max_cur_dd: float = 0.20
    max_concentration: float = 0.25


def copy_return(t: Trip, candles: list[Candle] | None, stop: float) -> float:
    """What OUR copy of this trip would have returned (gross): the leader's return, unless the price went
    `stop` against the entry while the trip was open (then our stop takes us out at -stop)."""
    if candles:
        ts = [c.t for c in candles]
        i = max(0, bisect.bisect_right(ts, t.open_ms) - 1)
        lim = t.entry_px * (1 - stop) if t.side > 0 else t.entry_px * (1 + stop)
        for c in candles[i:]:
            if c.t > t.close_ms:
                break
            if (t.side > 0 and c.l <= lim) or (t.side < 0 and c.h >= lim):
                return -stop
    return max(t.ret, -1.0)


def equity_curve(fills: list[Fill], candles: dict[str, list[Candle]], base: float, start_ms: int,
                 end_ms: int) -> list[float]:
    """Hourly mark-to-market equity: base + realized (closedPnl - fees) + open positions at 1h closes."""
    fl = sorted((f for f in fills if is_core_perp(f.coin)), key=lambda x: (x.time, x.tid))
    pos: dict[str, float] = {}
    avg: dict[str, float] = {}
    realized = 0.0
    closes = {c: ({k.t: k.c for k in cs}) for c, cs in candles.items()}
    last_px: dict[str, float] = {}
    out = []
    i = 0
    h = (start_ms // 3_600_000 + 1) * 3_600_000
    while h <= end_ms + 3_600_000:
        while i < len(fl) and fl[i].time < h:
            f = fl[i]
            s, e = f.start_pos, f.end_pos
            realized += f.closed_pnl - f.fee
            if abs(e) < EPS:
                avg.pop(f.coin, None)
            elif abs(s) < EPS or (s > 0) != (e > 0):
                avg[f.coin] = f.px
            elif abs(e) > abs(s):
                avg[f.coin] = (avg.get(f.coin, f.px) * abs(s) + f.px * (abs(e) - abs(s))) / abs(e)
            pos[f.coin] = e
            last_px[f.coin] = f.px
            i += 1
        u = 0.0
        for coin, sz in pos.items():
            if abs(sz) < EPS:
                continue
            px = closes.get(coin, {}).get(h - 3_600_000) or last_px.get(coin)
            u += sz * (px - avg.get(coin, px))
        out.append(base + realized + u)
        h += 3_600_000
    return out


def drawdowns(curve: list[float]) -> tuple[float, float]:
    peak, mdd = -float("inf"), 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    cur = (peak - curve[-1]) / peak if curve and peak > 0 else 0.0
    return mdd, cur


def full_score(address: str, fills: list[Fill], candles: dict[str, list[Candle]], account_value: float,
               now_ms: int, p: ScoreParams = ScoreParams()) -> Score:
    start = now_ms - p.blocks * p.block_days * DAY
    fl = [f for f in fills if f.time >= start]
    trips = [t for t in round_trips(fl) if t.close_ms >= start]
    s = Score(address, False, scored_ms=now_ms)
    s.trades = n = len(trips)
    if n == 0:
        s.reasons = ["no_round_trips"]
        return s
    nets = [t.net for t in trips]
    s.pnl = sum(nets)
    s.win_rate = sum(1 for x in nets if x > 0) / n
    gp, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    s.profit_factor = gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0)
    blocks = [0.0] * p.blocks
    for t in trips:
        b = int((t.close_ms - start) // (p.block_days * DAY))
        blocks[min(max(b, 0), p.blocks - 1)] += t.net
    s.positive_blocks = sum(1 for b in blocks if b > 0)
    base = max(account_value - s.pnl, account_value * 0.1, 1.0)
    curve = equity_curve(fl, candles, base, start, now_ms)
    s.max_dd, s.cur_dd = drawdowns(curve)
    s.concentration = max(nets) / s.pnl if s.pnl > 0 else 1.0
    rets = [copy_return(t, candles.get(t.coin), p.stop_pct / 100) for t in trips]
    s.copy_edge_bps = sum(rets) / n * 1e4 - p.cost_bps
    s.shrink = n / (n + p.shrink_n0)
    checks = [
        (n >= p.min_trades, f"trades<{p.min_trades}"),
        (s.pnl > 0, "pnl<=0"),
        (s.profit_factor > 1, "profit_factor<=1"),
        (s.positive_blocks >= p.min_positive_blocks, f"positive_blocks<{p.min_positive_blocks}"),
        (s.max_dd <= p.max_dd, "max_drawdown>35%"),
        (s.cur_dd <= p.max_cur_dd, "current_drawdown>20%"),
        (s.concentration <= p.max_concentration, "one_trade>25%_of_pnl"),
        (s.copy_edge_bps > 0, "copy_edge<=0"),
    ]
    s.reasons = [why for ok, why in checks if not ok]
    s.eligible = not s.reasons
    s.score = (s.shrink * min(max(s.copy_edge_bps, 0.0), 50.0) * min(s.profit_factor, 3.0) / 3.0
               * s.positive_blocks / p.blocks * (1 - s.max_dd))
    return s


def ranking(scores: list[Score]) -> list[str]:
    """Eligible wallets, best first; ties broken by address so the order is fully deterministic."""
    el = [s for s in scores if s.eligible]
    return [s.address for s in sorted(el, key=lambda s: (-round(s.score, 9), s.address))]
