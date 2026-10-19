"""Invo arena (owner request 2026-10-19): which set of at most 10 Invo traders would have made a 300$ wallet the most
money in 2 weeks, instead of the owner picking traders by hand.

Recipes pick the traders (top return, profit factor, win rate, hot hand, steady, the bot's own floors, most active),
each with 3, 5 or 10 traders and a 14, 30 or 60 day look-back. Baselines next to them: random wallets (luck), the
traders the bot follows now, and the best 10 in hindsight (a ceiling nobody can pick in advance).

Backtest = walk-forward: for every 2-week window (one starting every 7 days) a recipe picks using ONLY the calls before
the window starts (a call still open then counts at that moment's price), then the wallet copies its traders' calls
opened inside the window. Live = a season: the best recipes' picks are frozen for 2 weeks and scored on the calls that
come after, the same way (the honest check of the backtest).

Copies are replayed like the main Invo wallet (`invo_calls`, the trader's exit): notional = the call's committed share
x leverage x invo.size_mult of the wallet's equity at the open (live portfolios x invo.live_size_mult), capped at
invo.max_risk_pct of equity at risk.stop_pct, at least the minimum notional; closed at the trader's exit price, or at
the liquidation guard (`guard_pct`, = runner.invo_emergency_stop of the call's leverage, at most 10x) if a 1h candle
reached it first; minus our round-trip cost and the funding paid. A copy still open at the window's end counts at that
hour's close. Not modelled: our copy delay (Invo calls are seen seconds after they are posted), the wallet's risk
limits, a second trader boosting a held coin.

Known bias (shown in the report): the traders come from today's Invo rankings and their portfolios as they are now
(deleted portfolios and calls are gone), so the past looks better than it was. The live season has neither bias.

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
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from copybot.invo import Call, Portfolio

DAY = 86_400_000
HOUR = 3_600_000
WINDOW_DAYS = 14          # one season / backtest window
STEP_DAYS = 7             # a backtest window starts every this many days
LOOKBACKS = (14, 30, 60)  # days a recipe looks back
SIZES = (3, 5, 10)        # traders per wallet (owner: at most 10)
MAX_TRADERS = 10
MIN_TRADES = 5            # copies in the look-back to be picked at all
MIN_TRADES_RATIO = 10     # ... for the profit-factor and win-rate recipes
ACTIVE_DAYS = 14          # a pick must have opened a call this recently
HOT_DAYS = 7              # the hot-hand recipe ranks on this many last days
RANDOM_DRAWS = 20         # random wallets per window (the luck baseline)
SEASON_WALLETS = 6        # best backtest recipes that play a live season (+ random and your follows)
MAX_LEVERAGE = 10.0       # a copy takes the trader's leverage, at most this (owner request 2026-10-14)
GUARD_MAX_PCT = 30.0      # runner.EMERGENCY_STOP_PCT
VERSION = 1

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


# ---- replay ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Params:
    start_equity: float = 300.0
    size_mult: float = 1.0
    live_size_mult: float = 10.0
    cap_frac: float = 1.0 / 3.0       # invo.max_risk_pct / risk.stop_pct: the biggest copy as a share of equity
    min_notional: float = 10.0
    cost_bps: float = 13.0

    @classmethod
    def of(cls, cfg, cost_bps: float = 13.0) -> "Params":
        return cls(cfg.risk.start_equity, cfg.invo.size_mult, cfg.invo.live_size_mult,
                   cfg.invo.max_risk_pct / cfg.risk.stop_pct, cfg.risk.min_notional_usd, cost_bps)


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
    """1h candles per coin, read once per coin for the whole span."""

    def __init__(self, candles_of, funding_of=None, start_ms: int = 0, end_ms: int = 0):
        self.candles_of, self.funding_of = candles_of, funding_of
        self.start, self.end = start_ms, end_ms
        self._c: dict[str, tuple[list[int], list]] = {}
        self._f: dict[str, tuple[list[int], list[float]]] = {}

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

    def guard_hit(self, coin: str, long: bool, entry: float, pct: float, start: int, end: int) -> tuple[int, float] | None:
        """(time, price) where a copy opened at `start` first moved `pct`% against us before `end`, else None.
        The hour the copy opened in counts only from its entry (its earlier range is not ours): skipped."""
        ts, cs = self.candles(coin)
        lvl = entry * (1 - pct / 100) if long else entry * (1 + pct / 100)
        for i in range(bisect.bisect_right(ts, start), bisect.bisect_left(ts, end)):
            c = cs[i]
            if (c.l <= lvl) if long else (c.h >= lvl):
                return c.t, lvl
        return None

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


def simulate(copies: list[Copy], start: int, end: int, p: Params, prices: Prices,
             start_equity: float | None = None) -> Result:
    """A wallet copying `copies` opened in [start, end): sized on its equity at each open, closed at the trader's exit
    (or the guard), copies still open at `end` counted at that hour's price. Never reads a price after `end`."""
    eq0 = p.start_equity if start_equity is None else start_equity
    r = Result(start, end, eq0, eq0, curve=[(start, eq0)])
    todo = [c for c in copies if start <= c.open_ms < end]
    pending: list[tuple[int, int, float]] = []     # (exit ms, order, pnl)
    peak = eq0
    order = 0

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
        mult = p.live_size_mult if c.live else p.size_mult
        notional = max(min(c.exposure * mult * r.equity, p.cap_frac * r.equity), p.min_notional * 1.05)
        side = 1 if c.long else -1
        if c.close_ms is not None and c.close_ms < end:
            t_out, px = max(c.close_ms, c.open_ms + 1), c.close_px
        else:
            t_out, px = end, prices.mark(c.coin, end)
            r.open_left += 1
            if px is None:                          # no price: the copy counts flat
                px = c.entry
        hit = prices.guard_hit(c.coin, c.long, c.entry, guard_pct(c.lev), c.open_ms, t_out)
        if hit is not None:
            t_out, px = hit
        pnl = side * notional * (px / c.entry - 1) - notional * p.cost_bps / 1e4
        pnl -= notional * prices.funding(c.coin, side, c.open_ms, t_out)
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

    def run(self, names: list[str], start: int, end: int, lookback: int = 30, keep: bool = True) -> Result:
        """A wallet following `names` from `start` (portfolios chosen on the `lookback` days before it) to `end`."""
        kp = self.keep(start, lookback) if keep else None
        return simulate(wallet_copies(self.copies, names, kp), start, end, self.p, self.prices)


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


