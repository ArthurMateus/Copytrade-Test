"""Invo calls: parsers on REAL recorded answers, the client's token refresh, the watcher, and the bot end to end
(FakeHL + FakeTelegram + FakeInvo)."""
import queue
import threading
import time
from datetime import datetime, timezone

import pytest

from copybot import config, invo, log
from tests.fakes import fixture
from tests.fakes_invo import FakeInvo
from tests.test_e2e import LEADER, env, start_bot, stop_bot, wait_for  # noqa: F401 (env is a fixture)


# ---- parsers ------------------------------------------------------------------------------------------
def test_portfolios_are_paper_and_say_which_assets_are_open():
    ps = invo.parse_portfolios(fixture("invo_users_portfolios.json"))
    assert [p.title for p in ps][:1] == ["Casino🎰"] and all(p.kind == "paper" for p in ps)
    casino, _, small, _ = ps
    assert casino.closed == 1515 and casino.win_rate == pytest.approx(93.729, abs=0.01) and casino.open_count == 0
    assert small.open_count == 1 and small.open_assets == ("ASTER",)


def test_open_calls_parse_with_size_leverage_target_and_stop():
    pump, eth = invo.parse_calls(fixture("invo_investments_open.json"))
    assert (eth.ticker, eth.long, eth.leverage, eth.entry, eth.target, eth.stop) == ("ETH", True, 10, 2475.9, 3300, 2275)
    assert eth.size == pytest.approx(0.052220937)                      # now, at today's price
    assert eth.committed == pytest.approx(0.05000438213786726) and eth.exposure == pytest.approx(0.5000438213786726)
    assert eth.is_open and eth.owner == "nicush" and eth.portfolio_id == "f392d602-4882-40bc-8198-6d681943284d"
    assert pump.stop is None and pump.target == pytest.approx(0.006925)
    assert eth.created_ms == int(datetime(2026, 10, 8, 23, 0, 58, 636000, tzinfo=timezone.utc).timestamp() * 1000)


def test_closed_calls_carry_how_and_when_they_closed():
    ena, eth, zec = invo.parse_calls(fixture("invo_investments_closed.json"))
    assert not zec.is_open and zec.reason_closed == "stop_loss_hit" and zec.closing_price == 1158.1
    assert ena.reason_closed == "user_closed" and ena.closed_ms > ena.created_ms


def test_user_lookup_parses():
    assert invo.parse_user(fixture("invo_get_user.json")) == ("40629900-09d7-4bd8-94bd-f2cfec74e80a", "prateek")
    assert invo.parse_user({"user": None, "success": False}) is None


# ---- client -------------------------------------------------------------------------------------------
@pytest.fixture
def fake():
    f = FakeInvo()
    yield f
    f.close()


def test_client_refreshes_rotates_the_token_file_and_retries_once_on_expiry(fake, tmp_path):
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0\n", encoding="utf-8")
    fake.add_user("nicush")
    c = invo.InvoClient(fake.url, str(tok), min_interval_s=0)
    assert c.user("nicush")[0] == "uid-nicush"
    assert tok.read_text(encoding="utf-8") == fake.valid_refresh != "REFRESH0"      # rotated and saved
    fake.expire_access()
    assert c.portfolios("uid-nicush")                                               # 401 -> refresh -> retry
    assert [p for p, _ in fake.requests].count("/auth/refresh_token") == 2
    fake.refuse_refresh = True
    fake.expire_access()
    with pytest.raises(invo.InvoAuthError):
        c.portfolios("uid-nicush")


def test_a_token_pasted_with_bearer_and_a_bom_is_accepted(fake, tmp_path):
    tok = tmp_path / "invo.token"
    tok.write_bytes("﻿Bearer REFRESH0".encode("utf-8"))
    fake.add_user("x")
    assert invo.InvoClient(fake.url, str(tok), min_interval_s=0).user("x")


# ---- watcher ------------------------------------------------------------------------------------------
def test_watcher_copies_only_new_fresh_calls_and_reports_closes(fake, tmp_path):
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_user("nicush", n_portfolios=2)[0]
    old = fake.open_call(pid, "BTC")                        # already open when we start: never copied
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"nicush"}, 1, 180,
                     threading.Event())
    w.poll("nicush")
    assert out.empty()
    n_reads = sum(1 for p, _ in fake.requests if p == "/investments/get_investments")
    w.poll("nicush")                                        # nothing changed: no investment read at all
    assert sum(1 for p, _ in fake.requests if p == "/investments/get_investments") == n_reads
    new = fake.open_call(pid, "ETH")
    stale = fake.open_call(pid, "SOL", created_ms=int(time.time() * 1000) - 3600_000)
    w.poll("nicush")
    got = [out.get_nowait() for _ in range(out.qsize())]
    assert [(k, c.id) for k, _, c in got] == [("invo_open", new)]          # the hour-old call is not copied
    fake.close_call(pid, new)
    fake.close_call(pid, old)
    w.poll("nicush")
    closes = sorted(c.id for k, _, c in (out.get_nowait() for _ in range(out.qsize())) if k == "invo_close")
    assert closes == sorted([new, old])


