"""Hyperliquid data contracts (parsers over REAL recorded responses) and the rate-limited REST client.

Read-only public endpoints only. This module never signs anything and never reads a key.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from copybot import log


# ---------------------------------------------------------------------------------------------
# data contracts
# ---------------------------------------------------------------------------------------------
def is_core_perp(coin: str) -> bool:
    """'#123' are outcome pseudo-coins (HTTP 500 on candles), '@123' spot, 'xyz:TSLA' builder perps."""
    return bool(coin) and not coin.startswith(("#", "@")) and ":" not in coin and "/" not in coin


@dataclass(frozen=True)
class Window:
    pnl: float
    roi: float
    vlm: float


@dataclass(frozen=True)
class LbRow:
    address: str
    account_value: float
    day: Window
    week: Window
    month: Window
    all_time: Window


def parse_leaderboard(obj: dict) -> list[LbRow]:
    out = []
    for r in obj["leaderboardRows"]:
        w = {k: v for k, v in r["windowPerformances"]}
        def win(name):
            d = w.get(name) or {}
            return Window(float(d.get("pnl", 0)), float(d.get("roi", 0)), float(d.get("vlm", 0)))
        out.append(LbRow(r["ethAddress"].lower(), float(r["accountValue"]), win("day"), win("week"),
                         win("month"), win("allTime")))
    return out


@dataclass(frozen=True)
class Fill:
    coin: str
    px: float
    sz: float
    side: str          # "B" buy / "A" sell
    time: int          # exchange ms
    start_pos: float   # signed position before this fill
    dir: str           # "Open Long", "Close Short", "Long > Short", "Liquidated ...", "Buy"/"Sell" (spot) ...
    closed_pnl: float
    fee: float
    crossed: bool      # True = taker, False = maker
    oid: int
    tid: int
    hash: str

    @property
    def signed_sz(self) -> float:
        return self.sz if self.side == "B" else -self.sz

    @property
    def end_pos(self) -> float:
        return self.start_pos + self.signed_sz

    @property
    def notional(self) -> float:
        return self.px * self.sz


def parse_fill(d: dict) -> Fill:
    return Fill(coin=d["coin"], px=float(d["px"]), sz=float(d["sz"]), side=d["side"], time=int(d["time"]),
                start_pos=float(d["startPosition"]), dir=d.get("dir", ""), closed_pnl=float(d.get("closedPnl", 0)),
                fee=float(d.get("fee", 0)), crossed=bool(d.get("crossed", True)), oid=int(d.get("oid", 0)),
                tid=int(d["tid"]), hash=d.get("hash", ""))


def parse_fills(lst: list) -> list[Fill]:
    return [parse_fill(d) for d in lst]


@dataclass(frozen=True)
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


def parse_candles(lst: list) -> list[Candle]:
    return [Candle(int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"]))
            for c in lst]


@dataclass(frozen=True)
class Book:
    coin: str
    time: int
    bids: list[tuple[float, float]]   # best first
    asks: list[tuple[float, float]]

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2


def parse_book(obj: dict) -> Book:
    if not obj or "levels" not in obj:
        raise ValueError("empty book")
    bids = [(float(x["px"]), float(x["sz"])) for x in obj["levels"][0]]
    asks = [(float(x["px"]), float(x["sz"])) for x in obj["levels"][1]]
    if not bids or not asks:
        raise ValueError("one-sided book")
    return Book(obj["coin"], int(obj["time"]), bids, asks)


def parse_positions(obj: dict) -> dict[str, float]:
    """clearinghouseState -> {coin: signed size}."""
    return {ap["position"]["coin"]: float(ap["position"]["szi"]) for ap in obj.get("assetPositions", [])
            if float(ap["position"]["szi"]) != 0}


@dataclass(frozen=True)
class OpenPos:
    coin: str
    szi: float         # signed size
    value: float       # position value (USD)
    upnl: float        # unrealized pnl (USD)


@dataclass(frozen=True)
class Account:
    value: float                 # perp account value (margin summary)
    positions: tuple = ()        # OpenPos, non-zero only


STABLES = ("USDC", "USDT", "USDT0", "USDH", "USDE")


def parse_spot_usd(obj: dict) -> float:
    """spotClearinghouseState -> the stablecoins in the spot wallet (USD). On Hyperliquid spot USDC also backs perp
    positions (its `hold` part is margin), so it is part of what a trader trades with (found 2026-10-10: a wallet with
    1,481$ in perps and 50,626$ USDC in spot looked like it had lost 30% when only the perp value was counted)."""
    return sum(float(b.get("total") or 0) for b in (obj or {}).get("balances", []) if b.get("coin") in STABLES)


def parse_account(obj: dict) -> Account:
    """clearinghouseState -> perp account value and open positions with their unrealized pnl."""
    ps = tuple(OpenPos(p["coin"], float(p["szi"]), float(p.get("positionValue") or 0),
                       float(p.get("unrealizedPnl") or 0))
               for p in (ap["position"] for ap in obj.get("assetPositions", [])) if float(p["szi"]) != 0)
    return Account(float((obj.get("marginSummary") or {}).get("accountValue") or 0), ps)


@dataclass(frozen=True)
class Asset:
    name: str
    sz_decimals: int
    max_leverage: float
    funding: float     # hourly rate
    mark: float
    delisted: bool


def parse_meta(obj: list) -> dict[str, Asset]:
    meta, ctxs = obj
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        out[u["name"]] = Asset(u["name"], int(u["szDecimals"]), float(u["maxLeverage"]),
                               float(c.get("funding") or 0), float(c.get("markPx") or 0), bool(u.get("isDelisted")))
    return out


@dataclass(frozen=True)
class WsEvent:
    kind: str                 # "fills" | "mids" | "pong" | "error" | "sub" | "other"
    user: str = ""
    fills: tuple = ()
    snapshot: bool = False
    mids: dict | None = None
    text: str = ""


def parse_ws(msg: dict) -> WsEvent:
    ch = msg.get("channel")
    d = msg.get("data")
    if ch == "userFills":
        return WsEvent("fills", user=d["user"].lower(), fills=tuple(parse_fills(d.get("fills", []))),
                       snapshot=bool(d.get("isSnapshot")))
    if ch == "allMids":
        return WsEvent("mids", mids={k: float(v) for k, v in d["mids"].items()})
    if ch == "pong":
        return WsEvent("pong")
    if ch == "error":
        return WsEvent("error", text=str(d))
    if ch == "subscriptionResponse":
        return WsEvent("sub", text=json.dumps(d))
    return WsEvent("other", text=str(ch))


# ---------------------------------------------------------------------------------------------
# rate budget and REST client
# ---------------------------------------------------------------------------------------------
CRITICAL, BULK = "critical", "bulk"

BASE_WEIGHT = {"l2Book": 2, "allMids": 2, "clearinghouseState": 2, "orderStatus": 2, "spotClearinghouseState": 2,
               "exchangeStatus": 2, "userRole": 60}


def request_weight(body: dict) -> int:
    return BASE_WEIGHT.get(body.get("type", ""), 20)


def extra_weight(body: dict, result) -> int:
    t = body.get("type")
    if t in ("userFillsByTime", "userFills", "userFunding", "userNonFundingLedgerUpdates", "historicalOrders"):
        return len(result) // 20 if isinstance(result, list) else 0
    if t == "candleSnapshot":
        return len(result) // 60 if isinstance(result, list) else 0
    return 0


class RateBudget:
    """Token bucket over request weight. BULK work (scoring, backfill) may never dip into the critical
    reserve, so exits/stops/books always have budget. A 429 pauses BULK for a while and CRITICAL briefly."""

    def __init__(self, per_min: int, critical_reserve: int, clock=time.monotonic):
        self.cap = float(per_min)
        self.rate = per_min / 60.0
        self.reserve = float(critical_reserve)
        self.tokens = float(per_min)
        self.clock = clock
        self.t = clock()
        self.blocked_until = {CRITICAL: 0.0, BULK: 0.0}
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)

    def _refill(self):
        now = self.clock()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now

    def try_take(self, w: float, cls: str) -> bool:
        with self.lock:
            self._refill()
            if self.clock() < self.blocked_until[cls]:
                return False
            floor = self.reserve if cls == BULK else 0.0
            if self.tokens - w >= floor:
                self.tokens -= w
                return True
            return False

    def take(self, w: float, cls: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.try_take(w, cls):
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.05 if cls == CRITICAL else 0.5, left))

    def charge(self, w: float) -> None:
        with self.lock:
            self._refill()
            self.tokens -= w  # may go negative: we then wait for the refill

    def on_429(self) -> None:
        with self.lock:
            now = self.clock()
            self.tokens = min(self.tokens, 0.0)
            self.blocked_until[BULK] = max(self.blocked_until[BULK], now + 60.0)
            self.blocked_until[CRITICAL] = max(self.blocked_until[CRITICAL], now + 2.0)


class HttpError(Exception):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class Info:
    """POST /info with weight accounting and a HARD per-call timeout (the caller never waits longer)."""

    def __init__(self, url: str, budget: RateBudget, pool_size: int = 8):
        self.url = url
        self.budget = budget
        self.pool = cf.ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="http")

    def _do(self, body: dict, timeout: float):
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, e.read()[:200].decode(errors="replace")) from None

    def post(self, body: dict, cls: str = BULK, timeout: float = 10.0):
        t0 = time.monotonic()
        if not self.budget.take(request_weight(body), cls, timeout):
            raise TimeoutError(f"rate budget ({cls}) exhausted for {body.get('type')}")
        left = max(0.05, timeout - (time.monotonic() - t0))
        fut = self.pool.submit(self._do, body, left)
        try:
            res = fut.result(timeout=left)
        except cf.TimeoutError:
            raise TimeoutError(f"{body.get('type')} took longer than {timeout}s") from None
        except HttpError as e:
            if e.status == 429:
                self.budget.on_429()
                log.warn("http_429", type=body.get("type"), cls=cls)
            raise
        self.budget.charge(extra_weight(body, res))
        return res

    def timed_post(self, body: dict, cls: str, timeout: float):
        """Returns (result, local send ms, local receive ms) - for clock offset estimates."""
        t0 = time.time() * 1000
        res = self.post(body, cls, timeout)
        return res, t0, time.time() * 1000

    # ---- convenience --------------------------------------------------------------------------
    def book(self, coin: str, timeout: float) -> Book:
        return parse_book(self.post({"type": "l2Book", "coin": coin}, CRITICAL, timeout))

    def positions(self, user: str, timeout: float) -> dict[str, float]:
        return parse_positions(self.post({"type": "clearinghouseState", "user": user}, CRITICAL, timeout))

    def account(self, user: str, timeout: float = 20.0) -> Account:
        """A leader's live account for scoring (BULK: never competes with exits)."""
        return parse_account(self.post({"type": "clearinghouseState", "user": user}, BULK, timeout))

    def spot_usd(self, user: str, timeout: float = 20.0) -> float:
        """Stablecoins in the user's spot wallet (BULK)."""
        return parse_spot_usd(self.post({"type": "spotClearinghouseState", "user": user}, BULK, timeout))

    def meta(self, cls: str = CRITICAL, timeout: float = 5.0) -> dict[str, Asset]:
        return parse_meta(self.post({"type": "metaAndAssetCtxs"}, cls, timeout))

    def fills_page(self, user: str, start_ms: int, end_ms: int | None = None, timeout: float = 20.0) -> list[Fill]:
        body = {"type": "userFillsByTime", "user": user, "startTime": int(start_ms)}
        if end_ms:
            body["endTime"] = int(end_ms)
        return parse_fills(self.post(body, BULK, timeout))

    def candles(self, coin: str, start_ms: int, end_ms: int, interval: str = "1h", timeout: float = 20.0) -> list[Candle]:
        return parse_candles(self.post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval, "startTime": int(start_ms), "endTime": int(end_ms)}}, BULK, timeout))


