"""FOMO (fomo.family) data: leaderboard rows, per-trader swaps and the HTTP client.

Verified against real recorded responses (tests/fixtures/fomo_*.json):
  GET {api}/v2/leaderboard/{24h|7d|30d}      -> {"responseObject": {"leaderboard": [row, ...]}}  (150 rows)
  GET {api}/v2/users/{id}/swaps?limit=N      -> {"responseObject": {"swaps": [...], "hasNextPage": bool}}
The swaps are newest first, 25 per page by default; `limit` is the only paging parameter that works (page,
offset, skip, cursor, before, beforeTime and pageSize are all ignored by the server).
Both calls need the owner's logged-in session: the Cookie header comes from the FOMO_COOKIE environment
variable and is never logged. A 401/403 raises AuthError so the bot can alert and stop adding wallets.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL = "So11111111111111111111111111111111111111112"
SOL_NATIVE = "11111111111111111111111111111111"
QUOTES = frozenset({USDC, USDT, WSOL, SOL_NATIVE})


def read_cookie_file(path: str) -> str:
    """The cookie as one line. Windows tools save text in several encodings (UTF-8 with or without BOM, UTF-16 from
    PowerShell's '>'); all are understood. A leading 'cookie:' label and line breaks are dropped."""
    with open(path, "rb") as f:
        raw = f.read()
    utf16 = raw[:2] in (bytes([0xFF, 0xFE]), bytes([0xFE, 0xFF])) or (len(raw) > 1 and raw[1] == 0)
    text = raw.decode("utf-16" if utf16 else "utf-8-sig", errors="ignore")
    text = " ".join(text.replace(chr(0), "").split())
    return text[7:].strip() if text.lower().startswith("cookie:") else text


class AuthError(Exception):
    """FOMO refused our session (expired or missing cookie)."""


class FomoError(Exception):
    def __init__(self, status: int, msg: str = ""):
        super().__init__(f"fomo http {status} {msg}".strip())
        self.status = status


@dataclass
class LbRow:
    uid: str
    address: str
    handle: str
    followers: int
    swap_count: int
    num_trades: int
    volume: float
    pnl: float                     # pnl of the requested window
    window: str
    created_ms: int
    private: bool
    restricted: bool
    holdings_usd: float            # value of the top holdings shown on the row
    holdings_pnl: float            # unrealised pnl of those holdings (sum of the positive parts)
    total_holdings: int


@dataclass(frozen=True)
class Leg:
    """One side of a swap against a quote asset: a buy or a sell of `token`."""
    id: str
    ts: int                        # ms
    token: str
    side: str                      # "buy" | "sell"
    amount: float                  # tokens
    usd: float                     # USD value paid (buy) or received (sell)

    @property
    def px(self) -> float:
        return self.usd / self.amount if self.amount > 0 else 0.0


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_leaderboard(window: str, body: dict) -> list[LbRow]:
    rows = ((body or {}).get("responseObject") or {}).get("leaderboard") or []
    out = []
    for r in rows:
        if not r.get("id") or not r.get("address"):
            continue
        key = f"pnl{window}"
        pnl = r.get(key)
        if pnl is None:   # tolerate a differently named pnl field instead of silently using 0
            alt = [k for k in r if k.startswith("pnl") and isinstance(r[k], (int, float))]
            pnl = r[alt[0]] if alt else 0.0
        hold = r.get("topHoldings") or []
        out.append(LbRow(
            uid=r["id"], address=r["address"], handle=r.get("userHandle") or r.get("displayName") or "",
            followers=int(r.get("followers") or 0), swap_count=int(r.get("swapCount") or 0),
            num_trades=int(r.get("numTrades") or 0), volume=_f(r.get("totalVolume")), pnl=_f(pnl), window=window,
            created_ms=_ms(r["createdAt"]) if r.get("createdAt") else 0, private=bool(r.get("private")),
            restricted=bool(r.get("isRestricted")),
            holdings_usd=sum(_f(h.get("value")) for h in hold),
            holdings_pnl=sum(max(0.0, _f(h.get("pnl"))) for h in hold),
            total_holdings=int(r.get("totalHoldings") or 0)))
    return out


def parse_swaps(body: dict) -> tuple[list[Leg], bool]:
    """-> (legs sorted oldest first, hasNextPage). A swap between two non-quote tokens becomes a sell leg and
    a buy leg. Swaps with a zero amount or no USD value are ignored (they cannot be priced)."""
    ro = (body or {}).get("responseObject") or {}
    legs: list[Leg] = []
    for s in ro.get("swaps") or []:
        try:
            ts = _ms(s["createdAt"])
            tin, tout = s["inTokenAddress"], s["outTokenAddress"]
            ain, aout = _f(s.get("inHumanAmount")), _f(s.get("outHumanAmount"))
            uin, uout = _f(s.get("humanUsdAmountIn")), _f(s.get("humanUsdAmountOut"))
            sid = s["id"]
        except (KeyError, ValueError):
            continue
        if tin in QUOTES and tout not in QUOTES:
            usd = uin or uout
            if aout > 0 and usd > 0:
                legs.append(Leg(sid, ts, tout, "buy", aout, usd))
        elif tout in QUOTES and tin not in QUOTES:
            usd = uout or uin
            if ain > 0 and usd > 0:
                legs.append(Leg(sid, ts, tin, "sell", ain, usd))
        elif tin not in QUOTES and tout not in QUOTES:
            usd = uout or uin
            if ain > 0 and aout > 0 and usd > 0:
                legs.append(Leg(sid + ":s", ts, tin, "sell", ain, usd))
                legs.append(Leg(sid + ":b", ts, tout, "buy", aout, usd))
    legs.sort(key=lambda g: (g.ts, g.id))
    return legs, bool(ro.get("hasNextPage"))


class FomoClient:
    """Thin, throttled GET client. Never logs the cookie."""

    def __init__(self, base: str, cookie: str, min_interval_s: float = 0.3, timeout_s: float = 20.0,
                 cookie_file: str = ""):
        """`cookie_file` (FOMO_COOKIE_FILE) is re-read whenever it changes, so an expired session can be
        refreshed by saving the new cookie into the file: no restart needed. It wins over `cookie`."""
        self.base, self._cookie = base.rstrip("/"), cookie
        self._file, self._file_mtime = cookie_file, 0.0
        self.min_interval, self.timeout = min_interval_s, timeout_s
        self._lock = threading.Lock()
        self._last = 0.0
        self.blocked_until = 0.0

    def _current_cookie(self) -> str:
        if self._file:
            try:
                m = os.stat(self._file).st_mtime
                if m != self._file_mtime:
                    self._cookie = read_cookie_file(self._file)
                    self._file_mtime = m
            except OSError:
                pass       # keep the last cookie we had (an unreadable file must not break a running bot)
        return self._cookie

    @property
    def usable(self) -> bool:
        return bool(self._current_cookie())

    def get(self, path: str, timeout: float | None = None) -> dict:
        cookie = self._current_cookie()
        if not cookie:
            raise AuthError("no FOMO_COOKIE")
        with self._lock:
            wait = max(self._last + self.min_interval, self.blocked_until) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        req = urllib.request.Request(self.base + path, headers={
            "Cookie": cookie, "Accept": "application/json", "Origin": "https://fomo.family",
            "Referer": "https://fomo.family/", "User-Agent": "Mozilla/5.0 (copybot paper)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"fomo refused the session (http {e.code})") from None
            if e.code == 431:
                raise AuthError("the FOMO cookie is too big (http 431): run tools/fomo_cookie_trim.py") from None
            if e.code == 429:
                self.blocked_until = time.monotonic() + 30
            raise FomoError(e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FomoError(0, f"network: {e}") from None

    # ---- endpoints ----------------------------------------------------------------------------
    def leaderboard(self, window: str) -> list[LbRow]:
        return parse_leaderboard(window, self.get(f"/v2/leaderboard/{window}"))

    def swaps(self, uid: str, limit: int = 25) -> tuple[list[Leg], bool]:
        return parse_swaps(self.get(f"/v2/users/{uid}/swaps?limit={limit}"))


@dataclass
class History:
    legs: list[Leg] = field(default_factory=list)   # oldest first, unique ids
    complete: bool = False                          # reaches back to `since` (or the account's first swap)


def fetch_history(client: FomoClient, uid: str, since_ms: int, max_limit: int = 3000) -> History:
    """Raise `limit` until the page reaches `since_ms` or the account's first swap (hasNextPage is false).
    A server refusal of a large limit (HTTP 400) keeps what we have and reports the history incomplete."""
    limit, best = 200, History()
    while True:
        try:
            legs, more = client.swaps(uid, limit)
        except FomoError as e:
            if e.status == 400 and best.legs:
                return best
            raise
        best = History(legs, False)
        reached = bool(legs) and legs[0].ts <= since_ms
        if not more or reached:
            best.complete = True
            break
        if limit >= max_limit:
            break
        limit = min(limit * 3, max_limit)
    best.legs = [g for g in best.legs if g.ts >= since_ms]
    return best
