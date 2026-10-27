"""Invo (Involio) posted trade calls, copied on paper by the Invo wallets (owner request 2026-10-09).

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
Real-money portfolios (read 2026-10-12 from the app's code and answers; base WITHOUT /v1_0):
  "live" portfolios trade real money on Hyperliquid through Invo; /v1_0 shows them empty (0 calls, 0% win), their
  trades are only in (GET, read only; the other /dex/ endpoints place trades and are never called):
  GET /dex/portfolio/live/<id>/overview  -> {"data": {"accuracyPct", "allTimePnl": {"pct"}, "closedCount",
                                         "openTrades": {"count", "assets"}, "equity"}}
  GET /dex/portfolio/position/<id>/list?isOpen=true|false&limit=&cursor= -> {"data": {"positions": [{"id",
                                         "assetSymbol", "side", "leverage", "entryPx", "exitPx", "size": {"pct" (% of
                                         equity), "coin"}, "openedAt", "closedAt", "closeReason", "takeProfitPx",
                                         "stopLossPx"}], "nextCursor"}}
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
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from copybot import log

PREFIX = "invo:"          # leader name of an Invo trader in the ledger: "invo:<username>"
RESIZE_MIN = 0.02         # a committed size change smaller than 2% is noise, not an add or a trim
SLOW_MAX_S = 3.0          # Invo answered 429 (too many requests): spacing doubles up to this ...
CALM_S = 60.0             # ... and eases back (x0.75 per CALM_S without a 429) to invo.request_gap_s
DELETED = "call deleted by the trader"   # reason_closed of a call that vanished without showing in the closed list
VANISH_S = 10.0           # a call missing from our reads but not in the trader's closed list is only treated as closed
                          # (deleted) after this long: one incomplete answer must not close a copy (audit 2026-10-11)
RECHECK_S = 60.0          # open calls of a portfolio holding calls are re-read at least this often even if Invo's
                          # updatedAt did not move, so no add or trim is missed (owner request 2026-10-13)
REPLACED_S = 120.0        # a new call with the same portfolio, coin and side created within this of a vanished one
                          # is that call under a new id (Invo re-ids an edited call, live 2026-10-13)
VANISH_UNREAD_S = 3600.0  # ... and after this long when its portfolio was not fully read (missing from the list,
                          # inactive, fewer calls listed than counted): a deleted portfolio still closes its copies
PAGES_MAX = 10            # pages read of one list (portfolios 20 a page, open calls 50 a page)
REREAD_MAX = 5            # an open-call list shorter than the portfolio's count is read again this many rounds
OVERVIEW_S = 300.0        # a live portfolio's numbers (accuracy, return) are re-read this often; its positions every round


class InvoAuthError(Exception):
    """The Invo login is gone (refresh token rejected or missing)."""


UNREAD = object()          # Watcher.closed_call: Invo did not answer


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
    updated_ms: int = 0     # changes when the trader edits a call (add, trim, target...)
    pnl_pct: float = 0.0    # plSnapshot: the portfolio's % return (a $100 portfolio now worth $130 shows 28-30)


@dataclass(frozen=True)
class Call:
    id: str
    portfolio_id: str
    owner: str              # username
    ticker: str
    long: bool
    leverage: float
    size: float             # positionSize: share of the portfolio NOW (moves with the price)
    entry: float
    target: float | None
    stop: float | None
    created_ms: int
    is_open: bool
    closed_ms: int | None
    closing_price: float | None
    reason_closed: str | None
    entry_size: float = 0.0  # entrySize / 100: the share of the portfolio the trader COMMITTED (fixed unless it adds
                             # or trims; positionSize drifts with the price, e.g. PUMP 4.3% -> 3.2% as it fell)
    added: bool = False      # changes.isAdded
    units: float = 0.0       # live portfolios: the position's size in coins (its % of equity drifts with the price)
    live: bool = False       # from a real-money portfolio (sized with invo.live_size_mult)

    @property
    def committed(self) -> float:
        return self.entry_size if self.entry_size > 0 else self.size

    @property
    def exposure(self) -> float:
        """The trader's committed exposure as a share of its portfolio (committed size x leverage)."""
        return self.committed * max(self.leverage, 1.0)


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
                             int(p.get("closedPositions") or 0), _ms(p.get("updatedAt")) or 0,
                             float(p.get("plSnapshot") or 0)))
    return out


