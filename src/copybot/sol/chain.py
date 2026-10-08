"""Solana on-chain data for the FOMO book: FOMO traders' real wallets, their swaps, live alerts.

FOMO's own API is behind Cloudflare and refuses programs, and the `address` it shows for a trader is NOT the wallet
that trades (verified 2026-10-08: listed addresses with thousands of FOMO swaps have no on-chain transactions).
What IS public: FOMO pays the network fee of every user swap, so each FOMO trade is a transaction co-signed by
FOMO's fee payer (`Sol.fomo_fee_payer`) and by the trader's real wallet (the other signer). Verified on a real
recording: tests/fixtures/chain_tx_fomo_buy.json is the on-chain side of the first swap in fomo_swaps.json
(3565.6274 tokens for 89.0483 USDC one second after FOMO's timestamp; FOMO shows 88.0983 USDC before its fee).

Standard Solana JSON-RPC only (works on the free public endpoint and on Helius):
  getSignaturesForAddress(addr, {limit<=1000, before})  -> newest first, each with blockTime and err
  getTransaction(sig, {encoding: jsonParsed, maxSupportedTransactionVersion: 1}) -> meta.pre/postTokenBalances
  logsSubscribe({mentions: [addr]}) over the websocket -> a notification per transaction touching the wallet
FOMO trades are priced in USDC (40/40 sampled), so a swap is: the wallet's USDC/USDT goes one way and exactly one
other token the other way. SOL-priced swaps (about 1 in 40) have no USD price here and are skipped.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from copybot import log

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL = "So11111111111111111111111111111111111111112"
USD = frozenset({USDC, USDT})
MIN_USD = 0.01           # smaller USDC moves are fees/dust, not a swap


@dataclass(frozen=True)
class Leg:
    """One side of a swap against USD: a buy or a sell of `token`."""
    id: str
    ts: int                        # ms
    token: str
    side: str                      # "buy" | "sell"
    amount: float                  # tokens
    usd: float                     # USD paid (buy) or received (sell)

    @property
    def px(self) -> float:
        return self.usd / self.amount if self.amount > 0 else 0.0


class AuthError(Exception):
    """The RPC provider refused our key (HTTP 401/403)."""


class ChainError(Exception):
    def __init__(self, status: int, msg: str = ""):
        super().__init__(f"rpc http {status} {msg}".strip())
        self.status = status


# ---- pure parsers (tested on real recorded transactions) ------------------------------------------
def signers(tx: dict) -> list[str]:
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k["pubkey"] for k in keys if isinstance(k, dict) and k.get("signer")]


def trader_of(tx: dict, fee_payer: str) -> str | None:
    """The FOMO trader's real wallet: the signer that is not FOMO's fee payer (None if FOMO did not co-sign)."""
    s = signers(tx)
    if fee_payer not in s:
        return None
    other = [a for a in s if a != fee_payer]
    return other[0] if other else None


def _amount(b: dict) -> float:
    ui = b.get("uiTokenAmount") or {}
    try:
        return int(ui["amount"]) / 10 ** int(ui.get("decimals") or 0)
    except (KeyError, TypeError, ValueError):
        try:
            return float(ui.get("uiAmountString") or 0)
        except (TypeError, ValueError):
            return 0.0


def balances(tx: dict, wallet: str) -> dict[str, tuple[float, float]]:
    """Token balances of `wallet` before and after this transaction, by mint (a missing account counts as 0)."""
    meta = (tx or {}).get("meta") or {}
    d: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for i, arr in ((0, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("owner") == wallet and b.get("mint"):
                d[b["mint"]][i] += _amount(b)
    return {m: (v[0], v[1]) for m, v in d.items()}


def deltas(tx: dict, wallet: str) -> dict[str, float]:
    return {m: post - pre for m, (pre, post) in balances(tx, wallet).items()}


DUST_REL = 0.10          # a routed swap can leave a speck of the intermediate token: it moves < 10% of its balance


def parse_tx(tx: dict, wallet: str, sig: str = "") -> list[Leg]:
    """The swap legs of `wallet` in one transaction ([] for failed transactions, transfers, deposits, SOL-priced or
    ambiguous swaps). If several tokens move the trade's way (a routed swap leaving dust of the intermediate token),
    the one whose balance changed most in relative terms is the traded token, provided every other one changed by
    less than DUST_REL of its balance; otherwise the transaction is ambiguous and skipped."""
    if not tx or (tx.get("meta") or {}).get("err") is not None or not tx.get("blockTime"):
        return []
    sig = sig or ((tx.get("transaction") or {}).get("signatures") or [""])[0]
    bal = balances(tx, wallet)
    usd = sum(bal[m][1] - bal[m][0] for m in USD if m in bal)   # > 0: received USD (a sell), < 0: paid (a buy)
    if abs(usd) < MIN_USD:
        return []
    want = 1 if usd < 0 else -1                        # a buy receives the token, a sell gives it away
    toks = []
    for m, (pre, post) in bal.items():
        v = post - pre
        if m not in USD and m != WSOL and v * want > 0:
            toks.append((abs(v) / max(pre, post), m, v))
    if not toks:
        return []
    toks.sort(reverse=True)
    if len(toks) > 1 and toks[1][0] >= DUST_REL:
        return []
    _, mint, v = toks[0]
    return [Leg(sig, int(tx["blockTime"]) * 1000, mint, "buy" if want > 0 else "sell", abs(v), abs(usd))]


# ---- JSON-RPC client ---------------------------------------------------------------------------------
class Rpc:
    """Throttled JSON-RPC over HTTP POST. `url` may carry an API key: it is never logged."""

    def __init__(self, url: str, min_interval_s: float = 0.3, timeout_s: float = 20.0, name: str = "rpc"):
        self.url, self.min_interval, self.timeout, self.name = url, min_interval_s, timeout_s, name
        self._lock = threading.Lock()
        self._last = 0.0
        self.blocked_until = 0.0
        self.calls = 0

    def _slot(self) -> None:
        """Wait for this request's start time. Requests from several threads are spaced by min_interval but run
        concurrently (the endpoint's answer time does not slow the others down)."""
        with self._lock:
            start = max(self._last + self.min_interval, self.blocked_until, time.monotonic())
            self._last = start
            self.calls += 1
        wait = start - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def call(self, method: str, params: list, timeout: float | None = None, cost: int = 1):
        for attempt in range(4):
            self._slot()
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(self.url, body, {"Content-Type": "application/json",
                                                          "User-Agent": "copybot-paper/1"})
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    out = json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise AuthError(f"{self.name} refused the key (http {e.code})") from None
                if e.code == 429 or e.code >= 500:
                    self.blocked_until = time.monotonic() + 2 * (attempt + 1)
                    continue
                raise ChainError(e.code, method) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < 1:
                    continue
                raise ChainError(0, f"network: {type(e).__name__}") from None
            if "error" in out:
                err = out["error"] or {}
                if err.get("code") in (429, -32429) or "rate" in str(err.get("message", "")).lower():
                    self.blocked_until = time.monotonic() + 2 * (attempt + 1)
                    continue
                raise ChainError(200, f"{method}: {str(err.get('message', ''))[:120]}")
            return out.get("result")
        raise ChainError(429, f"{method}: rate limited")

    def signatures(self, addr: str, limit: int = 1000, before: str | None = None) -> list[dict]:
        p: dict = {"limit": limit, "commitment": "confirmed"}
        if before:
            p["before"] = before
        return self.call("getSignaturesForAddress", [addr, p]) or []

    def transaction(self, sig: str) -> dict | None:
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                                                  "commitment": "confirmed"}])


