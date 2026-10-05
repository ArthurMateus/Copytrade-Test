import logging
import queue
import time

import pytest

from copybot import config, log, tgfmt
from copybot.ledger import Position, State
from copybot.risk import Health
from copybot.tg import TelegramUI
from tests.fakes import FakeTelegram

PIN = "8642"


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def tg(tmp_path):
    fake = FakeTelegram()
    cfg = config.load("config", env={"TELEGRAM_BOT_TOKEN": fake.token, "TELEGRAM_CHAT_ID": str(fake.chat_id),
                                     "COPYBOT_PIN": PIN})
    cfg.telegram.api_base = fake.api_base
    cfg.telegram.edit_min_interval_s = 0.4
    cfg.telegram.min_send_interval_s = 0.05
    cfg.telegram.poll_timeout_s = 1
    log.setup(str(tmp_path))
    log.add_secret(cfg.tg_token)
    log.add_secret(cfg.pin)
    cmds, ids = queue.Queue(), []
    ui = TelegramUI(cfg, cmds.put, lambda k, m: ids.append((k, m)))
    ui.start()
    yield fake, ui, cmds, ids, tmp_path
    ui.stop.set()
    fake.close()


def test_owner_commands_only_and_pin_never_logged(tg):
    fake, ui, cmds, ids, tmp = tg
    fake.say("/status", chat_id=999)          # stranger
    fake.say(f"/flatten {PIN}")
    fake.say("/pause@mybot")
    c1 = cmds.get(timeout=5)
    c2 = cmds.get(timeout=5)
    assert (c1.name, c1.arg) == ("/flatten", PIN) and ui.check_pin(c1.arg) and not ui.check_pin("0000")
    assert c2.name == "/pause"
    assert cmds.empty()
    for h in logging.getLogger("copybot").handlers:
        h.flush()
    text = (tmp / "copybot.log").read_text(encoding="utf-8")
    assert "telegram_unauthorized" in text and "cmd=/flatten" in text
    assert PIN not in text and fake.token not in text and "TESTTOKEN" not in text


def test_card_is_edited_in_place_rate_limited_and_skips_unchanged(tg):
    fake, ui, cmds, ids, _ = tg
    ui.set_card("pos:BTC", "v1")
    assert wait_for(lambda: len(fake.sent) == 1)
    mid = fake.sent[0]["message_id"]
    assert ids == [("pos:BTC", mid)]
    t0 = time.time()
    for i in range(2, 30):                    # 28 updates in ~0.6 s
        ui.set_card("pos:BTC", f"v{i}")
        time.sleep(0.02)
    assert wait_for(lambda: fake.messages[mid] == "v29")
    assert len(fake.sent) == 1                # never a new message
    times = [t for t, m in fake.calls if m == "editMessageText"]
    assert len(times) <= 1 + (time.time() - t0) / 0.4 + 1
    assert all(b - a >= 0.39 for a, b in zip(times, times[1:]))
    n = len(fake.edits)
    ui.set_card("pos:BTC", "v29")             # unchanged -> no call
    time.sleep(0.6)
    assert len(fake.edits) == n


def test_final_card_edits_the_same_message_then_forgets(tg):
    fake, ui, cmds, ids, _ = tg
    ui.set_card("pos:ETH", "open")
    assert wait_for(lambda: len(fake.sent) == 1)
    ui.final_card("pos:ETH", "✅ closed")
    assert wait_for(lambda: ("pos:ETH", None) in ids)
    assert fake.messages[fake.sent[0]["message_id"]] == "✅ closed"
    assert len(fake.sent) == 1 and not ui.has_card("pos:ETH")


def test_restored_card_keeps_editing_old_message_after_restart(tg):
    fake, ui, cmds, ids, _ = tg
    fake.messages[555] = "before restart"
    ui.restore_card("pos:SOL", 555)
    ui.set_card("pos:SOL", "after restart")
    assert wait_for(lambda: fake.messages[555] == "after restart")
    assert not fake.sent


def test_deleted_message_is_reposted(tg):
    fake, ui, cmds, ids, _ = tg
    ui.restore_card("status", 999999)          # unknown to telegram
    ui.set_card("status", "s")
    assert wait_for(lambda: len(fake.sent) == 1)


def test_429_is_respected(tg):
    fake, ui, cmds, ids, _ = tg
    fake.fail_429 = 1
    ui.send("⚠️ hello")
    assert wait_for(lambda: len(fake.sent) == 1, timeout=5)
    calls = [t for t, m in fake.calls if m == "sendMessage"]
    assert calls[1] - calls[0] >= 0.95


def test_trade_card_has_every_field():
    p = Position(pos_id="x", coin="BTC", side=1, size=0.001, entry_px=100_000, stop_px=97_000, leverage=9,
                 leader="0x1234567890abcdef1234567890abcdef12345678", k=1, open_oid=1, opened_ms=0,
                 realized=-0.05, entry_notional=100, open_lag_ms=1234)
    s = tgfmt.trade_card(p, 101_000, 1_790_000_000_000)
    for needle in ("🟢 LONG", "BTC", "9x", "lag 1.2s", "100,000.0", "101,000.0", "97,000.0 🛑", "+1.00$",
                   "+1.00%", "−0.05$", "0x1234…5678", "Upd"):
        assert needle in s, needle
    t = {"coin": "BTC", "side": -1, "entry": 100.0, "exit": 90.0, "pnl": 9.9, "fees": 0.1, "notional": 100.0,
         "reason": "leader_close", "opened_ms": 0, "closed_ms": 3_600_000, "leader": p.leader}
    c = tgfmt.closed_card(t)
    assert "✅ WIN" in c and "SHORT" in c and "+9.90$" in c and "+9.90%" in c and "leader closed" in c
    assert "❌ LOSS" in tgfmt.closed_card({**t, "pnl": -1.0})


def test_status_leaders_progress_render():
    st = State(equity0=300, btc_px0=100_000, genesis_ms=0)
    st.followed = {"0x" + "a" * 40: 0}
    st.marks = {"day": {"key": "d", "equity": 300}}
    h = Health(now_ms=0, mids_age_s=0.2, clock_ok=True, feed_age_s=1, feed_connected=True)
    s = tgfmt.status_card(st, {}, h, 0, 101_000)
    assert "Equity" in s and "300.00$" in s and "▶️ copying" in s and "+1.00%" in s
    st.entries_paused, st.pause_reason = True, "daily loss"
    assert "⏸️" in tgfmt.status_card(st, {}, h, 0, None)
    assert "🟢 0xaaaa…aaaa" in tgfmt.leaders_card(st, {}, 0)
    assert "too early" in tgfmt.progress_text(st, {}, None, 0)
