"""Deterministic wallet scoring. Pure functions over leaderboard rows, VERIFIED fills and 1h candles.

Leaderboard numbers are only used for the cheap pre-screen; the final rank comes from fills.
Only the configured main coins count: a wallet is screened and scored on its main-coin trades alone, unless
it is DIVERSIFIED (net profitable in several coins, none dominating its profit): then every core perp it trades
counts (memecoins included) and the bot may copy all of them. What we score is what we copy.

Hard rejects are kept for wallets we cannot copy (too fast/HFT, mostly spot or maker, almost never closes a
main-coin trade, trades too small to mirror, no edge after our costs) and for live accounts that are empty or
sitting on big unrealized losses. A losing position the trader keeps open counts as a lost trade, so holding
losers instead of closing them cannot inflate the win rate. Everything about quality (trade count, win rate,
profit factor, consistency, drawdowns, concentration) becomes points of a 0-100 score; eligible wallets score
1-100 and the best ones are followed.
"""
from __future__ import annotations

import bisect
import statistics
from dataclasses import asdict, dataclass, field

from copybot.hl import Account, Candle, Fill, LbRow, is_core_perp

SCREEN_VERSION = 3   # bump when fill_screen changes: cached screen results of another version are redone
VERSION = 5          # bump when full_score changes: cached scores of another version are redone

DAY = 86_400_000
EPS = 1e-12
EMPTY_USD = 100.0    # a live perp account below this with no open position has left: nothing to copy


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


def diversification(trips: list[Trip], min_coins: int, max_share: float) -> tuple[bool, dict]:
    """(diversified, {coin: net pnl} of the 5 biggest contributors). Diversified = profitable overall, net
    profitable in at least `min_coins` coins and no coin above `max_share` of the profitable coins' total."""
    by: dict[str, float] = {}
    for t in trips:
        by[t.coin] = by.get(t.coin, 0.0) + t.net
    pos = {c: v for c, v in by.items() if v > 0}
    total = sum(pos.values())
    ok = (sum(by.values()) > 0 and len(pos) >= min_coins and total > 0
          and max(pos.values()) / total <= max_share)
    top = dict(sorted(((c, round(v, 2)) for c, v in by.items()), key=lambda x: (-abs(x[1]), x[0]))[:5])
    return ok, top


# ---- 2. screen of the first page of fills ---------------------------------------------------------
@dataclass
class Screen:
    ok: bool
    reason: str = ""
    metrics: dict = field(default_factory=dict)


def fill_screen(page: list[Fill], now_ms: int, our_notional: float, min_notional: float = 10.0,
                page_size: int = 2000, coins=None, min_trips: int = 30, alt_min_coins: int = 3,
                alt_max_share: float = 0.5) -> Screen:
    """Copyability gates on the first page of fills. Speed is judged on ALL fills (it is how the wallet
    behaves); everything else only on the `coins` we copy: the main coins (all core perps when None), or every
    core perp when the page shows a diversified wallet."""
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
    core_n = sum(f.notional for f in page if is_core_perp(f.coin))
    m["core_share"] = round(core_n / total, 3) if total else 0
    if not total or core_n / total < 0.5:
        return Screen(False, "core_perp_share<50%", m)
    # we only copy the main coins (or everything, for a diversified wallet): the rest is ignored, not held against it
    if coins is not None:
        div, _ = diversification(round_trips(page), alt_min_coins, alt_max_share)
        m["diversified"] = div
        if div:
            coins = None
    core = [f for f in page if is_core_perp(f.coin) and (coins is None or f.coin in coins)]
    core_n = sum(f.notional for f in core)
    m["main_share"] = round(core_n / total, 3)
    if not core_n:
        return Screen(False, "no_main_coin_trades", m)
    maker = sum(f.notional for f in core if not f.crossed) / core_n
    m["maker_share"] = round(maker, 3)
    if maker > 0.7:
        return Screen(False, "maker_share>70%", m)
    hist = (now_ms - page[0].time) / DAY
    m["history_days"] = round(hist, 1)
    if hist < 60:
        return Screen(False, "history<60d", m)
    trips = round_trips(core)
    m["round_trips"] = len(trips)
    if len(trips) < min_trips:   # holds a core position and almost never goes flat: nothing to copy
        return Screen(False, f"round_trips<{min_trips}", m)
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
    diversified: bool = False                    # profitable across coins: copied in every perp, not only main coins
    coin_pnl: dict = field(default_factory=dict)  # biggest net pnl contributors, all core perps
    copy_edge_bps: float = 0.0
    open_losers: int = 0          # losing positions still open (counted as lost trades)
    open_loss_pct: float = 0.0    # unrealized losses of all open positions / account value
    live: bool = False            # the live account was checked
    shrink: float = 0.0
    scored_ms: int = 0
    points: dict = field(default_factory=dict)   # component -> points; they add up to `score` (0-100)
    rules: dict = field(default_factory=dict)    # the eligibility floors it was scored under
    v: int = VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoreParams:
    stop_pct: float = 3.0
    cost_bps: float = 13.0         # our round trip: 2 x (taker 4.5 + slippage 1 + extra 1) bps
    shrink_n0: float = 50.0
    min_trades: int = 30           # hard floor: fewer closed trips is too little evidence
    full_trades: int = 150         # trips needed for the full sample-size points
    blocks: int = 6
    block_days: int = 30
    max_dd: float = 0.35           # the max drawdown at which its points are halved
    max_cur_dd: float = 0.20       # same for the current drawdown
    max_concentration: float = 0.25
    coins: tuple | None = None     # only trades in these coins count (None = all core perps) ...
    alt_min_coins: int = 3         # ... unless the wallet is diversified (see `diversification`)
    alt_max_share: float = 0.5
    min_win_rate: float = 0.0      # hard floor on the win rate
    min_score: float = 1.0         # hard floor on the 0-100 score
    min_profit_factor: float = 1.0 # hard floor on the profit factor (above 1 is always required)
    max_dd_cap: float = 1.0        # hard cap on the max drawdown (1.0 = none)
    max_open_loss: float = 1.0     # hard cap on live unrealized losses / account value (1.0 = none)

    def rules(self) -> dict:
        """The eligibility floors: a cached score made under other floors is redone."""
        return {"min_win_rate": self.min_win_rate, "min_score": self.min_score,
                "min_profit_factor": self.min_profit_factor, "max_dd_cap": self.max_dd_cap,
                "max_open_loss": self.max_open_loss}


