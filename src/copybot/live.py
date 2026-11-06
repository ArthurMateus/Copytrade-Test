"""REAL-money copies on Hyperliquid (owner request 2026-11-03; docs/PRODUCTION_PLAN.md, Phase 1).

The ONLY module that signs anything or reads a key (tests/test_safety.py enforces it). On the exchange it can only
place and cancel orders and set a coin's leverage: nothing that moves money out of the account exists anywhere (also
enforced). The key is an API / agent wallet's (HL_AGENT_KEY): Hyperliquid lets it trade the account HL_ACCOUNT_ADDRESS
but never take money out, so a leaked key cannot empty it. The main wallet's own key is refused.

- `Exchange`: signed actions (signed by Hyperliquid's own SDK, hyperliquid.utils.signing) and the account's reads.
- `LiveBroker`: the same shape as broker.PaperBroker, so the engine (positions.py, allocations.py), the RiskGate and the
  ledger are the paper ones. Entries are IOC limit orders at most `live.entry_slippage_bps` from the mid (not filled =
  skipped, like a missing book); exits are reduce-only IOC orders widening 1x / 3x / 8x `live.exit_slippage_bps` until
  filled. The part of an exit the exchange already did (our resting stop fired) is booked at its real fills.
- `Desk`: the live wallet's duties: the stop of every position also rests ON the exchange (it fires while the bot or
  the PC is down), the ledger is compared with the real account every `live.reconcile_s` (a mismatch pauses entries),
  the daily stop (held until the owner's next day), the floor, /livekill, /livestatus.
Fees are booked at broker.taker_fee_pct (the exchange's real fee is in its fills; /livestatus shows the real account
value next to the ledger's).
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from copybot import cardfmt, hl, log
from copybot.broker import NoBook, PaperFill
from copybot.config import Live as LiveCfg

NETWORKS = {"testnet": "https://api.hyperliquid-testnet.xyz", "mainnet": "https://api.hyperliquid.xyz"}
ENV_ACCOUNT, ENV_KEY = "HL_ACCOUNT_ADDRESS", "HL_AGENT_KEY"
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
EXIT_STEPS = (1, 3, 8)          # exit slippage multipliers, tried in turn until the exit fills
STOP_LIMIT = 0.10               # a resting stop fires as a market order filling at most 10% past its trigger
MIN_ORDER_USD = 10.0            # Hyperliquid's minimum order value
DAILY_STOP = "live_daily_stop:"  # pause reason + the owner's day it happened (resumed when that day is over)


class LiveError(Exception):
    """The exchange refused (or did not answer: LiveUnknown)."""


class LiveUnknown(LiveError):
    """No answer: the order may have been placed and filled anyway. Orders are then looked up by their cloid (code
    review 2026-11-03: a timed-out entry that filled was an unprotected position the ledger did not know)."""


def new_cloid() -> str:
    return "0x" + uuid.uuid4().hex


def credentials(env=None) -> tuple[str, str] | str:
    """(account address, agent key) from the environment, or why live trading cannot start."""
    env = os.environ if env is None else env
    acct, key = env.get(ENV_ACCOUNT, "").strip(), env.get(ENV_KEY, "").strip()
    if not acct or not key:
        return f"{ENV_ACCOUNT} and {ENV_KEY} must both be set"
    if not _ADDRESS.fullmatch(acct):
        return f"{ENV_ACCOUNT} is not an address (0x and 40 hex digits)"
    log.add_secret(key)
    log.add_secret(key.removeprefix("0x"))
    return acct.lower(), key


def px_wire(px: float, sz_decimals: int) -> float:
    """Hyperliquid prices: at most 5 significant figures and 6 - szDecimals decimals (whole numbers always allowed)."""
    if px >= 100_000:
        return float(round(px))
    return round(float(f"{px:.5g}"), max(0, 6 - sz_decimals))


class Exchange:
    def __init__(self, network: str, account: str, key: str, timeout: float = 5.0, base: str | None = None):
        # imported here: the paper bot never loads a signing library (tests/test_safety.py)
        from eth_account import Account
        from hyperliquid.utils import signing
        from hyperliquid.utils.types import Cloid
        self._signing, self._cloid = signing, Cloid
        self._wallet = Account.from_key(key)
        self.agent = self._wallet.address.lower()
        if self.agent == account:
            raise LiveError(f"{ENV_KEY} is the MAIN wallet's key: create an API wallet on app.hyperliquid.xyz "
                            "(More > API) and use its key instead (an API wallet cannot take money out)")
        self.account, self.network, self.mainnet = account, network, network == "mainnet"
        base = (base or NETWORKS[network]).rstrip("/")
        self.info = hl.Info(base + "/info", hl.RateBudget(1200, 300))
        self.url = base + "/exchange"
        self.timeout = timeout
        self.assets: dict[str, hl.Asset] = {}
        self.leverage: dict[str, int] = {}       # coin -> isolated leverage set this session
        self._nonce = 0
        self._lock = threading.Lock()

    # ---- reads ----------------------------------------------------------------------------------
    def _read(self, body: dict):
        return self.info.post(body, hl.CRITICAL, self.timeout)

    def check_agent(self) -> None:
        """The key must be an API wallet approved for this account on this network."""
        r = self._read({"type": "userRole", "user": self.agent}) or {}
        master = str((r.get("data") or {}).get("user", "")).lower()
        if r.get("role") != "agent" or master != self.account:
            raise LiveError(f"the API wallet {self.agent} is not approved for {self.account} on Hyperliquid "
                            f"{self.network} (role: {r.get('role')})")

    def load_meta(self) -> dict[str, hl.Asset]:
        self.assets.update(hl.parse_meta(self._read({"type": "metaAndAssetCtxs"})))
        return self.assets

    def asset(self, coin: str) -> hl.Asset:
        a = self.assets.get(coin)
        if a is None or a.delisted or a.index < 0:
            raise NoBook(f"{coin} is not listed on Hyperliquid {self.network}")
        return a

    def state(self) -> tuple[dict[str, float], float]:
        """({coin: signed size}, account value) of the real account."""
        r = self._read({"type": "clearinghouseState", "user": self.account}) or {}
        return hl.parse_positions(r), float((r.get("marginSummary") or {}).get("accountValue") or 0)

    def open_orders(self) -> list[dict]:
        return list(self._read({"type": "frontendOpenOrders", "user": self.account}) or [])

    def fills_since(self, ms: int) -> list[hl.Fill]:
        return hl.parse_fills(self._read({"type": "userFillsByTime", "user": self.account, "startTime": int(ms)}) or [])

    def book(self, coin: str) -> hl.Book:
        return self.info.book(coin, self.timeout)

    def mids(self) -> dict[str, float]:
        return {k: float(v) for k, v in (self._read({"type": "allMids"}) or {}).items() if hl.is_core_perp(k)}

    # ---- signed actions -------------------------------------------------------------------------
    def act(self, action: dict) -> dict:
        with self._lock:
            self._nonce = nonce = max(int(time.time() * 1000), self._nonce + 1)
        sig = self._signing.sign_l1_action(self._wallet, action, None, nonce, None, self.mainnet)
        body = {"action": action, "nonce": nonce, "signature": sig, "vaultAddress": None, "expiresAfter": None}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                res = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code >= 500:                           # a proxy/gateway error: the order may have been placed
                raise LiveUnknown(f"{action['type']}: HTTP {e.code}") from None
            raise LiveError(f"{action['type']}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}") from None
        except (OSError, ValueError) as e:              # no answer, timeout, not JSON: it may have been done
            raise LiveUnknown(f"{action['type']}: {type(e).__name__}: {e}") from None
        if not isinstance(res, dict) or res.get("status") != "ok":
            raise LiveError(f"{action['type']} refused: {str(res)[:200]}")
        return res.get("response") or {}

    def order(self, coin: str, is_buy: bool, sz: float, limit_px: float, reduce_only: bool = False,
              trigger_px: float | None = None, cloid: str | None = None) -> dict:
        """One IOC limit order, or (trigger_px) a resting stop-market order. Returns the exchange's status of it:
        {"filled": {...}}, {"resting": {"oid": n}} or {"error": "..."}. LiveUnknown: look it up by `cloid`."""
        a = self.asset(coin)
        kind = ({"trigger": {"triggerPx": px_wire(trigger_px, a.sz_decimals), "isMarket": True, "tpsl": "sl"}}
                if trigger_px else {"limit": {"tif": "Ioc"}})
        cloid = self._cloid.from_str(cloid or new_cloid())
        wire = self._signing.order_request_to_order_wire(
            {"coin": coin, "is_buy": is_buy, "sz": round(sz, a.sz_decimals), "limit_px": px_wire(limit_px, a.sz_decimals),
             "order_type": kind, "reduce_only": reduce_only, "cloid": cloid}, a.index)
        log.info("live_order", coin=coin, buy=is_buy, sz=round(sz, a.sz_decimals), limit=px_wire(limit_px, a.sz_decimals),
                 reduce_only=reduce_only, trigger=trigger_px or "", cloid=str(cloid))
        res = self.act(self._signing.order_wires_to_order_action([wire]))
        statuses = (res.get("data") or {}).get("statuses") or [{}]
        return statuses[0] if isinstance(statuses[0], dict) else {"error": str(statuses[0])}

    def filled_by_cloid(self, cloid: str, since_ms: int) -> tuple[float, float, float] | None:
        """(size, average price, fee) an order really filled, found by its cloid (LiveError if the account cannot be
        read). None: the exchange does not (yet) know the order. The size comes from the order's own status
        (original minus remaining), the price from its fills (its limit price while the fills are not listed yet) and
        the fee from the fills (0: the caller estimates it)."""
        r = self._read({"type": "orderStatus", "user": self.account, "oid": cloid}) or {}
        if r.get("status") != "order":
            return None
        o = (r.get("order") or {}).get("order") or {}
        oid = int(o.get("oid", -1))
        fills = [f for f in self.fills_since(since_ms) if f.oid == oid]
        fsz = sum(f.sz for f in fills)
        sz = fsz
        if o.get("origSz") is not None and o.get("sz") is not None:
            sz = max(fsz, float(o["origSz"]) - float(o["sz"]))
        if sz <= 0:
            return 0.0, 0.0, 0.0
        if fsz > 0:
            return sz, sum(f.sz * f.px for f in fills) / fsz, sum(f.fee for f in fills)
        return sz, float(o.get("limitPx") or 0), 0.0

    def cancel(self, coin: str, oid: int) -> None:
        self.act({"type": "cancel", "cancels": [{"a": self.asset(coin).index, "o": int(oid)}]})

    def set_leverage(self, coin: str, leverage: float) -> None:
        """Isolated margin at the leverage the RiskGate chose, so the real liquidation sits where the gate expects."""
        lev = max(1, int(leverage))
        if self.leverage.get(coin) == lev:
            return
        self.act({"type": "updateLeverage", "asset": self.asset(coin).index, "isCross": False, "leverage": lev})
        self.leverage[coin] = lev


class LiveBroker:
    """broker.PaperBroker's shape on the real exchange."""

    def __init__(self, ex: Exchange, cfg: LiveCfg, taker_fee_pct: float):
        self.ex, self.cfg, self.fee_pct = ex, cfg, taker_fee_pct
        self.held = lambda coin: (0.0, 0)    # (our size in the ledger, opened ms) of a coin: set by Desk.attach
        self.stop_oids = lambda coin: set()  # our stop orders' ids on a coin (Desk): what a fired stop filled
        self.exit_wait: dict[str, tuple[float, int]] = {}   # coin -> (no new exit try before, failures in a row)
        self.on_unknown = lambda coin: None  # an entry the exchange never answered (Desk.attach pauses entries)

    def can_exit(self, coin: str) -> bool:
        """A failing exit is retried after 5 s, 30 s, then every 2 min (code review: not every 0.25 s tick, each
        writing an intent and up to 7 requests; the exchange stop still holds the position meanwhile)."""
        return time.time() >= self.exit_wait.get(coin, (0.0, 0))[0]

    def _exit_failed(self, coin: str) -> None:
        n = self.exit_wait.get(coin, (0.0, 0))[1] + 1
        self.exit_wait[coin] = (time.time() + (5, 30, 120)[min(n, 3) - 1], n)

    def market(self, coin: str, is_buy: bool, size: float, exit: bool = False, mid: float | None = None,
               leverage: float | None = None) -> PaperFill:
        if not exit:
            return self._entry(coin, is_buy, size, leverage)
        try:
            f = self._exit(coin, is_buy, size, mid)
        except Exception:
            self._exit_failed(coin)
            raise
        self.exit_wait.pop(coin, None)
        return f

    def _send(self, coin: str, is_buy: bool, sz: float, limit: float, reduce_only: bool, what: str):
        """An IOC order -> (size, price, fee). No answer: what it really did, from its cloid."""
        cloid, t0 = new_cloid(), int(time.time() * 1000) - 5_000
        try:
            sz, px = self._filled(self.ex.order(coin, is_buy, sz, limit, reduce_only, cloid=cloid), coin, what)
            return sz, px, px * sz * self.fee_pct / 100
        except LiveUnknown as e:
            log.warn("live_order_unanswered", coin=coin, what=what, cloid=cloid, err=str(e)[:120])
            for wait in ((0.5, 1.5, 3.0) if what == "entry" else (0.5, 1.5)):
                time.sleep(wait)
                try:
                    got = self.ex.filled_by_cloid(cloid, t0)
                except LiveError:
                    continue
                if got is None:                  # not listed yet: it may still be indexed
                    continue
                if got[0] > 0 and got[2] <= 0:
                    got = (got[0], got[1], got[0] * got[1] * self.fee_pct / 100)
                log.warn("live_order_found", coin=coin, cloid=cloid, sz=got[0], px=got[1])
                return got
            raise                                # never seen: unknown, the caller pauses and the reconcile guards

    @staticmethod
    def _filled(st: dict, coin: str, what: str) -> tuple[float, float]:
        if "filled" in st:
            return float(st["filled"]["totalSz"]), float(st["filled"]["avgPx"])
        log.warn("live_not_filled", coin=coin, what=what, why=st.get("error", str(st)[:120]))
        return 0.0, 0.0

    def _fill(self, coin, is_buy, ref, parts) -> PaperFill:
        sz = sum(s for s, _, _ in parts)
        px = sum(s * p for s, p, _ in parts) / sz
        fee = sum(f for _, _, f in parts)
        slip = (px - ref) / ref * 1e4 * (1 if is_buy else -1) if ref else 0.0
        log.info("live_fill", coin=coin, buy=is_buy, sz=sz, px=px, fee=round(fee, 4), slip_bps=round(slip, 2))
        return PaperFill(px, sz, fee, slip, "live")

    def _entry(self, coin: str, is_buy: bool, size: float, leverage: float | None) -> PaperFill:
        self.ex.asset(coin)
        try:
            ref = self.ex.book(coin).mid
        except Exception as e:
            raise NoBook(f"{coin}: no live order book ({type(e).__name__})") from None
        held, _ = self.held(coin)
        a = self.ex.assets[coin]
        room = self.cfg.max_position_usd / ref - held       # trimmed to the cap (values review: not refused whole)
        if size > room:
            size = math.floor(room * 10 ** a.sz_decimals + 1e-9) / 10 ** a.sz_decimals
            if size * ref < MIN_ORDER_USD:
                raise NoBook(f"{coin}: at the live position cap of {self.cfg.max_position_usd:g}$")
            log.info("live_capped", coin=coin, size=size, cap=self.cfg.max_position_usd)
        if leverage:
            try:
                self.ex.set_leverage(coin, leverage)
            except LiveError as e:
                if not held:                     # (an open isolated position may refuse a change: keep its own)
                    raise NoBook(f"{coin}: leverage not set ({e})") from None
        limit = ref * (1 + (1 if is_buy else -1) * self.cfg.entry_slippage_bps / 1e4)
        try:
            sz, px, fee = self._send(coin, is_buy, size, limit, False, "entry")
        except LiveUnknown:                      # unknown even after looking: no more entries until the owner looks,
            self.on_unknown(coin)                # and the reconcile guards what it finds
            raise
        except LiveError as e:                   # the exchange answered "no": nothing was placed
            raise NoBook(f"{coin}: live entry refused ({str(e)[:120]})") from None
        if sz <= 0:
            raise NoBook(f"{coin}: live entry not filled within {self.cfg.entry_slippage_bps:g} bps of the mid")
        return self._fill(coin, is_buy, ref, [(sz, px, fee)])

    def _exit(self, coin: str, is_buy: bool, size: float, mid: float | None) -> PaperFill:
        a = self.ex.assets.get(coin)
        if a is None:
            raise LiveError(f"{coin}: not in the live coin list, cannot exit (the exchange stop order still holds)")
        lot = 10 ** -a.sz_decimals
        held, opened = self.held(coin)
        try:                                     # what the account really holds on our side
            pos, _ = self.ex.state()
            e = pos.get(coin, 0.0)
            real = abs(e) if (e < 0) == is_buy and e else 0.0
        except Exception as ex:
            log.warn("live_state_unread", coin=coin, err=str(ex)[:120])
            real = held                          # unknown: sell what we hold (reduce-only can never overshoot)
        parts = []
        gone = min(size, max(0.0, held - real))
        if gone > lot / 2:                       # the exchange already closed this part (our resting stop fired)
            parts.append(self._gone(coin, is_buy, gone, opened, mid))
        left = round(size - gone, a.sz_decimals)
        ref = mid or 0.0
        for mult in EXIT_STEPS:
            if left < lot / 2:
                break
            try:
                ref = self.ex.book(coin).mid
            except Exception:
                ref = mid or ref
            if not ref:
                continue
            limit = ref * (1 + (1 if is_buy else -1) * mult * self.cfg.exit_slippage_bps / 1e4)
            try:
                sz, px, fee = self._send(coin, is_buy, left, limit, True, "exit")
            except LiveError as e:
                log.warn("live_exit_failed", coin=coin, try_=mult, err=str(e)[:160])
                continue
            if sz > 0:
                parts.append((sz, px, fee))
                left = round(left - sz, a.sz_decimals)
        if left >= lot / 2 or not parts:         # (a part that did fill is booked by the reconcile from the account)
            raise LiveError(f"{coin}: exit not filled, {left:g} left (the stop order on the exchange still holds it)")
        return self._fill(coin, is_buy, ref or parts[0][1], parts)

    def _gone(self, coin: str, is_buy: bool, size: float, opened_ms: int, mid: float | None):
        """(size, price, fee) of the part the exchange closed without us, from the account's real fills."""
        try:
            fills = [f for f in self.ex.fills_since(opened_ms - 60_000) if f.coin == coin
                     and f.side == ("B" if is_buy else "A") and ("Close" in f.dir or "Liquidat" in f.dir)]
            ours = self.stop_oids(coin)
            if any(f.oid in ours for f in fills):     # our stop's own fills (code review: not any recent close)
                fills = [f for f in fills if f.oid in ours]
        except Exception as e:
            log.warn("live_fills_unread", coin=coin, err=str(e)[:120])
            fills = []
        took, cost, fee = 0.0, 0.0, 0.0
        for f in sorted(fills, key=lambda f: -f.time):
            take = min(f.sz, size - took)
            took, cost, fee = took + take, cost + take * f.px, fee + f.fee * take / f.sz
            if took >= size - 1e-12:
                break
        if took <= 0:
            px = mid or 0.0
            if not px:
                try:
                    px = self.ex.book(coin).mid
                except Exception:
                    px = 0.0
            log.warn("live_exit_by_exchange", coin=coin, size=size, px=px, fills="none found")
            return size, px, px * size * self.fee_pct / 100
        px = cost / took
        log.warn("live_exit_by_exchange", coin=coin, size=size, px=px, fills=len(fills))
        return size, px, fee * size / took