def backtest(copies: dict[str, list[Copy]], p: Params, prices: Prices, now_ms: int, follows: list[str] = (),
             seed: int = 7) -> dict:
    """Every strategy over every window. Returns the ranking (best mean 2-week return first) and the per-window
    results, plus each strategy's picks for a season starting now."""
    picker = Picker(copies, p, prices)
    starts = windows(copies, now_ms)
    rows: dict[str, dict] = {}

    def add(s: Strategy, t0: int, names: list[str], res: Result) -> None:
        row = rows.setdefault(s.key, {"key": s.key, "recipe": s.recipe, "k": s.k, "lookback": s.lookback,
                                      "label": s.label, "windows": []})
        row["windows"].append({"start": t0, "ret": res.ret, "pnl": res.pnl, "trades": res.trades,
                               "win_rate": res.win_rate, "max_dd": res.max_dd, "picks": names,
                               "by_trader": {n: round(v, 2) for n, v in sorted(res.by_trader.items(),
                                                                               key=lambda x: -x[1])}})

    rng = random.Random(seed)
    for t0 in starts:
        t1 = t0 + WINDOW_DAYS * DAY
        for s in strategies():
            names = picker.picks(s, t0)
            add(s, t0, names, picker.run(names, t0, t1, s.lookback))
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
    for row in rows.values():
        row.update(summarize([w["ret"] for w in row["windows"]]))
    ranked = sorted(rows.values(), key=lambda r: (r["recipe"] not in BASELINES, r["mean"], r["median"]), reverse=True)
    return {"v": VERSION, "ts": now_ms, "window_days": WINDOW_DAYS, "step_days": STEP_DAYS, "starts": starts,
            "traders": len(copies), "calls": sum(len(cs) for cs in copies.values()), "ranking": ranked}


def best_strategies(bt: dict, n: int = SEASON_WALLETS) -> list[Strategy]:
    """The `n` best recipes of a backtest (baselines excluded), at most two per recipe so a season compares ideas."""
    out, per = [], {}
    for r in bt.get("ranking", []):
        if r["recipe"] in BASELINES or per.get(r["recipe"], 0) >= 2 or not r["windows"]:
            continue
        per[r["recipe"]] = per.get(r["recipe"], 0) + 1
        out.append(Strategy(r["recipe"], r["k"], r["lookback"]))
        if len(out) >= n:
            break
    return out


