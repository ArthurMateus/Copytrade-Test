"""Discord UI against a loopback fake of the REST API: owner-only commands, cards edited in place, rate limits."""
import time

import pytest

from copybot import config
from copybot.discord_ui import DiscordUI, UIGroup, to_markdown
from copybot.tg import Command
from tests.fakes_sol import FakeDiscord, wait_for

OWNER = "42"


@pytest.fixture
def dc(tmp_path):
    fake = FakeDiscord()
    cfg = config.load("config", env={"DISCORD_BOT_TOKEN": "tok-secret", "DISCORD_CHANNEL_ID": fake.channel,
                                     "DISCORD_OWNER_ID": OWNER, "COPYBOT_PIN": "1234"})
    cfg.discord.api_base = fake.url
    cfg.discord.edit_min_interval_s = 0.3
    cfg.discord.min_send_interval_s = 0.0
    cfg.discord.poll_interval_s = 0.05
    cmds, ids = [], []
    ui = DiscordUI(cfg, cmds.append, lambda k, m: ids.append((k, m)))
    ui.start()
    yield fake, ui, cmds, ids
    ui.stop.set()
    fake.close()


def test_html_becomes_markdown():
    t = "📊 <b>Status</b> · x &amp; y\n<pre>Equity  300.00$\nP&amp;L  +1.00$</pre> <code>abc</code> <i>upd</i>"
    md = to_markdown(t)
    assert "**Status**" in md and "x & y" in md and "```\nEquity  300.00$\nP&L  +1.00$\n```" in md
    assert "`abc`" in md and "*upd*" in md and "<" not in md
    assert len(to_markdown("x" * 5000)) <= 2000


def test_sends_messages_and_edits_cards_in_place(dc):
    fake, ui, cmds, ids = dc
    ui.send("<b>hello</b>")
    ui.set_card("pos:1", "<b>card</b> v1")
    assert wait_for(lambda: len(fake.sent) == 2)
    assert fake.sent[0]["content"] == "**hello**"
    mid = fake.sent[1]["id"]
    assert ("pos:1", mid) in ids
    ui.set_card("pos:1", "<b>card</b> v2")
    assert wait_for(lambda: fake.edits and fake.edits[-1]["content"] == "**card** v2")
    assert len(fake.sent) == 2                                    # edited, not re-posted
    ui.final_card("pos:1", "✅ final")
    assert wait_for(lambda: fake.edits[-1]["content"] == "✅ final")
    assert wait_for(lambda: ("pos:1", None) in ids)               # the card is forgotten once final


def test_restored_card_is_edited_not_reposted(dc):
    fake, ui, cmds, ids = dc
    m = fake.say("bot", "old card")
    ui.restore_card("pos:9", m["id"])
    ui.set_card("pos:9", "new text")
    assert wait_for(lambda: fake.edits and fake.edits[-1]["content"] == "new text")
    assert not fake.sent


def test_only_the_owner_can_command_and_old_messages_are_ignored(dc):
    fake, ui, cmds, ids = dc
    fake.say(OWNER, "!status")                                    # sent before the bot started reading: ignored
    time.sleep(0.3)
    assert cmds == []
    fake.say("999", "!status")                                    # a stranger
    fake.say(OWNER, "hello there")                                # not a command
    fake.say(OWNER, "!status")
    fake.say(OWNER, "!sol")
    assert wait_for(lambda: [c.name for c in cmds] == ["/status", "/sol"])


def test_unknown_command_gets_a_reply(dc):
    fake, ui, cmds, ids = dc
    time.sleep(0.2)
    fake.say(OWNER, "!nope")
    assert wait_for(lambda: any("Unknown command" in m["content"] for m in fake.sent))


def test_pin_message_is_deleted_and_never_logged(dc, caplog):
    fake, ui, cmds, ids = dc
    time.sleep(0.2)
    m = fake.say(OWNER, "!flatten 1234")
    assert wait_for(lambda: cmds and cmds[0] == Command("/flatten", "1234"))
    assert wait_for(lambda: m["id"] in fake.deleted)
    assert ui.check_pin("1234") and not ui.check_pin("0000") and not ui.check_pin("")
    assert "1234" not in caplog.text and "tok-secret" not in caplog.text


def test_rate_limit_429_is_waited_out_and_the_message_is_not_lost(dc):
    fake, ui, cmds, ids = dc
    fake.fail_429 = 2
    ui.send("important")
    assert wait_for(lambda: [m["content"] for m in fake.sent] == ["important"], timeout=10)


def test_the_bot_token_is_sent_as_a_bot_authorization_header(dc):
    fake, ui, cmds, ids = dc
    ui.send("x")
    assert wait_for(lambda: fake.sent)
    assert fake.auth_seen == {"Bot tok-secret"}


def test_disabled_without_credentials():
    cfg = config.load("config", env={})
    ui = DiscordUI(cfg, lambda c: None, lambda k, m: None)
    assert not ui.enabled
    ui.send("x")
    ui.start()                                                    # no threads, no crash
    assert ui.outbox.empty()


def test_group_fans_out_and_routes_restored_card_ids(dc):
    fake, ui, cmds, ids = dc

    class Tg:
        def __init__(self):
            self.log, self.stop = [], type("E", (), {"set": lambda s: self.log.append("stop")})()

        send = lambda self, t: self.log.append(("send", t))
        set_card = lambda self, k, t, new=False: self.log.append(("card", k))
        final_card = lambda self, k, t: self.log.append(("final", k))
        restore_card = lambda self, k, m: self.log.append(("restore", k, m))
        has_card = lambda self, k: False
        check_pin = lambda self, g: g == "1234"
        start = lambda self: self.log.append("start")

    tg = Tg()
    g = UIGroup(tg, ui)
    g.send("hi")
    g.set_card("sol:status", "s")
    assert ("send", "hi") in tg.log and wait_for(lambda: any(m["content"] == "hi" for m in fake.sent))
    g.restore_card("pos:1", 5)
    g.restore_card("dc:pos:1", 77)
    assert ("restore", "pos:1", 5) in tg.log and ("restore", "dc:pos:1", 77) not in tg.log
    assert ui.cards["pos:1"].msg_id == "77"
    assert g.check_pin("1234")
