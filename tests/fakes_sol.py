"""Loopback fakes of the NETWORK only for the Solana side: FOMO REST and DexScreener.

Response shapes are the real recorded ones (tests/fixtures/fomo_*.json, dexscreener_tokens.json). Our own code is
never mocked.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tests.fakes import fixture

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
COOKIE = "session=abc123; other=xyz"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(handler_cls):
    srv = _Server(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


_swap_n = [0]


def make_swap(side: str, token: str, amount: float, usd: float, ts_ms: int, wallet="W", sid=None) -> dict:
    """A raw swap in the exact shape of tests/fixtures/fomo_swaps.json. side: buy | sell (against USDC)."""
    _swap_n[0] += 1
    s = copy.deepcopy(fixture("fomo_swaps.json")["responseObject"]["swaps"][0])
    s["id"] = sid or f"swap-{_swap_n[0]:08d}"
    s["address"] = wallet
    s["createdAt"] = iso(ts_ms)
    if side == "buy":
        s.update(inTokenAddress=USDC, inHumanAmount=usd, humanUsdAmountIn=usd, outTokenAddress=token,
                 outHumanAmount=amount, humanUsdAmountOut=usd, display={"side": "buy"})
    else:
        s.update(inTokenAddress=token, inHumanAmount=amount, humanUsdAmountIn=usd, outTokenAddress=USDC,
                 outHumanAmount=usd, humanUsdAmountOut=usd, display={"side": "sell"})
    return s


def make_row(uid: str, address: str, handle: str, pnl: float, window="30d", swaps=200, volume=40_000.0,
             holdings_value=0.0) -> dict:
    """A leaderboard row in the shape of tests/fixtures/fomo_leaderboard_30d.json."""
    r = copy.deepcopy(fixture("fomo_leaderboard_30d.json")["responseObject"]["leaderboard"][0])
    for k in [k for k in r if k.startswith("pnl") and k != "pnl"]:
        del r[k]
    r.update(id=uid, address=address, userHandle=handle, displayName=handle, swapCount=swaps, numTrades=swaps // 2,
             totalVolume=volume, **{f"pnl{window}": pnl}, private=False, isRestricted=False)
    r["topHoldings"] = ([{"tokenAddress": "H", "networkId": 1399811149, "humanAmount": 1.0, "price": holdings_value,
                          "value": holdings_value, "pnl": 0.0}] if holdings_value else [])
    r["totalHoldings"] = len(r["topHoldings"])
    return r


class FakeFomo:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {"30d": [], "7d": [], "24h": []}
        self.swaps: dict[str, list[dict]] = {}      # uid -> raw swaps (any order)
        self.requests: list[str] = []
        self.refuse = False                           # simulate an expired session
        self.max_limit = 100_000
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
                u = urlparse(self.path)
                fake.requests.append(self.path)
                if fake.refuse or self.headers.get("Cookie") != COOKIE:
                    return self._send(403, {"authorization": False})
                parts = u.path.strip("/").split("/")
                if parts[:2] == ["v2", "leaderboard"]:
                    return self._send(200, {"success": True, "responseObject": {"leaderboard": fake.rows[parts[2]]}})
                if len(parts) == 4 and parts[:2] == ["v2", "users"] and parts[3] == "swaps":
                    limit = int(parse_qs(u.query).get("limit", ["25"])[0])
                    if limit > fake.max_limit:
                        return self._send(400, {"message": "limit too large"})
                    allsw = sorted(fake.swaps.get(parts[2], []), key=lambda s: s["createdAt"] + s["id"], reverse=True)
                    return self._send(200, {"success": True, "responseObject": {
                        "swaps": allsw[:limit], "hasNextPage": len(allsw) > limit}})
                self._send(404, None)

        self.http = _serve(H)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    def close(self):
        self.http.shutdown()
        self.http.server_close()


class FakeDex:
    def __init__(self):
        self.tokens: dict[str, dict] = {}      # mint -> {"px", "liq" (None = no liquidity field), "sym"}
        self.requests: list[str] = []
        self.down = False
        fake = self
        tmpl = fixture("dexscreener_tokens.json")[1]

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                if fake.down:
                    self.send_response(500)
                    self.end_headers()
                    return
                mints = urlparse(self.path).path.rsplit("/", 1)[-1].split(",")
                out = []
                for m in mints:
                    t = fake.tokens.get(m)
                    if not t:
                        continue
                    p = copy.deepcopy(tmpl)
                    p["baseToken"] = {"address": m, "name": t["sym"], "symbol": t["sym"]}
                    p["priceUsd"] = repr(t["px"])
                    if t.get("liq") is None:
                        p.pop("liquidity", None)
                    else:
                        p["liquidity"] = {"usd": t["liq"], "base": 1, "quote": 1}
                    out.append(p)
                b = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.http = _serve(H)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    def set(self, mint: str, px: float, liq: float | None = 500_000.0, sym: str = "MEME"):
        self.tokens[mint] = {"px": px, "liq": liq, "sym": sym}

    def close(self):
        self.http.shutdown()
        self.http.server_close()


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()
