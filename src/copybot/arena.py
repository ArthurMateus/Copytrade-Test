"""Invo arena (owner request 2026-10-19, extended 2026-10-20): which groups of at most 10 Invo traders would have made a
300$ wallet the most money in 2 weeks, in every size x stop the Invo book runs, instead of the owner picking traders by
hand; the best groups are then copied live in their own paper wallets.

Recipes pick the traders (top return, profit factor, win rate, hot hand, steady, the bot's own floors, most active),
each with 3, 5, 7 or 10 traders and a 14, 30 or 60 day look-back. Baselines next to them: random wallets (luck), the
traders the bot follows now, the best 10 in hindsight (a ceiling nobody can pick in advance) and "the arena's own
choice": at each window the 3 recipes with the best backtest SO FAR (only windows that had ended), measured on the
window they had not seen. That last one is the honest number for "use the backtest's best groups live".

Backtest = walk-forward: for every 2-week window (one starting every 7 days) a recipe picks using ONLY the calls before
the window starts (a call still open then counts at that moment's price), then its group's calls opened inside the
window are copied in EVERY wallet variant of the Invo book (owner request 2026-10-20: "different wallets for each
trader combination, for different risks and stops"): trader size and each invo.risk_wallets_pct level, at each of
invo.stops_pct (0 = the trader's exit). Groups are ranked on the wallet the bot really runs (trader size at
invo.main_stop_pct); the card shows the 3 best and 3 worst combinations (group x size x stop) and the average per stop.

Copies are replayed like the live Invo wallets:
- trader size: notional = the call's committed share x leverage x invo.size_mult of the wallet's equity at the open
  (live portfolios x invo.live_size_mult), capped at invo.max_risk_pct of equity at risk.stop_pct, at least the minimum
  notional; an r% risk wallet: r% of equity at its own stop (the trader's exit: at the 30% guard), at least the minimum;
- our ~5 s delay (owner request 2026-10-20): every entry, and every exit that follows the trader, is that much worse
  than the trader's price: the entry gap the bot MEASURED on its real copies (`delay_cost`: per coin with enough
  copies, else all of them; a configured default before there are enough) plus a margin;
- exit at the trader's close, at our stop (3%, 8%) or at the liquidation guard (`guard_pct`, the trader's exit) if a 1h
  candle reached it first; a copy still open at the window's end counts at that hour's close; nothing reads a price
  after the window;
- costs kept apart (owner request 2026-10-20): taker fees, the rest of our round-trip cost (slippage), funding, delay.
Not modelled: the wallets' risk limits and a second trader boosting a held coin (the live wallets have both).

Known bias (shown on the card): the traders come from today's Invo rankings and their portfolios as they are now
(deleted portfolios and calls are gone), so the past looks better than it was. The live wallets have neither bias.

Live (owner choice 2026-10-20): the 3 best groups are frozen (traders AND the portfolios their look-back kept) and copied
by the bot in 18 paper wallets each (runner `arena_build`, data/arena/wallets/), until the owner switches them to the
newest best groups (/arena -> button). State: data/arena/live.json; last result: data/arena/arena.json.

The calls come from the daily Invo search, which already reads every candidate's portfolios (`save_calls`, no extra
requests to Invo): data/cache/invo/calls/<user>.json, merged run after run so the history grows.
Pure except the files read and written here; prices come from `candles_of(coin, start, end)` (1h, cached by the
Scorer) and `funding_of(coin, start, end)`.
"""
from __future__ import annotations

import bisect
import html
import json
import math
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from copybot.invo import Call, Portfolio

DAY = 86_400_000
HOUR = 3_600_000
WINDOW_DAYS = 14          # one backtest window
STEP_DAYS = 7             # a backtest window starts every this many days
LOOKBACKS = (14, 30, 60)  # days a recipe looks back
SIZES = (3, 5, 7, 10)     # traders per group (owner: at most 10)
MAX_TRADERS = 10
MIN_TRADES = 5            # copies in the look-back to be picked at all
MIN_TRADES_RATIO = 10     # ... for the profit-factor and win-rate recipes
ACTIVE_DAYS = 14          # a pick must have opened a call this recently
HOT_DAYS = 7              # the hot-hand recipe ranks on this many last days
RANDOM_DRAWS = 20         # random wallets per window (the luck baseline)
LIVE_GROUPS = 3           # best groups copied live (owner choice 2026-10-20: 3 x 18 wallets)
CHOOSER_MIN = 2           # ended windows the arena's own choice needs before it chooses
MIN_GAPS = 20             # measured entry gaps needed to replace the configured delay cost (per coin likewise)
MAX_LEVERAGE = 10.0       # a copy takes the trader's leverage, at most this (owner request 2026-10-14)
GUARD_MAX_PCT = 30.0      # runner.EMERGENCY_STOP_PCT
VERSION = 2

RECIPES = {
    "top_return": "most money made",
    "top_pf": "best profit factor (wins ÷ losses)",
    "top_winrate": "highest win rate",
    "hot_hand": f"most money in the last {HOT_DAYS} days",
    "steady": "most money per unit of drawdown",
    "bot_rules": "the bot's floors (≥80% wins, ≥20 calls, PF ≥ 2), most money",
    "active": "most copies among profitable traders",
}
BASELINES = {
    "chooser": f"the arena's own choice (its best {LIVE_GROUPS} so far, on weeks it had not seen)",
    "random": "random traders (luck)",
    "follows": "the traders the bot follows now",
    "hindsight": "best traders in hindsight (a ceiling, not a strategy)",
}


