"""The chat side of the bot, independent of the platform (Discord: copybot/discord.py; Telegram was removed at the
owner's request on 2026-10-10).

- The command names, their aliases and the help text.
- `ChatUI`: a rate-limited outbox. New messages only for new events; live cards (one per open trade, /status,
  /leaders ...) are EDITED in place, at most every `edit_min_interval_s` and only when their text changed. All writes
  are globally spaced by `min_send_interval_s`; HTTP 429 honours retry_after. A platform subclass implements
  `_call("sendMessage" | "editMessageText", params)` and `start()`.
- Message text is a small HTML subset (<b>, <i>, <code>, <pre>, &lt; &gt; &amp;) that the platform converts.
- The PIN is never logged.
"""
from __future__ import annotations

import hmac
import queue
import threading
import time
from dataclasses import dataclass, field

from copybot import log
from copybot.config import Config

# Three books with the SAME 13 commands each (owner request 2026-10-10): /<book><verb> for book in hyper, fomo, invo and
# verb in VERBS. `canon` turns every name into the one canonical name a book's handler uses: Hyperliquid's are the
# short originals (/status, /trades ...), FOMO's and Invo's keep their prefix (/fomo = /fomostatus, /invo = /invostatus).
# Older names (/hyperpositions, /fomoleaders, /fomowallet ...) are kept as aliases of the new ones.
BOOKS = ("hyper", "fomo", "invo")
VERBS = ("status", "trades", "traders", "wallets", "progress", "search", "add", "follow", "unfollow", "pause", "resume",
         "flatten", "reset")
HYPER_COMMANDS = tuple(f"/hyper{v}" for v in VERBS)
FOMO_COMMANDS = tuple(f"/fomo{v}" for v in VERBS)
INVO_COMMANDS = tuple(f"/invo{v}" for v in VERBS)
INVO_EXTRA = ("/invoblock", "/invounblock")       # Invo only: block one portfolio of a followed trader
ALIASES = {f"/hyper{v}": f"/{v}" for v in VERBS}
ALIASES.update({"/fomostatus": "/fomo", "/invostatus": "/invo",
                # older names
                "/hyperwallet": "/wallets", "/hyperpositions": "/trades", "/hyperleaders": "/traders",
                "/positions": "/trades", "/leaders": "/traders",
                "/fomowallet": "/fomo", "/fomopositions": "/fomotrades", "/fomoleaders": "/fomotraders"})
COMMANDS = tuple(dict.fromkeys(("/help", "/restart", "/picks", "/fomo", "/invo") + tuple(f"/{v}" for v in VERBS)
                               + tuple(ALIASES) + FOMO_COMMANDS + INVO_COMMANDS + INVO_EXTRA))
# what Discord shows in its slash-command list
SLASH_COMMANDS = ("/help", "/restart", "/picks") + HYPER_COMMANDS + FOMO_COMMANDS + INVO_COMMANDS + INVO_EXTRA
PIN_COMMANDS = ("/flatten", "/reset", "/fomoflatten", "/fomoreset", "/invoflatten", "/invoreset")
# take a wallet address (or Invo username) as their argument
WALLET_COMMANDS = ("/add", "/follow", "/unfollow", "/fomoadd", "/fomofollow", "/fomounfollow", "/invoadd",
                   "/invofollow", "/invounfollow", "/invoblock", "/invounblock")


def canon(name: str) -> str:
    return ALIASES.get(name, name)


class ChatError(Exception):
    def __init__(self, code: int, desc: str, retry_after: float = 0):
        super().__init__(f"chat {code}: {desc}")
        self.code, self.desc, self.retry_after = code, desc, retry_after


@dataclass
class Card:
    key: str
    msg_id: int | None
    text: str = ""            # last text the chat has
    want: str = ""            # text we want shown
    last_edit: float = 0.0
    final: bool = False


@dataclass
class Command:
    name: str
    arg: str = field(default="", repr=False)   # may hold the PIN: never logged


