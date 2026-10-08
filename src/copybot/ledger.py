"""Append-only JSON-lines ledger (fsync on every write) and the State it folds into.

Event sourcing: the live bot does `ledger.append(ev)` and then `state.apply(ev)`. A restart replays the
same events through the same `apply`, so the in-memory state after a restart is identical to the state
before the crash (up to the last durable line).

Anything uncertain at replay (truncated last line, an order intent without its result, an event that does
not fit the state, a position without a valid stop) is collected in `state.uncertain`. The runner then
pauses entries and alerts, while exits keep being managed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Position:
    pos_id: str
    coin: str
    side: int              # +1 long, -1 short
    size: float            # absolute size, coins
    entry_px: float        # average entry
    stop_px: float
    leverage: float
    leader: str
    k: float               # our size per unit of the leader's position (copy ratio)
    open_oid: int          # leader order id that opened (later fills of it rebase k instead of adding)
    opened_ms: int
    realized: float = 0.0  # net realized so far: price pnl - fees + funding
    fees: float = 0.0
    funding: float = 0.0
    entry_notional: float = 0.0
    open_lag_ms: float | None = None
    sym: str = ""          # display symbol (Solana tokens; Hyperliquid coins are their own name)
    # other followed leaders on the SAME side of this coin (consensus):
    # leader -> {"lpos": their |position|, "oid": their opening order, "frac": share of our size they added}
    backers: dict = field(default_factory=dict)

    def risk_usd(self) -> float:
        return abs(self.entry_px - self.stop_px) * self.size

    def upnl(self, mark: float) -> float:
        return self.side * (mark - self.entry_px) * self.size

    def stop_hit(self, mark: float) -> bool:
        return mark <= self.stop_px if self.side > 0 else mark >= self.stop_px


@dataclass
class LeaderStats:
    trades: int = 0
    cum: float = 0.0
    peak: float = 0.0
    consec_losses: int = 0

    @property
    def drawdown(self) -> float:
        return self.peak - self.cum


@dataclass
class State:
    equity0: float = 0.0
    btc_px0: float = 0.0
    genesis_ms: int = 0
    realized: float = 0.0                       # cumulative net realized P&L of the wallet
    positions: dict[str, Position] = field(default_factory=dict)   # coin -> position (one net position per coin)
    followed: dict[str, int] = field(default_factory=dict)         # leader -> followed since (ms)
    paused_leaders: dict[str, str] = field(default_factory=dict)   # leader -> reason (no new entries)
    dropped: dict[str, int] = field(default_factory=dict)          # leader -> dropped at (ms), for cooldown
    leader_stats: dict[str, LeaderStats] = field(default_factory=dict)
    entries_paused: bool = False
    pause_reason: str = ""
    marks: dict[str, dict] = field(default_factory=dict)          # "day"/"week" -> {"key":..., "equity":...}
    closed: list[dict] = field(default_factory=list)
    lags_ms: list[float] = field(default_factory=list)
    sel: dict = field(default_factory=dict)                        # selection hysteresis state
    cards: dict[str, int] = field(default_factory=dict)            # telegram card key -> message id
    open_intents: dict[str, dict] = field(default_factory=dict)
    uncertain: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    cursors: dict[str, int] = field(default_factory=dict)         # leader -> newest swap time handled (Solana)
    seq: int = 0

    # ---- derived -------------------------------------------------------------------------
    def equity(self, mids: dict[str, float] | None = None) -> float:
        """Wallet equity: start + realized + unrealized (positions without a mark count at entry)."""
        u = 0.0
        for p in self.positions.values():
            m = (mids or {}).get(p.coin)
            if m:
                u += p.upnl(m)
        return self.equity0 + self.realized + u

    def total_risk(self) -> float:
        return sum(p.risk_usd() for p in self.positions.values())

    def bump(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    # ---- the one fold ---------------------------------------------------------------------
    def apply(self, ev: dict) -> None:
        self.seq = max(self.seq, int(ev.get("seq", 0)))
        t = ev["ev"]
        fn = getattr(self, "_ev_" + t, None)
        if fn is None:
            self.uncertain.append(f"unknown event type {t!r} seq={ev.get('seq')}")
            return
        fn(ev)
        iid = ev.get("intent")
        if iid and t != "intent":
            self.open_intents.pop(iid, None)

    def _ev_genesis(self, ev):
        self.equity0 = float(ev["equity0"])
        self.btc_px0 = float(ev.get("btc_px0") or 0.0)
        self.genesis_ms = int(ev["ts"])

    def _ev_boot(self, ev):
        pass

    def _ev_note(self, ev):
        pass

    def _ev_intent(self, ev):
        self.open_intents[ev["intent"]] = ev

    def _ev_intent_abort(self, ev):
        self.open_intents.pop(ev["intent"], None)

    def _ev_open(self, ev):
        p = Position(**ev["pos"])
        if p.coin in self.positions:
            self.uncertain.append(f"open on already-open coin {p.coin} seq={ev.get('seq')}")
            return
        fee = float(ev["fee"])
        p.fees += fee
        p.realized -= fee
        p.entry_notional = p.size * p.entry_px
        self.realized -= fee
        self.positions[p.coin] = p
        if ev.get("lag_ms") is not None:
            self.lags_ms.append(float(ev["lag_ms"]))
        self.bump("orders")

    def _get(self, ev) -> Position | None:
        p = self.positions.get(ev["coin"])
        if p is None or p.pos_id != ev["pos_id"]:
            self.uncertain.append(f"{ev['ev']} for unknown position {ev['coin']}/{ev.get('pos_id')} seq={ev.get('seq')}")
            return None
        return p

    def _ev_add(self, ev):
        p = self._get(ev)
        if not p:
            return
        sz, px, fee = float(ev["sz"]), float(ev["px"]), float(ev["fee"])
        p.entry_px = (p.entry_px * p.size + px * sz) / (p.size + sz)
        p.size += sz
        p.entry_notional += sz * px
        p.fees += fee
        p.realized -= fee
        self.realized -= fee
        if ev.get("k") is not None:
            p.k = float(ev["k"])
        if ev.get("lag_ms") is not None:
            self.lags_ms.append(float(ev["lag_ms"]))
        self.bump("orders")

    def _ev_back(self, ev):
        """A second leader opened the same side: record it as a backer, with its extra size if any.
        Without `sz` it only refreshes the backer's leader position."""
        p = self._get(ev)
        if not p:
            return
        sz = float(ev.get("sz") or 0.0)
        if ev["leader"] not in p.backers:
            self.bump("consensus")
        b = p.backers.setdefault(ev["leader"], {"lpos": 0.0, "oid": ev.get("oid"), "frac": 0.0})
        b["lpos"] = float(ev["lpos"])
        if sz > 0:
            px, fee = float(ev["px"]), float(ev["fee"])
            new = p.size + sz
            for other in p.backers.values():   # earlier shares shrink as the position grows
                other["frac"] *= p.size / new
            b["frac"] += sz / new
            p.entry_px = (p.entry_px * p.size + px * sz) / new
            p.size = new
            p.entry_notional += sz * px
            p.fees += fee
            p.realized -= fee
            self.realized -= fee
            p.k = float(ev["k"])
            if ev.get("lag_ms") is not None:
                self.lags_ms.append(float(ev["lag_ms"]))
            self.bump("orders")

    def _ev_unback(self, ev):
        """A backer left. Its share is trimmed by a separate reduce, so the others' shares grow back."""
        p = self._get(ev)
        if not p:
            return
        b = p.backers.pop(ev["leader"], None)
        if b and b["frac"] < 1:
            for other in p.backers.values():
                other["frac"] /= 1 - b["frac"]

    def _ev_handover(self, ev):
        """The owner left while a backer still holds: the backer becomes the owner (the extra size was
        already trimmed back to a normal copy, so no share is left with the remaining backers)."""
        p = self._get(ev)
        if not p:
            return
        b = p.backers.pop(ev["leader"], {})
        p.leader = ev["leader"]
        p.k = float(ev["k"])
        p.open_oid = b.get("oid") or 0
        for other in p.backers.values():
            other["frac"] = 0.0
        self.bump("handovers")

    def _ev_rebase(self, ev):
        p = self._get(ev)
        if p:
            p.k = float(ev["k"])

    def _ev_reduce(self, ev):
        p = self._get(ev)
        if not p:
            return
        sz, px, fee = min(float(ev["sz"]), p.size), float(ev["px"]), float(ev["fee"])
        pnl = p.side * (px - p.entry_px) * sz - fee
        p.size -= sz
        p.fees += fee
        p.realized += pnl
        self.realized += pnl
        if ev.get("lag_ms") is not None:
            self.lags_ms.append(float(ev["lag_ms"]))
        self.bump("orders")

    def _ev_close(self, ev):
        p = self._get(ev)
        if not p:
            return
        px, fee = float(ev["px"]), float(ev["fee"])
        pnl = p.side * (px - p.entry_px) * p.size - fee
        p.fees += fee
        p.realized += pnl
        self.realized += pnl
        del self.positions[p.coin]
        if ev.get("lag_ms") is not None:
            self.lags_ms.append(float(ev["lag_ms"]))
        self.bump("orders")
        st = self.leader_stats.setdefault(p.leader, LeaderStats())
        st.trades += 1
        st.cum += p.realized
        st.peak = max(st.peak, st.cum)
        st.consec_losses = st.consec_losses + 1 if p.realized < 0 else 0
        self.closed.append({
            "pos_id": p.pos_id, "coin": p.coin, "side": p.side, "leader": p.leader, "entry": p.entry_px,
            "exit": px, "pnl": p.realized, "fees": p.fees, "funding": p.funding, "notional": p.entry_notional,
            "opened_ms": p.opened_ms, "closed_ms": int(ev["ts"]), "reason": ev.get("reason", ""),
            "open_lag_ms": p.open_lag_ms, "leverage": p.leverage, "sym": p.sym,
        })

    def _ev_funding(self, ev):
        p = self._get(ev)
        if not p:
            return
        amt = float(ev["amount"])
        p.funding += amt
        p.realized += amt
        self.realized += amt

    def _ev_stop_set(self, ev):
        p = self._get(ev)
        if p:
            p.stop_px = float(ev["stop_px"])

    def _ev_follow(self, ev):
        self.followed[ev["leader"]] = int(ev["ts"])
        self.paused_leaders.pop(ev["leader"], None)

    def _ev_unfollow(self, ev):
        self.followed.pop(ev["leader"], None)
        self.paused_leaders.pop(ev["leader"], None)
        self.dropped[ev["leader"]] = int(ev["ts"])

    def _ev_leader_pause(self, ev):
        self.paused_leaders[ev["leader"]] = ev.get("reason", "")

    def _ev_pause(self, ev):
        self.entries_paused = True
        self.pause_reason = ev.get("reason", "")

    def _ev_resume(self, ev):
        self.entries_paused = False
        self.pause_reason = ""

    def _ev_mark(self, ev):
        self.marks[ev["kind"]] = {"key": ev["key"], "equity": float(ev["equity"])}

    def _ev_sel(self, ev):
        self.sel = ev["state"]

    def _ev_card(self, ev):
        self.cards[ev["key"]] = int(ev["msg_id"])

    def _ev_card_drop(self, ev):
        self.cards.pop(ev["key"], None)

    def _ev_ack(self, ev):
        """The owner acknowledged the uncertainties raised so far (/resume)."""
        self.uncertain.clear()

    def _ev_cursor(self, ev):
        self.cursors[ev["leader"]] = max(self.cursors.get(ev["leader"], 0), int(ev["t"]))

    def _ev_count(self, ev):
        self.bump(ev["name"], int(ev.get("n", 1)))


