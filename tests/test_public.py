"""Phase 4 (docs/PRODUCTION_PLAN.md, owner request 2026-11-03): the public read-only Discord channel gets one wallet's
new trades, closed trades and a daily summary, in % only; commands and everything else stay in the owner's channel."""
from tests.test_e2e import env, stop_bot, wait_for  # noqa: F401 (env is a fixture)
from tests.test_invo import invo_env, start_with_invo  # noqa: F401 (invo_env is a fixture)

PUBLIC = "5550099"


def test_the_public_channel_shows_trades_and_the_day_in_percent_only(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    with open(cdir / "discord.toml", "a", encoding="utf-8") as fh:
        fh.write("public_daily_hour = 0\n")                     # (the summary is due at once)
    monkeypatch.setenv("DISCORD_PUBLIC_CHANNEL_ID", PUBLIC)
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert wait_for(lambda: any("Copy wallet" in m["text"] and "since the start" in m["text"]
                                    for m in dc.public))
        dc.say("/follow nicush")
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=True, leverage=10, size=0.05, entry=2990.0)
        assert wait_for(lambda: any("🆕" in m["text"] and "ETH" in m["text"] for m in dc.public), timeout=10)
        hl.mids["ETH"] = 3060.0
        f.close_call(pid, cid)
        assert wait_for(lambda: any("WIN" in m["text"] and "of the wallet" in m["text"] for m in dc.public),
                        timeout=10)
        texts = [m["text"] for m in dc.public]
        assert all(m["channel"] == PUBLIC for m in dc.public)
        assert not any("$" in t for t in texts)                 # percentages only
        assert not any("nicush" in t for t in texts)            # traders stay private (public_show_traders)
        assert sum("since the start" in t for t in texts) == 1   # one summary a day
        assert not any("🆕" in m["text"] for m in dc.sent)       # the owner's channel is unchanged
    finally:
        stop_bot(bot, th)


def test_without_a_public_channel_nothing_is_posted_elsewhere(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    monkeypatch.delenv("DISCORD_PUBLIC_CHANNEL_ID", raising=False)
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert wait_for(lambda: any("Copybot started" in m["text"] for m in dc.sent))
        bot.public_tick()
        assert not dc.public
    finally:
        stop_bot(bot, th)
