"""Deterministic synthetic trader histories in the REAL fill/candle shapes (for scoring tests). Prices and
fills are mutually consistent: closedPnl is the actual price move, candles contain every fill price."""
from __future__ import annotations

import random

from tests.fakes import make_fill

H = 3_600_000
DAY = 86_400_000


def candles_for(coin: str, start_ms: int, hours: int, px0: float, seed: int, vol: float = 0.004) -> list[dict]:
    rng = random.Random(f"{coin}-{seed}")
    out, px = [], px0
    t = (start_ms // H) * H
    for _ in range(hours):
        o = px
        c = o * (1 + rng.gauss(0, vol))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 3)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append({"t": t, "T": t + H - 1, "s": coin, "i": "1h", "o": f"{o:.6g}", "c": f"{c:.6g}",
                    "h": f"{hi:.6g}", "l": f"{lo:.6g}", "v": "1000", "n": 100})
        px = c
        t += H
    return out


def trader(now_ms: int, seed: int = 1, days: int = 180, trips: int = 400, win: float = 0.62, hold_h: int = 3,
           size_usd: float = 20_000, maker: bool = False, coins=("BTC", "ETH", "SOL"), first_day: int | None = None,
           big_winner: float = 0.0, adds: bool = True):
    """Returns (raw fills, {coin: raw candles}). The trader picks the right direction with prob `win`."""
    rng = random.Random(seed)
    start = now_ms - days * DAY
    hours = days * 24 + 2
    px0 = {"BTC": 100_000, "ETH": 3_000, "SOL": 150, "DOGE": 0.2}
    cs = {c: candles_for(c, start, hours, px0.get(c, 10), 0) for c in coins}  # one market for all traders
    fills = []
    first = (first_day if first_day is not None else 0) * 24
    slots = sorted(rng.sample(range(first, hours - hold_h - 2), trips))
    busy_until = {c: -1 for c in coins}
    oid = seed * 1_000_000
    for k, h0 in enumerate(slots):
        coin = coins[k % len(coins)]
        if h0 <= busy_until[coin]:
            continue
        cc = cs[coin]
        entry = float(cc[h0]["o"])
        exitp = float(cc[h0 + hold_h]["o"])
        right = 1 if exitp >= entry else -1
        side = right if rng.random() < win else -right
        sz = size_usd / entry
        if big_winner and k == len(slots) // 2:
            sz *= big_winner
        t0 = cc[h0]["t"] + 60_000
        oid += 1
        bs = "B" if side > 0 else "A"
        if adds:
            fills.append(make_fill(coin, entry, sz / 2, bs, 0.0, t=t0, oid=oid, crossed=not maker))
            oid += 1
            fills.append(make_fill(coin, entry, sz / 2, bs, side * sz / 2, t=t0 + 600_000, oid=oid, crossed=not maker))
        else:
            fills.append(make_fill(coin, entry, sz, bs, 0.0, t=t0, oid=oid, crossed=not maker))
        oid += 1
        pnl = side * (exitp - entry) * sz
        fills.append(make_fill(coin, exitp, sz, "A" if side > 0 else "B", side * sz, t=cc[h0 + hold_h]["t"] + 60_000,
                               oid=oid, crossed=not maker, closed_pnl=round(pnl, 6)))
        busy_until[coin] = h0 + hold_h + 1
    fills.sort(key=lambda f: (f["time"], f["tid"]))
    return fills, cs