class ChatUI:
    name = "chat"

    def __init__(self, cfg: Config, on_command, on_card_id, limits, clock=time.monotonic):
        """on_command(Command) and on_card_id(key, msg_id | None) are called from worker threads; they must
        only enqueue work for the trading loop. `limits` has edit_min_interval_s and min_send_interval_s."""
        self.cfg = cfg
        self.enabled = False
        self.on_command, self.on_card_id = on_command, on_card_id
        self.clock = clock
        self.outbox: queue.Queue = queue.Queue()
        self.cards: dict[str, Card] = {}
        self.lock = threading.Lock()
        self.last_write = 0.0
        self.blocked_until = 0.0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.started = time.time()
        self.limits = limits

    # ---- API used by the trading loop (non-blocking) ------------------------------------------
    def send(self, text: str) -> None:
        if self.enabled:
            self.outbox.put(text)
            self.wake.set()

    def restore_card(self, key: str, msg_id: int) -> None:
        with self.lock:
            self.cards[key] = Card(key, msg_id)

    def set_card(self, key: str, text: str, new: bool = False) -> None:
        """Show `text` in the card `key`; creates the message if needed. new=True posts a fresh message
        (e.g. a new /status request) and abandons the old one."""
        if not self.enabled:
            return
        with self.lock:
            c = self.cards.get(key)
            if c is None or new:
                c = self.cards[key] = Card(key, None)
            c.want = text
        self.wake.set()

    def final_card(self, key: str, text: str) -> None:
        if not self.enabled:
            return
        with self.lock:
            c = self.cards.get(key) or Card(key, None)
            self.cards[key] = c
            c.want, c.final = text, True
        self.wake.set()

    def has_card(self, key: str) -> bool:
        with self.lock:
            return key in self.cards

    def check_pin(self, given: str) -> bool:
        return bool(self.cfg.pin) and hmac.compare_digest(given.encode(), self.cfg.pin.encode())

    def shutdown(self) -> None:
        self.stop.set()

    # ---- the outbox worker ----------------------------------------------------------------------
    def _call(self, method: str, params: dict):
        raise NotImplementedError

    def _write(self, method: str, params: dict):
        """One rate-limited write. Blocks this worker thread only."""
        while not self.stop.is_set():
            now = self.clock()
            wait = max(self.blocked_until - now, self.last_write + self.limits.min_send_interval_s - now)
            if wait > 0:
                time.sleep(min(wait, 1.0))
                continue
            self.last_write = self.clock()
            try:
                return self._call(method, params)
            except ChatError as e:
                if e.code == 429:
                    self.blocked_until = self.clock() + max(1.0, e.retry_after)
                    log.warn(f"{self.name}_429", retry_after=e.retry_after)
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
                log.exception(f"{self.name}_out_error")
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
            except ChatError as e:
                log.error(f"{self.name}_send_failed", err=e.desc)
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
                except ChatError as e:
                    log.error(f"{self.name}_send_failed", err=e.desc, card=c.key)
                    continue
                c.msg_id, c.text, c.last_edit = res["message_id"], c.want, self.clock()
                self.on_card_id(c.key, c.msg_id)
            elif c.final or self.clock() - c.last_edit >= self.limits.edit_min_interval_s:
                want = c.want
                try:
                    self._write("editMessageText", {"message_id": c.msg_id, "text": want})
                    c.text = want
                except ChatError as e:
                    if "not modified" in e.desc:
                        c.text = want
                    elif "not found" in e.desc or "can't be edited" in e.desc:
                        c.msg_id = None   # deleted by the user / too old: post it again
                        continue
                    else:
                        log.error(f"{self.name}_edit_failed", err=e.desc, card=c.key)
                c.last_edit = self.clock()
            if c.final and c.text == c.want:
                self._forget(c)

    def _forget(self, c: Card) -> None:
        with self.lock:
            if self.cards.get(c.key) is c:
                del self.cards[c.key]
        self.on_card_id(c.key, None)


HELP = ("🤖 <b>Copybot (paper)</b> · three books, the same commands in each\n"
        "Put the book in front: <b>/hyper</b>… (Hyperliquid) · <b>/fomo</b>… (FOMO / Solana) · <b>/invo</b>… (Invo calls)\n\n"
        "status – the book at a glance (live)\n"
        "trades – open trades at live prices, P&amp;L vs the start (live)\n"
        "traders – followed traders and what copying them made (live)\n"
        "wallets – the same trades at other risk levels, compared (live)\n"
        "progress – success metrics\n"
        "search – look for traders now and follow the best 7\n"
        "add &lt;wallet or user&gt; – check it with the strict rules, follow it if it passes\n"
        "follow &lt;wallet or user&gt; – follow it without the rules (your pick) · unfollow &lt;…&gt;\n"
        "pause · resume – new copies in that book (exits and stops always run)\n"
        "flatten &lt;PIN&gt; – close every trade of that book and pause it\n"
        "reset &lt;PIN&gt; – that book back to the start (no open trades), traders kept\n\n"
        "Examples: /hypertrades · /fomoadd &lt;wallet&gt; · /invofollow &lt;user&gt;\n"
        "/picks – the best traders of each book (also daily at 13h) · /restart – restart the bot")