def parse_live_positions(body: dict, portfolio_id: str, owner: str = "") -> tuple[list[Call], str | None]:
    """A live (real-money) portfolio's positions as Calls (size.pct is a % of the trader's equity) + the next cursor."""
    d = (body or {}).get("data") or {}
    out = []
    for p in d.get("positions") or []:
        entry, created = _f(p.get("entryPx")), _ms(p.get("openedAt"))
        if not p.get("id") or not p.get("assetSymbol") or not entry or entry <= 0 or created is None:
            continue
        size = p.get("size") or {}
        share = (_f(size.get("pct")) or 0.0) / 100
        out.append(Call(p["id"], portfolio_id, owner, str(p["assetSymbol"]).upper(), p.get("side") == "long",
                        float(p.get("leverage") or 1), share, entry, _f(p.get("takeProfitPx")),
                        _f(p.get("stopLossPx")), created, bool(p.get("isOpen")), _ms(p.get("closedAt")),
                        _f(p.get("exitPx")), p.get("closeReason"), share, False, _f(size.get("coin")) or 0.0,
                        live=True))
    return out, d.get("nextCursor") or None


def parse_live_overview(body: dict, p: Portfolio) -> Portfolio:
    """A live portfolio with its real numbers (accuracy, % return, closed trades, open trades) instead of /v1_0's zeros."""
    d = (body or {}).get("data") or {}
    ot = d.get("openTrades") or {}
    return replace(p, win_rate=_f(d.get("accuracyPct")) or 0.0, closed=int(d.get("closedCount") or 0),
                   pnl_pct=_f((d.get("allTimePnl") or {}).get("pct")) or 0.0, open_count=int(ot.get("count") or 0),
                   open_assets=tuple(ot.get("assets") or ()))


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
                        t.get("reasonClosed"), (_f(t.get("entrySize")) or 0.0) / 100,
                        bool((t.get("changes") or {}).get("isAdded"))))
    return out