def test_watcher_reports_an_unknown_username_once(fake, tmp_path):
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"ghost"}, 1, 180,
                     threading.Event())
    w.poll("ghost")
    w.poll("ghost")
    assert [out.get_nowait() for _ in range(out.qsize())] == [("invo_unknown", "ghost")]


# ---- the bot end to end -----------------------------------------------------------------------------------
@pytest.fixture
def invo_env(env, tmp_path):
    hl, tg, data, cdir = env
    f = FakeInvo()
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    (cdir / "invo.toml").write_text(f'api_base = "{f.url}"\npoll_s = 1.0\n', encoding="utf-8")
    yield hl, tg, data, cdir, f, tok
    f.close()


def start_with_invo(env, tok, monkeypatch):
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    return start_bot(env)


def test_bot_copies_an_invo_call_and_closes_it_when_the_trader_does(invo_env, monkeypatch):
    hl, tg, data, cdir, f, tok = invo_env
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, tg, data, cdir), tok, monkeypatch)
    try:
        assert bot.invo is not None
        tg.say("/invofollow @nicush")
        assert wait_for(lambda: "invo:nicush" in bot.invo.st.followed)
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)     # baseline taken
        f.open_call(pid, "ETH", long=True, leverage=10, size=0.05, entry=2990.0, target=3300, stop=2800)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        p = bot.invo.st.positions["ETH"]
        assert p.leader == "invo:nicush" and p.side == 1
        assert p.size * p.entry_px == pytest.approx(0.05 * 10 * 300, rel=0.05)    # their 5% x 10x of our 300$
        assert "ETH" not in bot.st.positions                                      # the main wallet never copies it
        assert all("ETH" not in w.st.positions for w in bot.sides if not w.own_leaders)    # HL side wallets: no
        # the fixed-risk Invo wallets copy the same call: r% of their own 300$ at our 3% stop (100$ at 1%, 500$ at 5%)
        assert {w.name for w in bot.invo_extra} == {f"invo_risk_{r:g}pct" for r in (1, 2, 5, 10, 20)}
        assert wait_for(lambda: all("ETH" in w.st.positions for w in bot.invo_extra), timeout=10)
        for w in bot.invo_extra:
            q = w.st.positions["ETH"]
            assert q.size * q.entry_px == pytest.approx(300 * w.risk_pct / 100 / 0.03, rel=0.05), w.name
            assert "invo:nicush" in w.st.followed
        tg.say("/invowallets")
        assert wait_for(lambda: any("Invo wallets" in m["text"] and "invo 20% risk" in m["text"]
                                    for m in tg.sent + tg.edits))
        text = lambda: " ".join(m["text"] for m in tg.sent + tg.edits)
        assert wait_for(lambda: "Invo call by @nicush" in text() and "<b>ETH</b>" in text())   # its live trade card
        for cmd, needle in (("/invo", "Invo calls"), ("/invotrades", "Invo trades"), ("/invotraders", "Invo traders")):
            tg.say(cmd)
            assert wait_for(lambda: needle in text()), cmd
        assert "@nicush" in text() and "Invo: " in text()                        # Invo's own record on the card
        n_edits = len(tg.edits)
        hl.mids["ETH"] = 3015.0                                                     # the price moves: cards are edited
        assert wait_for(lambda: len(tg.edits) > n_edits, timeout=10)
        cid = f.calls[pid][0]["id"]
        f.close_call(pid, cid)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
        assert bot.invo.st.closed[-1]["leader"] == "invo:nicush"
        assert wait_for(lambda: all("ETH" not in w.st.positions for w in bot.invo_extra), timeout=10)
        assert wait_for(lambda: "closed by the trader" in text())               # the card became the summary
        assert "REFRESH" not in " ".join(m["text"] for m in tg.sent)               # no token ever shown
        tg.say("/invounfollow nicush")
        assert wait_for(lambda: "invo:nicush" not in bot.invo.st.followed)
        assert set(bot.st.followed) == {LEADER}                                   # the main wallet untouched
    finally:
        stop_bot(bot, th)


