"""Discord: the same owner-only commands and live cards as Telegram, over Discord's REST API + Gateway websocket.

- Reuses TelegramUI's outbox, rate limits and edit-in-place card logic; only the transport differs.
- Commands are guild slash commands (registered on connect). Only DISCORD_OWNER_ID may use them; every reply to
  an interaction is ephemeral (only the owner sees it), so /flatten's PIN never shows in the channel.
- Cards are the same Telegram HTML converted to Discord markdown, in an embed whose colour follows the money
  (green making / red losing).
- The bot token is only in a request header and the identify payload, never logged. The PIN is never logged.
"""
from __future__ import annotations

import html
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request

from websockets.sync.client import connect

from copybot import log
from copybot.config import Config
from copybot.tg import COMMANDS, Command, TelegramUI, TgError

GREEN, RED, ORANGE, BLURPLE = 0x2ECC71, 0xE74C3C, 0xF39C12, 0x5865F2
MAX_EMBED = 4096

DESCRIPTIONS = {
    "/status": "Wallet, P&L and health (live)",
    "/trades": "Open trades at live prices and P&L vs the start (live)",
    "/traders": "Followed traders and what copying them made (live)",
    "/wallets": "The same copies at 1/2/5/10/20% risk, compared (live)",
    "/positions": "Short list of open trades",
    "/leaders": "Followed traders, one line each (live)",
    "/progress": "Success metrics of the test",
    "/pause": "Pause new copies (exits and stops keep running)",
    "/resume": "Resume new copies",
    "/flatten": "Close EVERYTHING in every wallet and pause (needs the PIN)",
    "/search": "Look for new traders now and re-pick the best 7",
    "/reset": "Every wallet back to the start (no open trades); traders kept (needs the PIN)",
    "/restart": "Restart the bot",
    "/help": "List the commands",
}

_TAG = re.compile(r"(</?(?:b|i|code|pre)>)")
_MD = re.compile(r"([\\*_~`|])")


def html_to_md(text: str) -> str:
    """Telegram HTML (b, i, code, pre) -> Discord markdown, escaping markdown characters in plain text."""
    out, code = [], False
    for part in _TAG.split(text):
        if part in ("<b>", "</b>"):
            out.append("**")
        elif part in ("<i>", "</i>"):
            out.append("*")
        elif part in ("<code>", "</code>"):
            out.append("`")
            code = part == "<code>"
        elif part == "<pre>":
            out.append("```\n")
            code = True
        elif part == "</pre>":
            out.append("\n```")
            code = False
        else:
            t = html.unescape(part)
            out.append(t if code else _MD.sub(r"\\\1", t))
    return "".join(out)


def color_for(text: str) -> int:
    """Embed colour from the first money/status marker of the first two lines."""
    head = "\n".join(text.split("\n")[:2])
    marks = [(head.find(m), c) for m, c in (("🟢", GREEN), ("✅", GREEN), ("▶️", GREEN), ("🔴", RED), ("❌", RED),
                                            ("🛑", RED), ("⛔", RED), ("⚠️", ORANGE), ("⏸️", ORANGE)) if m in head]
    return min(marks)[1] if marks else BLURPLE


def embed(text: str) -> dict:
    md = html_to_md(text)
    if len(md) > MAX_EMBED:
        md = md[:MAX_EMBED - 2] + " …"
    return {"description": md, "color": color_for(text)}


class DiscordApi:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self._headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json",
                         "User-Agent": "DiscordBot (https://github.com/ArthurMateus/Copytrade-Test, 1.0)"}

    def call(self, verb: str, path: str, body: dict | list | None = None, timeout: float = 10.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data, self._headers, method=verb)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                b = json.loads(e.read())
            except Exception:
                b = {}
            desc = str(b.get("message", f"HTTP {e.code}")) if isinstance(b, dict) else f"HTTP {e.code}"
            if e.code == 404 or (isinstance(b, dict) and b.get("code") == 10008):
                desc = f"message to edit not found ({desc})"
            ra = float(b.get("retry_after", 0)) if isinstance(b, dict) else 0.0
            raise TgError(e.code, desc, ra) from None
        except urllib.error.URLError as e:
            raise TgError(0, f"network: {e.reason}") from None
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise TgError(0, f"network: {type(e).__name__}: {e}") from None