class InvoClient:
    """Throttled JSON POST client. The refresh token lives in `token_file` (INVO_TOKEN_FILE) and is replaced there on
    every refresh; tokens are never logged (each new one is registered with the log redactor)."""

    def __init__(self, base: str, token_file: str, min_interval_s: float = 1.0, timeout_s: float = 20.0):
        self.base, self.token_file = base.rstrip("/"), token_file
        # the score and live-portfolio endpoints live at the API root, without /v1_0
        self.root = self.base[: -len("/v1_0")] if self.base.endswith("/v1_0") else self.base
        self.min_interval, self.timeout = min_interval_s, timeout_s
        self._lock = threading.Lock()
        self._auth_lock = threading.Lock()     # the watcher and the search share one login: one refresh at a time
        self._last = 0.0
        self.slow = 0.0                        # extra spacing while Invo says "too many requests" (0 = normal)
        self.last_429 = 0.0                    # monotonic time of Invo's last 429
        self._access = ""
        self._unsaved = ""                     # a rotated refresh token we could not write to the file yet
        self.save_failed = False               # the watcher reports it (the Invo book goes dark at the next restart)

    # ---- tokens ------------------------------------------------------------------------------------
    def _refresh_token(self) -> str:
        if self._unsaved:
            return self._unsaved
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
            self._unsaved = new_refresh             # the old refresh token is spent: keep only the new one
            self.save_token()

    def save_token(self) -> bool:
        """Write the rotated refresh token to INVO_TOKEN_FILE (retried by the watcher every round while it fails: a
        locked file must not lose the only valid login, audit 2026-10-11)."""
        if not self._unsaved:
            return True
        try:
            tmp = Path(self.token_file).with_suffix(".tmp")
            tmp.write_text(self._unsaved, encoding="utf-8")
            os.replace(tmp, self.token_file)
        except OSError as e:
            if not self.save_failed:
                log.error("invo_token_save_failed", err=f"{type(e).__name__}"[:60])
            self.save_failed = True
            return False
        self._unsaved, self.save_failed = "", False
        return True

    # ---- requests ------------------------------------------------------------------------------------
    def post(self, path: str, body: dict, root: bool = False) -> dict:
        return self._call((self.root if root else self.base) + path, json.dumps(body).encode(), path)

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET at the API root (read-only /dex/ portfolio views)."""
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        # a refusal here is about that one view (e.g. our account may not see real-money portfolios), not the login
        return self._call(self.root + path + q, None, path, login_errors=False)

    def _call(self, url: str, data: bytes | None, path: str, login_errors: bool = True) -> dict:
        for attempt in range(2):
            with self._auth_lock:
                if not self._access:
                    self.refresh()
            used = self._access
            with self._lock:
                wait = self._last + max(self.min_interval, self.slow) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
                if self.slow and self._last - self.last_429 >= CALM_S:          # calm again: ease back
                    self.last_429 = self._last
                    self.slow = 0.0 if self.slow * 0.75 <= self.min_interval else self.slow * 0.75
                    log.info("invo_throttle_eased", gap_s=round(max(self.slow, self.min_interval), 2))
            headers = {"Authorization": "Bearer " + used, "User-Agent": "copybot-paper/1"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data, headers, method="POST" if data is not None else "GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    with self._auth_lock:               # expired: refresh once and retry (unless another thread
                        if self._access == used:        # already did)
                            self._access = ""
                    continue
                if e.code == 429:          # too many requests (seen live 2026-10-12): slow every Invo reader down
                    self.throttle(e.headers.get("Retry-After") if e.headers else None)
                if e.code in (401, 403) and login_errors:
                    raise InvoAuthError(f"invo refused the login (http {e.code})") from None
                raise InvoError(e.code, path) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise InvoError(0, f"network: {type(e).__name__}") from None
        raise InvoAuthError("invo refused the login")

    def throttle(self, retry_after=None) -> None:
        """Invo answered 429: double the spacing of every request (watcher and search share it, up to SLOW_MAX_S)
        and honour Retry-After (capped at 30 s). It eases back after CALM_S without a 429."""
        try:
            pause = min(30.0, max(0.0, float(retry_after or 0)))
        except ValueError:
            pause = 0.0
        with self._lock:
            now = time.monotonic()
            self.slow = min(SLOW_MAX_S, max(self.slow * 2, self.min_interval * 2, 0.5))
            self.last_429 = now
            self._last = max(self._last, now + pause)
        log.warn("invo_throttled", gap_s=round(self.slow, 2), retry_after_s=pause)

    def throttled(self) -> bool:
        """Invo said "too many requests" in the last CALM_S seconds (the search then waits: copies come first)."""
        return bool(self.slow) and time.monotonic() - self.last_429 < CALM_S

    def user(self, username: str) -> tuple[str, str] | None:
        return parse_user(self.post("/users/get_user", {"userId": None, "usersUsername": username}))

    def portfolios(self, user_id: str, size: int = 20) -> list[Portfolio]:
        """Every portfolio of the trader: the next page is read while a page comes back full (2026-10-13: only the
        first 20 were read, so a trader's other portfolios and their calls were invisible)."""
        out: dict[str, Portfolio] = {}
        for page in range(1, PAGES_MAX + 1):
            body = self.post("/portfolios/v2/get_users_portfolios",
                             {"userId": user_id, "params": {"isDeleted": False, "page": page, "size": size}})
            if not isinstance(body, dict) or not isinstance(body.get("portfolios"), list):
                # an answer without the list is a failed read, not "no portfolios" (that would close every copy)
                raise InvoError(200, "portfolio list missing in the answer")
            got = parse_portfolios(body)
            out.update((p.id, p) for p in got)
            if len(body["portfolios"]) < size:
                break
        return list(out.values())

    def open_calls(self, portfolio_id: str, size: int = 50) -> list[Call]:
        """Every open call of the portfolio, page by page while a page is full (2026-10-13: only the first 50 were
        read; in a portfolio with more open calls the others looked deleted and their copies were closed)."""
        out: dict[str, Call] = {}
        for page in range(1, PAGES_MAX + 1):
            body = self.post("/investments/get_investments",
                             {"portfolioId": portfolio_id, "isOpen": True, "params": {"page": page, "size": size}})
            out.update((c.id, c) for c in parse_calls(body))
            if len((body or {}).get("investmentsTicker") or []) < size:
                break
        return list(out.values())

    def live_open_calls(self, portfolio_id: str) -> list[Call]:
        """Every open position of a live portfolio (following the cursor)."""
        out: dict[str, Call] = {}
        cursor = None
        for _ in range(PAGES_MAX):
            got, cursor = self.live_calls(portfolio_id, True, cursor=cursor)
            out.update((c.id, c) for c in got)
            if not cursor or not got:
                break
        return list(out.values())

    def closed_calls(self, portfolio_id: str, page: int, size: int = 50) -> list[Call]:
        return parse_calls(self.post("/investments/get_investments",
                                     {"portfolioId": portfolio_id, "isOpen": False,
                                      "params": {"page": page, "size": size}}))

    def live_overview(self, p: Portfolio) -> Portfolio:
        return parse_live_overview(self.get(f"/dex/portfolio/live/{p.id}/overview"), p)

    def live_calls(self, portfolio_id: str, is_open: bool, limit: int = 50,
                   cursor: str | None = None) -> tuple[list[Call], str | None]:
        params = {"isOpen": "true" if is_open else "false", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return parse_live_positions(self.get(f"/dex/portfolio/position/{portfolio_id}/list", params), portfolio_id)

class Watcher:
    """Polls the followed Invo traders and turns their calls into events for the trading loop:
    ("invo_open", username, Call) for a call that appeared since the last poll and is fresh, and
    ("invo_close", username, Call) for one that disappeared, and ("invo_resize", username, old Call, new Call) when the
    trader added to or trimmed an open call (its committed size changed by more than RESIZE_MIN). The first poll of a trader is only a baseline: calls
    already open when we start watching are never copied (we would enter late); it is reported as
    ("invo_baseline", username, [open Calls]) so copies whose call closed meanwhile can be closed. One portfolio-list request per
    trader per poll; open calls are only read for portfolios whose open-trade list changed. A call that disappears is
    only reported closed once it shows in its portfolio's closed calls (with Invo's closing price and reason), or after
    VANISH_S if it never does (deleted): one incomplete answer must not close a copy (audit 2026-10-11). A call whose
    portfolio was not fully read (missing from the list, inactive, fewer calls listed than counted) waits
    VANISH_UNREAD_S instead: its absence was not seen (2026-10-13)."""

    def __init__(self, client: InvoClient, out, traders, poll_s: float, max_age_s: float, stop: threading.Event,
                 now=lambda: int(time.time() * 1000), skip=lambda name, portfolio: None):
        """`skip(username, Portfolio) -> reason or None`: new calls in a portfolio with a reason are not copied
        (owner request 2026-10-10: weak portfolios of a good trader, or ones the owner blocked)."""
        self.client, self.out, self.traders, self.poll_s, self.max_age_s = client, out, traders, poll_s, max_age_s
        self.stop, self.now = stop, now
        self.uids: dict[str, str] = {}
        self.known: dict[str, dict[str, Call]] = {}        # username -> open calls by id (after the baseline)
        self.sig: dict[str, tuple] = {}                    # portfolio id -> (open count, assets) last seen
        self.calls: dict[str, dict[str, Call]] = {}        # portfolio id -> its open calls last read
        self.auth_ok = True
        self.last_ok = 0.0
        self.fails = 0                                     # rounds in a row in which every read failed
        self.unknown: set[str] = set()
        self.stats: dict[str, list[Portfolio]] = {}       # username -> its active portfolios (for /invotraders)
        self.skip = skip
        self.ports: dict[str, Portfolio] = {}              # portfolio id -> its last read
        self.missing: dict[str, float] = {}                # call id -> when it first went missing (not confirmed closed)
        self.rereads: dict[str, int] = {}                  # portfolio id -> reads in a row that returned too few calls
        self.missing_logged: set[str] = set()              # call ids already logged as missing
        self.read_at: dict[str, float] = {}                # portfolio id -> when its open calls were last read
        self.late: set[str] = set()                        # open call ids first seen too old (maybe a re-id)
        self.claimed: set[str] = set()                     # new ids that replaced a vanished call
        self.overviews: dict[str, tuple[float, Portfolio]] = {}   # live portfolio id -> (read at, with real numbers)
        self.live_bad: set[str] = set()                    # live portfolio ids whose positions Invo refuses to show

    def start(self) -> None:
        threading.Thread(target=self.run, name="invo", daemon=True).start()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.stop.wait(self.round())
            except InvoAuthError as e:
                if self.auth_ok:
                    self.out.put(("invo_auth", False, str(e)))
                self.auth_ok = False
                self.stop.wait(300)
            except Exception as e:
                log.warn("invo_poll_failed", err=f"{type(e).__name__}: {e}"[:200])
                self.stop.wait(max(self.poll_s, 1.0))

    def round(self) -> float:
        """Read every watched trader once; returns the seconds to wait before the next round. A trader whose read
        fails is skipped for this round only (owner request 2026-10-11: one bad trader used to end the round for
        the traders after it). When EVERY read fails (Invo refusing us), wait 2, 4, 8 ... up to 60 s instead of
        asking again at once."""
        names = set(self.traders())
        for gone in set(self.known) - names:                 # watched again later: a fresh baseline
            self.forget(gone)
        failed = 0
        for name in sorted(names):
            try:
                self.poll(name)
            except InvoAuthError:
                raise
            except Exception as e:
                failed += 1
                log.warn("invo_poll_failed", trader=name, err=f"{type(e).__name__}: {e}"[:200])
        self.client.save_token()                             # retried while the token file is locked
        if names and failed == len(names):
            self.fails += 1
            return min(60.0, 2.0 ** self.fails)
        self.fails = 0
        self.last_ok = time.time()
        if not self.auth_ok:
            self.auth_ok = True
            self.out.put(("invo_auth", True, ""))
        return self.poll_s if names else max(self.poll_s, 1.0)

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
        live_failed = False
        full: set[str] = set()      # active portfolios whose open calls we have in full: only there a missing call
        ports = [self.live_stats(p) if p.kind == "live" else p for p in self.client.portfolios(uid)]
        self.stats[name] = [p for p in ports if p.active]
        for p in ports:
            self.ports[p.id] = p
            if not p.active:
                continue
            if p.kind == "live":                            # real money: its open positions, read every round
                try:
                    got = self.client.live_open_calls(p.id)
                except InvoAuthError:
                    raise
                except Exception as e:                      # refused / failed: alert once, keep what we knew (a
                    live_failed = True                      # failed read never closes a copy)
                    why = f"{type(e).__name__}: {e}"[:120]
                    log.warn("invo_live_read_failed", trader=name, portfolio=p.id, err=why)
                    if p.id not in self.live_bad:
                        self.live_bad.add(p.id)
                        self.out.put(("invo_live", name, False, why))
                    now_open.update(self.calls.get(p.id, {}))
                    continue
                if p.id in self.live_bad:
                    self.live_bad.discard(p.id)
                    self.out.put(("invo_live", name, True, ""))
                self.calls[p.id] = {c.id: c for c in got if c.is_open}
                now_open.update(self.calls[p.id])
                full.add(p.id)
                continue
            sig = (p.open_count, tuple(sorted(p.open_assets)), p.updated_ms)     # an edit changes updated_ms
            if p.open_count and (self.sig.get(p.id) != sig or p.id not in self.calls
                                 or time.monotonic() - self.read_at.get(p.id, 0.0) >= RECHECK_S):
                got = self.calls[p.id] = {c.id: c for c in self.client.open_calls(p.id) if c.is_open}
                self.read_at[p.id] = time.monotonic()
                # fewer calls than the portfolio counts: Invo's list may lag its count, so read it again next round
                # instead of waiting for the next change (the new call would be too old by then; audit 2026-10-11)
                n = self.rereads[p.id] = self.rereads.get(p.id, 0) + 1 if len(got) < p.open_count else 0
                if 0 < n < REREAD_MAX:
                    self.sig.pop(p.id, None)
                    now_open.update(got)
                    continue
            elif not p.open_count:
                self.calls[p.id] = {}
            self.sig[p.id] = sig
            now_open.update(self.calls.get(p.id, {}))
            if len(self.calls.get(p.id, {})) >= p.open_count:
                full.add(p.id)
        before = self.known.get(name)
        if before is None and (live_failed or any(0 < self.rereads.get(p.id, 0) < REREAD_MAX
                                                  for p in ports if p.active)):
            return                     # a first read with a short call list: take the baseline once it is complete
        self.known[name] = now_open
        if before is None:                                  # baseline: never copy calls opened before we watched
            log.info("invo_baseline", trader=name, open_calls=len(now_open))
            # the trading loop matches our open copies to these calls (after a restart) and closes the copies whose
            # call closed while we were not watching
            self.out.put(("invo_baseline", name, list(now_open.values())))
            return
        # Invo re-ids an edited call (same coin, side and creation time, new id): the old id vanishes from the open list
        # without ever showing in the closed list, the new one looks like a 20-minute-old new call. That closed our
        # copy as "deleted" and never copied the new id (live 2026-10-13, prateek's BTC short). Same call: re-link.
        for cid, c in list(before.items()):
            if cid in now_open:
                continue
            twin = next((n for nid, n in now_open.items()
                         if (nid not in before or nid in self.late) and nid not in self.claimed
                         and n.portfolio_id == c.portfolio_id and n.ticker == c.ticker and n.long == c.long
                         and abs(n.created_ms - c.created_ms) <= REPLACED_S * 1000), None)
            if twin is None:
                continue
            self.claimed.add(twin.id)
            self.late.discard(twin.id)
            self.missing.pop(cid, None)
            del before[cid]
            log.info("invo_call_replaced", trader=name, ticker=c.ticker, old=cid, new=twin.id,
                     old_size=round(c.committed, 4), new_size=round(twin.committed, 4))
            self.out.put(("invo_replace", name, c, twin))
        for cid, c in now_open.items():
            if cid not in before and cid not in self.claimed and cid not in self.late:
                age_s = (self.now() - c.created_ms) / 1000
                port = self.ports.get(c.portfolio_id)
                why = self.skip(name, port) if port is not None else None
                if why:
                    log.info("invo_call_skipped", trader=name, ticker=c.ticker, portfolio=port.title[:30], why=why)
                elif age_s <= self.max_age_s:
                    self.out.put(("invo_open", name, c))
                else:
                    log.info("invo_call_too_old", trader=name, ticker=c.ticker, age_s=round(age_s))
                    self.late.add(cid)       # (maybe a re-id of a call whose old id vanishes later)
        for cid, c in before.items():
            if cid in now_open:
                self.missing.pop(cid, None)
                new = now_open[cid]
                if c.units > 0 and new.units > 0:           # live: the coin amount says if it was added to / trimmed
                    if abs(new.units - c.units) / c.units > RESIZE_MIN:
                        new = now_open[cid] = replace(new, entry_size=c.committed * new.units / c.units)
                        self.out.put(("invo_resize", name, c, new))
                    else:                                   # unchanged: keep the size committed at the start
                        now_open[cid] = replace(new, entry_size=c.entry_size, size=c.size)
                elif c.exposure > 0 and abs(new.exposure - c.exposure) / c.exposure > RESIZE_MIN:
                    self.out.put(("invo_resize", name, c, new))
                elif c.exposure > 0:            # below RESIZE_MIN: compare the next read with the size we follow,
                    now_open[cid] = replace(new, entry_size=c.entry_size, size=c.size, leverage=c.leverage)
                continue                        # so small adds or trims add up until they count (2026-10-13)
            done = self.closed_call(c)
            if done is not None and done is not UNREAD:     # confirmed: the trader closed it (real price and reason)
                self.missing.pop(cid, None)
                self.out.put(("invo_close", name, done))
                continue
            first = self.missing.setdefault(cid, self.now() / 1000)
            if done is UNREAD:                              # Invo did not answer (429...): never close on that
                self.known[name][cid] = c
                continue
            # its portfolio missing from the list, inactive or listing fewer calls than it counts: we did not see the
            # call's absence, only that it was not in what we read (2026-10-13: a still-open prateek call closed)
            wait = VANISH_S if c.portfolio_id in full else VANISH_UNREAD_S
            if self.now() / 1000 - first >= wait:          # gone and never closed: deleted (or hidden) by the trader
                self.missing.pop(cid, None)
                log.warn("invo_call_vanished", trader=name, ticker=c.ticker, call=cid,
                         missing_s=round(self.now() / 1000 - first), portfolio_read=c.portfolio_id in full)
                self.out.put(("invo_close", name, replace(c, reason_closed=DELETED)))
            else:                                           # maybe one incomplete answer: keep it, look again
                if cid not in self.missing_logged:
                    self.missing_logged.add(cid)
                    port = self.ports.get(c.portfolio_id)
                    log.info("invo_call_missing", trader=name, ticker=c.ticker, call=cid,
                             portfolio_read=c.portfolio_id in full, listed=port is not None,
                             active=port.active if port else None, open_count=port.open_count if port else None,
                             read=len(self.calls.get(c.portfolio_id, {})))
                self.known[name][cid] = c

    def live_stats(self, p: Portfolio) -> Portfolio:
        """A live portfolio with its real numbers (read at most every OVERVIEW_S; a failed read keeps the last)."""
        at, seen = self.overviews.get(p.id, (0.0, None))
        if seen is not None and time.monotonic() - at < OVERVIEW_S:
            return replace(seen, active=p.active, title=p.title)
        try:
            got = self.client.live_overview(p)
        except InvoAuthError:
            raise
        except Exception as e:
            log.warn("invo_live_overview_failed", portfolio=p.id, err=f"{type(e).__name__}: {e}"[:120])
            return seen or p
        self.overviews[p.id] = (time.monotonic(), got)
        return got

    def closed_call(self, c: Call):
        """The call as it appears in its portfolio's closed calls (newest page), None (not there) or UNREAD (Invo did
        not answer: a vanished call is then kept, live 2026-10-12: copies closed as "deleted" while Invo said 429)."""
        try:
            port = self.ports.get(c.portfolio_id)
            if port is not None and port.kind == "live":
                return next((x for x in self.client.live_calls(c.portfolio_id, False)[0] if x.id == c.id), None)
            return next((x for x in self.client.closed_calls(c.portfolio_id, 1) if x.id == c.id), None)
        except InvoAuthError:
            raise
        except Exception as e:
            log.warn("invo_close_check_failed", call=c.id, err=f"{type(e).__name__}: {e}"[:120])
            return UNREAD

    def forget(self, name: str) -> None:
        self.known.pop(name, None)
