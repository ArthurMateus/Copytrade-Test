"""Loopback fakes of the NETWORK only: Hyperliquid REST + websocket, and the Telegram Bot API.

Responses are built from the real recorded fixtures in tests/fixtures. Our own code is never mocked.
"""
from __future__ import annotations

import copy
import html
import json
import re
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
        self.ws_refuse = False
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
        if self.ws_refuse:            # simulate an outage: accept, then close at once
            conn.close()
            return
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


_MD_TOKEN = re.compile(r"(```\n?.*?\n?```|`[^`]*`|\*\*|\*|\\.|[^`*\\]+)", re.S)


def md_to_card(md: str) -> str:
    """Discord markdown -> the bot's card HTML (b, i, code, pre, escaped text), so assertions can be written against
    what the bot rendered. The inverse of copybot.discord.html_to_md for the subset it produces."""
    out, bold, ital = [], False, False
    for t in _MD_TOKEN.findall(md):
        if t.startswith("```"):
            out.append("<pre>" + html.escape(t[3:-3].strip("\n"), quote=False) + "</pre>")
        elif t.startswith("`"):
            out.append("<code>" + html.escape(t[1:-1], quote=False) + "</code>")
        elif t == "**":
            out.append("</b>" if bold else "<b>")
            bold = not bold
        elif t == "*":
            out.append("</i>" if ital else "<i>")
            ital = not ital
        elif t.startswith("\\"):
            out.append(html.escape(t[1:], quote=False))
        else:
            out.append(html.escape(t, quote=False))
    return "".join(out)


class FakeDiscord:
    """Loopback Discord: REST (/api/v10/...) and a Gateway websocket that says hello, accepts identify, answers
    heartbeats and can push slash-command interactions."""

    def __init__(self, token="DCTOKEN.fake.discord-bot-token", channel="5550001", owner="7770001", guild="9990001",
                 app="1110001"):
        self.token, self.channel, self.owner, self.guild, self.app = token, channel, owner, guild, app
        self.messages: dict[int, dict] = {}     # id -> embed
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.replies: list[dict] = []           # interaction callbacks
        self.commands = None                    # registered slash commands
        self.identified: list[dict] = []
        self.calls: list[tuple[float, str]] = []
        self.fail_429 = 0
        self.next_id = 10_000
        self.seq = 0
        self.lock = threading.Lock()
        self._conns: list = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self, verb):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
                code, obj = fake.rest(verb, urlparse(self.path).path, body, self.headers.get("Authorization", ""))
                b = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PATCH(self):
                self._go("PATCH")

            def do_PUT(self):
                self._go("PUT")

        self.http = _Server(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.ws = serve(self._ws, "127.0.0.1", 0)
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()

    @property
    def api_base(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}/api/v10"

    @property
    def gateway_url(self):
        return f"ws://127.0.0.1:{self.ws.socket.getsockname()[1]}/?v=10&encoding=json"

    def close(self):
        self.http.shutdown()
        self.ws.shutdown()

    def text(self, mid) -> str:
        """A message's current text, as card HTML (see md_to_card)."""
        return md_to_card(self.messages[mid]["description"])

    # ---- REST -----------------------------------------------------------------------------------
    def rest(self, verb, path, body, auth):
        p = path.removeprefix("/api/v10")
        with self.lock:
            if p.startswith("/interactions/"):
                self.replies.append(body)
                return 204, None
            if auth != f"Bot {self.token}":
                return 401, {"message": "401: Unauthorized", "code": 0}
            self.calls.append((time.time(), f"{verb} {p}"))
            if self.fail_429 > 0:
                self.fail_429 -= 1
                return 429, {"message": "You are being rate limited.", "retry_after": 1.0, "global": False}
            if verb == "GET" and p == f"/channels/{self.channel}":
                return 200, {"id": self.channel, "guild_id": self.guild, "type": 0}
            if verb == "PUT" and p == f"/applications/{self.app}/guilds/{self.guild}/commands":
                self.commands = body
                return 200, body
            if verb == "POST" and p == f"/channels/{self.channel}/messages":
                mid = self.next_id
                self.next_id += 1
                self.messages[mid] = body["embeds"][0]
                self.sent.append({**body["embeds"][0], "id": mid, "message_id": mid,
                                  "text": md_to_card(body["embeds"][0]["description"])})
                return 200, {"id": str(mid), "channel_id": self.channel}
            if verb == "PATCH" and p.startswith(f"/channels/{self.channel}/messages/"):
                mid = int(p.rsplit("/", 1)[1])
                if mid not in self.messages:
                    return 404, {"message": "Unknown Message", "code": 10008}
                self.messages[mid] = body["embeds"][0]
                self.edits.append({**body["embeds"][0], "id": mid, "message_id": mid,
                                   "text": md_to_card(body["embeds"][0]["description"])})
                return 200, {"id": str(mid)}
        return 404, {"message": "404: Not Found", "code": 0}

    # ---- Gateway --------------------------------------------------------------------------------
    def _next(self):
        with self.lock:
            self.seq += 1
            return self.seq

    def _ws(self, conn):
        conn.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 500}}))
        try:
            for raw in conn:
                m = json.loads(raw)
                if m["op"] == 2:
                    if m["d"]["token"] != self.token:
                        conn.close(4004, "Authentication failed.")
                        return
                    self.identified.append(m["d"])
                    with self.lock:
                        self._conns.append(conn)
                    conn.send(json.dumps({"op": 0, "t": "READY", "s": self._next(), "d": {
                        "session_id": "s1", "application": {"id": self.app}, "user": {"id": "bot"}}}))
                elif m["op"] == 1:
                    conn.send(json.dumps({"op": 11}))
        except Exception:
            pass
        finally:
            with self.lock:
                if conn in self._conns:
                    self._conns.remove(conn)

    def connected(self) -> bool:
        with self.lock:
            return bool(self._conns)

    def say(self, text: str, user=None) -> None:
        """The owner types a slash command, e.g. "/hyperadd 0xabc" or "/reset 1234": the argument goes in the
        option the bot registers for it ("pin" or "wallet"). Waits for the bot's gateway connection first."""
        from copybot.chat import PIN_COMMANDS, WALLET_COMMANDS, canon
        end = time.time() + 15
        while not self.connected() and time.time() < end:
            time.sleep(0.02)
        name, _, arg = text.strip().partition(" ")
        opt = "pin" if canon(name) in PIN_COMMANDS else ("wallet" if canon(name) in WALLET_COMMANDS else None)
        self.interact(name.lstrip("/"), {opt: arg.strip()} if opt and arg.strip() else None, user=user)

    def interact(self, name, options=None, user=None):
        d = {"id": f"i{self._next()}", "token": "itoken", "type": 2, "channel_id": self.channel,
             "member": {"user": {"id": user or self.owner}},
             "data": {"name": name, "options": [{"name": k, "type": 3, "value": v} for k, v in (options or {}).items()]}}
        msg = json.dumps({"op": 0, "t": "INTERACTION_CREATE", "s": self._next(), "d": d})
        with self.lock:
            conns = self._conns[-1:]          # like Discord: only the newest session gets the interaction
        for c in conns:
            c.send(msg)

    def drop(self):
        with self.lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
