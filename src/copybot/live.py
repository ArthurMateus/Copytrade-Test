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
  the 150$ floor, /livekill, /livestatus.
Fees are booked at broker.taker_fee_pct (the exchange's real fee is in its fills; /livestatus shows the real account
value next to the ledger's).
"""
from __future__ import annotations

import json
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


class LiveError(Exception):
    """The exchange refused or did not answer. An entry that fails this way is not booked; the reconcile finds out
    what really happened (and pauses entries if the account holds more than the ledger)."""


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
            raise LiveError(f"{action['type']}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}") from None
        except (OSError, ValueError) as e:              # no answer, timeout, not JSON
            raise LiveError(f"{action['type']}: {type(e).__name__}: {e}") from None
        if not isinstance(res, dict) or res.get("status") != "ok":
            raise LiveError(f"{action['type']} refused: {str(res)[:200]}")
        return res.get("response") or {}

    def order(self, coin: str, is_buy: bool, sz: float, limit_px: float, reduce_only: bool = False,
              trigger_px: float | None = None) -> dict:
        """One IOC limit order, or (trigger_px) a resting stop-market order. Returns the exchange's status of it:
        {"filled": {...}}, {"resting": {"oid": n}} or {"error": "..."}."""
        a = self.asset(coin)
        kind = ({"trigger": {"triggerPx": px_wire(trigger_px, a.sz_decimals), "isMarket": True, "tpsl": "sl"}}
                if trigger_px else {"limit": {"tif": "Ioc"}})
        cloid = self._cloid.from_str("0x" + uuid.uuid4().hex)
        wire = self._signing.order_request_to_order_wire(
            {"coin": coin, "is_buy": is_buy, "sz": round(sz, a.sz_decimals), "limit_px": px_wire(limit_px, a.sz_decimals),
             "order_type": kind, "reduce_only": reduce_only, "cloid": cloid}, a.index)
        log.info("live_order", coin=coin, buy=is_buy, sz=round(sz, a.sz_decimals), limit=px_wire(limit_px, a.sz_decimals),
                 reduce_only=reduce_only, trigger=trigger_px or "", cloid=str(cloid))
        res = self.act(self._signing.order_wires_to_order_action([wire]))
        statuses = (res.get("data") or {}).get("statuses") or [{}]
        return statuses[0] if isinstance(statuses[0], dict) else {"error": str(statuses[0])}

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

    def market(self, coin: str, is_buy: bool, size: float, exit: bool = False, mid: float | None = None,
               leverage: float | None = None) -> PaperFill:
        return self._exit(coin, is_buy, size, mid) if exit else self._entry(coin, is_buy, size, leverage)

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
        if (held + size) * ref > self.cfg.max_position_usd + 1e-9:
            raise NoBook(f"{coin}: over the live position cap of {self.cfg.max_position_usd:g}$")
        if leverage:
            try:
                self.ex.set_leverage(coin, leverage)
            except LiveError as e:
                if not held:                     # (an open isolated position may refuse a change: keep its own)
                    raise NoBook(f"{coin}: leverage not set ({e})") from None
        limit = ref * (1 + (1 if is_buy else -1) * self.cfg.entry_slippage_bps / 1e4)
        sz, px = self._filled(self.ex.order(coin, is_buy, size, limit), coin, "entry")
        if sz <= 0:
            raise NoBook(f"{coin}: live entry not filled within {self.cfg.entry_slippage_bps:g} bps of the mid")
        return self._fill(coin, is_buy, ref, [(sz, px, px * sz * self.fee_pct / 100)])

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
                sz, px = self._filled(self.ex.order(coin, is_buy, left, limit, reduce_only=True), coin, "exit")
            except LiveError as e:
                log.warn("live_exit_failed", coin=coin, try_=mult, err=str(e)[:160])
                continue
            if sz > 0:
                parts.append((sz, px, px * sz * self.fee_pct / 100))
                left = round(left - sz, a.sz_decimals)
        if left >= lot / 2 or not parts:
            raise LiveError(f"{coin}: exit not filled, {left:g} left (the stop order on the exchange still holds it)")
        return self._fill(coin, is_buy, ref or parts[0][1], parts)

    def _gone(self, coin: str, is_buy: bool, size: float, opened_ms: int, mid: float | None):
        """(size, price, fee) of the part the exchange closed without us, from the account's real fills."""
        try:
            fills = [f for f in self.ex.fills_since(opened_ms - 60_000) if f.coin == coin
                     and f.side == ("B" if is_buy else "A") and ("Close" in f.dir or "Liquidat" in f.dir)]
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
        self.dirty: set[str] = set()     # coins whose exchange stop must be (re)placed
        self.problems: list[str] = []
        self.account_value: float | None = None
        self.floor_armed = True
        self.last_reconcile = self.last_mids = self.last_meta = self.last_retry = 0.0

    def attach(self, w, twin) -> None:
        self.w, self.twin = w, twin
        self.broker.held = lambda c: ((w.st.positions[c].size, w.st.positions[c].opened_ms)
                                      if c in w.st.positions else (0.0, 0))

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
            self.mids.update(self.ex.mids())
        if now - self.last_meta >= 3600:          # funding rates and listings
            self.last_meta = now
            self.ex.load_meta()
        self.check_floor()
        if self.dirty and now - self.last_retry >= 5.0:   # a stop the exchange refused: retried every 5 s
            self.last_retry = now
            for coin in sorted(self.dirty):
                self.sync_stop(coin)
        if now - self.last_reconcile >= self.cfg.reconcile_s:
            self.reconcile()

    def check_floor(self) -> None:
        eq = self.w.st.equity(self.mids)
        if eq > self.cfg.floor_usd:
            self.floor_armed = True
        elif self.floor_armed:
            self.floor_armed = False
            if not self.w.st.entries_paused:
                self.w.rec({"ev": "pause", "reason": "live_floor"})
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
            if len(have) == 1 and abs(have[0]["sz"] - p.size) < lot / 2 and abs(have[0]["px"] - want_px) <= want_px * 1e-4:
                self.dirty.discard(coin)
                return
            is_buy = p.side < 0
            limit = p.stop_px * (1 + STOP_LIMIT if is_buy else 1 - STOP_LIMIT)
            st = self.ex.order(coin, is_buy, p.size, limit, reduce_only=True, trigger_px=p.stop_px)
            oid = (st.get("resting") or {}).get("oid")
            if oid is None:
                raise LiveError(st.get("error", str(st)[:120]))
            for o in have:                        # the new stop rests first, then the old ones go
                try:
                    self.ex.cancel(coin, o["oid"])
                except LiveError as e:
                    log.warn("live_stop_cancel_failed", coin=coin, oid=o["oid"], err=str(e)[:120])
            self.stops[coin] = [{"oid": int(oid), "px": want_px, "sz": round(p.size, a.sz_decimals)}]
            self.dirty.discard(coin)
            log.info("live_stop", coin=coin, px=want_px, sz=p.size, oid=oid)
        except (LiveError, NoBook) as e:
            self.dirty.add(coin)
            log.warn("live_stop_failed", coin=coin, err=str(e)[:160])
            self.say(f"⚠️ 💵 LIVE {cardfmt.esc(coin)}: the stop order could not be placed on the exchange "
                     f"({cardfmt.esc(str(e)[:120])}); retrying. The bot's own stop still runs while it is up.",
                     f"live_stop:{coin}", 600)

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
        for coin, p in list(self.w.st.positions.items()):
            e = pos.get(coin, 0.0)
            mine = abs(e) if e and (e > 0) == (p.side > 0) else 0.0
            a = self.ex.assets.get(coin)
            lot = 10 ** -(a.sz_decimals if a else 8)
            if mine < p.size - lot / 2:          # the exchange closed (part of) it: our resting stop fired
                log.warn("live_exchange_smaller", coin=coin, ledger=p.size, exchange=e)
                try:
                    if mine < lot / 2:
                        self.w.pm.close(coin, "exchange_closed")
                    else:
                        self.w.pm.reduce(coin, p.size - mine, "exchange_reduced")
                except (LiveError, NoBook) as ex:
                    problems.append(f"{coin}: the account holds {e:g}, booking it failed ({str(ex)[:80]})")
            elif mine > p.size + lot / 2:
                problems.append(f"{coin}: the account holds {e:g}, the bot's ledger {p.side * p.size:g}")
        for coin, e in pos.items():
            if coin not in self.w.st.positions:
                problems.append(f"{coin}: the account holds {e:g}, a position the bot did not open")
        for coin in set(self.w.st.positions) | {c for c in stops if c not in pos}:
            self.sync_stop(coin)
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

    # ---- commands ---------------------------------------------------------------------------------
    def kill(self) -> str:
        """/livekill: pause, close every live position (the ledger's, then anything left on the account) and cancel
        every open order on the account."""
        w = self.w
        w.rec({"ev": "pause", "reason": "/livekill"})
        n, errors = 0, []
        for coin in list(w.st.positions):
            try:
                w.pm.close(coin, "live_kill")
                n += 1
            except Exception as e:
                errors.append(f"{coin}: {str(e)[:100]}")
        try:
            pos, _ = self.ex.state()
            for coin, e in pos.items():
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
            for o in self.ex.open_orders():
                self.ex.cancel(o["coin"], o["oid"])
            self.stops.clear()
            self.dirty.clear()
        except Exception as e:
            errors.append(f"account: {type(e).__name__}: {str(e)[:100]}")
        log.warn("live_kill", closed=n, errors=" | ".join(errors))
        return (f"🛑 💵 <b>LIVE kill</b>: {n} position(s) closed, open orders cancelled · ⏸️ live entries paused "
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
        lines.append(f"Closed since the live start: live {len(mine)} trades {cardfmt.fusd(sum(t['pnl'] for t in mine))}"
                     f" · paper {len(twin)} trades {cardfmt.fusd(sum(t['pnl'] for t in twin))}")
        if self.problems:
            lines += ["⚠️ Mismatch:"] + [f"• {cardfmt.esc(x)}" for x in self.problems[:5]]
        return "\n".join(lines)
