"""Telegram: owner-only command polling and a rate-limited outbox that edits live cards in place.

- New messages only for new events. Live cards (one per open trade, /status, /leaders) are EDITED.
- Per-card edits at most every `edit_min_interval_s`; skipped when the text did not change.
- All writes to the chat are globally spaced by `min_send_interval_s`; HTTP 429 honours retry_after.
- The token is only in the request URL, which is never logged. The PIN is never logged.
"""
from __future__ import annotations

import hmac
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from copybot import log
from copybot.config import Config

COMMANDS = ("/status", "/positions", "/leaders", "/progress", "/pause", "/resume", "/flatten", "/help",
            "/sol", "/solpositions", "/solleaders", "/solprogress", "/solpause", "/solresume", "/solflatten")


class TgError(Exception):
    def __init__(self, code: int, desc: str, retry_after: float = 0):
        super().__init__(f"telegram {code}: {desc}")
        self.code, self.desc, self.retry_after = code, desc, retry_after


class TgApi:
    def __init__(self, base: str, token: str):
        self._url = f"{base}/bot{token}/"

    def call(self, method: str, params: dict, timeout: float = 10.0):
        req = urllib.request.Request(self._url + method, json.dumps(params).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = {"description": f"HTTP {e.code}"}
            ra = float((body.get("parameters") or {}).get("retry_after", 0))
            raise TgError(e.code, body.get("description", ""), ra) from None
        except urllib.error.URLError as e:
            raise TgError(0, f"network: {e.reason}") from None
        if not body.get("ok"):
            raise TgError(int(body.get("error_code", 0)), body.get("description", ""))
        return body["result"]


@dataclass
class Card:
    key: str
    msg_id: int | None
    text: str = ""            # last text Telegram has
    want: str = ""            # text we want shown
    last_edit: float = 0.0
    final: bool = False


@dataclass
class Command:
    name: str
    arg: str = field(default="", repr=False)   # may hold the PIN: never logged


class TelegramUI:
    def __init__(self, cfg: Config, on_command, on_card_id, clock=time.monotonic):
        """on_command(Command) and on_card_id(key, msg_id | None) are called from worker threads; they must
        only enqueue work for the trading loop."""
        self.cfg = cfg
        self.enabled = bool(cfg.tg_token and cfg.tg_chat_id)
        self.api = TgApi(cfg.telegram.api_base, cfg.tg_token) if self.enabled else None
        self.chat = cfg.tg_chat_id
        self.on_command, self.on_card_id = on_command, on_card_id
        self.clock = clock
        self.outbox: queue.Queue = queue.Queue()
        self.cards: dict[str, Card] = {}
        self.lock = threading.Lock()
        self.last_write = 0.0
        self.blocked_until = 0.0
        self.offset = 0
        self.stop = threading.Event()
        self.wake = threading.Event()

    # ---- API used by the trading loop (non-blocking) ------------------------------------------
    def send(self, text: str) -> None:
        self.outbox.put(text)
        self.wake.set()

    def restore_card(self, key: str, msg_id: int) -> None:
        with self.lock:
            self.cards[key] = Card(key, msg_id)

    def set_card(self, key: str, text: str, new: bool = False) -> None:
        """Show `text` in the card `key`; creates the message if needed. new=True posts a fresh message
        (e.g. a new /status request) and abandons the old one."""
        with self.lock:
            c = self.cards.get(key)
            if c is None or new:
                c = self.cards[key] = Card(key, None)
            c.want = text
        self.wake.set()

    def final_card(self, key: str, text: str) -> None:
        with self.lock:
            c = self.cards.get(key) or Card(key, None)
            self.cards[key] = c
            c.want, c.final = text, True
        self.wake.set()

    def has_card(self, key: str) -> bool:
        with self.lock:
            return key in self.cards

    # ---- worker threads -----------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            log.warn("telegram_disabled", why="TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
            return
        threading.Thread(target=self._poll_loop, name="tg-poll", daemon=True).start()
        threading.Thread(target=self._out_loop, name="tg-out", daemon=True).start()

    def _write(self, method: str, params: dict):
        """One rate-limited write. Blocks this worker thread only."""
        while not self.stop.is_set():
            now = self.clock()
            wait = max(self.blocked_until - now, self.last_write + self.cfg.telegram.min_send_interval_s - now)
            if wait > 0:
                time.sleep(min(wait, 1.0))
                continue
            self.last_write = self.clock()
            try:
                return self.api.call(method, {"chat_id": self.chat, "parse_mode": "HTML",
                                              "disable_web_page_preview": True, **params})
            except TgError as e:
                if e.code == 429:
                    self.blocked_until = self.clock() + max(1.0, e.retry_after)
                    log.warn("telegram_429", retry_after=e.retry_after)
                    continue
                raise
        return None

    def _out_loop(self) -> None:
        while not self.stop.is_set():
            self.wake.wait(0.5)
            self.wake.clear()
            try:
                self._drain_once()
            except Exception:
                log.exception("telegram_out_error")
                time.sleep(2)

    def _drain_once(self) -> None:
        # new event messages first (they matter most), then card edits
        while True:
            try:
                text = self.outbox.get_nowait()
            except queue.Empty:
                break
            try:
                self._write("sendMessage", {"text": text})
            except TgError as e:
                log.error("telegram_send_failed", err=e.desc)
        with self.lock:
            cards = list(self.cards.values())
        for c in cards:
            if self.stop.is_set():
                return
            if not c.want or c.want == c.text:
                if c.final and c.want == c.text:
                    self._forget(c)
                continue
            if c.msg_id is None:
                try:
                    res = self._write("sendMessage", {"text": c.want})
                except TgError as e:
                    log.error("telegram_send_failed", err=e.desc, card=c.key)
                    continue
                c.msg_id, c.text, c.last_edit = res["message_id"], c.want, self.clock()
                self.on_card_id(c.key, c.msg_id)
            elif c.final or self.clock() - c.last_edit >= self.cfg.telegram.edit_min_interval_s:
                want = c.want
                try:
                    self._write("editMessageText", {"message_id": c.msg_id, "text": want})
                    c.text = want
                except TgError as e:
                    if "not modified" in e.desc:
                        c.text = want
                    elif "not found" in e.desc or "can't be edited" in e.desc:
                        c.msg_id = None   # deleted by the user / too old: post it again
                        continue
                    else:
                        log.error("telegram_edit_failed", err=e.desc, card=c.key)
                c.last_edit = self.clock()
            if c.final and c.text == c.want:
                self._forget(c)

    def _forget(self, c: Card) -> None:
        with self.lock:
            if self.cards.get(c.key) is c:
                del self.cards[c.key]
        self.on_card_id(c.key, None)

    def _poll_loop(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                ups = self.api.call("getUpdates", {"offset": self.offset, "timeout": self.cfg.telegram.poll_timeout_s,
                                                   "allowed_updates": ["message"]},
                                    timeout=self.cfg.telegram.poll_timeout_s + 10)
                backoff = 1.0
            except TgError as e:
                log.warn("telegram_poll_error", err=e.desc)
                time.sleep(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            for u in ups:
                self.offset = max(self.offset, u["update_id"] + 1)
                self.handle_update(u)

    def handle_update(self, u: dict) -> None:
        msg = u.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        name, _, arg = text.partition(" ")
        name = name.split("@")[0].lower()
        if chat != str(self.chat):
            log.warn("telegram_unauthorized", chat=chat, cmd=name)
            return
        if name not in COMMANDS:
            self.send("❓ Unknown command. /help")
            return
        log.info("telegram_command", cmd=name)     # never the argument (may be the PIN)
        self.on_command(Command(name, arg.strip()))

    def check_pin(self, given: str) -> bool:
        return bool(self.cfg.pin) and hmac.compare_digest(given.encode(), self.cfg.pin.encode())


HELP = ("🤖 <b>Copybot (paper)</b>\n"
        "/status – wallet, P&amp;L, health (live)\n"
        "/positions – open positions\n"
        "/leaders – followed wallets (live)\n"
        "/progress – success metrics\n"
        "/pause · /resume – new entries (exits always run)\n"
        "/flatten &lt;PIN&gt; – close everything and pause")