# ---- live seasons ---------------------------------------------------------------------------------------------------
def new_season(copies: dict[str, list[Copy]], p: Params, prices: Prices, bt: dict, now_ms: int,
               follows: list[str] = (), seed: int | None = None) -> dict:
    """A season starting now: the best backtest recipes' picks (look-back ending now), frozen for WINDOW_DAYS, next
    to one random wallet and the bot's follows."""
    picker = Picker(copies, p, prices)
    wallets = []
    by_key = {r["key"]: r for r in bt.get("ranking", [])}
    for s in best_strategies(bt):
        names = picker.picks(s, now_ms)
        if names:
            keep = picker.keep(now_ms, s.lookback)
            wallets.append({"key": s.key, "label": s.label, "picks": names, "lookback": s.lookback,
                            "portfolios": {n: sorted(keep.get(n, ())) for n in names},
                            "backtest_mean": by_key.get(s.key, {}).get("mean")})
    pool = eligible(picker, now_ms)
    if pool:
        rng = random.Random(now_ms if seed is None else seed)
        names = rng.sample(pool, min(MAX_TRADERS, len(pool)))
        wallets.append({"key": "random", "label": BASELINES["random"], "picks": names, "lookback": 30,
                        "portfolios": None, "backtest_mean": None})
    if follows:
        wallets.append({"key": "follows", "label": BASELINES["follows"], "picks": list(follows)[:MAX_TRADERS],
                        "lookback": 0, "portfolios": None, "backtest_mean": None})
    return {"v": VERSION, "start": now_ms, "end": now_ms + WINDOW_DAYS * DAY, "wallets": wallets, "standings": [],
            "done": False}


def score_season(season: dict, copies: dict[str, list[Copy]], p: Params, prices: Prices, now_ms: int) -> dict:
    """The season's standings at `now_ms` (or its end): each wallet replayed on the calls opened since it started."""
    end = min(now_ms, season["end"])
    rows = []
    for w in season["wallets"]:
        keep = None if w.get("portfolios") is None else {n: set(v) for n, v in w["portfolios"].items()}
        res = simulate(wallet_copies(copies, w["picks"], keep), season["start"], end, p, prices)
        rows.append({"key": w["key"], "label": w["label"], "picks": w["picks"], "equity": res.equity, "ret": res.ret,
                     "pnl": res.pnl, "trades": res.trades, "win_rate": res.win_rate, "max_dd": res.max_dd,
                     "open": res.open_left, "backtest_mean": w.get("backtest_mean"),
                     "by_trader": {n: round(v, 2) for n, v in sorted(res.by_trader.items(), key=lambda x: -x[1])},
                     "curve": [(t, round(e, 2)) for t, e in res.curve]})
    rows.sort(key=lambda r: -r["equity"])
    return {**season, "standings": rows, "as_of": end, "done": now_ms >= season["end"]}


# ---- the run the bot does after each Invo search --------------------------------------------------------------------
def run(cache_dir, out_dir, coin_of, candles_of, p: Params, now_ms: int, follows: list[str] = (),
        funding_of=None) -> dict:
    """Backtest, then the live season: scored, ended (kept in the history) and a new one started when due. Writes
    out_dir/arena.json (and the report page) and returns {"backtest", "season", "ended", "started"}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    copies = prepare(load_calls(cache_dir), coin_of)
    first = min((c.open_ms for cs in copies.values() for c in cs), default=now_ms)
    prices = Prices(candles_of, funding_of, first - DAY, now_ms + HOUR)
    bt = backtest(copies, p, prices, now_ms, follows)
    state_p = out / "season.json"
    try:
        state = json.loads(state_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"current": None, "past": []}
    ended = started = None
    cur = state.get("current")
    if cur is not None:
        cur = score_season(cur, copies, p, prices, now_ms)
        if cur["done"]:
            ended = cur
            state["past"] = ([season_summary(cur)] + list(state.get("past") or []))[:26]
            cur = None
    if cur is None and copies:
        cur = score_season(new_season(copies, p, prices, bt, now_ms, follows), copies, p, prices, now_ms)
        started = cur
    state["current"] = cur
    _write(state_p, state)
    res = {"backtest": bt, "season": cur, "past": state.get("past") or [], "ended": ended, "started": started}
    _write(out / "arena.json", {k: v for k, v in res.items() if k not in ("ended", "started")})
    (out / "index.html").write_text(report_html(res, now_ms), encoding="utf-8")
    return res


def load_result(out_dir) -> dict | None:
    """The last run's result (arena.json), for /arena after a restart; None before the first run."""
    try:
        return json.loads((Path(out_dir) / "arena.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def season_summary(s: dict) -> dict:
    """What a finished season keeps: who won, and whether the backtest's favourite did."""
    st = s.get("standings") or []
    return {"start": s["start"], "end": s["end"],
            "standings": [{k: r[k] for k in ("key", "label", "picks", "ret", "pnl", "trades", "backtest_mean")}
                          for r in st]}


def _write(p: Path, obj) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=list), encoding="utf-8")
    tmp.replace(p)


