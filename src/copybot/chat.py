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

# Three books. Hyperliquid commands are /hyper<x> (the short originals /status, /trades, ... are kept as aliases),
# the Solana/FOMO book's are /fomo<x>, Invo's /invo<x>. `canon` turns every name into one canonical name.
ALIASES = {"/hyperstatus": "/status", "/hypertrades": "/trades", "/hypertraders": "/traders",
           "/hyperwallet": "/wallets", "/hyperwallets": "/wallets", "/hyperpositions": "/positions",
           "/hyperleaders": "/leaders", "/hyperprogress": "/progress", "/hypersearch": "/search",
           "/hyperpause": "/pause", "/hyperresume": "/resume", "/hyperflatten": "/flatten", "/hyperreset": "/reset",
           "/hyperadd": "/add",
           "/fomostatus": "/fomo"}
HYPER_COMMANDS = ("/hyperstatus", "/hypertrades", "/hypertraders", "/hyperwallet", "/hyperpositions",
                  "/hyperleaders", "/hyperprogress", "/hypersearch", "/hyperpause", "/hyperresume", "/hyperflatten",
                  "/hyperreset", "/hyperadd")
FOMO_COMMANDS = ("/fomo", "/fomotrades", "/fomotraders", "/fomowallet", "/fomopositions", "/fomoleaders",
                 "/fomoprogress", "/fomosearch", "/fomopause", "/fomoresume", "/fomoflatten", "/fomoreset",
                 "/fomoadd", "/fomofollow", "/fomounfollow", "/fomowallets")
INVO_COMMANDS = ("/invo", "/invotrades", "/invotraders", "/invowallets", "/invofollow", "/invounfollow", "/invosearch")
COMMANDS = ("/status", "/trades", "/traders", "/wallets", "/positions", "/leaders", "/progress", "/search", "/pause",
            "/resume", "/flatten", "/reset", "/restart", "/help", "/add", "/picks") + tuple(ALIASES) + FOMO_COMMANDS \
    + INVO_COMMANDS
# what Discord shows in its slash-command list
SLASH_COMMANDS = ("/help", "/restart", "/picks") + HYPER_COMMANDS + FOMO_COMMANDS + INVO_COMMANDS
PIN_COMMANDS = ("/flatten", "/reset", "/fomoflatten", "/fomoreset")
WALLET_COMMANDS = ("/add", "/fomoadd", "/fomofollow", "/fomounfollow", "/invofollow", "/invounfollow")   # take a wallet
                                                                                     # address or username argument


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


HELP = ("🤖 <b>Copybot (paper)</b>\n\n"
        "⚡ <b>Hyperliquid</b>\n"
        "/hyperstatus – wallet, P&amp;L, health (live)\n"
        "/hypertrades – open trades at live prices + P&amp;L vs the start (live)\n"
        "/hypertraders – followed traders and what copying them earned (live)\n"
        "/hyperwallet – the same copies at 1/2/5/10/20% risk, compared (live)\n"
        "/hyperpositions – open positions (short list)\n"
        "/hyperleaders – followed wallets (live)\n"
        "/hyperprogress – success metrics, long vs short\n"
        "/hyperpause · /hyperresume – new entries (exits always run)\n"
        "/hyperflatten &lt;PIN&gt; – close everything and pause\n"
        "/hypersearch – look for new traders now and re-pick the best 7\n"
        "/picks – the best traders of each book under the strict rules (also sent daily at 13h), with the follow "
        "commands\n"
        "/hyperadd 0x… – check one wallet with the strict rules, follow it if it passes\n"
        "/invo · /invotrades · /invotraders · /invowallets – the Invo wallets (live cards)\n"
        "/invofollow &lt;user&gt; · /invounfollow &lt;user&gt; – copy Invo traders' posted calls (paper)\n"
        "/invosearch – search Invo traders now with the strict rules and follow the best 7\n"
        "/hyperreset &lt;PIN&gt; – every Hyperliquid wallet back to the start (no open trades), traders kept\n\n"
        "/restart – restart the bot")
