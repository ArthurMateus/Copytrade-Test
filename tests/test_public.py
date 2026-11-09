"""The signal channels (docs/PRODUCTION_PLAN.md Phases 4/5, owner requests 2026-11-03 and 2026-11-09): members
(DISCORD_PAID_CHANNEL_ID) get one wallet's trades at once with what they need to copy them by hand, the free channel
(DISCORD_PUBLIC_CHANNEL_ID) gets the same posts `free_delay_min` later; both get the daily summary, members a weekly
report. Percentages only; commands and everything else stay in the owner's channel."""
import json
import time

from tests.test_e2e import env, stop_bot, wait_for  # noqa: F401 (env is a fixture)
from tests.test_invo import invo_env, start_with_invo  # noqa: F401 (invo_env is a fixture)

FREE, PAID = "5550099", "5550098"


def setup(cdir, monkeypatch, delay_min=0.05):
    today = time.gmtime(time.time() - 3 * 3600).tm_wday        # (config/discord.toml: utc_offset_hours = -3)
    with open(cdir / "discord.toml", "a", encoding="utf-8") as fh:
        fh.write(f"utc_offset_hours = -3.0\npublic_daily_hour = 0\nfree_delay_min = {delay_min}\n"
                 f"weekly_report_day = {today}\n")
    monkeypatch.setenv("DISCORD_PUBLIC_CHANNEL_ID", FREE)
    monkeypatch.setenv("DISCORD_PAID_CHANNEL_ID", PAID)


def test_members_get_trades_at_once_and_the_free_channel_30_minutes_later(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    (cdir / "discord.toml").write_text((cdir / "discord.toml").read_text(encoding="utf-8")
                                       .replace("utc_offset_hours", "#utc_offset_hours"), encoding="utf-8")
    setup(cdir, monkeypatch)                                    # free delay 3 s in this test
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    paid = lambda: [m["text"] for m in dc.public if m["channel"] == PAID]
    free = lambda: [m["text"] for m in dc.public if m["channel"] == FREE]
    try:
        assert wait_for(lambda: any("since the start" in t for t in paid()) and
                        any("since the start" in t for t in free()))          # the daily summary: both, at once
        assert wait_for(lambda: any("week" in t for t in paid()))              # the weekly report: members only
        assert not any("week" in t for t in free())
        dc.say("/follow nicush")
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=True, leverage=10, size=0.05, entry=2990.0)
        assert wait_for(lambda: any("🆕" in t and "ETH" in t for t in paid()), timeout=10)
        post = next(t for t in paid() if "🆕" in t)
        assert "Entry:" in post and "Stop:" in post and "of your balance as margin" in post
        assert not any("🆕" in t for t in free())                              # not yet for the free channel
        q = json.loads((data / "free_queue.json").read_text(encoding="utf-8"))
        assert q and "🆕" in q[0]["text"]                                       # kept across a restart
        assert wait_for(lambda: any("🆕" in t and "min ago" in t for t in free()), timeout=10)
        f.resize_call(pid, cid, 0.10)                                           # the trader doubles it
        assert wait_for(lambda: any("↕️" in t and "raised" in t for t in paid()), timeout=15)
        hl.mids["ETH"] = 3060.0
        f.close_call(pid, cid)
        assert wait_for(lambda: any("WIN" in t and "of the wallet" in t for t in paid()), timeout=10)
        assert wait_for(lambda: any("WIN" in t for t in free()), timeout=10)
        texts = paid() + free()
        assert not any("$" in t for t in texts)                 # percentages only
        assert not any("nicush" in t for t in texts)            # traders stay private (public_show_traders)
        assert sum("since the start" in t for t in paid()) == 1  # one summary a day
        assert not any("🆕" in m["text"] for m in dc.sent)       # the owner's channel is unchanged
    finally:
        stop_bot(bot, th)


def test_without_signal_channels_nothing_is_posted_elsewhere(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    monkeypatch.delenv("DISCORD_PUBLIC_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_PAID_CHANNEL_ID", raising=False)
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert wait_for(lambda: any("Lockstep started" in m["text"] for m in dc.sent))
        bot.public_tick()
        assert not dc.public
    finally:
        stop_bot(bot, th)
