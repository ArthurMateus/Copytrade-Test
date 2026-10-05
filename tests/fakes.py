"""Loopback fakes of the NETWORK only: Hyperliquid REST + websocket, and the Telegram Bot API.

Responses are built from the real recorded fixtures in tests/fixtures. Our own code is never mocked.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from websockets.sync.server import serve

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def make_book(coin: str, mid: float, spread_bps: float = 1.0, depth: float = 1e6, levels: int = 10) -> dict:
    """Same shape as the recorded l2Book (tests/fixtures/l2book_btc.json)."""
    tick = mid * spread_bps / 1e4 / 2
    lvl = lambda px: {"px": f"{px:.6g}", "sz": f"{depth / mid / levels:.6g}", "n": 3}
    bids = [lvl(mid - tick - i * tick) for i in range(levels)]
    asks = [lvl(mid + tick + i * tick) for i in range(levels)]
    return {"coin": coin, "time": int(time.time() * 1000), "levels": [bids, asks]}


def make_fill(coin, px, sz, side, start_pos, t=None, oid=1, tid=None, crossed=True, closed_pnl=0.0, fee=None):
    """Same shape as the recorded fills (tests/fixtures/user_fills_full_page.json)."""
    end = start_pos + (sz if side == "B" else -sz)
    if start_pos == 0:
        d = "Open Long" if side == "B" else "Open Short"
    elif start_pos > 0:
        d = "Open Long" if end > start_pos else ("Close Long" if end >= 0 else "Long > Short")
    else:
        d = "Open Short" if end < start_pos else ("Close Short" if end <= 0 else "Short > Long")
    t = int(time.time() * 1000) if t is None else t
    make_fill.n += 1
    return {"coin": coin, "px": str(px), "sz": str(sz), "side": side, "time": t, "startPosition": str(start_pos),
            "dir": d, "closedPnl": str(closed_pnl), "hash": "0x" + "0" * 64, "oid": oid, "crossed": crossed,
            "fee": str(fee if fee is not None else round(px * sz * 0.00045, 6)), "tid": tid or make_fill.n,
            "feeToken": "USDC", "twapId": None}


make_fill.n = 10_000_000


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeHL:
    def __init__(self):
        meta = fixture("meta_and_asset_ctxs.json")
        self.meta = meta
        self.mids = {"BTC": 100_000.0, "ETH": 3_000.0, "SOL": 150.0, "DOGE": 0.2}
        self.positions: dict[str, dict[str, float]] = {}     # user -> {coin: szi}
        self.fills: dict[str, list[dict]] = {}               # user -> raw fills (any order)
        self.candles: dict[str, list[dict]] = {}             # coin -> raw candles
        self.leaderboard = fixture("leaderboard_sample.json")
        self.requests: list[dict] = []
        self.fail_429 = 0
        self.delay = 0.0
        self.book_fail = False
        self.lock = threading.Lock()
        self._ws_conns: list = []
        self.ws_subs: dict = {}
        self.mids_push = True

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

            def do_GET(self):
                if urlparse(self.path).path.endswith("/leaderboard"):
                    self._send(200, fake.leaderboard)
                else:
                    self._send(404, None)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.lock:
                    fake.requests.append(body)
                    if fake.fail_429 > 0:
                        fake.fail_429 -= 1
                        return self._send(429, None)
                if fake.delay:
                    time.sleep(fake.delay)
                code, obj = fake.info(body)
                self._send(code, obj)

        self.http = _Server(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.ws = serve(self._ws_handler, "127.0.0.1", 0)
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        self._stop = threading.Event()
        threading.Thread(target=self._mids_loop, daemon=True).start()

    # ---- urls ---------------------------------------------------------------------------------
    @property
    def info_url(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}/info"

    @property
    def lb_url(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}/Mainnet/leaderboard"

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.ws.socket.getsockname()[1]}/ws"

    def close(self):
        self._stop.set()
        self.http.shutdown()
        self.ws.shutdown()

    # ---- REST -----------------------------------------------------------------------------------
    def info(self, body):
        t = body.get("type")
        if t == "allMids":
            return 200, {k: str(v) for k, v in self.mids.items()}
        if t == "l2Book":
            if self.book_fail:
                return 500, None
            return 200, make_book(body["coin"], self.mids[body["coin"]])
        if t == "clearinghouseState":
            ch = fixture("clearinghouse_state.json")
            tmpl = ch["assetPositions"][0]
            aps = []
            for coin, szi in self.positions.get(body["user"].lower(), {}).items():
                ap = copy.deepcopy(tmpl)
                ap["position"]["coin"], ap["position"]["szi"] = coin, str(szi)
                aps.append(ap)
            ch["assetPositions"] = aps
            ch["time"] = int(time.time() * 1000)
            return 200, ch
        if t == "metaAndAssetCtxs":
            return 200, self.meta
        if t == "userFillsByTime":
            fl = sorted(self.fills.get(body["user"].lower(), []), key=lambda f: (f["time"], f["tid"]))
            fl = [f for f in fl if f["time"] >= body["startTime"] and f["time"] <= body.get("endTime", 1 << 62)]
            return 200, fl[:2000]
        if t == "candleSnapshot":
            r = body["req"]
            if r["coin"].startswith("#"):
                return 500, None
            cs = [c for c in self.candles.get(r["coin"], []) if r["startTime"] <= c["t"] <= r["endTime"]]
            return 200, cs[:5000]
        return 200, None

    # ---- websocket ------------------------------------------------------------------------------
    def _ws_handler(self, conn):
        with self.lock:
            self._ws_conns.append(conn)
            self.ws_subs[conn] = set()
        try:
            for raw in conn:
                m = json.loads(raw)
                if m.get("method") == "ping":
                    conn.send(json.dumps({"channel": "pong"}))
                elif m.get("method") == "subscribe":
                    sub = m["subscription"]
                    users = {s[1] for s in self.ws_subs[conn] if s[0] == "userFills"}
                    if sub["type"] == "userFills" and len(users) >= 15:
                        conn.send(json.dumps({"channel": "error", "data": "Cannot track more than 15 total users."}))
                        continue
                    key = (sub["type"], sub.get("user", "").lower())
                    self.ws_subs[conn].add(key)
                    conn.send(json.dumps({"channel": "subscriptionResponse", "data": m}))
                    if sub["type"] == "userFills":
                        snap = self.fills.get(key[1], [])[-5:]
                        conn.send(json.dumps({"channel": "userFills", "data": {
                            "isSnapshot": True, "user": key[1], "fills": snap}}))
                elif m.get("method") == "unsubscribe":
                    sub = m["subscription"]
                    self.ws_subs[conn].discard((sub["type"], sub.get("user", "").lower()))
        except Exception:
            pass
        finally:
            with self.lock:
                if conn in self._ws_conns:
                    self._ws_conns.remove(conn)
                self.ws_subs.pop(conn, None)

    def _mids_loop(self):
        while not self._stop.wait(0.1):
            if not self.mids_push:
                continue
            msg = json.dumps({"channel": "allMids", "data": {"mids": {k: str(v) for k, v in self.mids.items()}}})
            for c in list(self._ws_conns):
                if ("allMids", "") in self.ws_subs.get(c, set()):
                    try:
                        c.send(msg)
                    except Exception:
                        pass

    def subscribed_users(self) -> set[str]:
        with self.lock:
            return {u for subs in self.ws_subs.values() for (t, u) in subs if t == "userFills"}

    def push_fills(self, user: str, fills: list[dict]):
        """A leader trades: record the fills, move its position, and stream them like the real websocket."""
        user = user.lower()
        self.fills.setdefault(user, []).extend(fills)
        pos = self.positions.setdefault(user, {})
        for f in fills:
            szi = float(f["startPosition"]) + (float(f["sz"]) if f["side"] == "B" else -float(f["sz"]))
            if abs(szi) < 1e-12:
                pos.pop(f["coin"], None)
            else:
                pos[f["coin"]] = szi
        msg = json.dumps({"channel": "userFills", "data": {"user": user, "fills": fills}})
        for c in list(self._ws_conns):
            if ("userFills", user) in self.ws_subs.get(c, set()):
                c.send(msg)

    def drop_ws(self):
        for c in list(self._ws_conns):
            try:
                c.close()
            except Exception:
                pass


class FakeTelegram:
    def __init__(self, token="123456789:TESTTOKENTESTTOKENTESTTOKENTEST", chat_id=4242):
        self.token = token
        self.chat_id = chat_id
        self.updates: list[dict] = []
        self.sent: list[dict] = []        # every sendMessage
        self.edits: list[dict] = []       # every editMessageText
        self.messages: dict[int, str] = {}
        self.calls: list[tuple[float, str]] = []
        self.next_id = 100
        self.upd_id = 1
        self.fail_429 = 0
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

            def do_POST(self):
                path = urlparse(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not path.startswith(f"/bot{fake.token}/"):
                    return self._send(401, {"ok": False, "description": "Unauthorized"})
                method = path.rsplit("/", 1)[1]
                self._send(*fake.handle(method, body))

        self.http = _Server(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def api_base(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    def close(self):
        self.http.shutdown()

    def say(self, text, chat_id=None):
        with self.lock:
            self.updates.append({"update_id": self.upd_id, "message": {
                "message_id": self.upd_id, "chat": {"id": chat_id or self.chat_id}, "text": text}})
            self.upd_id += 1

    def handle(self, method, body):
        if method == "getUpdates":
            deadline = time.time() + min(float(body.get("timeout", 0)), 1.0)
            while True:
                with self.lock:
                    ups = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
                if ups or time.time() >= deadline:
                    return 200, {"ok": True, "result": ups}
                time.sleep(0.02)
        with self.lock:
            self.calls.append((time.time(), method))
            if self.fail_429 > 0:
                self.fail_429 -= 1
                return 429, {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                             "parameters": {"retry_after": 1}}
            if method == "sendMessage":
                mid = self.next_id
                self.next_id += 1
                self.messages[mid] = body["text"]
                self.sent.append({**body, "message_id": mid})
                return 200, {"ok": True, "result": {"message_id": mid, "text": body["text"]}}
            if method == "editMessageText":
                mid = body["message_id"]
                if mid not in self.messages:
                    return 400, {"ok": False, "description": "Bad Request: message to edit not found"}
                if self.messages[mid] == body["text"]:
                    return 400, {"ok": False, "description": "Bad Request: message is not modified"}
                self.messages[mid] = body["text"]
                self.edits.append(body)
                return 200, {"ok": True, "result": {"message_id": mid}}
        return 200, {"ok": True, "result": True}