# ---- the calls, saved by the daily Invo search ----------------------------------------------------------------------
def _call_from(d: dict) -> Call:
    return Call(**{k: v for k, v in d.items() if k in Call.__dataclass_fields__})


def _portfolio_from(d: dict) -> Portfolio:
    d = dict(d)
    d["open_assets"] = tuple(d.get("open_assets") or ())
    return Portfolio(**{k: v for k, v in d.items() if k in Portfolio.__dataclass_fields__})


def save_calls(cache_dir, name: str, read: list[tuple[Portfolio, list[Call], list[Call]]], now_ms: int) -> None:
    """Merges what the search read of a trader (portfolio, closed calls, open calls) into its file. A call keeps its
    newest version (open -> closed); calls no longer listed are kept (history beyond what the search reads)."""
    d = Path(cache_dir) / "invo" / "calls"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name.lower()}.json"
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    ports = dict(old.get("portfolios") or {})
    calls = dict(old.get("calls") or {})
    for port, closed, opened in read:
        ports[port.id] = asdict(port)
        for c in [*opened, *closed]:            # closed last: a call seen both ways is closed
            calls[c.id] = asdict(c)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"name": name, "ts": now_ms, "portfolios": ports, "calls": calls}), encoding="utf-8")
    tmp.replace(p)


