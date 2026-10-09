"""Invo (Involio) posted trade calls, copied on paper by the "invo calls" side wallet (owner request 2026-10-09).

Invo is a social trading app: traders post "calls" (coin, long/short, leverage, entry, take-profit, stop-loss, size as a
share of their portfolio) in portfolios that are PAPER portfolios (`"type": "paper"`, verified by Invo against market
prices, not real fills: an Invo BTC entry of $82,951.85 cannot be a Hyperliquid fill). Invo shows no Hyperliquid
address for its traders, so the calls themselves are the signal. The bot reads them with a dedicated (mock) Invo
account and paper-trades them at live Hyperliquid prices.

API (recorded from the web app 2026-10-09, tests/fixtures/invo_*.json), all POST with a JSON body:
  /v1_0/users/get_user                   {"userId": null, "usersUsername": "<name>"}  -> {"user": {"id", "username"..}}
  /v1_0/portfolios/v2/get_users_portfolios  {"userId": id, "params": {"isDeleted": false, "page": 1, "size": 20}}
                                         -> {"portfolios": [{"id", "title", "active", "type", "openTrades":
                                             {"count", "assets"}, "winRate", "closedPositions", ...}]}
  /v1_0/investments/get_investments      {"portfolioId": id, "isOpen": true|false, "params": {"page", "size"}}
                                         -> {"investmentsTicker": [{"id", "ticker", "directionLong", "leverage",
                                             "entryPrice", "priceTarget", "stopLoss", "positionSize", "isOpen",
                                             "createdAt", "closedAt", "closingPrice", "reasonClosed", ...}]}
Auth (from the app's code): requests carry "Authorization: Bearer <access token>"; GET /v1_0/auth/refresh_token with
"Authorization: Bearer <refresh token>" answers {"accessToken", "refreshToken", "success", "error"}. Every refresh
hands out a NEW refresh token (written back to INVO_TOKEN_FILE), which is why the bot needs its own account: two
programs sharing one login would keep logging each other out.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from copybot import log

PREFIX = "invo:"          # leader name of an Invo trader in the ledger: "invo:<username>"


class InvoAuthError(Exception):
    """The Invo login is gone (refresh token rejected or missing)."""


class InvoError(Exception):
    def __init__(self, status: int, msg: str = ""):
        super().__init__(f"invo http {status} {msg}".strip())
        self.status = status


@dataclass(frozen=True)
class Portfolio:
    id: str
    title: str
    active: bool
    kind: str               # "paper" for every portfolio seen so far
    open_count: int
    open_assets: tuple
    win_rate: float         # percent, Invo's own number
    closed: int


@dataclass(frozen=True)
class Call:
    id: str
    portfolio_id: str
    owner: str              # username
    ticker: str
    long: bool
    leverage: float
    size: float             # positionSize: share of the trader's portfolio (0.05 = 5%)
    entry: float
    target: float | None
    stop: float | None
    created_ms: int
    is_open: bool
    closed_ms: int | None
    closing_price: float | None
    reason_closed: str | None

    @property
    def exposure(self) -> float:
        """The trader's exposure as a share of its portfolio (size x leverage)."""
        return self.size * max(self.leverage, 1.0)


def _ms(iso) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)


def _f(x) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def parse_user(body: dict) -> tuple[str, str] | None:
    u = (body or {}).get("user") or {}
    return (u["id"], u.get("username", "")) if u.get("id") else None


def parse_portfolios(body: dict) -> list[Portfolio]:
    out = []
    for p in (body or {}).get("portfolios") or []:
        ot = p.get("openTrades") or {}
        out.append(Portfolio(p["id"], p.get("title", ""), bool(p.get("active")), p.get("type") or "",
                             int(ot.get("count") or 0), tuple(ot.get("assets") or ()), float(p.get("winRate") or 0),
                             int(p.get("closedPositions") or 0)))
    return out