class Desk:
    """The live wallet's own duties, run from the trading loop (Bot.tick)."""

    def __init__(self, ex: Exchange, cfg: LiveCfg, broker: LiveBroker, say, shared_mids: dict):
        """`say(html, key, every_s)`: a Discord message, at most once per `every_s` per key (0 = every time)."""
        self.ex, self.cfg, self.broker, self.say = ex, cfg, broker, say
        self.mids = shared_mids if ex.mainnet else {}   # testnet prices differ from the paper wallets' mainnet ones
        self.assets = ex.assets
        self.w = None                    # the live SideWallet (attach)
        self.twin = None                 # the paper wallet it copies
        self.stops: dict[str, list[dict]] = {}   # coin -> our resting stop orders on the exchange
        self.stop_ids: dict[str, set] = {}       # coin -> every stop order id placed (what a fired stop filled)
        self.real: dict[str, float] = {}         # coin -> what the account holds on our side (last reconcile)
        self.dirty: set[str] = set()     # coins whose exchange stop must be (re)placed
        self.problems: list[str] = []
        self.account_value: float | None = None
        self.floor_armed = True
        self.last_reconcile = self.last_mids = self.last_meta = self.last_retry = 0.0
        self.last_ok = time.time()       # the last reconcile that read the account (the bot's watchdog)

    def attach(self, w, twin) -> None:
        self.w, self.twin = w, twin
        self.broker.held = lambda c: ((w.st.positions[c].size, w.st.positions[c].opened_ms)
                                      if c in w.st.positions else (0.0, 0))
        self.broker.stop_oids = lambda c: self.stop_ids.get(c, set())
        self.broker.on_unknown = self._unknown_order

    def _unknown_order(self, coin: str) -> None:
        if not self.w.st.entries_paused:
            self.w.rec({"ev": "pause", "reason": "live_unknown_order"})
        self.say(f"⚠️ 💵 <b>LIVE {cardfmt.esc(coin)}: an order got no answer</b> · ⏸️ new live entries paused. The "
                 "reconcile guards whatever filled; check Hyperliquid, then /liveresume.", "live_unknown", 0)

    def boot(self) -> None:
        """Raises LiveError when the account cannot be traded (the caller pauses the live wallet's entries)."""
        self.ex.check_agent()
        self.ex.load_meta()
        self.last_meta = time.time()
        if not self.ex.mainnet:
            self.mids.update(self.ex.mids())
            self.last_mids = time.time()
        self.floor_armed = self.w.st.equity(self.mids) > self.cfg.floor_usd
        self.reconcile()

    # ---- every tick -------------------------------------------------------------------------------
    def tick(self) -> None:
        now = time.time()
        if not self.ex.mainnet and now - self.last_mids >= 1.0:
            self.last_mids = now
            try:                                  # (a testnet outage must not skip the duties below)
                self.mids.update(self.ex.mids())
            except Exception as e:
                self.last_mids = now + 10         # next try in ~11 s
                log.warn("live_mids_failed", err=str(e)[:120])
        if now - self.last_meta >= 3600:          # funding rates and listings
            self.last_meta = now
            self.ex.load_meta()
        self.check_floor()
        self.check_day()
        if self.dirty and now - self.last_retry >= 5.0:   # a stop the exchange refused: retried every 5 s
            self.last_retry = now
            for coin in sorted(self.dirty):
                self.sync_stop(coin)
        if now - self.last_reconcile >= self.cfg.reconcile_s:
            self.reconcile()

    def check_day(self) -> None:
        """The daily stop, held for the rest of the owner's day (values review: the RiskGate alone lets entries back in
        once the day climbs back above -15%). The day mark is the wallet at the day's start: NET from it, not from the
        day's best."""
        st, day = self.w.st, self.w.st.marks.get("day")
        if day is None or day["equity"] <= 0:
            return
        if st.entries_paused and st.pause_reason.startswith(DAILY_STOP):
            if st.pause_reason[len(DAILY_STOP):] != day["key"]:          # a new day
                if st.equity(self.mids) <= self.cfg.floor_usd:           # (never past the floor: it needs /liveresume)
                    self.w.rec({"ev": "pause", "reason": "live_floor"})
                    return
                self.w.rec({"ev": "resume"})
                self.say("▶️ 💵 New day: live copies resumed after yesterday's daily stop.", "live_day", 0)
            return
        eq = st.equity(self.mids)
        lim = self.w.cfg.risk.daily_loss_pct
        if (day["equity"] - eq) / day["equity"] * 100 >= lim and not st.entries_paused:
            self.w.rec({"ev": "pause", "reason": DAILY_STOP + day["key"]})
            log.warn("live_daily_stop", equity=round(eq, 2), day_start=round(day["equity"], 2))
            self.say(f"⚠️ 💵 <b>LIVE daily stop</b>: {cardfmt.fusd(eq - day['equity'])} today (−{lim:g}% of the day's "
                     "start) · no new live entries until tomorrow. Open trades keep their stops.", "live_day", 0)

    def check_floor(self) -> None:
        eq = self.w.st.equity(self.mids)
        if eq > self.cfg.floor_usd:
            self.floor_armed = True
        elif self.floor_armed:
            self.floor_armed = False
            if self.w.st.pause_reason != "live_floor":
                self.w.rec({"ev": "pause", "reason": "live_floor"})   # (replaces a daily-stop reason: no automatic resume)
            log.warn("live_floor", equity=round(eq, 2), floor=self.cfg.floor_usd)
            self.say(f"⚠️ 💵 <b>LIVE wallet at {eq:.2f}$</b>, at or below its {self.cfg.floor_usd:g}$ floor: no new "
                     "live entries. Open trades keep their stops. /liveresume continues.", "live_floor", 0)

    def on_notify(self, kind: str, coin: str | None = None, **kw) -> None:
        """The live wallet's PositionManager changed a position: its exchange stop follows at once."""
        if coin:
            self.dirty.add(coin)
            self.sync_stop(coin)

    # ---- the stop on the exchange -----------------------------------------------------------------
    def sync_stop(self, coin: str) -> None:
        p = self.w.st.positions.get(coin)
        have = self.stops.get(coin, [])
        try:
            if p is None:
                for o in have:
                    self.ex.cancel(coin, o["oid"])
                self.stops.pop(coin, None)
                self.dirty.discard(coin)
                return
            a = self.ex.asset(coin)
            lot = 10 ** -a.sz_decimals
            want_px = px_wire(p.stop_px, a.sz_decimals)
            size = round(max(p.size, self.real.get(coin, 0.0)), a.sz_decimals)   # (more on the account: covered too)
            if len(have) == 1 and abs(have[0]["sz"] - size) < lot / 2 and abs(have[0]["px"] - want_px) <= want_px * 1e-4:
                self.dirty.discard(coin)
                return
            oid = self._place_stop(coin, p.side, size, p.stop_px)
            for o in have:                        # the new stop rests first, then the old ones go
                try:
                    self.ex.cancel(coin, o["oid"])
                except LiveError as e:
                    log.warn("live_stop_cancel_failed", coin=coin, oid=o["oid"], err=str(e)[:120])
            self.stops[coin] = [{"oid": int(oid), "px": want_px, "sz": size}]
            self.dirty.discard(coin)
            log.info("live_stop", coin=coin, px=want_px, sz=size, oid=oid)
        except (LiveError, NoBook) as e:
            self.dirty.add(coin)
            log.warn("live_stop_failed", coin=coin, err=str(e)[:160])
            what = "placed" if p is not None else "cancelled (the position is closed)"
            self.say(f"⚠️ 💵 LIVE {cardfmt.esc(coin)}: the stop order could not be {what} on the exchange "
                     f"({cardfmt.esc(str(e)[:120])}); retrying. The bot's own stop still runs while it is up.",
                     f"live_stop:{coin}", 600)

    def _place_stop(self, coin: str, side: int, size: float, stop_px: float) -> int:
        """A resting reduce-only stop-market order for a `side` position; its order id."""
        is_buy = side < 0
        limit = stop_px * (1 + STOP_LIMIT if is_buy else 1 - STOP_LIMIT)
        st = self.ex.order(coin, is_buy, size, limit, reduce_only=True, trigger_px=stop_px)
        oid = (st.get("resting") or {}).get("oid")
        if oid is None:
            raise LiveError(st.get("error", str(st)[:120]))
        self.stop_ids.setdefault(coin, set()).add(int(oid))
        return int(oid)

    # ---- the real account vs the ledger -----------------------------------------------------------
    def reconcile(self) -> None:
        self.last_reconcile = time.time()
        pos, self.account_value = self.ex.state()
        stops: dict[str, list[dict]] = {}
        for o in self.ex.open_orders():
            if o.get("isTrigger") and o.get("reduceOnly"):
                stops.setdefault(o["coin"], []).append({"oid": int(o["oid"]), "px": float(o.get("triggerPx") or 0),
                                                        "sz": float(o.get("sz") or 0)})
        self.stops = stops
        problems = []
        self.real = {}
        for coin, p in list(self.w.st.positions.items()):
            e = pos.get(coin, 0.0)
            mine = abs(e) if e and (e > 0) == (p.side > 0) else 0.0
            self.real[coin] = mine
            a = self.ex.assets.get(coin)
            lot = 10 ** -(a.sz_decimals if a else 8)
            if mine < p.size - lot / 2:          # the exchange closed (part of) it: our resting stop fired
                log.warn("live_exchange_smaller", coin=coin, ledger=p.size, exchange=e)
                try:
                    if mine < lot / 2:
                        self.w.pm.close(coin, "exchange_closed")
                    else:
                        self.w.pm.reduce(coin, p.size - mine, "exchange_reduced")
                        self._rescale_shares(coin)
                except (LiveError, NoBook) as ex:
                    problems.append(f"{coin}: the account holds {e:g}, booking it failed ({str(ex)[:80]})")
            elif mine > p.size + lot / 2:
                problems.append(f"{coin}: the account holds {e:g}, the bot's ledger {p.side * p.size:g}")
        for coin, e in pos.items():
            if coin not in self.w.st.positions:
                problems.append(f"{coin}: the account holds {e:g}, a position the bot did not open")
                if not stops.get(coin):           # (code review #1: e.g. an entry that filled without an answer)
                    self._guard(coin, e)
        for coin in set(self.w.st.positions) | {c for c in stops if c not in pos}:
            self.sync_stop(coin)
        # the account holds less than the ledger says (real fees and funding, money taken out): the bot would size and
        # limit on money that is not there (values review 2026-11-03)
        eq = self.w.st.equity(self.mids)
        if self.account_value is not None and self.account_value < eq - max(3.0, eq * 0.03):
            log.warn("live_account_below_ledger", account=self.account_value, ledger=round(eq, 2))
            self.say(f"⚠️ 💵 The Hyperliquid account holds {cardfmt.fusd(self.account_value, sign=False)}, the live "
                     f"ledger counts {cardfmt.fusd(eq, sign=False)}. If money left the account, lower "
                     "live.start_equity (or /livekill and start fresh).", "live_drift", 3600)
        if problems and not self.w.st.entries_paused:   # (a resume does not hold while it still differs)
            self.w.rec({"ev": "pause", "reason": "live_mismatch"})
        if problems and problems != self.problems:
            log.error("live_mismatch", problems=" | ".join(problems))
            self.say("⚠️ 💵 <b>LIVE account does not match the bot's ledger</b> · ⏸️ new live entries paused\n"
                     + "\n".join(f"• {cardfmt.esc(x)}" for x in problems[:8])
                     + "\nUse this Hyperliquid account only for the bot. /livekill &lt;PIN&gt; closes everything; "
                       "/liveresume continues once it matches.", "live_mismatch", 0)
        elif not problems and self.problems:
            self.say("✅ 💵 LIVE account matches the bot's ledger again (entries stay paused until /liveresume).",
                     "live_match", 0)
        self.problems = problems
        self.last_ok = time.time()

    def _rescale_shares(self, coin: str) -> None:
        """After a reduce the exchange did (a stop part fired), each trader's share shrinks pro rata."""
        p = self.w.st.positions.get(coin)
        if p is None or not p.shares:
            return
        total = sum(v["size"] for v in p.shares.values())
        if total <= 0 or abs(total - p.size) < 1e-12:
            return
        f = p.size / total
        shares = {a: dict(v, size=v["size"] * f, k=v.get("k", 0) * f) for a, v in p.shares.items()}
        self.w.pm.record_shares(p, shares)

    def _guard(self, coin: str, e: float) -> None:
        """A stop for a position the ledger does not know, at the live stop distance from now."""
        try:
            a = self.ex.asset(coin)
            ref = self.mids.get(coin) or self.ex.book(coin).mid
            side = 1 if e > 0 else -1
            stop = ref * (1 - side * self.w.cfg.risk.stop_pct / 100)
            oid = self._place_stop(coin, side, round(abs(e), a.sz_decimals), stop)
            log.warn("live_guard_stop", coin=coin, size=e, stop=stop, oid=oid)
        except Exception as ex:
            log.warn("live_guard_stop_failed", coin=coin, err=str(ex)[:160])

    # ---- commands ---------------------------------------------------------------------------------
    def kill(self) -> str:
        """/livekill: pause, close every live position (the ledger's, then anything left on the account) and cancel
        every open order on the account."""
        w = self.w
        w.rec({"ev": "pause", "reason": "/livekill"})
        n, errors = 0, []
        for coin in list(w.st.positions):
            self.broker.exit_wait.pop(coin, None)   # (the owner's command does not wait out a failed exit's backoff)
            try:
                w.pm.close(coin, "live_kill")
                if coin in w.st.positions:
                    errors.append(f"{coin}: exit pending (not filled yet; its exchange stop still holds it)")
                else:
                    n += 1
            except Exception as e:
                errors.append(f"{coin}: {str(e)[:100]}")
        try:
            pos, _ = self.ex.state()
        except Exception as e:
            pos = {}
            errors.append(f"account not read: {type(e).__name__}: {str(e)[:100]}")
        left = set(w.st.positions)
        for coin, e in pos.items():               # one coin's failure never stops the others (code review #6)
            try:
                a = self.ex.assets.get(coin)
                ref = self.ex.book(coin).mid
                for mult in EXIT_STEPS:
                    limit = ref * (1 + (1 if e < 0 else -1) * mult * self.cfg.exit_slippage_bps / 1e4)
                    st = self.ex.order(coin, e < 0, abs(e), limit, reduce_only=True)
                    if "filled" in st:
                        n += 1
                        e -= float(st["filled"]["totalSz"]) * (1 if e > 0 else -1)
                        if a is None or abs(e) < 10 ** -a.sz_decimals / 2:
                            break
                if a is not None and abs(e) >= 10 ** -a.sz_decimals / 2:
                    left.add(coin)
                    errors.append(f"{coin}: {e:g} still open")
            except Exception as ex:
                left.add(coin)
                errors.append(f"{coin}: {type(ex).__name__}: {str(ex)[:100]}")
        try:                                      # the orders of what is flat now; a coin still held keeps its stop
            for o in self.ex.open_orders():
                if o["coin"] not in left:
                    self.ex.cancel(o["coin"], o["oid"])
        except Exception as e:
            errors.append(f"orders: {type(e).__name__}: {str(e)[:100]}")
        log.warn("live_kill", closed=n, errors=" | ".join(errors))
        return (f"🛑 💵 <b>LIVE kill</b>: {n} position(s) closed, their orders cancelled · ⏸️ live entries paused "
                "(/liveresume continues)" + ("".join(f"\n⚠️ {cardfmt.esc(x)}" for x in errors) if errors else ""))

    def resume(self) -> None:
        if self.w.st.uncertain:
            self.w.rec({"ev": "ack", "items": list(self.w.st.uncertain)})
        self.w.rec({"ev": "resume"})
        self.floor_armed = self.w.st.equity(self.mids) > self.cfg.floor_usd

    def status_text(self) -> str:
        w, st = self.w, self.w.st
        eq = st.equity(self.mids)
        money = "REAL money" if self.ex.mainnet else "test money"
        lines = [f"💵 <b>Live wallet</b> · Hyperliquid {self.ex.network} ({money}) · copies "
                 f"<code>{cardfmt.esc(self.twin.name)}</code>",
                 f"Wallet: {cardfmt.fusd(eq, sign=False)} ({cardfmt.fusd(eq - st.equity0)} since the start of "
                 f"{cardfmt.fusd(st.equity0, sign=False)})"]
        if self.account_value is not None:
            lines.append(f"Account on the exchange: {cardfmt.fusd(self.account_value, sign=False)}")
        day = st.marks.get("day")
        if day and day["equity"] > 0:
            lines.append(f"Today: {cardfmt.fusd(eq - day['equity'])} ({(eq / day['equity'] - 1) * 100:+.2f}%) · stops "
                         f"new entries at −{w.cfg.risk.daily_loss_pct:g}%")
        lines.append(f"Floor: {cardfmt.fusd(self.cfg.floor_usd, sign=False)} · biggest position "
                     f"{cardfmt.fusd(self.cfg.max_position_usd, sign=False)}")
        lines.append("⏸️ Entries paused: " + cardfmt.esc(st.pause_reason or "uncertain restart")
                     if st.entries_paused or st.uncertain else "▶️ Copying")
        for p in st.positions.values():
            m = self.mids.get(p.coin)
            on_ex = "✅ stop on the exchange" if self.stops.get(p.coin) else "❌ no stop on the exchange yet"
            lines.append(f"{'⬆️' if p.side > 0 else '⬇️'} {cardfmt.esc(p.coin)} {p.size:g} @ {cardfmt.fpx(p.entry_px)}"
                         f" · stop {cardfmt.fpx(p.stop_px)} · {on_ex}"
                         + (f" · {cardfmt.fusd(p.upnl(m))}" if m else ""))
        since = st.genesis_ms
        twin = [t for t in self.twin.st.closed if t["closed_ms"] >= since]
        mine = [t for t in st.closed]
        pct = lambda ts, w: sum(t["pnl"] for t in ts) / w.st.equity0 * 100 if w.st.equity0 else 0.0
        # in % of each wallet's start (live 100$, paper 300$); a gap in trades = copies live skipped (min order, cap)
        lines.append(f"Closed since the live start: live {len(mine)} trades {pct(mine, w):+.2f}% · paper "
                     f"{len(twin)} trades {pct(twin, self.twin):+.2f}%")
        if self.problems:
            lines += ["⚠️ Mismatch:"] + [f"• {cardfmt.esc(x)}" for x in self.problems[:5]]
        return "\n".join(lines)
