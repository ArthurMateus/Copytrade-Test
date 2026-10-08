"""Discord UI against a loopback fake of Discord's REST API and Gateway (only the network is faked)."""
import queue
import threading

import pytest

from copybot import config, log
from copybot.discord import BLURPLE, GREEN, RED, DiscordUI, color_for, embed, html_to_md
from copybot.runner import Bot
from tests.fakes import FakeDiscord, FakeHL, FakeTelegram
from tests.test_e2e import LEADER, PIN, env_for, leader_fill, seed_ledger, wait_for, write_config


def dc_cfg(fake: FakeDiscord, **env):
    cfg = config.load("config", env={"DISCORD_BOT_TOKEN": fake.token, "DISCORD_CHANNEL_ID": fake.channel,
                                     "DISCORD_OWNER_ID": fake.owner, "COPYBOT_PIN": PIN, **env})
    cfg.discord.api_base, cfg.discord.gateway_url = fake.api_base, fake.gateway_url
    cfg.discord.edit_min_interval_s, cfg.discord.min_send_interval_s = 0.3, 0.02
    return cfg


@pytest.fixture
def dc():
    fake = FakeDiscord()
    cmds, cards = queue.Queue(), []
    ui = DiscordUI(dc_cfg(fake), cmds.put, lambda k, m: cards.append((k, m)))
    ui.start()
    yield fake, ui, cmds, cards
    ui.stop.set()
    fake.close()


# ---- formatting -------------------------------------------------------------------------------------
def test_html_becomes_discord_markdown():
    md = html_to_md("🟢 <b>BTC</b> ⬆️ LONG\nStop-loss: <b>97,000 · 4.0% away</b>\n<code>0xab…cd</code>\n"
                    "<i>updated 08:30</i> 5 * 3 _x_ &lt;PIN&gt;")
    assert md == ("🟢 **BTC** ⬆️ LONG\nStop-loss: **97,000 · 4.0% away**\n`0xab…cd`\n*updated 08:30* "
                  "5 \\* 3 \\_x\\_ <PIN>")


def test_embed_colour_follows_the_money_and_is_capped():
    assert color_for("🟢 <b>BTC</b> ⬆️ LONG · +1.00$") == GREEN
    assert color_for("❌ LOSS · <b>BTC</b>") == RED
    assert color_for("💼 <b>Trades</b> · 1 open\n🔴 <b>Total: −1.00$</b>") == RED
    assert color_for("📊 <b>Status</b> · copying") == BLURPLE
    e = embed("x" * 5000)
    assert len(e["description"]) <= 4096


# ---- commands --------------------------------------------------------------------------------------
def test_slash_commands_registered_and_only_the_owner_commands(dc):
    fake, ui, cmds, _ = dc
    assert wait_for(lambda: fake.commands is not None and fake.connected())
    names = {c["name"] for c in fake.commands}
    assert {"hyperstatus", "hypertrades", "hypertraders", "hyperwallet", "hyperflatten", "hyperpause", "hyperresume",
            "hyperreset", "fomo", "fomotrades", "fomotraders", "fomowallet", "fomoreset", "help"} <= names
    assert "status" not in names and "flatten" not in names            # Discord lists the clear /hyper* and /fomo* names
    flat = next(c for c in fake.commands if c["name"] == "hyperflatten")
    assert flat["options"][0]["name"] == "pin" and flat["options"][0]["required"]
    assert fake.identified[0]["intents"] == 0
    fake.interact("hyperstatus")                              # /hyperstatus is /status for the bot
    c = cmds.get(timeout=5)
    assert c.name == "/status" and c.arg == ""
    fake.interact("hyperflatten", {"pin": PIN})
    c = cmds.get(timeout=5)
    assert c.name == "/flatten" and c.arg == PIN
    fake.interact("hyperstatus", user="1234")                     # someone else in the server
    assert wait_for(lambda: any("Only the owner" in r["data"]["content"] for r in fake.replies))
    assert cmds.empty()
    assert all(r["data"]["flags"] == 64 for r in fake.replies)   # every answer is private (ephemeral)