def parse_calls(body: dict) -> list[Call]:
    """Crypto ticker calls only (Invo also has business/property/material investments: ignored)."""
    out = []
    for t in (body or {}).get("investmentsTicker") or []:
        if t.get("assetTypeId") not in (None, "crypto") or not t.get("ticker") or not t.get("id"):
            continue
        entry = _f(t.get("entryPrice"))
        created = _ms(t.get("createdAt"))
        if not entry or entry <= 0 or created is None:
            continue
        out.append(Call(t["id"], (t.get("portfolio") or {}).get("id", ""), (t.get("owner") or {}).get("username", ""),
                        str(t["ticker"]).upper(), bool(t.get("directionLong")), float(t.get("leverage") or 1),
                        float(t.get("positionSize") or 0), entry, _f(t.get("priceTarget")), _f(t.get("stopLoss")),
                        created, bool(t.get("isOpen")), _ms(t.get("closedAt")), _f(t.get("closingPrice")),
                        t.get("reasonClosed")))
    return out


class InvoClient:
    """Throttled JSON POST client. The refresh token lives in `token_file` (INVO_TOKEN_FILE) and is replaced there on
    every refresh; tokens are never logged (each new one is registered with the log redactor)."""

    def __init__(self, base: str, token_file: str, min_interval_s: float = 1.0, timeout_s: float = 20.0):
        self.base, self.token_file = base.rstrip("/"), token_file
        self.min_interval, self.timeout = min_interval_s, timeout_s
        self._lock = threading.Lock()
        self._last = 0.0
        self._access = ""

    # ---- tokens ------------------------------------------------------------------------------------
    def _refresh_token(self) -> str:
        try:
            raw = Path(self.token_file).read_bytes()
        except OSError:
            raise InvoAuthError("INVO_TOKEN_FILE not readable") from None
        utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
        tok = raw.decode("utf-16" if utf16 else "utf-8-sig", errors="ignore").strip().strip('"')
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        if not tok:
            raise InvoAuthError("INVO_TOKEN_FILE is empty")
        log.add_secret(tok)
        return tok

    def refresh(self) -> None:
        # GET (verified 2026-10-09: POST answers 405 Method Not Allowed)
        req = urllib.request.Request(self.base + "/auth/refresh_token", method="GET",
                                     headers={"Authorization": "Bearer " + self._refresh_token(),
                                              "User-Agent": "copybot-paper/1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise InvoAuthError(f"refresh refused (http {e.code})") from None
            raise InvoError(e.code, "refresh") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise InvoError(0, f"network: {type(e).__name__}") from None
        access, new_refresh = body.get("accessToken"), body.get("refreshToken")
        if not access:
            raise InvoAuthError("refresh gave no access token")
        log.add_secret(access)
        self._access = access
        if new_refresh:
            log.add_secret(new_refresh)
            tmp = Path(self.token_file).with_suffix(".tmp")
            tmp.write_text(new_refresh, encoding="utf-8")
            os.replace(tmp, self.token_file)        # the old refresh token is spent: keep only the new one

    # ---- requests ------------------------------------------------------------------------------------
    def post(self, path: str, body: dict) -> dict:
        for attempt in range(2):
            if not self._access:
                self.refresh()
            with self._lock:
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            req = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                         {"Authorization": "Bearer " + self._access,
                                          "Content-Type": "application/json", "User-Agent": "copybot-paper/1"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._access = ""                   # expired: refresh once and retry
                    continue
                if e.code in (401, 403):
                    raise InvoAuthError(f"invo refused the login (http {e.code})") from None
                raise InvoError(e.code, path) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise InvoError(0, f"network: {type(e).__name__}") from None
        raise InvoAuthError("invo refused the login")

    def user(self, username: str) -> tuple[str, str] | None:
        return parse_user(self.post("/users/get_user", {"userId": None, "usersUsername": username}))

    def portfolios(self, user_id: str) -> list[Portfolio]:
        return parse_portfolios(self.post("/portfolios/v2/get_users_portfolios",
                                          {"userId": user_id, "params": {"isDeleted": False, "page": 1, "size": 20}}))

    def open_calls(self, portfolio_id: str) -> list[Call]:
        return parse_calls(self.post("/investments/get_investments",
                                     {"portfolioId": portfolio_id, "isOpen": True, "params": {"page": 1, "size": 50}}))


class Watcher:
    """Polls the followed Invo traders and turns their calls into events for the trading loop:
    ("invo_open", username, Call) for a call that appeared since the last poll and is fresh, and
    ("invo_close", username, Call) for one that disappeared. The first poll of a trader is only a baseline: calls
    already open when we start watching are never copied (we would enter late). One portfolio-list request per
    trader per poll; open calls are only read for portfolios whose open-trade list changed."""

    def __init__(self, client: InvoClient, out, traders, poll_s: float, max_age_s: float, stop: threading.Event,
                 now=lambda: int(time.time() * 1000)):
        self.client, self.out, self.traders, self.poll_s, self.max_age_s = client, out, traders, poll_s, max_age_s
        self.stop, self.now = stop, now
        self.uids: dict[str, str] = {}
        self.known: dict[str, dict[str, Call]] = {}        # username -> open calls by id (after the baseline)
        self.sig: dict[str, tuple] = {}                    # portfolio id -> (open count, assets) last seen
        self.calls: dict[str, dict[str, Call]] = {}        # portfolio id -> its open calls last read
        self.auth_ok = True
        self.last_ok = 0.0
        self.unknown: set[str] = set()
        self.stats: dict[str, list[Portfolio]] = {}       # username -> its active portfolios (for /invotraders)

    def start(self) -> None:
        threading.Thread(target=self.run, name="invo", daemon=True).start()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                for name in sorted(self.traders()):
                    self.poll(name)
                self.last_ok = time.time()
                if not self.auth_ok:
                    self.auth_ok = True
                    self.out.put(("invo_auth", True, ""))
            except InvoAuthError as e:
                if self.auth_ok:
                    self.out.put(("invo_auth", False, str(e)))
                self.auth_ok = False
                self.stop.wait(300)
            except Exception as e:
                log.warn("invo_poll_failed", err=f"{type(e).__name__}: {e}"[:200])
            self.stop.wait(self.poll_s)

    def poll(self, name: str) -> None:
        uid = self.uids.get(name)
        if uid is None:
            found = self.client.user(name)
            if not found:
                log.warn("invo_unknown_trader", trader=name)
                if name not in self.unknown:
                    self.unknown.add(name)
                    self.out.put(("invo_unknown", name))
                return
            uid = self.uids[name] = found[0]
        now_open: dict[str, Call] = {}
        ports = self.client.portfolios(uid)
        self.stats[name] = [p for p in ports if p.active]
        for p in ports:
            if not p.active:
                continue
            sig = (p.open_count, tuple(sorted(p.open_assets)))
            if p.open_count and (self.sig.get(p.id) != sig or p.id not in self.calls):
                self.calls[p.id] = {c.id: c for c in self.client.open_calls(p.id) if c.is_open}
            elif not p.open_count:
                self.calls[p.id] = {}
            self.sig[p.id] = sig
            now_open.update(self.calls.get(p.id, {}))
        before = self.known.get(name)
        self.known[name] = now_open
        if before is None:                                  # baseline: never copy calls opened before we watched
            log.info("invo_baseline", trader=name, open_calls=len(now_open))
            return
        for cid, c in now_open.items():
            if cid not in before:
                age_s = (self.now() - c.created_ms) / 1000
                if age_s <= self.max_age_s:
                    self.out.put(("invo_open", name, c))
                else:
                    log.info("invo_call_too_old", trader=name, ticker=c.ticker, age_s=round(age_s))
        for cid, c in before.items():
            if cid not in now_open:
                self.out.put(("invo_close", name, c))

    def forget(self, name: str) -> None:
        self.known.pop(name, None)