class DiscordUI(TelegramUI):
    def __init__(self, cfg: Config, on_command, on_card_id, clock=time.monotonic):
        super().__init__(cfg, on_command, on_card_id, clock)
        self.enabled = bool(cfg.dc_token and cfg.dc_channel_id and cfg.dc_owner_id)
        self.api = DiscordApi(cfg.discord.api_base, cfg.dc_token) if self.enabled else None
        self.channel = cfg.dc_channel_id
        self.owner = str(cfg.dc_owner_id)
        self.limits = cfg.discord
        self.name = "discord"
        self.registered = False
        self.connected = False

    # ---- transport (TelegramUI's outbox calls this) ---------------------------------------------
    def _call(self, method: str, params: dict):
        if method == "sendMessage":
            r = self.api.call("POST", f"/channels/{self.channel}/messages",
                              {"embeds": [embed(params["text"])], "allowed_mentions": {"parse": []}})
            return {"message_id": int(r["id"])}
        if method == "editMessageText":
            self.api.call("PATCH", f"/channels/{self.channel}/messages/{params['message_id']}",
                          {"embeds": [embed(params["text"])]})
            return {"message_id": params["message_id"]}
        raise ValueError(method)

    # ---- worker threads -------------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            log.warn("discord_disabled", why="DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID or DISCORD_OWNER_ID not set")
            return
        threading.Thread(target=self._gateway_loop, name="dc-gateway", daemon=True).start()
        threading.Thread(target=self._out_loop, name="dc-out", daemon=True).start()

    def _gateway_loop(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as e:   # never let the command thread die: /flatten must keep working
                log.warn("discord_gateway_error", err=f"{type(e).__name__}: {e}"[:200])
            self.connected = False
            self.stop.wait(backoff)
            backoff = min(60.0, backoff * 2)

    def _session(self) -> None:
        """One Gateway connection: hello -> identify -> heartbeats + dispatches, until it closes."""
        with connect(self.cfg.discord.gateway_url, open_timeout=10, max_size=None) as ws:
            hello = json.loads(ws.recv(timeout=15))
            interval = hello["d"]["heartbeat_interval"] / 1000
            ws.send(json.dumps({"op": 2, "d": {"token": self.cfg.dc_token, "intents": 0,
                                               "properties": {"os": "windows", "browser": "copybot",
                                                              "device": "copybot"}}}))
            seq, next_beat = None, time.monotonic() + interval * 0.5
            while not self.stop.is_set():
                now = time.monotonic()
                if now >= next_beat:
                    ws.send(json.dumps({"op": 1, "d": seq}))
                    next_beat = now + interval
                try:
                    raw = ws.recv(timeout=max(0.05, min(1.0, next_beat - now)))
                except TimeoutError:
                    continue
                m = json.loads(raw)
                if m.get("s") is not None:
                    seq = m["s"]
                op = m.get("op")
                if op == 0:
                    try:
                        self._dispatch(m.get("t"), m.get("d") or {})
                    except Exception:
                        log.exception("discord_dispatch_error", t=m.get("t"))
                elif op == 1:
                    next_beat = 0.0           # the gateway asks for a heartbeat now
                elif op in (7, 9):
                    log.warn("discord_reconnect_requested", op=op)
                    return

    def _dispatch(self, t: str, d: dict) -> None:
        if t == "READY":
            self.connected = True
            log.info("discord_connected")
            if not self.registered:
                self._register(d["application"]["id"])
        elif t == "INTERACTION_CREATE" and d.get("type") == 2:
            self._interaction(d)

    def _register(self, app_id: str) -> None:
        """Guild slash commands appear at once (global ones can take an hour)."""
        guild = self.api.call("GET", f"/channels/{self.channel}")["guild_id"]
        cmds = []
        for c in COMMANDS:
            cmd = {"name": c[1:], "description": DESCRIPTIONS.get(c, c[1:]), "type": 1}
            if c in ("/flatten", "/reset"):
                cmd["options"] = [{"type": 3, "name": "pin", "description": "Your COPYBOT_PIN", "required": True}]
            cmds.append(cmd)
        self.api.call("PUT", f"/applications/{app_id}/guilds/{guild}/commands", cmds)
        self.registered = True
        log.info("discord_commands_registered", n=len(cmds))

    def _reply(self, d: dict, text: str) -> None:
        """Answer the interaction, visible only to the user (Discord needs an answer within 3 s)."""
        try:
            self.api.call("POST", f"/interactions/{d['id']}/{d['token']}/callback",
                          {"type": 4, "data": {"content": text, "flags": 64}})
        except TgError as e:
            log.warn("discord_reply_failed", err=e.desc)

    def _interaction(self, d: dict) -> None:
        user = ((d.get("member") or {}).get("user") or d.get("user") or {}).get("id", "")
        name = "/" + (d.get("data") or {}).get("name", "")
        if str(user) != self.owner:
            log.warn("discord_unauthorized", user=user, cmd=name)
            return self._reply(d, "⛔ Only the owner can command this bot.")
        if name not in COMMANDS:
            return self._reply(d, "❓ Unknown command.")
        opts = {o["name"]: o.get("value", "") for o in (d.get("data") or {}).get("options") or []}
        log.info("discord_command", cmd=name)     # never the argument (may be the PIN)
        self._reply(d, f"👍 {name}" + (" (results in the channel)" if name not in ("/pause", "/resume", "/restart")
                                        else ""))
        self.on_command(Command(name, str(opts.get("pin", "")).strip()))


class MultiUI:
    """Fans every message and card out to all chats (Telegram and Discord). Card ids of Discord are kept in the
    ledger under 'dc:<key>' so both platforms keep editing their own messages after a restart."""

    def __init__(self, tg: TelegramUI, dc: DiscordUI):
        self.tg, self.dc = tg, dc
        self.uis = [tg, dc]

    def send(self, text: str) -> None:
        for u in self.uis:
            if u.enabled:
                u.send(text)

    def set_card(self, key: str, text: str, new: bool = False) -> None:
        for u in self.uis:
            if u.enabled:
                u.set_card(key, text, new)

    def final_card(self, key: str, text: str) -> None:
        for u in self.uis:
            if u.enabled:
                u.final_card(key, text)

    def restore_card(self, key: str, msg_id: int) -> None:
        if key.startswith("dc:"):
            self.dc.restore_card(key[3:], msg_id)
        else:
            self.tg.restore_card(key, msg_id)

    def check_pin(self, given: str) -> bool:
        return self.tg.check_pin(given)

    def start(self) -> None:
        for u in self.uis:
            u.start()

    def shutdown(self) -> None:
        for u in self.uis:
            u.stop.set()
