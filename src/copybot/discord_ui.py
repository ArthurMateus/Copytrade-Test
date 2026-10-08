"""Discord: owner-only text commands (prefix, default "!") and a rate-limited outbox that edits live cards in place.

Same interface as TelegramUI (send / set_card / final_card / restore_card / has_card / start / stop), so the bot
talks to both through UIGroup. Plain REST + polling of one channel: no gateway websocket, no extra library.
Needs a bot token (DISCORD_BOT_TOKEN), the channel id (DISCORD_CHANNEL_ID) and the owner's user id
(DISCORD_OWNER_ID). Reading command text needs the "Message Content Intent" switched on for the bot in the
Discord developer portal. Only the owner can command it; a command message that carries the PIN is deleted.
The token is only in the request header, which is never logged.
"""
from __future__ import annotations

import hmac
import html
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request

from copybot import log
from copybot.config import Config
from copybot.tg import COMMANDS, Card, Command

MAX_LEN = 1990


class DcError(Exception):
    def __init__(self, code: int, desc: str, retry_after: float = 0.0):
        super().__init__(f"discord {code}: {desc}")
        self.code, self.desc, self.retry_after = code, desc, retry_after


class DcApi:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self._h = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
                   "User-Agent": "DiscordBot (https://github.com/ArthurMateus/Copytrade-Test, 0.1)"}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 10.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data, self._h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                b = json.loads(e.read())
            except Exception:
                b = {}
            raise DcError(e.code, str(b.get("message") or f"HTTP {e.code}"), float(b.get("retry_after") or 0)) from None
        except urllib.error.URLError as e:
            raise DcError(0, f"network: {e.reason}") from None


_TAGS = [(r"<b>(.*?)</b>", r"**\1**"), (r"<i>(.*?)</i>", r"*\1*"), (r"<code>(.*?)</code>", r"`\1`")]


def to_markdown(text: str) -> str:
    """Telegram HTML (b, i, code, pre) -> Discord markdown."""
    text = re.sub(r"<pre>(.*?)</pre>", lambda m: "```\n" + m.group(1).strip("\n") + "\n```", text, flags=re.S)
    for pat, rep in _TAGS:
        text = re.sub(pat, rep, text, flags=re.S)
    text = re.sub(r"</?[a-z]+[^>]*>", "", text)
    text = html.unescape(text)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN - 3] + "..."


