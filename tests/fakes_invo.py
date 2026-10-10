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
                with self.lock:
                    open_calls = list(self.calls.get(pid, []))
                p.update(id=pid, ownerId=body["userId"],
                         openTrades={"count": len(open_calls), "assets": [c["ticker"] for c in open_calls]})
                if pid in self.updated:
                    p["updatedAt"] = iso(self.updated[pid])
                p.update(self.port_meta.get(pid, {}))
                out.append(p)
            return {"portfolios": out}
        if path == "/investments/get_investments":
            pg = body.get("params") or {}
            page, size = int(pg.get("page", 1)), int(pg.get("size", 50))
            with self.lock:
                if body.get("isOpen"):
                    calls = copy.deepcopy(self.calls.get(body["portfolioId"], []))
                else:
                    calls = copy.deepcopy(self.closed.get(body["portfolioId"], [])[(page - 1) * size: page * size])
            r = copy.deepcopy(fixture("invo_investments_open.json"))
            r["investmentsTicker"] = calls
            return r
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

    def close_call(self, pid: str, cid: str) -> None:
        with self.lock:
            self.calls[pid] = [c for c in self.calls.get(pid, []) if c["id"] != cid]