class Ledger:
    """Append-only, fsync'd JSON lines. Thread-safe. The only writer of durable state."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._f = None
        self.frozen = False   # set by /reset after archiving: nothing more may be written to this file

    def replay(self) -> State:
        st = State()
        if self.path.exists():
            raw = self.path.read_bytes()
            lines = raw.split(b"\n")
            tail = lines.pop()  # text after the last newline: empty unless the last write was torn
            if tail.strip():
                st.uncertain.append(f"truncated last ledger line ({len(tail)} bytes) ignored")
                # cut the torn tail so new lines start clean
                with open(self.path, "r+b") as f:
                    f.truncate(len(raw) - len(tail))
                    f.flush()
                    os.fsync(f.fileno())
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    st.uncertain.append(f"unreadable ledger line {i + 1}")
                    continue
                st.apply(ev)
        for iid, iev in st.open_intents.items():
            st.uncertain.append(f"order intent {iid} ({iev.get('action')} {iev.get('coin')}) has no recorded result")
        for p in st.positions.values():
            bad = p.stop_px <= 0 or (p.side > 0 and p.stop_px >= p.entry_px) or (p.side < 0 and p.stop_px <= p.entry_px)
            if bad:
                st.uncertain.append(f"position {p.coin} has no valid stop (stop={p.stop_px} entry={p.entry_px})")
        self._seq = st.seq
        return st

    def append(self, ev: dict) -> dict:
        with self._lock:
            if self.frozen:
                return {"seq": self._seq, "ts": ev.get("ts") or now_ms(), **ev}
            self._seq += 1
            ev = {"seq": self._seq, "ts": ev.get("ts") or now_ms(), **{k: v for k, v in ev.items() if k != "ts"}}
            line = (json.dumps(ev, separators=(",", ":"), default=_default) + "\n").encode()
            if self._f is None:
                self._f = open(self.path, "ab")
            self._f.write(line)
            self._f.flush()
            os.fsync(self._f.fileno())
            return ev

    def close(self) -> None:
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None


def _default(o):
    if hasattr(o, "__dataclass_fields__"):
        return asdict(o)
    raise TypeError(type(o))