class DiscordUI:
    def __init__(self, cfg: Config, on_command, on_card_id, clock=time.monotonic):
        self.cfg, self.dc = cfg, cfg.discord
        self.enabled = bool(cfg.discord_token and cfg.discord_channel and cfg.discord_owner)
        self.api = DcApi(self.dc.api_base, cfg.discord_token) if self.enabled else None
        self.channel, self.owner = cfg.discord_channel, str(cfg.discord_owner)
        self.on_command, self.on_card_id, self.clock = on_command, on_card_id, clock
        self.outbox: queue.Queue = queue.Queue()
        self.cards: dict[str, Card] = {}
        self.lock = threading.Lock()
        self.last_write = 0.0
        self.blocked_until = 0.0
        self.last_id: str | None = None
        self.warned_content = False
        self.stop = threading.Event()
        self.wake = threading.Event()

    # ---- API used by the trading loop (non-blocking) -----------------------------------------------
    def send(self, text: str) -> None:
        if self.enabled:
            self.outbox.put(text)
            self.wake.set()

    def restore_card(self, key: str, msg_id) -> None:
        with self.lock:
            self.cards[key] = Card(key, str(msg_id))

    def set_card(self, key: str, text: str, new: bool = False) -> None:
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

    def check_pin(self, given: str) -> bool:
        return bool(self.cfg.pin) and hmac.compare_digest(given.encode(), self.cfg.pin.encode())

    # ---- threads ------------------------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._poll_loop, name="dc-poll", daemon=True).start()
        threading.Thread(target=self._out_loop, name="dc-out", daemon=True).start()

    def _write(self, method: str, path: str, body: dict | None):
        while not self.stop.is_set():
            now = self.clock()
            wait = max(self.blocked_until - now, self.last_write + self.dc.min_send_interval_s - now)
            if wait > 0:
                time.sleep(min(wait, 1.0))
                continue
            self.last_write = self.clock()
            try:
                return self.api.call(method, path, body)
            except DcError as e:
                if e.code == 429:
                    self.blocked_until = self.clock() + max(1.0, e.retry_after)
                    log.warn("discord_429", retry_after=e.retry_after)
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
                log.exception("discord_out_error")
                time.sleep(2)

    def _drain_once(self) -> None:
        while True:
            try:
                text = self.outbox.get_nowait()
            except queue.Empty:
                break
            try:
                self._write("POST", f"/channels/{self.channel}/messages", {"content": to_markdown(text)})
            except DcError as e:
                log.error("discord_send_failed", err=e.desc)
        with self.lock:
            cards = list(self.cards.values())
        for c in cards:
            if self.stop.is_set():
                return
            if not c.want or c.want == c.text:
                if c.final and c.want == c.text:
                    self._forget(c)
                continue
            body = {"content": to_markdown(c.want)}
            if c.msg_id is None:
                try:
                    res = self._write("POST", f"/channels/{self.channel}/messages", body)
                except DcError as e:
                    log.error("discord_send_failed", err=e.desc, card=c.key)
                    continue
                c.msg_id, c.text, c.last_edit = str(res["id"]), c.want, self.clock()
                self.on_card_id(c.key, c.msg_id)
            elif c.final or self.clock() - c.last_edit >= self.dc.edit_min_interval_s:
                want = c.want
                try:
                    self._write("PATCH", f"/channels/{self.channel}/messages/{c.msg_id}", body)
                    c.text = want
                except DcError as e:
                    if e.code == 404:
                        c.msg_id = None   # deleted by the user: post it again
                        continue
                    log.error("discord_edit_failed", err=e.desc, card=c.key)
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
                self.poll_once()
                backoff = 1.0
                self.stop.wait(self.dc.poll_interval_s)
            except DcError as e:
                log.warn("discord_poll_error", err=e.desc)
                self.stop.wait(backoff)
                backoff = min(60.0, backoff * 2)

    def poll_once(self) -> None:
        if self.last_id is None:   # start from "now": old messages are never executed
            latest = self.api.call("GET", f"/channels/{self.channel}/messages?limit=1") or []
            self.last_id = latest[0]["id"] if latest else "0"
            return
        msgs = self.api.call("GET", f"/channels/{self.channel}/messages?limit=50&after={self.last_id}") or []
        for m in sorted(msgs, key=lambda x: int(x["id"])):
            self.last_id = m["id"]
            self.handle_message(m)

    def handle_message(self, m: dict) -> None:
        author = m.get("author") or {}
        if author.get("bot"):
            return
        text = (m.get("content") or "").strip()
        if not text:
            if not self.warned_content:
                self.warned_content = True
                log.warn("discord_empty_content", why="enable MESSAGE CONTENT INTENT for the bot in the developer portal")
            return
        if not text.startswith(self.dc.prefix):
            return
        name, _, arg = text[len(self.dc.prefix):].partition(" ")
        name = "/" + name.lower()
        if str(author.get("id")) != self.owner:
            log.warn("discord_unauthorized", user=author.get("id"), cmd=name)
            return
        if name not in COMMANDS:
            self.send("❓ Unknown command. !help")
            return
        log.info("discord_command", cmd=name)         # never the argument (may be the PIN)
        if arg.strip():                               # a PIN must not stay in the channel history
            try:
                self.api.call("DELETE", f"/channels/{self.channel}/messages/{m['id']}")
            except DcError:
                log.warn("discord_pin_message_not_deleted", why="give the bot the Manage Messages permission")
        self.on_command(Command(name, arg.strip()))


class _StopAll:
    def __init__(self, uis):
        self.uis = uis

    def set(self):
        for u in self.uis:
            u.stop.set()


class UIGroup:
    """Telegram + Discord behind one interface. Card ids of the Discord copy are stored under 'dc:<key>'."""

    def __init__(self, tg, dc):
        self.tg, self.dc = tg, dc
        self.uis = [u for u in (tg, dc) if u is not None]
        self.stop = _StopAll(self.uis)

    def send(self, text: str) -> None:
        for u in self.uis:
            u.send(text)

    def set_card(self, key: str, text: str, new: bool = False) -> None:
        for u in self.uis:
            u.set_card(key, text, new=new)

    def final_card(self, key: str, text: str) -> None:
        for u in self.uis:
            u.final_card(key, text)

    def restore_card(self, key: str, msg_id) -> None:
        if key.startswith("dc:"):
            if self.dc is not None:
                self.dc.restore_card(key[3:], msg_id)
        else:
            self.tg.restore_card(key, msg_id)

    def has_card(self, key: str) -> bool:
        return any(u.has_card(key) for u in self.uis)

    def check_pin(self, given: str) -> bool:
        return self.tg.check_pin(given)

    def start(self) -> None:
        for u in self.uis:
            u.start()
