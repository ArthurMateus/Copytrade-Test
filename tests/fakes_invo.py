"""Loopback fake of the Invo API (NETWORK only). Answers are built from the REAL recorded ones
(tests/fixtures/invo_*.json); our own code is never mocked."""
from __future__ import annotations

import copy
import itertools
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.fakes import fixture


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeInvo:
    """users: username -> user id; portfolios: user id -> [portfolio ids]; calls: portfolio id -> [raw open calls].
    Tokens: the refresh token REFRESH0 is valid at start; every refresh rotates it (the old one is then refused)."""

    def __init__(self, refresh_token: str = "REFRESH0"):
        self.users: dict[str, str] = {}
        self.portfolios: dict[str, list[str]] = {}
        self.calls: dict[str, list[dict]] = {}
        self.updated: dict[str, int] = {}               # portfolio id -> last edit (ms): its updatedAt
        self.closed: dict[str, list[dict]] = {}          # portfolio id -> [raw closed calls], newest first
        self.ranked: list[str] = []                      # usernames on the Discover rankings
        self.port_meta: dict[str, dict] = {}             # portfolio id -> fields overriding the recorded template
        self.requests: list[tuple[str, dict]] = []
        self.valid_refresh = refresh_token
        self.valid_access = ""
        self.refuse_refresh = False
        self.broken_users: set[str] = set()              # user ids whose portfolio list answers 500
        self.throttle = False                            # every API call answers 429 (rate limited)
        self.throttle_closed = False                     # only the closed-calls reads answer 429
        self.blank_users: set[str] = set()               # user ids whose portfolio list answers 200 without the list
        self.hidden: set[str] = set()                    # open call ids counted by the portfolio but not listed yet
        # real-money ("live") portfolios: shapes recorded 2026-10-12 from /dex/portfolio/... (read-only views)
        self.live: dict[str, dict] = {}                  # portfolio id -> overview numbers
        self.live_pos: dict[str, list[dict]] = {}        # portfolio id -> raw positions (open and closed), newest first
        self.score_top: list[dict] = []                  # /analytics/score/user/top items
        self.invo_score: dict[str, float] = {}           # portfolio id -> Invo's score (0-10)
        self.refuse_live = False                         # /dex/ views answer 403 (our account may not see them)
        self._n = itertools.count(1)
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):                     # the real API renews the login with GET only
                path = self.path.split("/v1_0", 1)[-1]
                fake.requests.append((path, {}))
                auth = self.headers.get("Authorization", "")
                if path.startswith("/dex/"):
                    if not fake.valid_access or auth != "Bearer " + fake.valid_access:
                        return self._send(401, {"status": "error", "message": "Missing authorization header"})
                    if fake.refuse_live:
                        return self._send(403, {"success": False, "error": "forbidden"})
                    return self._send(200, fake.answer_get(path))
                if path == "/auth/refresh_token":
                    if fake.refuse_refresh or auth != "Bearer " + fake.valid_refresh:
                        return self._send(401, {"status": "error", "message": "invalid refresh token"})
                    k = next(fake._n)
                    fake.valid_access, fake.valid_refresh = f"ACCESS{k}", f"REFRESH{k}"
                    return self._send(200, {"accessToken": fake.valid_access, "refreshToken": fake.valid_refresh,
                                            "success": True, "error": None})
                return self._send(405, {"detail": "Method Not Allowed"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                path = self.path.split("/v1_0", 1)[-1]
                fake.requests.append((path, body))
                auth = self.headers.get("Authorization", "")
                if path == "/auth/refresh_token":
                    return self._send(405, {"detail": "Method Not Allowed"})       # as the real API answers
                if not fake.valid_access or auth != "Bearer " + fake.valid_access:
                    return self._send(401, {"status": "error", "message": "Missing authorization header"})
                if fake.throttle or (fake.throttle_closed and path == "/investments/get_investments"
                                     and body.get("isOpen") is False):
                    return self._send(429, {"message": "Too Many Requests"})
                if path == "/portfolios/v2/get_users_portfolios" and body.get("userId") in fake.broken_users:
                    return self._send(500, {"status": "error", "message": "Internal server error"})
                if path == "/portfolios/v2/get_users_portfolios" and body.get("userId") in fake.blank_users:
                    return self._send(200, {"success": True})
                return self._send(200, fake.answer(path, body))

        self.http = _Server(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.http.server_address[1]}/v1_0"

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    def expire_access(self):
        self.valid_access = "EXPIRED"

    # ---- answers in the recorded shapes -------------------------------------------------------------
    def answer(self, path: str, body: dict) -> dict:
        if path == "/users/get_user":
            uid = self.users.get(body.get("usersUsername") or "")
            if uid is None:
                return {"error": {"message": "User not found"}, "success": False, "user": None}
            r = copy.deepcopy(fixture("invo_get_user.json"))
            r["user"].update(id=uid, username=body["usersUsername"])
            return r
        if path == "/portfolios/v2/get_users_portfolios":
            tmpl = fixture("invo_users_portfolios.json")["portfolios"][2]
            out = []
            for pid in self.portfolios.get(body["userId"], []):
                p = copy.deepcopy(tmpl)
                if pid in self.live:                     # /v1_0 shows a live portfolio empty
                    p.update(id=pid, ownerId=body["userId"], type="live", title="Live Portfolio", winRate=0,
                             closedPositions=0, plSnapshot=0, openTrades={"count": 0, "assets": []})
                    out.append(p)
                    continue
                with self.lock:
                    open_calls = list(self.calls.get(pid, []))
                p.update(id=pid, ownerId=body["userId"],
                         openTrades={"count": len(open_calls), "assets": [c["ticker"] for c in open_calls]})
                if pid in self.updated:
                    p["updatedAt"] = iso(self.updated[pid])
                p.update({"winRate": 95.0, "plSnapshot": 150.0})   # a portfolio good enough to copy, unless a test says
                p.update(self.port_meta.get(pid, {}))
                out.append(p)
            pg = body.get("params") or {}                # paged like Invo (20 a page)
            page, size = int(pg.get("page", 1)), int(pg.get("size", 20))
            return {"portfolios": out[(page - 1) * size: page * size]}
        if path == "/investments/get_investments":
            pg = body.get("params") or {}
            page, size = int(pg.get("page", 1)), int(pg.get("size", 50))
            with self.lock:
                if body.get("isOpen"):
                    calls = [copy.deepcopy(c) for c in reversed(self.calls.get(body["portfolioId"], []))
                             if c["id"] not in self.hidden][(page - 1) * size: page * size]   # newest first, paged
                else:
                    calls = copy.deepcopy(self.closed.get(body["portfolioId"], [])[(page - 1) * size: page * size])
            r = copy.deepcopy(fixture("invo_investments_open.json"))
            r["investmentsTicker"] = calls
            return r
        if path == "/analytics/score/user/top":
            return {"currentUser": None, "items": copy.deepcopy(self.score_top)}
        if path == "/analytics/score/portfolio":
            sc = self.invo_score.get(body.get("portfolioId"))
            if sc is None:
                return {"status": "pending", "detail": None}
            part = {"score": 10, "scoreCap": 10, "isCapped": False, "capReason": None}
            return {"status": "complete", "detail": {"portfolioId": body["portfolioId"], "score": sc, "baseScore": sc,
                                                     "isCapped": False, "profitability": part, "risk": part,
                                                     "consistency": part, "trackRecord": part}}
        if path in ("/trending/get_portfolios_pl", "/trending/get_users"):
            # rankings: real portfolio objects (as get_users_portfolios returns them), each with its trader as owner
            pg = body.get("params") or body
            page, size = int(pg.get("page", 1)), int(pg.get("size", 20))
            names = self.ranked[(page - 1) * size: page * size]
            tmpl = fixture("invo_users_portfolios.json")["portfolios"][0]
            out = []
            for n in names:
                q = copy.deepcopy(tmpl)
                q["owner"]["username"] = n
                out.append(q)
            return {"portfolios": out} if path.endswith("portfolios_pl") else {"users": [q["owner"] for q in out]}
        return {"success": False}

    def answer_get(self, path: str) -> dict:
        from urllib.parse import parse_qs, urlsplit
        u = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")          # dex portfolio live <id> overview | dex portfolio position <id> list
        pid = parts[3] if len(parts) > 3 else ""
        with self.lock:
            pos = copy.deepcopy(self.live_pos.get(pid, []))
            ov = dict(self.live.get(pid, {}))
        if u.path.endswith("/overview"):
            op = [p for p in pos if p["isOpen"]]
            return {"success": True, "data": {"portfolioId": pid, "status": "active", "pnlUnit": "cash",
                                              "allTimePnl": {"pct": str(ov.get("pnl", 120.0)), "cash": "1000"},
                                              "equity": {"value": "10000", "unit": "cash"},
                                              "closedCount": ov.get("closed", 300),
                                              "accuracyPct": ov.get("accuracy", 75.0),
                                              "openTrades": {"count": len(op),
                                                             "assets": sorted({p["assetSymbol"] for p in op})},
                                              "sync": "ready"}}
        if u.path.endswith("/list"):
            want = q.get("isOpen") == "true"
            sel = [p for p in pos if p["isOpen"] == want]
            start, limit = int(q.get("cursor") or 0), int(q.get("limit") or 50)
            page = sel[start: start + limit]
            nxt = str(start + limit) if start + limit < len(sel) else None
            return {"success": True, "data": {"portfolioId": pid, "pnlUnit": "cash", "isOpen": want,
                                              "positions": page, "nextCursor": nxt, "sync": "ready"}}
        return {"success": False}

    def add_live(self, name: str, **numbers) -> str:
        """A real-money portfolio for `name` (created if needed): /v1_0 lists it empty, /dex/ serves its trades."""
        if name not in self.users:
            self.add_user(name, 0)
        uid = self.users[name]
        pid = f"live-{name}-{next(self._n)}"
        self.portfolios[uid].append(pid)
        self.live[pid] = numbers
        self.live_pos[pid] = []
        return pid

    def live_open(self, pid: str, ticker: str, long: bool = True, leverage: float = 5, pct: float = 2.0,
                  entry: float = 3000.0, coins: float = 1.0, created_ms: int | None = None) -> str:
        """A real position opens: size.pct = % of the trader's equity, size.coin = its amount."""
        cid = f"pos-{next(self._n)}"
        p = {"id": cid, "shortId": cid, "assetSymbol": ticker, "side": "long" if long else "short",
             "leverage": leverage, "marginMode": "cross", "entryPx": str(entry), "markPx": str(entry), "exitPx": None,
             "pnl": {}, "holdTimeSeconds": None, "size": {"pct": str(pct), "cash": "100", "coin": str(coins)},
             "openedAt": iso(created_ms if created_ms is not None else int(time.time() * 1000)), "closedAt": None,
             "isOpen": True, "closeReason": None, "takeProfitPx": None, "stopLossPx": None}
        with self.lock:
            self.live_pos[pid].insert(0, p)
        return cid

    def live_set(self, pid: str, cid: str, **kw) -> None:
        with self.lock:
            for p in self.live_pos[pid]:
                if p["id"] == cid:
                    if "coins" in kw:
                        p["size"]["coin"] = str(kw.pop("coins"))
                    if "pct" in kw:
                        p["size"]["pct"] = str(kw.pop("pct"))
                    p.update(kw)

    def live_close(self, pid: str, cid: str, price: float, reason: str = "manual",
                   closed_ms: int | None = None) -> None:
        self.live_set(pid, cid, isOpen=False, exitPx=str(price), closeReason=reason,
                      closedAt=iso(closed_ms if closed_ms is not None else int(time.time() * 1000)))

    def add_user(self, name: str, n_portfolios: int = 1) -> list[str]:
        uid = f"uid-{name}"
        self.users[name] = uid
        pids = [f"pf-{name}-{i}" for i in range(n_portfolios)]
        self.portfolios[uid] = pids
        for p in pids:
            self.calls.setdefault(p, [])
        return pids

    def open_call(self, pid: str, ticker: str, long: bool = True, leverage: float = 10, size: float = 0.05,
                  entry: float = 3000.0, target: float | None = None, stop: float | None = None,
                  created_ms: int | None = None) -> str:
        c = copy.deepcopy(fixture("invo_investments_open.json")["investmentsTicker"][1])
        cid = f"call-{next(self._n)}"
        c.update(id=cid, ticker=ticker, name=ticker, directionLong=long, leverage=leverage, positionSize=size,
                 entrySize=size * 100,
                 entryPrice=entry, priceTarget=target, stopLoss=stop, isOpen=True,
                 createdAt=iso(created_ms if created_ms is not None else int(time.time() * 1000)))
        c["portfolio"]["id"] = pid
        with self.lock:
            self.calls.setdefault(pid, []).append(c)
        return cid

    def add_closed(self, pid: str, ticker: str, long: bool, leverage: float, size: float, entry: float, close: float,
                   created_ms: int, closed_ms: int) -> None:
        """A closed call in the recorded shape (tests/fixtures/invo_investments_closed.json)."""
        c = copy.deepcopy(fixture("invo_investments_closed.json")["investmentsTicker"][0])
        c.update(id=f"closed-{next(self._n)}", ticker=ticker, name=ticker, directionLong=long, leverage=leverage,
                 positionSize=size, entrySize=size * 100, entryPrice=entry, closingPrice=close, isOpen=False,
                 createdAt=iso(created_ms), closedAt=iso(closed_ms))
        c["portfolio"]["id"] = pid
        with self.lock:
            lst = self.closed.setdefault(pid, [])
            lst.append(c)
            lst.sort(key=lambda x: x["createdAt"], reverse=True)

    def resize_call(self, pid: str, cid: str, size: float) -> None:
        """The trader adds to (size up) or trims (size down) an open call: Invo changes its committed entrySize,
        flags the change, and the portfolio's updatedAt moves."""
        with self.lock:
            for c in self.calls.get(pid, []):
                if c["id"] == cid:
                    c.update(entrySize=size * 100, positionSize=size, changes={"isAdded": True})
            self.updated[pid] = int(time.time() * 1000)

    def reid_call(self, pid: str, cid: str, **fields) -> str:
        """The trader edits an open call (stop, target, size) and Invo lists it under a NEW id with the same creation
        time; the old id leaves the open list and never shows in the closed list (live 2026-10-13, prateek)."""
        new = f"call-{next(self._n)}"
        with self.lock:
            for c in self.calls.get(pid, []):
                if c["id"] == cid:
                    c.update(id=new, **fields)
            self.updated[pid] = int(time.time() * 1000)
        return new

    def close_call(self, pid: str, cid: str, price: float | None = None, reason: str = "manual") -> None:
        """The trader closes the call: it leaves the open list and is first in the closed list (as on Invo)."""
        with self.lock:
            gone = [c for c in self.calls.get(pid, []) if c["id"] == cid]
            self.calls[pid] = [c for c in self.calls.get(pid, []) if c["id"] != cid]
            for c in gone:
                c.update(isOpen=False, closedAt=iso(int(time.time() * 1000)), reasonClosed=reason,
                         closingPrice=price if price is not None else c["entryPrice"])
                self.closed.setdefault(pid, []).insert(0, c)

    def delete_call(self, pid: str, cid: str) -> None:
        """The call disappears without being closed (deleted by the trader, or hidden by Invo)."""
        with self.lock:
            self.calls[pid] = [c for c in self.calls.get(pid, []) if c["id"] != cid]
