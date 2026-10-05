"""Websocket feed (own thread): allMids + userFills for the leaders we follow or still hold copies of.
Plus the exchange clock estimate. Neither ever blocks the trading loop."""
from __future__ import annotations

import asyncio
import collections
import json
import queue
import threading
import time

import websockets

from copybot import hl, log

MAX_WS_USERS = 15   # Hyperliquid: "Cannot track more than 15 total users."


class Feed:
    def __init__(self, url: str, out: queue.Queue):
        self.url, self.out = url, out
        self.lock = threading.Lock()
        self._users: set[str] = set()
        self._mids: dict[str, float] = {}
        self.mids_ts = 0.0          # local time of the last price update (ws or REST fallback)
        self.last_msg = 0.0
        self.connected = False
        self.stop = threading.Event()
        self.reconnects = 0

    # ---- thread-safe accessors -------------------------------------------------------------------
    def set_users(self, users: set[str]) -> None:
        users = {u.lower() for u in users}
        if len(users) > MAX_WS_USERS:
            log.error("ws_too_many_users", n=len(users))
            users = set(sorted(users)[:MAX_WS_USERS])
        with self.lock:
            self._users = users

    def set_mids(self, mids: dict[str, float], source: str) -> None:
        with self.lock:
            self._mids.update(mids)
            self.mids_ts = time.time()

    def mids(self) -> tuple[dict[str, float], float]:
        with self.lock:
            return dict(self._mids), self.mids_ts

    # ---- thread -----------------------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._main()), name="ws", daemon=True).start()

    async def _main(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=None, open_timeout=10, ping_interval=None) as ws:
                    await self._session(ws)
                    backoff = 1.0
            except Exception as e:
                log.warn("ws_error", err=f"{type(e).__name__}: {e}"[:200])
            if self.connected:
                self.connected = False
                self.out.put(("ws_down",))
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _session(self, ws) -> None:
        subscribed: set[str] = set()
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        self.connected = True
        self.last_msg = time.time()
        self.reconnects += 1
        self.out.put(("ws_up", self.reconnects))
        log.info("ws_connected", n=self.reconnects)
        last_ping = time.time()
        while not self.stop.is_set():
            with self.lock:
                want = set(self._users)
            for u in sorted(want - subscribed):
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": u}}))
                subscribed.add(u)
                log.info("ws_subscribe", user=u)
            for u in sorted(subscribed - want):
                await ws.send(json.dumps({"method": "unsubscribe", "subscription": {"type": "userFills", "user": u}}))
                subscribed.discard(u)
                log.info("ws_unsubscribe", user=u)
            if time.time() - last_ping > 20:
                await ws.send(json.dumps({"method": "ping"}))
                last_ping = time.time()
            try:
                raw = await asyncio.wait_for(ws.recv(), 0.5)
            except asyncio.TimeoutError:
                if time.time() - self.last_msg > 60:
                    raise ConnectionError("websocket silent for 60 s")
                continue
            self.last_msg = time.time()
            ev = hl.parse_ws(json.loads(raw))
            if ev.kind == "mids":
                self.set_mids(ev.mids, "ws")
            elif ev.kind == "fills":
                self.out.put(("fills", ev, time.time()))
            elif ev.kind == "error":
                log.error("ws_server_error", text=ev.text)
                self.out.put(("alert", f"websocket error: {ev.text}"))


class Clock:
    """Exchange clock offset from l2Book's server time: offset = server - (send + recv) / 2,
    uncertainty = round trip / 2. Keeps the tightest of the recent samples."""

    def __init__(self):
        self.samples: collections.deque = collections.deque(maxlen=10)
        self.lock = threading.Lock()

    def sample(self, info: hl.Info, timeout: float) -> None:
        res, t0, t1 = info.timed_post({"type": "l2Book", "coin": "BTC"}, hl.CRITICAL, timeout)
        server = float(res["time"])
        with self.lock:
            self.samples.append((server - (t0 + t1) / 2, (t1 - t0) / 2, time.time()))

    def status(self, tolerance_ms: float, max_age_s: float) -> tuple[bool, float, str]:
        with self.lock:
            fresh = [s for s in self.samples if time.time() - s[2] <= max_age_s]
        if not fresh:
            return False, 0.0, "no recent clock estimate"
        off, unc, _ = min(fresh, key=lambda s: s[1])
        if unc > tolerance_ms:
            return False, off, f"clock uncertainty {unc:.0f} ms"
        return True, off, f"+-{unc:.0f}ms"
