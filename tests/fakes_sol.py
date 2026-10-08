"""Loopback fakes of the NETWORK only for the Solana side: a Solana JSON-RPC node (HTTP + logsSubscribe websocket)
and DexScreener.

Transactions are built from REAL recorded ones (tests/fixtures/chain_tx_fomo_buy.json: signers, token balances)
and DexScreener answers from dexscreener_tokens.json. Our own code is never mocked.
"""
from __future__ import annotations

import copy
import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from websockets.sync.server import serve

from copybot.config import Sol
from tests.fakes import fixture

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FEE_PAYER = Sol().fomo_fee_payer
KEY = "test-helius-key"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(handler_cls):
    srv = _Server(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FakeSolana:
    """Wallet transactions are added with `swap(...)`. A FOMO swap is co-signed by FOMO's fee payer and listed under
    both addresses, like on mainnet. `refuse` = HTTP 401 for requests carrying an api key (a revoked Helius key)."""

    def __init__(self):
        self.tx: dict[str, dict] = {}
        self.sigs: dict[str, list[dict]] = {}           # address -> signature infos, newest first
        self.bal: dict[tuple[str, str], float] = {}
        self.calls: list[tuple[str, bool]] = []          # (method, carried an api key)
        self.refuse = False
        self.bulk_refuse = False                         # Helius refuses getTransactionsForAddress (plan)
        self.subs: dict[int, tuple[object, str]] = {}    # subscription -> (connection, mentioned address)
        self.lock = threading.Lock()
        self._n = itertools.count(1)
        self.template = fixture("chain_tx_fomo_buy.json")
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = parse_qs(urlparse(self.path).query).get("api-key", [""])[0]
                fake.calls.append((body["method"], bool(key)))
                if key and (fake.refuse or key != KEY):
                    self.send_response(401)
                    self.end_headers()
                    return
                if body["method"] == "getTransactionsForAddress" and (not key or fake.bulk_refuse):
                    # the public endpoint does not know it; Helius refuses it on plans without it
                    reply = {"error": {"code": -32601 if not key else -32403,
                                       "message": "Method not found" if not key else "not available on your plan"}}
                else:
                    reply = {"result": fake.rpc(body["method"], body["params"])}
                out = json.dumps({"jsonrpc": "2.0", "id": body.get("id"), **reply}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.http = _serve(H)
        self.ws = serve(self._ws, "127.0.0.1", 0)
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.ws.socket.getsockname()[1]}"

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.ws.shutdown()

    def subscribed(self, address: str) -> bool:
        with self.lock:
            return any(a == address for _, a in self.subs.values())

    # ---- JSON-RPC ----------------------------------------------------------------------------------
    def rpc(self, method: str, params: list):
        if method == "getTransaction":
            return copy.deepcopy(self.tx.get(params[0]))
        if method == "getSignaturesForAddress":
            opts = params[1] if len(params) > 1 else {}
            with self.lock:
                lst = list(self.sigs.get(params[0], []))
            if opts.get("before"):
                idx = [i for i, s in enumerate(lst) if s["signature"] == opts["before"]]
                lst = lst[idx[0] + 1:] if idx else []
            return lst[: opts.get("limit", 1000)]
        if method == "getTransactionsForAddress":        # Helius: full transactions, newest first, paginated
            opts = params[1]
            with self.lock:
                lst = list(self.sigs.get(params[0], []))
            start = int(opts.get("paginationToken") or 0)
            end = start + min(int(opts.get("limit", 100)), 100)
            data = []
            for info in lst[start:end]:
                tx = copy.deepcopy(self.tx[info["signature"]])
                tx["slot"] = info["slot"]
                data.append(tx)
            return {"data": data, "paginationToken": str(end) if end < len(lst) else None}
        return None

    # ---- websocket: logsSubscribe(mentions) ------------------------------------------------------------
    def _ws(self, conn):
        try:
            for raw in conn:
                m = json.loads(raw)
                if m.get("method") == "logsSubscribe":
                    sub = next(self._n)
                    with self.lock:
                        self.subs[sub] = (conn, m["params"][0]["mentions"][0])
                    conn.send(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": sub}))
                elif m.get("method") == "logsUnsubscribe":
                    with self.lock:
                        self.subs.pop(m["params"][0], None)
                    conn.send(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": True}))
        except Exception:
            pass
        finally:
            with self.lock:
                for k in [k for k, (c, _) in self.subs.items() if c is conn]:
                    del self.subs[k]

    def _notify(self, address: str, sig: str) -> None:
        with self.lock:
            targets = [(k, c) for k, (c, a) in self.subs.items() if a == address]
        for sub, conn in targets:
            try:
                conn.send(json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
                    "result": {"context": {"slot": 1}, "value": {"signature": sig, "err": None, "logs": []}},
                    "subscription": sub}}))
            except Exception:
                pass

    # ---- building transactions ----------------------------------------------------------------------
    def _balance(self, idx: int, owner: str, mint: str, amount: float) -> dict:
        units = int(round(amount * 1e6))
        return {"accountIndex": idx, "mint": mint, "owner": owner,
                "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "uiTokenAmount": {"amount": str(units), "decimals": 6, "uiAmount": units / 1e6,
                                  "uiAmountString": repr(units / 1e6)}}

    def swap(self, wallet: str, side: str, token: str, amount: float, usd: float, ts_ms: int | None = None,
             fomo: bool = True, failed: bool = False) -> str:
        """`wallet` buys/sells `amount` of `token` for `usd` USDC. Returns the signature."""
        ts = int((ts_ms if ts_ms is not None else time.time() * 1000) // 1000)
        n = next(self._n)
        sig = f"FakeSig{n:012d}{wallet[:8]}"
        tx = copy.deepcopy(self.template)
        tx["blockTime"], tx["meta"]["err"] = ts, ({"InstructionError": [0, "Custom"]} if failed else None)
        tx["transaction"]["signatures"] = [sig]
        keys = [k for k in tx["transaction"]["message"]["accountKeys"] if not k.get("signer")]
        signer = lambda a: {"pubkey": a, "signer": True, "writable": True, "source": "transaction"}
        tx["transaction"]["message"]["accountKeys"] = ([signer(FEE_PAYER)] if fomo else []) + [signer(wallet)] + keys
        u0, t0 = self.bal.get((wallet, USDC), 1e6), self.bal.get((wallet, token), 0.0)
        if side == "sell" and t0 < amount:          # a sell of tokens it got elsewhere (an orphan sell)
            t0 = amount
        u1, t1 = (u0 - usd, t0 + amount) if side == "buy" else (u0 + usd, max(0.0, t0 - amount))
        if not failed:
            self.bal[(wallet, USDC)], self.bal[(wallet, token)] = u1, t1
        else:
            u1, t1 = u0, t0
        pre = [self._balance(2, wallet, USDC, u0)] + ([self._balance(3, wallet, token, t0)] if t0 > 0 else [])
        post = [self._balance(2, wallet, USDC, u1)] + ([self._balance(3, wallet, token, t1)] if t1 > 0 else [])
        tx["meta"]["preTokenBalances"], tx["meta"]["postTokenBalances"] = pre, post
        info = {"signature": sig, "slot": ts * 100_000 + n, "blockTime": ts, "err": tx["meta"]["err"], "memo": None,
                "confirmationStatus": "confirmed"}
        with self.lock:
            self.tx[sig] = tx
            for a in ([FEE_PAYER] if fomo else []) + [wallet]:
                lst = self.sigs.setdefault(a, [])
                lst.append(info)
                lst.sort(key=lambda s: -s["slot"])               # newest first, like mainnet
        self._notify(wallet, sig)
        return sig


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