def load_calls(cache_dir) -> dict[str, tuple[dict[str, Portfolio], list[Call]]]:
    """Every saved trader: name -> (portfolios by id, calls)."""
    out = {}
    for p in sorted((Path(cache_dir) / "invo" / "calls").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            ports = {k: _portfolio_from(v) for k, v in (d.get("portfolios") or {}).items()}
            calls = [_call_from(v) for v in (d.get("calls") or {}).values()]
        except (OSError, ValueError, TypeError):
            continue
        out[d.get("name") or p.stem] = (ports, calls)
    return out


# ---- wallet variants and the delay ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Variant:
    """One wallet of a group: its size (None = the trader's size, else r% risk) and its stop (0 = the trader's exit)."""
    risk: float | None = None
    stop: float = 0.0

    @property
    def key(self) -> str:
        return f"{'trader' if self.risk is None else f'r{self.risk:g}'}·{'exit' if self.stop <= 0 else f's{self.stop:g}'}"

    @property
    def size_label(self) -> str:
        return "trader size" if self.risk is None else f"{self.risk:g}% risk"

    @property
    def stop_label(self) -> str:
        return f"{self.stop:g}% stop" if self.stop > 0 else "trader's exit"


def delay_cost(gaps: list[tuple[str, float]], default_bps: float, margin_bps: float) -> dict:
    """What our late entry costs, per side, from the bot's real copies: `gaps` = (coin, % we got in worse than the
    trader; + = worse). The mean of all of them (or the configured default before MIN_GAPS copies) plus the margin,
    and per coin where that coin has MIN_GAPS copies. Never below the margin (a lucky streak is not an edge)."""
    vals = [g * 100 for _, g in gaps]
    measured = len(vals) >= MIN_GAPS
    base = max(0.0, statistics.fmean(vals)) if measured else default_bps
    by: dict[str, list[float]] = {}
    for coin, g in gaps:
        by.setdefault(coin, []).append(g * 100)
    return {"bps": round(base + margin_bps, 2), "measured": len(vals), "default": not measured,
            "margin_bps": margin_bps,
            "by_coin": {c: round(max(0.0, statistics.fmean(v)) + margin_bps, 2)
                        for c, v in sorted(by.items()) if len(v) >= MIN_GAPS}}


# ---- replay ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Params:
    start_equity: float = 300.0
    size_mult: float = 1.0
    live_size_mult: float = 10.0
    cap_frac: float = 1.0 / 3.0       # invo.max_risk_pct / risk.stop_pct: the biggest copy as a share of equity
    min_notional: float = 10.0
    cost_bps: float = 13.0            # our whole round trip (fees + slippage)
    fee_bps: float = 4.5              # ... of which the taker fee, per side
    delay_bps: float = 0.0            # our late entry and late trader-driven exit, per side (delay_cost)
    delay_by_coin: tuple = ()         # ((coin, bps), ...) where measured
    risks: tuple = (1.0, 2.0, 5.0, 10.0, 20.0)   # invo.risk_wallets_pct
    stops: tuple = (3.0, 8.0, 0.0)    # invo.stops_pct
    main_stop: float = 0.0            # invo.main_stop_pct

    @classmethod
    def of(cls, cfg, cost_bps: float = 13.0, delay: dict | None = None) -> "Params":
        d = delay or {"bps": cfg.invo.arena_delay_bps + cfg.invo.arena_delay_margin_bps, "by_coin": {}}
        return cls(cfg.risk.start_equity, cfg.invo.size_mult, cfg.invo.live_size_mult,
                   cfg.invo.max_risk_pct / cfg.risk.stop_pct, cfg.risk.min_notional_usd, cost_bps,
                   cfg.broker.taker_fee_pct * 100, d["bps"], tuple(sorted((d.get("by_coin") or {}).items())),
                   tuple(cfg.invo.risk_wallets_pct), tuple(cfg.invo.stops_pct), cfg.invo.main_stop_pct)

    @property
    def main(self) -> Variant:
        return Variant(None, self.main_stop)

    def variants(self) -> list[Variant]:
        stops = [self.main_stop, *[s for s in self.stops if s != self.main_stop]]
        return [Variant(r, s) for s in stops for r in (None, *self.risks)]

    def delay(self, coin: str) -> float:
        return dict(self.delay_by_coin).get(coin, self.delay_bps)


@dataclass(frozen=True)
class Copy:
    """One Invo call, in Hyperliquid's coin and price unit."""
    trader: str
    portfolio: str
    coin: str
    long: bool
    lev: float
    exposure: float           # committed share of the trader's portfolio x leverage
    live: bool
    entry: float
    open_ms: int
    close_ms: int | None
    close_px: float | None


def guard_pct(lev: float) -> float:
    """The liquidation guard of a trader's-exit copy at this leverage (= runner.invo_emergency_stop)."""
    lev = max(1, math.ceil(min(max(lev, 1.0), MAX_LEVERAGE) - 1e-9))
    return min(GUARD_MAX_PCT, (1.0 / lev - 0.025) * 0.95 * 100)


def prepare(traders: dict[str, tuple[dict, list[Call]]], coin_of) -> dict[str, list[Copy]]:
    """name -> its calls as copies (calls on coins Hyperliquid does not list are dropped), oldest first.
    `coin_of(ticker)` -> coin, or (coin, price factor) as Bot.invo_trade_coin."""
    out = {}
    for name, (ports, calls) in traders.items():
        cs = []
        for c in calls:
            r = coin_of(c.ticker)
            coin, fx = r if isinstance(r, tuple) else (r, 1.0)
            if not coin or c.entry <= 0 or c.exposure <= 0:
                continue
            if c.closed_ms is not None and (not c.closing_price or c.closing_price <= 0) and not c.is_open:
                continue
            p = ports.get(c.portfolio_id)
            live = c.live or (p is not None and p.kind == "live")
            closed = None if c.is_open else c.closed_ms
            cs.append(Copy(name, c.portfolio_id, coin, c.long, c.leverage or 1.0, c.exposure, live, c.entry * fx,
                           c.created_ms, closed, c.closing_price * fx if closed and c.closing_price else None))
        if cs:
            out[name] = sorted(cs, key=lambda x: (x.open_ms, x.trader, x.coin))
    return out


class Prices:
    """1h candles per coin, read once per coin for the whole span; stop hits cached (every variant and window
    re-asks the same ones)."""

    def __init__(self, candles_of, funding_of=None, start_ms: int = 0, end_ms: int = 0):
        self.candles_of, self.funding_of = candles_of, funding_of
        self.start, self.end = start_ms, end_ms
        self._c: dict[str, tuple[list[int], list]] = {}
        self._f: dict[str, tuple[list[int], list[float]]] = {}
        self._h: dict[tuple, tuple[int, float] | None] = {}

    def candles(self, coin: str):
        if coin not in self._c:
            try:
                cs = sorted(self.candles_of(coin, self.start, self.end) or [], key=lambda c: c.t)
            except Exception:
                cs = []
            self._c[coin] = ([c.t for c in cs], cs)
        return self._c[coin]

    def mark(self, coin: str, t: int) -> float | None:
        """The price at `t`: the close of the last hour that started before it."""
        ts, cs = self.candles(coin)
        i = bisect.bisect_left(ts, t) - 1
        return cs[i].c if i >= 0 else None

    def hit(self, coin: str, long: bool, entry: float, pct: float, start: int, end: int,
            horizon: int | None = None) -> tuple[int, float] | None:
        """(time, price) where a copy opened at `start` first moved `pct`% against us before `end`, else None.
        The hour the copy opened in counts only from its entry (its earlier range is not ours): skipped. The first
        hit up to `horizon` (the copy's own close) is cached, so other windows and variants only compare times."""
        key = (coin, long, entry, pct, start, horizon)
        if key not in self._h:
            ts, cs = self.candles(coin)
            lvl = entry * (1 - pct / 100) if long else entry * (1 + pct / 100)
            got = None
            for i in range(bisect.bisect_right(ts, start), bisect.bisect_left(ts, horizon or self.end)):
                c = cs[i]
                if (c.l <= lvl) if long else (c.h >= lvl):
                    got = (c.t, lvl)
                    break
            self._h[key] = got
        got = self._h[key]
        return got if got is not None and got[0] < end else None

    def funding(self, coin: str, side: int, start: int, end: int) -> float:
        """Funding a notional of 1 paid (+) while held."""
        if self.funding_of is None:
            return 0.0
        if coin not in self._f:
            try:
                rs = sorted(self.funding_of(coin, self.start, self.end) or [])
            except Exception:
                rs = []
            self._f[coin] = ([int(t) for t, _ in rs], [float(r) for _, r in rs])
        ts, rs = self._f[coin]
        return side * sum(rs[bisect.bisect_left(ts, start):bisect.bisect_right(ts, end)])


@dataclass
class Result:
    start_ms: int
    end_ms: int
    equity: float
    start_equity: float
    trades: int = 0
    wins: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    max_dd: float = 0.0               # biggest drop from the best realized equity, share of it
    open_left: int = 0                # copies still open at the end (counted at that price)
    recent: float = 0.0               # $ made by copies closed in the last HOT_DAYS
    last_open_ms: int = 0
    fees: float = 0.0                 # $ paid: taker fees
    slippage: float = 0.0             # ... the rest of our round-trip cost
    funding: float = 0.0              # ... funding (negative = received)
    delay: float = 0.0                # ... getting in (and out after the trader) later than the trader
    by_trader: dict = field(default_factory=dict)   # trader -> $ made
    curve: list = field(default_factory=list)       # [(ms, equity)] at every exit

    @property
    def ret(self) -> float:
        return self.equity / self.start_equity - 1

    @property
    def pnl(self) -> float:
        return self.equity - self.start_equity

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def pf(self) -> float:
        return self.gross_win / self.gross_loss if self.gross_loss > 0 else (99.0 if self.gross_win > 0 else 0.0)


def notional_of(c: Copy, v: Variant, equity: float, p: Params) -> float:
    """The copy's size in wallet variant v, as the live wallets size it (runner: invo_size / the risk sizers)."""
    if v.risk is None:
        mult = p.live_size_mult if c.live else p.size_mult
        return max(min(c.exposure * mult * equity, p.cap_frac * equity), p.min_notional * 1.05)
    at = v.stop if v.stop > 0 else guard_pct(v.risk / GUARD_MAX_PCT)
    return max(equity * v.risk / at, p.min_notional * 1.05)


def simulate(copies: list[Copy], start: int, end: int, p: Params, prices: Prices, v: Variant | None = None,
             start_equity: float | None = None) -> Result:
    """A wallet (variant v, default the main one) copying `copies` opened in [start, end): sized on its equity at each
    open, in after the trader by the delay cost, closed at the trader's exit (late by the delay too), its stop or the
    guard; copies still open at `end` counted at that hour's price. Never reads a price after `end`."""
    v = v or p.main
    eq0 = p.start_equity if start_equity is None else start_equity
    r = Result(start, end, eq0, eq0, curve=[(start, eq0)])
    todo = [c for c in copies if start <= c.open_ms < end]
    pending: list[tuple[int, int, float]] = []     # (exit ms, order, pnl)
    peak = eq0
    order = 0
    fee_bps = min(2 * p.fee_bps, p.cost_bps)          # cost_bps is the whole round trip: fees, then slippage
    slip_bps = p.cost_bps - fee_bps

    def settle(until: int) -> None:
        nonlocal peak
        pending.sort()
        while pending and pending[0][0] <= until:
            t, _, pnl = pending.pop(0)
            r.equity += pnl
            r.curve.append((t, r.equity))
            peak = max(peak, r.equity)
            if peak > 0:
                r.max_dd = max(r.max_dd, 1 - r.equity / peak)

    for c in todo:
        settle(c.open_ms)
        if r.equity <= 0:
            break
        notional = notional_of(c, v, r.equity, p)
        side = 1 if c.long else -1
        d = p.delay(c.coin) / 1e4
        ours = c.entry * (1 + side * d)               # we get in after them: a worse price
        if c.close_ms is not None and c.close_ms < end:
            t_out, theirs = max(c.close_ms, c.open_ms + 1), c.close_px
            px = theirs * (1 - side * d)              # ... and out after them
        else:
            t_out, theirs = end, prices.mark(c.coin, end)
            r.open_left += 1
            if theirs is None:                         # no price: the copy counts flat
                theirs = c.entry
            px = theirs
        hit = prices.hit(c.coin, c.long, ours, v.stop if v.stop > 0 else guard_pct(c.lev), c.open_ms, t_out,
                         c.close_ms)
        if hit is not None:                            # our own stop: no delay on the exit
            t_out, px = hit
            theirs = px
        gross = side * notional * (px / ours - 1)
        delay = side * notional * (theirs / c.entry - 1) - gross
        fees = notional * fee_bps / 1e4
        slip = notional * slip_bps / 1e4
        fund = notional * prices.funding(c.coin, side, c.open_ms, t_out)
        pnl = gross - fees - slip - fund
        r.fees += fees
        r.slippage += slip
        r.funding += fund
        r.delay += delay
        pending.append((t_out, order, pnl))
        order += 1
        r.trades += 1
        r.last_open_ms = max(r.last_open_ms, c.open_ms)
        r.by_trader[c.trader] = r.by_trader.get(c.trader, 0.0) + pnl
        if pnl > 0:
            r.wins += 1
            r.gross_win += pnl
        else:
            r.gross_loss -= pnl
        if t_out >= end - HOT_DAYS * DAY:
            r.recent += pnl
    settle(end)
    return r


# ---- recipes --------------------------------------------------------------------------------------------------------
def kept_portfolios(copies: list[Copy], start: int, end: int, p: Params, prices: Prices) -> set[str]:
    """The trader's portfolios a wallet would copy at `end`: the ones whose copies made money in [start, end) (the bot
    stops copying a portfolio that loses after our costs)."""
    by: dict[str, list[Copy]] = {}
    for c in copies:
        if start <= c.open_ms < end:
            by.setdefault(c.portfolio, []).append(c)
    return {pid for pid, cs in by.items() if simulate(cs, start, end, p, prices).pnl > 0}


def trader_stats(copies: dict[str, list[Copy]], start: int, end: int, p: Params, prices: Prices,
                 keep_out: dict | None = None) -> dict[str, Result]:
    """Each trader alone in [start, end), on its portfolios that made money there (its copied ones; written to
    `keep_out` when given)."""
    out = {}
    for name, cs in copies.items():
        if not any(start <= c.open_ms < end for c in cs):
            continue
        keep = kept_portfolios(cs, start, end, p, prices)
        if keep_out is not None:
            keep_out[name] = keep
        mine = [c for c in cs if c.portfolio in keep]
        if mine:
            out[name] = simulate(mine, start, end, p, prices)
    return out


def pick(recipe: str, stats: dict[str, Result], k: int, end: int) -> list[str]:
    """The `k` traders a recipe picks from the look-back results. Every recipe needs MIN_TRADES copies, a call in the
    last ACTIVE_DAYS and money made."""
    ok = {n: s for n, s in stats.items()
          if s.trades >= MIN_TRADES and s.pnl > 0 and s.last_open_ms >= end - ACTIVE_DAYS * DAY}
    key = {
        "top_return": lambda s: (s.pnl,),
        "top_pf": lambda s: (min(s.pf, 99.0), s.pnl),
        "top_winrate": lambda s: (s.win_rate, s.pnl),
        "hot_hand": lambda s: (s.recent, s.pnl),
        "steady": lambda s: (s.ret / max(s.max_dd, 0.02), s.pnl),
        "bot_rules": lambda s: (s.pnl,),
        "active": lambda s: (s.trades, s.pnl),
    }[recipe]
    if recipe in ("top_pf", "top_winrate"):
        ok = {n: s for n, s in ok.items() if s.trades >= MIN_TRADES_RATIO}
    if recipe == "bot_rules":
        ok = {n: s for n, s in ok.items() if s.win_rate >= 0.8 and s.trades >= 20 and s.pf >= 2}
    if recipe == "hot_hand":
        ok = {n: s for n, s in ok.items() if s.recent > 0}
    return [n for n, _ in sorted(ok.items(), key=lambda x: (*key(x[1]), x[0]), reverse=True)[:k]]


def wallet_copies(copies: dict[str, list[Copy]], names: list[str], keep: dict[str, set[str]] | None) -> list[Copy]:
    """The calls a wallet following `names` copies (only each trader's kept portfolios when given), oldest first."""
    out = [c for n in names for c in copies.get(n, ()) if keep is None or c.portfolio in keep.get(n, ())]
    return sorted(out, key=lambda c: (c.open_ms, c.trader, c.coin))


@dataclass(frozen=True)
class Strategy:
    recipe: str
    k: int = MAX_TRADERS
    lookback: int = 30

    @property
    def key(self) -> str:
        if self.recipe in BASELINES:
            return self.recipe
        return f"{self.recipe}·{self.k}·{self.lookback}d"

    @property
    def label(self) -> str:
        if self.recipe in BASELINES:
            return BASELINES[self.recipe]
        return f"{RECIPES[self.recipe]} · top {self.k} · {self.lookback}-day look-back"


def strategies() -> list[Strategy]:
    return [Strategy(r, k, lb) for r in RECIPES for k in SIZES for lb in LOOKBACKS]


class Picker:
    """Look-back results per (end, look-back), shared by every recipe."""

    def __init__(self, copies: dict[str, list[Copy]], p: Params, prices: Prices):
        self.copies, self.p, self.prices = copies, p, prices
        self._stats: dict[tuple[int, int], dict[str, Result]] = {}
        self._keep: dict[tuple[int, int], dict[str, set[str]]] = {}

    def stats(self, end: int, lookback: int) -> dict[str, Result]:
        k = (end, lookback)
        if k not in self._stats:
            self._keep[k] = {}
            self._stats[k] = trader_stats(self.copies, end - lookback * DAY, end, self.p, self.prices, self._keep[k])
        return self._stats[k]

    def keep(self, end: int, lookback: int) -> dict[str, set[str]]:
        self.stats(end, lookback)
        return self._keep[(end, lookback)]

    def picks(self, s: Strategy, end: int) -> list[str]:
        return pick(s.recipe, self.stats(end, s.lookback), s.k, end)

    def run(self, names: list[str], start: int, end: int, lookback: int = 30, keep: bool = True,
            v: Variant | None = None) -> Result:
        """A wallet following `names` from `start` (portfolios chosen on the `lookback` days before it) to `end`."""
        kp = self.keep(start, lookback) if keep else None
        return simulate(wallet_copies(self.copies, names, kp), start, end, self.p, self.prices, v)


def eligible(picker: Picker, end: int, lookback: int = 30) -> list[str]:
    """Traders any recipe could pick (enough copies, active), for the random baseline: no performance filter."""
    start = end - lookback * DAY
    out = []
    for n, cs in picker.copies.items():
        opened = [c.open_ms for c in cs if start <= c.open_ms < end]
        if len(opened) >= MIN_TRADES and max(opened) >= end - ACTIVE_DAYS * DAY:
            out.append(n)
    return sorted(out)


# ---- backtest -------------------------------------------------------------------------------------------------------
def windows(copies: dict[str, list[Copy]], now_ms: int) -> list[int]:
    """Window starts: every STEP_DAYS from the oldest call + the longest look-back, while a whole window fits."""
    first = min((c.open_ms for cs in copies.values() for c in cs), default=now_ms)
    t0, out = first + max(LOOKBACKS) * DAY, []
    last = now_ms - WINDOW_DAYS * DAY
    t = last
    while t >= t0:                      # anchored on now, so the newest window ends today
        out.append(t)
        t -= STEP_DAYS * DAY
    return sorted(out)


def summarize(rets: list[float]) -> dict:
    if not rets:
        return {"n_windows": 0, "mean": 0.0, "median": 0.0, "worst": 0.0, "best": 0.0, "positive": 0.0, "chained": 1.0}
    chained = 1.0
    for x in rets[::-1][::max(1, WINDOW_DAYS // STEP_DAYS)]:   # back-to-back windows ending today (no overlap)
        chained *= 1 + x
    return {"n_windows": len(rets), "mean": statistics.fmean(rets), "median": statistics.median(rets),
            "worst": min(rets), "best": max(rets), "positive": sum(1 for x in rets if x > 0) / len(rets),
            "chained": chained}


def _costs(rs: list[Result]) -> dict:
    """Average $ per 2-week window: fees, slippage, funding, delay; trades per window."""
    n = max(1, len(rs))
    return {k: round(sum(getattr(r, k) for r in rs) / n, 3) for k in ("fees", "slippage", "funding", "delay")} | \
        {"trades": round(sum(r.trades for r in rs) / n, 1)}


def backtest(copies: dict[str, list[Copy]], p: Params, prices: Prices, now_ms: int, follows: list[str] = (),
             seed: int = 7, picker: Picker | None = None) -> dict:
    """Every strategy in every wallet variant over every window. Returns the strategies ranked on the main wallet
    (best mean 2-week return first) with their windows, the best and worst combinations (strategy x variant), the
    average per stop and per size, and the baselines (the arena's own choice, random, follows, hindsight)."""
    picker = picker or Picker(copies, p, prices)
    starts = windows(copies, now_ms)
    vs = p.variants()
    main = p.main
    rows: dict[str, dict] = {}
    per_v: dict[tuple[str, str], list[Result]] = {}

    def add(s: Strategy, t0: int, names: list[str], res: Result) -> None:
        row = rows.setdefault(s.key, {"key": s.key, "recipe": s.recipe, "k": s.k, "lookback": s.lookback,
                                      "label": s.label, "windows": []})
        row["windows"].append({"start": t0, "ret": res.ret, "pnl": res.pnl, "trades": res.trades,
                               "win_rate": res.win_rate, "max_dd": res.max_dd, "picks": names,
                               "fees": round(res.fees, 3), "funding": round(res.funding, 3),
                               "delay": round(res.delay, 3),
                               "by_trader": {n: round(x, 2) for n, x in sorted(res.by_trader.items(),
                                                                               key=lambda x: -x[1])}})

    rng = random.Random(seed)
    for t0 in starts:
        t1 = t0 + WINDOW_DAYS * DAY
        for s in strategies():
            names = picker.picks(s, t0)
            for v in vs:
                res = picker.run(names, t0, t1, s.lookback, v=v)
                per_v.setdefault((s.key, v.key), []).append(res)
                if v == main:
                    add(s, t0, names, res)
        pool = eligible(picker, t0)
        # luck: random traders, every portfolio copied (no filter on how they did)
        draws = [picker.run(rng.sample(pool, min(MAX_TRADERS, len(pool))), t0, t1, keep=False)
                 for _ in range(RANDOM_DRAWS)] if pool else []
        rets = sorted(d.ret for d in draws)
        mid = draws[[d.ret for d in draws].index(rets[len(rets) // 2])] if draws else simulate([], t0, t1, p, prices)
        add(Strategy("random"), t0, [f"{RANDOM_DRAWS} random draws of {min(MAX_TRADERS, len(pool))}"], mid)
        if follows:
            add(Strategy("follows"), t0, list(follows), picker.run(list(follows)[:MAX_TRADERS], t0, t1, keep=False))
        solo = trader_stats(copies, t0, t1, p, prices)
        best = [n for n, _ in sorted(solo.items(), key=lambda x: -x[1].pnl)[:MAX_TRADERS]]
        add(Strategy("hindsight"), t0, best, picker.run(best, t0, t1, keep=False))
    # the arena's own choice: at each window, its LIVE_GROUPS best strategies on the windows that had ENDED by then
    keys = [s.key for s in strategies()]
    for i, t0 in enumerate(starts):
        seen = [j for j, tj in enumerate(starts) if tj + WINDOW_DAYS * DAY <= t0]
        if len(seen) < CHOOSER_MIN:
            continue
        chosen = sorted(keys, key=lambda k: (-statistics.fmean(rows[k]["windows"][j]["ret"] for j in seen), k))
        chosen = chosen[:LIVE_GROUPS]
        ws = [rows[k]["windows"][i] for k in chosen]
        row = rows.setdefault("chooser", {"key": "chooser", "recipe": "chooser", "k": MAX_TRADERS, "lookback": 0,
                                          "label": BASELINES["chooser"], "windows": []})
        row["windows"].append({"start": t0, "ret": statistics.fmean(w["ret"] for w in ws),
                               "pnl": statistics.fmean(w["pnl"] for w in ws), "trades": sum(w["trades"] for w in ws),
                               "win_rate": statistics.fmean(w["win_rate"] for w in ws),
                               "max_dd": max(w["max_dd"] for w in ws), "picks": chosen, "by_trader": {}})
    for row in rows.values():
        row.update(summarize([w["ret"] for w in row["windows"]]))
    ranked = sorted(rows.values(), key=lambda r: (r["recipe"] not in BASELINES, r["mean"], r["median"]), reverse=True)
    combos = []
    for s in strategies():
        for v in vs:
            rs = per_v.get((s.key, v.key)) or []
            if rs:
                combos.append({"key": s.key, "label": s.label, "variant": v.key, "size": v.size_label,
                               "stop": v.stop_label, **summarize([r.ret for r in rs]), **_costs(rs)})
    combos.sort(key=lambda c: (-c["mean"], -c["median"], c["key"], c["variant"]))

    def avg_by(field_: str) -> dict:
        by: dict[str, list[float]] = {}
        for c in combos:
            by.setdefault(c[field_], []).append(c["mean"])
        return {k: statistics.fmean(x) for k, x in by.items()}
    return {"v": VERSION, "ts": now_ms, "window_days": WINDOW_DAYS, "step_days": STEP_DAYS, "starts": starts,
            "traders": len(copies), "calls": sum(len(cs) for cs in copies.values()), "ranking": ranked,
            "main": main.key, "variants": len(vs), "strategies": len(keys), "best": combos[:3],
            "worst": combos[-3:][::-1] if len(combos) > 3 else [], "by_stop": avg_by("stop"), "by_size": avg_by("size"),
            "delay_bps": p.delay_bps, "delay_by_coin": dict(p.delay_by_coin)}


def best_groups(bt: dict, picker: Picker, now_ms: int, n: int = LIVE_GROUPS) -> list[dict]:
    """The groups to copy live: the `n` best strategies of the backtest (on the main wallet), each picking NOW with its
    look-back; a group with nobody, or with the same traders as a better one, is skipped."""
    out, seen = [], set()
    for r in bt.get("ranking", []):
        if r["recipe"] in BASELINES or not r["windows"]:
            continue
        s = Strategy(r["recipe"], r["k"], r["lookback"])
        names = picker.picks(s, now_ms)
        if not names or tuple(sorted(names)) in seen:
            continue
        seen.add(tuple(sorted(names)))
        keep = picker.keep(now_ms, s.lookback)
        out.append({"key": s.key, "label": s.label, "picks": names, "lookback": s.lookback,
                    "portfolios": {x: sorted(keep.get(x, ())) for x in names},
                    "backtest_mean": r["mean"], "chained": r["chained"], "positive": r["positive"]})
        if len(out) >= n:
            break
    return out


def replay_live(live: dict, copies: dict[str, list[Copy]], p: Params, prices: Prices, now_ms: int) -> list[dict]:
    """What the replay says the live groups made since they started (main wallet): next to the real wallets, it shows
    how far the backtest's way of counting is from real fills."""
    out = []
    for g in live.get("groups") or []:
        keep = {n: set(v) for n, v in (g.get("portfolios") or {}).items()}
        res = simulate(wallet_copies(copies, g["picks"], keep), live["start"], now_ms, p, prices)
        out.append({"slot": g["slot"], "ret": res.ret, "pnl": res.pnl, "trades": res.trades})
    return out


# ---- the run the bot does after each Invo search --------------------------------------------------------------------
def run(cache_dir, out_dir, coin_of, candles_of, p: Params, now_ms: int, follows: list[str] = (),
        funding_of=None, live: dict | None = None, delay: dict | None = None, groups: int = LIVE_GROUPS) -> dict:
    """Backtest, the best groups now, and the replay of the live groups. Writes out_dir/arena.json and returns
    {"backtest", "next", "live_replay", "delay"}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    copies = prepare(load_calls(cache_dir), coin_of)
    first = min((c.open_ms for cs in copies.values() for c in cs), default=now_ms)
    prices = Prices(candles_of, funding_of, first - DAY, now_ms + HOUR)
    picker = Picker(copies, p, prices)
    bt = backtest(copies, p, prices, now_ms, follows, picker=picker)
    res = {"v": VERSION, "ts": now_ms, "backtest": bt, "next": best_groups(bt, picker, now_ms, groups),
           "live_replay": replay_live(live, copies, p, prices, now_ms) if live else [],
           "delay": delay or {"bps": p.delay_bps, "by_coin": dict(p.delay_by_coin), "measured": 0, "default": True}}
    _write(out / "arena.json", res)
    return res


def load_result(out_dir) -> dict | None:
    """The last run's result (arena.json), for /arena after a restart; None before the first run (or an old one)."""
    try:
        res = json.loads((Path(out_dir) / "arena.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return res if res.get("v") == VERSION else None


def load_live(out_dir) -> dict | None:
    """The live groups (data/arena/live.json): {"start", "groups": [{"slot", "key", "label", "picks", "portfolios",
    ...}]}; None before the first ones."""
    try:
        d = json.loads((Path(out_dir) / "live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if d.get("groups") else None


def save_live(out_dir, live: dict) -> None:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    _write(Path(out_dir) / "live.json", live)


def new_live(groups: list[dict], now_ms: int) -> dict:
    return {"v": VERSION, "start": now_ms, "groups": [{**g, "slot": i} for i, g in enumerate(groups, 1)]}


def same_groups(live: dict | None, groups: list[dict]) -> bool:
    if not live:
        return not groups
    return sorted(tuple(sorted(g["picks"])) for g in live["groups"]) == sorted(tuple(sorted(g["picks"])) for g in groups)


def _write(p: Path, obj) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=list), encoding="utf-8")
    tmp.replace(p)


# ---- Discord --------------------------------------------------------------------------------------------------------
def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.1f}%"


def _day(ms: int, offset_h: float = 0.0) -> str:
    return time.strftime("%d/%m", time.gmtime(ms / 1000 + offset_h * 3600))


def _usd(x: float) -> str:
    return ("+" if x >= 0 else "−") + f"{abs(x):.2f}$"


def _medal(i: int) -> str:
    return {0: "🥇", 1: "🥈", 2: "🥉"}.get(i, f"{i + 1}.")


def card(res: dict | None, live: dict | None, now_ms: int, offset_h: float = 0.0) -> str:
    """/arena: the live groups' wallets (3 best and 3 worst of group x size x stop, average per stop and per group,
    fees and funding), then the backtest (3 best and 3 worst combinations, per stop, the honest check).
    `live` = {"start", "groups", "wallets": [{"slot", "size", "stop", "equity0", "pnl", "trades", "wins", "open",
    "fees", "funding"}]} built by the runner from the live wallets' ledgers."""
    e = html.escape
    lines = ["🏟️ <b>Invo arena</b> · the best groups of ≤10 Invo traders, each in every size × stop (300$ each, paper)"]
    if live and live.get("groups"):
        ws = sorted(live.get("wallets") or [], key=lambda w: (-w["pnl"], w["slot"], w["size"], w["stop"]))
        days = max(1, math.ceil((now_ms - live["start"]) / DAY))
        lines.append(f"\n<b>Live</b> since {_day(live['start'], offset_h)} (day {days}) · {len(live['groups'])} groups × "
                     f"{len(ws) // max(1, len(live['groups']))} wallets")
        for g in live["groups"]:
            lines.append(f"<b>G{g['slot']}</b> {e(g['label'])}: {e(', '.join(g['picks']))}")

        def line(i: int, w: dict) -> str:
            won = f" · {w['wins'] / w['trades'] * 100:.0f}% won" if w["trades"] else ""
            pct = w["pnl"] / w["equity0"] * 100 if w["equity0"] else 0.0
            dot = "🟢" if w["pnl"] > 0.005 else ("🔴" if w["pnl"] < -0.005 else "⚪")
            return (f"{_medal(i) if abs(w['pnl']) > 0.005 or i >= 3 else f'{i + 1}.'} <b>G{w['slot']} · "
                    f"{e(w['size'])} · {e(w['stop'])}</b> {dot} {_usd(w['pnl'])} ({pct:+.1f}%) · {w['trades']} trades"
                    f"{won} · {w['open']} open · fees {w['fees']:.2f}$ · funding {_usd(-w['funding'])}")
        if len(ws) <= 6:
            lines += [line(i, w) for i, w in enumerate(ws)]
        elif ws:
            lines += ["🏆 <b>Best</b>", *(line(i, w) for i, w in enumerate(ws[:3]))]
            lines += ["🔻 <b>Worst</b>", *(line(len(ws) - 3 + i, w) for i, w in enumerate(ws[-3:]))]
        if ws:
            def avg(k: str) -> str:
                by: dict = {}
                for w in ws:
                    by.setdefault(w[k], []).append(w["pnl"] / w["equity0"] * 100 if w["equity0"] else 0.0)
                return " · ".join(f"{e(str(name))} {sum(v) / len(v):+.2f}%" for name, v in
                                  sorted(by.items(), key=lambda x: -sum(x[1]) / len(x[1])))
            lines.append(f"<b>By stop</b> (average): {avg('stop')}")
            lines.append("<b>By group</b> (average): " + " · ".join(
                f"G{s} {x:+.2f}%" for s, x in sorted(
                    ((s, statistics.fmean(w['pnl'] / w['equity0'] * 100 for w in ws if w['slot'] == s))
                     for s in {w['slot'] for w in ws}), key=lambda y: -y[1])))
            lines.append(f"Costs so far, all {len(ws)} wallets: fees {sum(w['fees'] for w in ws):.2f}$ · funding "
                         f"{_usd(-sum(w['funding'] for w in ws))}")
        rep = (res or {}).get("live_replay") or []
        if rep:
            lines.append("Replay of the same calls (trader size · main stop): " + " · ".join(
                f"G{x['slot']} {_pct(x['ret'])}" for x in rep) + " (real fills differ: delay, limits)")
    elif res and res.get("backtest"):
        lines.append("\nNo live groups yet: the best groups start after the next backtest that finds some.")
    if not res or not res.get("backtest"):
        lines.append("\n<b>Backtest</b>: no run yet. It runs after each Invo search (daily); the first needs the search "
                     "to have read the traders once.")
        return "\n".join(lines)
    bt = res["backtest"]
    d = res.get("delay") or {}
    how = ("configured, not measured yet" if d.get("default") else f"measured on {d.get('measured', 0)} real copies")
    lines.append(f"\n<b>Backtest</b> · {bt['traders']} traders, {bt['calls']} calls, {len(bt['starts'])} two-week windows · "
                 f"{bt.get('strategies', 0)} groups × {bt.get('variants', 0)} wallets · delay cost "
                 f"{bt.get('delay_bps', 0) / 100:.2f}% per entry and exit ({how}, margin included)")

    def combo(i: int, c: dict) -> str:
        return (f"{_medal(i) if i < 3 else '•'} {_pct(c['mean'])} a fortnight · 300$ → {300 * c['chained']:.0f}$ · "
                f"{c['positive'] * 100:.0f}% up · <b>{e(c['size'])} · {e(c['stop'])}</b> · {e(c['label'])}\n"
                f"    per 2 weeks: {c['trades']:g} trades · fees {c['fees']:.2f}$ · funding {_usd(-c['funding'])} · "
                f"delay {c['delay']:.2f}$")
    if bt.get("best"):
        lines += ["🏆 <b>Best combinations</b> (average 2-week return)", *(combo(i, c) for i, c in enumerate(bt["best"]))]
    if bt.get("worst"):
        lines += ["🔻 <b>Worst</b>", *(combo(3, c) for c in bt["worst"])]
    if bt.get("by_stop"):
        lines.append("<b>By stop</b> (average of every group and size): " + " · ".join(
            f"{e(k)} {_pct(x)}" for k, x in sorted(bt["by_stop"].items(), key=lambda y: -y[1])))
    base = {r["recipe"]: r for r in bt["ranking"] if r["recipe"] in BASELINES}
    top = next((r for r in bt["ranking"] if r["recipe"] not in BASELINES), None)
    if top:
        lines.append(f"Best group on the main wallet: {_pct(top['mean'])} · {e(top['label'])}")
    for k in ("chooser", "random", "follows", "hindsight"):
        if k in base:
            lines.append(f"  vs {_pct(base[k]['mean'])} · {e(BASELINES[k])}"
                         + (f" ({base[k]['n_windows']} windows)" if k == "chooser" else ""))
    if "chooser" not in base and bt["starts"]:
        lines.append(f"  (the arena's own choice needs {CHOOSER_MIN + 2} windows of history: not enough yet)")
    nxt = res.get("next") or []
    if nxt and live and not same_groups(live, nxt):
        lines.append("\n<b>Best groups now</b> (not live: /arena asks to switch):")
        lines += [f"• {e(g['label'])}: {e(', '.join(g['picks']))}" for g in nxt]
    lines.append("The backtest flatters (today's rankings, deleted calls are gone); the live wallets are the honest "
                 "check.")
    return "\n".join(lines)