# ---- Discord --------------------------------------------------------------------------------------------------------
def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.1f}%"


def _day(ms: int, offset_h: float = 0.0) -> str:
    return time.strftime("%d/%m", time.gmtime(ms / 1000 + offset_h * 3600))


def card(res: dict | None, now_ms: int, offset_h: float = 0.0, report: str = "") -> str:
    """The /arena card: the live season's standings and the backtest's best recipes."""
    if not res or not res.get("backtest"):
        return ("🏟️ <b>Invo arena</b>: no run yet. It runs after each Invo search (daily); the first needs the "
                "search to have read the traders once.")
    e = html.escape
    lines = ["🏟️ <b>Invo arena</b> · which ≤10 traders make a $300 wallet the most in 2 weeks"]
    s = res.get("season")
    if s:
        day = min(WINDOW_DAYS, max(1, math.ceil((min(now_ms, s["end"]) - s["start"]) / DAY)))
        lines.append(f"\n<b>Live season</b> {_day(s['start'], offset_h)} → {_day(s['end'], offset_h)} · day "
                     f"{day}/{WINDOW_DAYS}")
        for i, r in enumerate(s.get("standings") or [], 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(f"{medal} ${r['equity']:.0f} ({_pct(r['ret'])}) · {r['trades']} copies · {e(r['label'])}")
            lines.append(f"    {e(', '.join(r['picks'][:MAX_TRADERS]))}")
    bt = res["backtest"]
    top = [r for r in bt["ranking"] if r["recipe"] not in BASELINES][:5]
    base = {r["recipe"]: r for r in bt["ranking"] if r["recipe"] in BASELINES}
    lines.append(f"\n<b>Backtest</b> · {bt['traders']} traders, {len(bt['starts'])} two-week windows · average 2-week "
                 "return (back-to-back $300 →):")
    for r in top:
        lines.append(f"• {_pct(r['mean'])} · ${300 * r['chained']:.0f} · {r['positive'] * 100:.0f}% of windows up · "
                     f"{e(r['label'])}")
    for k in ("random", "follows", "hindsight"):
        if k in base:
            lines.append(f"  vs {_pct(base[k]['mean'])} · {e(BASELINES[k])}")
    past = res.get("past") or []
    if past:
        p0 = past[0]
        w = p0["standings"][0] if p0["standings"] else None
        if w:
            lines.append(f"\nLast season ({_day(p0['start'], offset_h)}): won by {e(w['label'])} {_pct(w['ret'])}")
    if report:
        lines.append(f"\n📄 Full report: <code>{e(report)}</code>")
    lines.append("Paper only: nothing here trades. The backtest flatters (today's rankings, deleted calls are gone); "
                 "the live season is the honest check.")
    return "\n".join(lines)


def ended_card(s: dict, offset_h: float = 0.0) -> str:
    """The final standings of a season that just ended, and whether the backtest's favourite won."""
    e = html.escape
    st = s.get("standings") or []
    lines = [f"🏆 <b>Arena season over</b> · {_day(s['start'], offset_h)} → {_day(s['end'], offset_h)} · $300 each"]
    for i, r in enumerate(st, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} ${r['equity']:.0f} ({_pct(r['ret'])}) · {r['trades']} copies · {e(r['label'])}")
        if i <= 3:
            lines.append(f"    {e(', '.join(r['picks'][:MAX_TRADERS]))}")
    fav = max((r for r in st if r.get("backtest_mean") is not None), key=lambda r: r["backtest_mean"], default=None)
    if fav and st:
        rank = st.index(fav) + 1
        lines.append(f"\nThe backtest's favourite ({e(fav['label'])}) finished #{rank} of {len(st)}"
                     + (": the backtest called it." if rank == 1 else "."))
    lines.append("A new season starts now with fresh picks.")
    return "\n".join(lines)


def report_html(res: dict, now_ms: int) -> str:
    from copybot.arena_report import render
    return render(res, now_ms)