# component -> weight; the weights add up to 100
WEIGHTS = {"edge": 25, "profit_factor": 15, "consistency": 15, "trades": 15, "win_rate": 10, "max_dd": 10,
           "cur_dd": 5, "concentration": 5}


def _clamp(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def points(s: Score, p: ScoreParams) -> dict:
    """Each component earns 0..1 of its weight. Linear, capped, deterministic."""
    q = {
        "edge": s.copy_edge_bps / 50.0,                                       # 50 bps after costs = full
        "profit_factor": (s.profit_factor - 1.0) / 2.0,                       # PF 1 = none, PF 3 = full
        "consistency": s.positive_blocks / p.blocks,                          # 6 of 6 positive months = full
        "trades": (s.trades - p.min_trades) / max(1, p.full_trades - p.min_trades),
        "win_rate": (s.win_rate - 0.40) / 0.30,                               # 40% = none, 70% = full
        "max_dd": 1.0 - s.max_dd / (2 * p.max_dd),                            # 0% = full, 35% = half, 70% = none
        "cur_dd": 1.0 - s.cur_dd / (2 * p.max_cur_dd),                        # 0% = full, 20% = half, 40% = none
        "concentration": 1.0 - (s.concentration - p.max_concentration) / 0.5,   # <= 25% = full, 75% = none
    }
    return {k: round(WEIGHTS[k] * _clamp(v), 2) for k, v in q.items()}


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
               now_ms: int, p: ScoreParams = ScoreParams(), live: Account | None = None) -> Score:
    """`live` is the wallet's account right now (None = not checked: the live gates are skipped)."""
    start = now_ms - p.blocks * p.block_days * DAY
    fl = [f for f in fills if f.time >= start]
    trips = [t for t in round_trips(fl) if t.close_ms >= start]
    s = Score(address, False, scored_ms=now_ms)
    s.diversified, s.coin_pnl = diversification(trips, p.alt_min_coins, p.alt_max_share)
    if p.coins is not None and not s.diversified:
        fl = [f for f in fl if f.coin in p.coins]
        trips = [t for t in trips if t.coin in p.coins]
    s.trades = n = len(trips)
    if n == 0:
        s.reasons = ["no_round_trips"]
        return s
    held: list[float] = []   # unrealized pnl of the losing positions it keeps open, in the coins we score
    if live is not None:
        s.live = True
        core = [q for q in live.positions if is_core_perp(q.coin)]
        held = [q.upnl for q in core if q.upnl < 0 and (p.coins is None or s.diversified or q.coin in p.coins)]
        s.open_losers = len(held)
        s.open_loss_pct = -sum(q.upnl for q in core if q.upnl < 0) / max(live.value, account_value, 1.0)
    nets = [t.net for t in trips]
    s.pnl = sum(nets)
    s.win_rate = sum(1 for x in nets if x > 0) / (n + len(held))
    gp, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0) - sum(held)
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
    # hard gates: only what makes a wallet useless to copy; quality is scored, not gated
    checks = [
        (n >= p.min_trades, f"trades<{p.min_trades}"),
        (s.pnl > 0, "pnl<=0"),
        (s.profit_factor > 1, "profit_factor<=1"),
        (s.copy_edge_bps > 0, "copy_edge<=0"),
        (s.win_rate >= p.min_win_rate, f"win_rate<{p.min_win_rate * 100:.0f}%"),
        (s.profit_factor >= p.min_profit_factor, f"profit_factor<{p.min_profit_factor:g}"),
        (s.max_dd <= p.max_dd_cap, f"max_drawdown>{p.max_dd_cap * 100:.0f}%"),
        (live is None or bool(live.positions) or live.value >= EMPTY_USD, "account_empty"),
        (s.open_loss_pct <= p.max_open_loss, f"open_losses>{p.max_open_loss * 100:.0f}%"),
    ]
    s.points = points(s, p)
    if sum(s.points.values()) < p.min_score:
        checks.append((False, f"score<{p.min_score:g}"))
    s.reasons = [why for ok, why in checks if not ok]
    s.eligible = not s.reasons
    s.rules = p.rules()
    # an eligible wallet scores 1-100; a rejected one scores 0 and is never ranked
    s.score = round(min(100.0, max(1.0, sum(s.points.values()))), 2) if s.eligible else 0.0
    return s


def ranking(scores: list[Score]) -> list[str]:
    """Eligible wallets of the current scoring version, best first; ties broken by address so the order is
    fully deterministic."""
    el = [s for s in scores if s.eligible and s.v == VERSION]
    return [s.address for s in sorted(el, key=lambda s: (-round(s.score, 9), s.address))]