class FallbackRpc(Rpc):
    """`primary` (Helius: fast, costs credits) until `daily_credits` were spent on it this UTC day, then `fallback`
    (the free public endpoint). Used for history and discovery, so a big search can never spend the month's credits."""

    def __init__(self, primary: Rpc, fallback: Rpc, daily_credits: int):
        self.primary, self.fallback, self.daily_credits = primary, fallback, daily_credits
        self.name = f"{primary.name}, then {fallback.name}"
        self._lock = threading.Lock()
        self.day, self.used = "", 0

    @property
    def calls(self) -> int:
        return self.primary.calls + self.fallback.calls

    def on_primary(self, cost: int = 1) -> bool:
        with self._lock:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if today != self.day:
                self.day, self.used = today, 0
            if self.used + cost > self.daily_credits:
                return False
            self.used += cost
            return True

    def call(self, method: str, params: list, timeout: float | None = None, cost: int = 1):
        if self.on_primary(cost):
            return self.primary.call(method, params, timeout, cost)
        return self.fallback.call(method, params, timeout, cost)


class ChainClient:
    """Wallet swaps from the chain. Each transaction is fetched once (its legs are cached by signature).
    `parallel` transactions are read at the same time (the Rpc still spaces the requests to its rate limit)."""

    BULK_PAGE = 100
    BULK_RETRY_S = 6 * 3600

    def __init__(self, rpc: Rpc, cache_size: int = 20_000, parallel: int = 1, bulk: bool = False):
        """`bulk`: the endpoint may offer Helius' getTransactionsForAddress (100 full transactions per call, 10
        credits). If it refuses, the client reads transactions one by one and tries bulk again 6 hours later."""
        self.rpc = rpc
        self._legs: OrderedDict[str, tuple[Leg, ...]] = OrderedDict()
        self._lock = threading.Lock()
        self.cache_size = cache_size
        self.parallel = max(1, parallel)
        self.bulk = bulk
        self.bulk_off_until = 0.0

    def _remember(self, key: str, legs: tuple[Leg, ...]) -> None:
        with self._lock:
            self._legs[key] = legs
            while len(self._legs) > self.cache_size:
                self._legs.popitem(last=False)

    def bulk_ready(self) -> bool:
        return self.bulk and time.time() >= self.bulk_off_until

    def bulk_legs(self, wallet: str, want: list[dict]) -> list[Leg]:
        """Legs of the wanted signature infos via getTransactionsForAddress, newest first, 100 per call. Any wanted
        transaction the pages did not return is read on its own."""
        need = {s["signature"] for s in want}
        oldest = min((s.get("blockTime") or 0) for s in want)
        out: list[Leg] = []
        token = None
        while need:
            opts: dict = {"transactionDetails": "full", "sortOrder": "desc", "limit": self.BULK_PAGE,
                          "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}
            if token:
                opts["paginationToken"] = token
            res = self.rpc.call("getTransactionsForAddress", [wallet, opts], cost=10) or {}
            items = res.get("data") or []
            for tx in items:
                sig = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
                if sig in need:
                    need.discard(sig)
                    legs = tuple(parse_tx(tx, wallet, sig))
                    self._remember(f"{wallet}:{sig}", legs)
                    out.extend(legs)
            token = res.get("paginationToken")
            if not items or not token or (items[-1].get("blockTime") or 0) < oldest:
                break
        if need:
            out += self.legs_many(sorted(need), wallet, missing_ok=True)
        return sorted(out, key=lambda g: (g.ts, g.id))

    def bulk_failed(self, e: Exception) -> None:
        self.bulk_off_until = time.time() + self.BULK_RETRY_S
        log.warn("sol_bulk_unavailable", err=f"{type(e).__name__}: {e}"[:160], retry_h=self.BULK_RETRY_S // 3600)

    def legs_of(self, sig: str, wallet: str, missing_ok: bool = False) -> tuple[Leg, ...]:
        """`missing_ok`: an old transaction the node does not return is skipped (history) instead of raising
        (live reads raise, so the poller retries a transaction the node has not indexed yet)."""
        key = f"{wallet}:{sig}"
        with self._lock:
            if key in self._legs:
                return self._legs[key]
        tx = self.rpc.transaction(sig)
        if tx is None:                         # not visible yet at this node: do not cache, retry next time
            if missing_ok:
                return ()
            raise ChainError(0, "transaction not found yet")
        legs = tuple(parse_tx(tx, wallet, sig))
        self._remember(key, legs)
        return legs

    def legs_many(self, sigs: list[str], wallet: str, missing_ok: bool = False) -> list[Leg]:
        if self.parallel <= 1 or len(sigs) <= 1:
            out = [self.legs_of(s, wallet, missing_ok) for s in sigs]
        else:
            with ThreadPoolExecutor(self.parallel) as ex:
                out = list(ex.map(lambda s: self.legs_of(s, wallet, missing_ok), sigs))
        return sorted((g for legs in out for g in legs), key=lambda g: (g.ts, g.id))

    def swaps(self, wallet: str, limit: int = 25) -> tuple[list[Leg], bool]:
        """The wallet's swaps among its newest `limit` transactions -> (legs oldest first, page was full)."""
        sigs = self.rpc.signatures(wallet, limit)
        legs = self.legs_many([s["signature"] for s in sigs if s.get("err") is None], wallet)
        return legs, len(sigs) >= limit


@dataclass
class History:
    legs: list[Leg] = field(default_factory=list)      # oldest first
    newest_sig: str = ""                               # newest transaction looked at (the next update stops there)
    complete: bool = False                             # reaches back to `since` (or the wallet's first transaction)
    n_sigs: int = 0
    span_ms: int = 0                                   # time covered by the signatures read


def signatures_until(rpc: Rpc, wallet: str, since_ms: int, stop_sig: str = "", max_sigs: int = 2000
                     ) -> tuple[list[dict], bool]:
    """Newest-first signatures back to `since_ms`, `stop_sig` (exclusive) or `max_sigs` -> (sigs, complete)."""
    out: list[dict] = []
    before = None
    while len(out) < max_sigs:
        n = min(1000, max_sigs - len(out))
        page = rpc.signatures(wallet, n, before)
        for s in page:
            if s["signature"] == stop_sig or (s.get("blockTime") or 0) * 1000 < since_ms:
                return out, True
            out.append(s)
        if len(page) < n:
            return out, True                           # reached the wallet's first transaction
        before = page[-1]["signature"]
    return out, False


def fetch_history(client: ChainClient, wallet: str, since_ms: int, stop_sig: str = "", max_sigs: int = 2000,
                  min_span_ms: int = 0) -> History:
    """Swaps since `since_ms` (or newer than `stop_sig`). If the newest `max_sigs` transactions cover less than
    `min_span_ms` the wallet is too busy to score and no transaction is downloaded (History.complete False)."""
    sigs, complete = signatures_until(client.rpc, wallet, since_ms, stop_sig, max_sigs)
    h = History(newest_sig=sigs[0]["signature"] if sigs else stop_sig, complete=complete, n_sigs=len(sigs))
    times = [s["blockTime"] * 1000 for s in sigs if s.get("blockTime")]
    h.span_ms = (max(times) - min(times)) if times else 0
    if not complete and h.span_ms < min_span_ms:
        return h
    want = [s for s in sigs if s.get("err") is None]
    with client._lock:
        todo = [s for s in want if f"{wallet}:{s['signature']}" not in client._legs]
    if client.bulk_ready() and len(todo) > 10:
        try:
            client.bulk_legs(wallet, todo)
        except (ChainError, AuthError) as e:          # not offered (plan, endpoint): one by one instead
            if not (isinstance(e, ChainError) and e.status == 429):     # busy is not "not offered"
                client.bulk_failed(e)
    h.legs = client.legs_many([s["signature"] for s in want], wallet, missing_ok=True)
    return h


# ---- discovering FOMO traders ---------------------------------------------------------------------------
def sample_fomo(client: ChainClient, fee_payer: str, pages: int, per_page: int, rng: random.Random | None = None,
                stop: threading.Event | None = None) -> list[tuple[str, list[Leg]]]:
    """Walk back `pages` pages (1000 transactions each, about 2 minutes of FOMO trading) of FOMO's fee payer and
    open `per_page` random transactions of each -> [(trader wallet, its legs in that transaction)]."""
    rng = rng or random.Random()
    out: list[tuple[str, list[Leg]]] = []
    before = None

    def one(sig: str) -> tuple[str, list[Leg]] | None:
        try:
            tx = client.rpc.transaction(sig)
        except ChainError:                 # one unreadable transaction does not stop the sampling
            return None
        w = trader_of(tx, fee_payer) if tx else None
        return (w, parse_tx(tx, w, sig)) if w else None

    with ThreadPoolExecutor(client.parallel) as ex:
        for _ in range(pages):
            if stop is not None and stop.is_set():
                break
            page = client.rpc.signatures(fee_payer, 1000, before)
            if not page:
                break
            before = page[-1]["signature"]
            ok = [s["signature"] for s in page if s.get("err") is None]
            out += [r for r in ex.map(one, rng.sample(ok, min(per_page, len(ok)))) if r]
    return out


# ---- live alerts ---------------------------------------------------------------------------------------
class LogWatch:
    """logsSubscribe(mentions=[wallet]) for each wanted wallet; calls `on_tx(wallet)` when one transacts.
    Only a wake-up: the poller still reads the swaps (so a dead websocket never loses a trade, it only adds lag)."""

    def __init__(self, url: str, wanted: Callable[[], set[str]], on_tx: Callable[[str], None],
                 stop: threading.Event):
        self.url, self.wanted, self.on_tx, self.stop = url, wanted, on_tx, stop
        self.connected = False
        self.notes = 0

    def start(self) -> None:
        threading.Thread(target=self.run, name="sol-ws", daemon=True).start()

    def run(self) -> None:
        from websockets.sync.client import connect
        backoff = 2.0
        while not self.stop.is_set():
            try:
                with connect(self.url, open_timeout=15, close_timeout=2, max_size=2 ** 22) as ws:
                    subs: dict[str, int] = {}          # wallet -> subscription id
                    pending: dict[int, str] = {}       # request id -> wallet
                    rid = 0
                    self.connected = True
                    backoff = 2.0
                    log.info("sol_ws_connected")
                    last_ping = time.time()
                    while not self.stop.is_set():
                        want = self.wanted()
                        for w in sorted(want - set(subs) - set(pending.values())):
                            rid += 1
                            pending[rid] = w
                            ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                                                "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                        for w in sorted(set(subs) - want):
                            rid += 1
                            ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "logsUnsubscribe",
                                                "params": [subs.pop(w)]}))
                        if time.time() - last_ping > 30:
                            ws.ping()
                            last_ping = time.time()
                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        msg = json.loads(raw)
                        if "id" in msg and msg["id"] in pending:
                            w = pending.pop(msg["id"])
                            if isinstance(msg.get("result"), int):
                                subs[w] = msg["result"]
                        elif msg.get("method") == "logsNotification":
                            sub = ((msg.get("params") or {}).get("subscription"))
                            for w, s in subs.items():
                                if s == sub:
                                    self.notes += 1
                                    self.on_tx(w)
            except Exception as e:
                if not self.stop.is_set():
                    log.warn("sol_ws_down", err=type(e).__name__)
            self.connected = False
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)
