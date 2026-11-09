"""A loopback Hyperliquid testnet for the live wallet: /info (the account's reads) and /exchange (signed orders,
cancels, leverage). Every action's signature is checked: it must recover to the approved API wallet."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler

from hyperliquid.utils import signing

from tests.fakes import _Server, fixture, make_book


class FakeExchange:
    def __init__(self, account: str, agent: str):
        self.account, self.agent = account.lower(), agent.lower()
        self.meta = fixture("meta_and_asset_ctxs.json")
        self.coins = [u["name"] for u in self.meta[0]["universe"]]
        self.decimals = {u["name"]: int(u["szDecimals"]) for u in self.meta[0]["universe"]}
        self.mids = {"BTC": 100_000.0, "ETH": 3_000.0, "SOL": 150.0}
        self.positions: dict[str, float] = {}       # coin -> signed size
        self.orders: dict[int, dict] = {}           # resting (stop) orders by oid
        self.fills: list[dict] = []
        self.actions: list[dict] = []               # every accepted action
        self.leverage: dict[str, tuple] = {}
        self.cloids: dict[str, int] = {}            # client order id -> exchange order id
        self.account_value = 300.0
        self.fill_ratio = 1.0                       # < 1: IOC orders fill partly
        self.no_fill = False                        # IOC orders find no liquidity
        self.down = False                           # /exchange does not answer
        self.lock = threading.Lock()
        self._oid = 1000
        self._tid = 1
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
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.lock:
                    if self.path.endswith("/exchange"):
                        if fake.down:
                            return self._send(502, {"err": "down"})
                        return self._send(200, fake.exchange(body))
                    return self._send(200, fake.info(body))

        self.http = _Server(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    def close(self):
        self.http.shutdown()

    # ---- /info ----------------------------------------------------------------------------------
    def info(self, body):
        t = body.get("type")
        if t == "userRole":
            if body["user"].lower() == self.agent:
                return {"role": "agent", "data": {"user": self.account}}
            return {"role": "missing"}
        if t == "metaAndAssetCtxs":
            return self.meta
        if t == "allMids":
            return {k: str(v) for k, v in self.mids.items()}
        if t == "l2Book":
            return make_book(body["coin"], self.mids[body["coin"]])
        if t == "clearinghouseState":
            return {"marginSummary": {"accountValue": str(self.account_value)},
                    "assetPositions": [{"position": {"coin": c, "szi": str(s)}} for c, s in self.positions.items() if s]}
        if t == "userAbstraction":
            return getattr(self, "abstraction", "default")
        if t == "spotClearinghouseState":
            usdc = getattr(self, "spot_usdc", 0.0)
            return {"balances": [{"coin": "USDC", "token": 0, "total": str(usdc), "hold": "0.0", "entryNtl": "0.0"}]}
        if t == "frontendOpenOrders":
            return [dict(o) for o in self.orders.values()]
        if t == "orderStatus":
            oid = self.cloids.get(body["oid"])
            return {"status": "order", "order": {"order": {"oid": oid}, "status": "filled"}} if oid else                 {"status": "unknownOid"}
        if t == "userFillsByTime":
            return [f for f in self.fills if f["time"] >= body.get("startTime", 0)]
        return None

    # ---- /exchange ------------------------------------------------------------------------------
    def exchange(self, body):
        a = body["action"]
        signer = signing.recover_agent_or_user_from_l1_action(a, body["signature"], body.get("vaultAddress"),
                                                              body["nonce"], body.get("expiresAfter"), False)
        if signer.lower() != self.agent:
            return {"status": "err", "response": f"User or API Wallet {signer} does not exist."}
        self.actions.append(a)
        if a["type"] == "order":
            return {"status": "ok", "response": {"type": "order",
                                                 "data": {"statuses": [self._order(w) for w in a["orders"]]}}}
        if a["type"] == "cancel":
            for c in a["cancels"]:
                self.orders.pop(c["o"], None)
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        if a["type"] == "updateLeverage":
            self.leverage[self.coins[a["asset"]]] = (a["leverage"], a["isCross"])
            return {"status": "ok", "response": {"type": "default"}}
        return {"status": "err", "response": f"unknown action {a['type']}"}

    def _order(self, w):
        coin, buy, sz, px = self.coins[w["a"]], w["b"], float(w["s"]), float(w["p"])
        if "trigger" in w["t"]:
            self._oid += 1
            self.orders[self._oid] = {"coin": coin, "side": "B" if buy else "A", "sz": str(sz), "oid": self._oid,
                                      "isTrigger": True, "triggerPx": w["t"]["trigger"]["triggerPx"],
                                      "reduceOnly": w["r"], "orderType": "Stop Market", "limitPx": w["p"]}
            return {"resting": {"oid": self._oid}}
        pos = self.positions.get(coin, 0.0)
        if w["r"]:
            if not pos or (pos > 0) == buy:
                return {"error": "Reduce only order would increase position."}
            sz = min(sz, abs(pos))
        mid = self.mids[coin]
        fill_px = mid * (1.0001 if buy else 0.9999)
        if self.no_fill or (buy and px < fill_px) or (not buy and px > fill_px):
            return {"error": "Order could not immediately match against any resting orders."}
        sz = round(sz * self.fill_ratio, self.decimals[coin])
        if sz <= 0:
            return {"error": "Order could not immediately match against any resting orders."}
        self._oid += 1
        if w.get("c"):
            self.cloids[w["c"]] = self._oid
        self._fill(coin, buy, sz, fill_px, self._oid)
        return {"filled": {"totalSz": str(sz), "avgPx": str(fill_px), "oid": self._oid}}

    def _fill(self, coin, buy, sz, px, oid):
        start = self.positions.get(coin, 0.0)
        end = round(start + (sz if buy else -sz), 10)
        self.positions[coin] = end
        if not end:
            self.positions.pop(coin)
        closing = start and (start > 0) != buy
        self.fills.append({"coin": coin, "px": str(px), "sz": str(sz), "side": "B" if buy else "A",
                           "time": int(time.time() * 1000), "startPosition": str(start),
                           "dir": ("Close Long" if start > 0 else "Close Short") if closing else
                           ("Open Long" if buy else "Open Short"), "closedPnl": "0", "fee": str(px * sz * 0.00045),
                           "crossed": True, "oid": oid, "tid": self._tid, "hash": "0x"})
        self._tid += 1

    # ---- test helpers ---------------------------------------------------------------------------
    def fire_stop(self, coin: str) -> None:
        """The price reached the resting stop: the exchange fills it (the bot did nothing)."""
        with self.lock:
            for oid, o in list(self.orders.items()):
                if o["coin"] == coin and o["isTrigger"]:
                    pos = self.positions.get(coin, 0.0)
                    self.orders.pop(oid)
                    if pos:
                        self._fill(coin, o["side"] == "B", min(float(o["sz"]), abs(pos)), float(o["triggerPx"]), oid)

    def stops(self, coin: str) -> list[dict]:
        return [o for o in self.orders.values() if o["coin"] == coin and o["isTrigger"]]