def test_cards_are_edited_in_place_and_reposted_if_deleted(dc):
    fake, ui, _, cards = dc
    ui.set_card("pos:1", "🟢 <b>ETH</b> ⬆️ LONG · +1.00$")
    assert wait_for(lambda: len(fake.sent) == 1)
    mid = fake.sent[0]["id"]
    assert wait_for(lambda: ("pos:1", mid) in cards)
    ui.set_card("pos:1", "🔴 <b>ETH</b> ⬆️ LONG · −2.00$")
    assert wait_for(lambda: "−2.00$" in fake.text(mid))
    assert fake.messages[mid]["color"] == RED and len(fake.sent) == 1
    del fake.messages[mid]                                    # the owner deleted the message
    ui.set_card("pos:1", "🟢 <b>ETH</b> ⬆️ LONG · +3.00$")
    assert wait_for(lambda: len(fake.sent) == 2 and "+3.00$" in fake.sent[1]["description"])


def test_rate_limit_is_honoured(dc):
    fake, ui, _, _ = dc
    fake.fail_429 = 1
    ui.send("⚠️ hello")
    assert wait_for(lambda: len(fake.sent) == 1, timeout=6)
    posts = [t for t, c in fake.calls if c.startswith("POST")]
    assert posts[1] - posts[0] >= 0.95


def test_gateway_reconnects_after_a_drop(dc):
    fake, ui, cmds, _ = dc
    assert wait_for(fake.connected)
    fake.drop()
    assert wait_for(lambda: len(fake.identified) >= 2, timeout=10)
    assert wait_for(fake.connected)
    fake.interact("help")
    assert cmds.get(timeout=5).name == "/help"


# ---- the whole bot on Telegram AND Discord ------------------------------------------------------------
def test_full_bot_on_telegram_and_discord(tmp_path):
    hl, tg, fake = FakeHL(), FakeTelegram(), FakeDiscord()
    data = tmp_path / "data"
    data.mkdir()
    cdir = write_config(tmp_path, hl, tg, data)
    (cdir / "discord.toml").write_text(f'api_base = "{fake.api_base}"\ngateway_url = "{fake.gateway_url}"\n'
                                       f'edit_min_interval_s = 0.3\nmin_send_interval_s = 0.02\n', encoding="utf-8")
    seed_ledger(data)
    e = {**env_for(tg), "DISCORD_BOT_TOKEN": fake.token, "DISCORD_CHANNEL_ID": fake.channel,
         "DISCORD_OWNER_ID": fake.owner}
    cfg = config.load(cdir, env=e)
    log.setup(str(data / "logs"))
    for s in (cfg.tg_token, cfg.pin, cfg.dc_token):
        log.add_secret(s)
    bot = Bot(cfg)
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    try:
        assert wait_for(lambda: LEADER in hl.subscribed_users() and bot.health().clock_ok and fake.connected())
        assert wait_for(lambda: any("Copybot started" in m["description"] for m in fake.sent))
        hl.push_fills(LEADER, [leader_fill(hl, "ETH", 50, "B")])
        # the same live trade card on both platforms
        assert wait_for(lambda: any("<b>ETH</b> ⬆️ LONG" in m["text"] for m in tg.sent))
        assert wait_for(lambda: any("**ETH** ⬆️ LONG" in m["description"] for m in fake.sent))
        card = next(m["id"] for m in fake.sent if "**ETH** ⬆️ LONG" in m["description"])
        hl.mids["ETH"] = 3030.0
        assert wait_for(lambda: "3,030" in fake.text(card) or "3030" in fake.text(card))
        # a Discord command answers in both chats (one bot, one state)
        fake.interact("hypertrades")
        assert wait_for(lambda: any("💼 **Trades** · 1 open" in m["description"] for m in fake.sent))
        assert wait_for(lambda: any("💼 <b>Trades</b> · 1 open" in m["text"] for m in tg.sent))
        # restart keeps editing the same Discord card (its id is in the ledger under dc:)
        assert wait_for(lambda: f"dc:pos:{bot.st.positions['ETH'].pos_id}" in bot.st.cards)
        # /flatten with the PIN from Discord closes everything; the PIN never reaches the log or the channel
        fake.interact("hyperflatten", {"pin": PIN})
        assert wait_for(lambda: not bot.st.positions and bot.st.entries_paused)
        assert wait_for(lambda: "✅ WIN" in fake.text(card) or "❌ LOSS" in fake.text(card))
        for h in log.log.handlers:
            h.flush()
        text = (data / "logs" / "copybot.log").read_text(encoding="utf-8")
        assert PIN not in text and fake.token not in text and "event=discord_command cmd=/flatten" in text
        assert not any(PIN in m["description"] for m in fake.sent)
    finally:
        bot.stop.set()
        th.join(5)
        bot.shutdown()
        hl.close()
        tg.close()
        fake.close()