def get_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


FILLS_PAGE = 2000


def fetch_fills_history(info: Info, user: str, start_ms: int, end_ms: int, prior: list[Fill] | None = None,
                        max_pages: int = 6) -> tuple[list[Fill], bool]:
    """Page forward from start_ms. Inclusive start time -> dedupe by tid. Returns (fills, complete).

    Only the latest 10,000 fills of a wallet are retrievable: max_pages=6 covers that and stops."""
    by_tid = {f.tid: f for f in (prior or [])}
    cursor = max([f.time for f in prior], default=start_ms) if prior else start_ms
    complete = False
    for _ in range(max_pages):
        page = info.fills_page(user, cursor, end_ms)
        new = [f for f in page if f.tid not in by_tid]
        for f in page:
            by_tid[f.tid] = f
        if len(page) < FILLS_PAGE:
            complete = True
            break
        last = page[-1].time
        if last <= cursor and not new:
            # 2000 fills in the same millisecond: cannot page further safely
            break
        cursor = last
    fills = sorted(by_tid.values(), key=lambda f: (f.time, f.tid))
    return fills, complete


def chunk_ranges(start_ms: int, end_ms: int, chunk_ms: int) -> list[tuple[int, int]]:
    """Aligned chunks so closed chunks are cacheable forever."""
    out = []
    s = (start_ms // chunk_ms) * chunk_ms
    while s < end_ms:
        out.append((s, s + chunk_ms - 1))
        s += chunk_ms
    return out


def round_size(sz: float, decimals: int) -> float:
    q = 10 ** decimals
    return math.floor(sz * q + 1e-9) / q