def test_a_huge_call_is_capped_at_max_risk_pct_also_after_the_traders_add(invo_env, monkeypatch):
    """The STRK case: 25% x 5x = 125% of the portfolio. The copy is capped at invo.max_risk_pct (2%) of our 300$
    at our 3% stop = 200$, and the trader's later add does not push it past the cap."""
    hl, tg, data, cdir, f, tok = invo_env
    pid = f.add_user("lazy")[0]
    bot, th = start_with_invo((hl, tg, data, cdir), tok, monkeypatch)
    try:
        tg.say("/invofollow lazy")
        assert wait_for(lambda: bot.invo_watch.known.get("lazy") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=False, leverage=5, size=0.25, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        p = bot.invo.st.positions["ETH"]
        assert p.size * p.entry_px == pytest.approx(300 * 0.02 / 0.03, rel=0.05)          # 200$, not 375$
        f.resize_call(pid, cid, 0.40)                                                       # they add 60%
        time.sleep(3)
        p = bot.invo.st.positions["ETH"]
        assert p.size * p.entry_px <= 300 * 0.02 / 0.03 * 1.05                              # still at the cap
        f.close_call(pid, cid)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
    finally:
        stop_bot(bot, th)


def test_without_a_token_file_invo_is_off_and_says_how_to_turn_it_on(env, monkeypatch):
    hl, tg, data, cdir = env
    monkeypatch.delenv("INVO_TOKEN_FILE", raising=False)
    bot, th = start_bot(env)
    try:
        assert bot.invo is None and all(not w.own_leaders for w in bot.sides)
        tg.say("/invofollow nicush")
        assert wait_for(lambda: any("Invo is off" in m["text"] for m in tg.sent))
    finally:
        stop_bot(bot, th)


def test_our_stop_closes_an_invo_copy_and_its_card_becomes_the_summary(invo_env, monkeypatch):
    hl, tg, data, cdir, f, tok = invo_env
    pid = f.add_user("akira")[0]
    bot, th = start_with_invo((hl, tg, data, cdir), tok, monkeypatch)
    try:
        tg.say("/invofollow akira")
        assert wait_for(lambda: bot.invo_watch.known.get("akira") is not None, timeout=10)
        f.open_call(pid, "SOL", long=True, leverage=5, size=0.05, entry=150.0)
        assert wait_for(lambda: "SOL" in bot.invo.st.positions, timeout=10)
        hl.mids["SOL"] = 140.0                                    # -6.7%: through our 3% stop
        assert wait_for(lambda: "SOL" not in bot.invo.st.positions, timeout=10)
        assert bot.invo.st.closed[-1]["reason"] == "stop"
        text = lambda: " ".join(m["text"] for m in tg.sent + tg.edits)
        assert wait_for(lambda: "stop-loss hit" in text() and "Invo call by @akira" in text())
        assert "closed by the trader" not in text()
    finally:
        stop_bot(bot, th)


def test_committed_size_comes_from_entrySize_not_the_price_dependent_positionSize():
    pump, eth = invo.parse_calls(fixture("invo_investments_open.json"))
    assert pump.entry_size == pytest.approx(0.04998597660369806) and pump.size == pytest.approx(0.0324751614)
    assert pump.committed == pump.entry_size and pump.exposure == pytest.approx(0.4998597660369806)
    p = invo.parse_portfolios(fixture("invo_users_portfolios.json"))[0]
    assert p.updated_ms > 0


def test_watcher_reports_an_add_or_a_trim_of_an_open_call(fake, tmp_path):
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_user("nicush")[0]
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"nicush"}, 1, 180,
                     threading.Event())
    w.poll("nicush")
    cid = fake.open_call(pid, "ETH", size=0.05)
    w.poll("nicush")
    assert out.get_nowait()[0] == "invo_open"
    time.sleep(0.01)
    fake.resize_call(pid, cid, 0.10)                       # the trader doubles it
    w.poll("nicush")
    k, name, old, new = out.get_nowait()
    assert k == "invo_resize" and old.committed == pytest.approx(0.05) and new.committed == pytest.approx(0.10)
    time.sleep(0.01)
    fake.resize_call(pid, cid, 0.101)                      # a 1% change is noise
    w.poll("nicush")
    assert out.empty()


def test_bot_mirrors_the_traders_add_and_partial_close(invo_env, monkeypatch):
    hl, tg, data, cdir, f, tok = invo_env
    pid = f.add_user("glitty")[0]
    bot, th = start_with_invo((hl, tg, data, cdir), tok, monkeypatch)
    try:
        tg.say("/invofollow glitty")
        assert wait_for(lambda: bot.invo_watch.known.get("glitty") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=True, leverage=5, size=0.04, entry=3000.0)      # 4% x 5x = 20% of 300$
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        size0 = bot.invo.st.positions["ETH"].size
        assert size0 * 3000 == pytest.approx(0.20 * 300, rel=0.05)
        f.resize_call(pid, cid, 0.06)                                                      # they add 50%
        assert wait_for(lambda: bot.invo.st.positions["ETH"].size > size0 * 1.4, timeout=10)
        assert bot.invo.st.positions["ETH"].size == pytest.approx(size0 * 1.5, rel=0.03)
        time.sleep(0.01)
        f.resize_call(pid, cid, 0.03)                                                      # they take half off
        assert wait_for(lambda: bot.invo.st.positions["ETH"].size < size0, timeout=10)
        assert bot.invo.st.positions["ETH"].size == pytest.approx(size0 * 0.75, rel=0.03)
        text = lambda: " ".join(m["text"] for m in tg.sent + tg.edits)
        assert wait_for(lambda: "resized by the trader 6.0% → 3.0%" in text())
        f.close_call(pid, cid)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
    finally:
        stop_bot(bot, th)
