"""Invo calls: parsers on REAL recorded answers, the client's token refresh, the watcher, and the bot end to end
(FakeHL + FakeDiscord + FakeInvo)."""
import queue
import threading
import time
from datetime import datetime, timezone

import pytest

from copybot import config, invo, log
from tests.fakes import fixture
from tests.fakes_invo import FakeInvo
from tests.test_e2e import LEADER, PIN, env, start_bot, stop_bot, wait_for  # noqa: F401 (env is a fixture)


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
    (k, name, base), = [out.get_nowait() for _ in range(out.qsize())]     # the baseline: reported, never copied
    assert (k, name, [c.id for c in base]) == ("invo_baseline", "nicush", [old])
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


def test_a_429_slows_every_invo_reader_and_eases_back(fake, tmp_path, monkeypatch):
    """Live 2026-10-12: Invo answered 429 to every read and copies came 2 minutes late. A 429 doubles the shared
    spacing (watcher and search), the search waits while it lasts, and it eases back after CALM_S without one."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    fake.add_user("aaa")
    c = invo.InvoClient(fake.url, str(tok), min_interval_s=0.25)
    fake.throttle = True
    for want in (0.5, 1.0, 2.0, 3.0, 3.0):
        with pytest.raises(invo.InvoError):
            c.user("aaa")
        assert c.slow == want
    assert c.throttled()
    fake.throttle = False
    monkeypatch.setattr(invo, "CALM_S", 0.0)
    c._last = 0.0
    c.user("aaa")
    assert c.slow == pytest.approx(2.25)                     # x0.75 per calm period ...
    for _ in range(10):
        c._last = 0.0
        c.user("aaa")
    assert c.slow == 0.0                                     # ... back to request_gap_s


def test_a_vanished_call_is_not_closed_while_invo_does_not_answer(fake, tmp_path, monkeypatch):
    """Live 2026-10-12: during 429s the closed-calls check failed and copies were closed as "deleted by the trader"
    after VANISH_S. A close now needs Invo's ANSWER (the call closed, or absent from the closed calls)."""
    monkeypatch.setattr(invo, "VANISH_S", 0.0)
    monkeypatch.setattr(invo, "SLOW_MAX_S", 0.0)
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_user("akira")[0]
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"akira"}, 0, 180,
                     threading.Event())
    drain = lambda: [out.get_nowait() for _ in range(out.qsize())]
    w.round()
    drain()
    a = fake.open_call(pid, "ETH")
    w.round()
    assert [k for k, *_ in drain()] == ["invo_open"]
    fake.delete_call(pid, a)
    fake.throttle_closed = True
    for _ in range(3):
        w.round()
    assert drain() == [] and a in w.known["akira"]            # Invo did not answer: kept
    fake.throttle_closed = False
    w.round()
    got = drain()
    assert [(k, c.id) for k, _, c in got] == [("invo_close", a)]
    assert got[0][2].reason_closed == "call deleted by the trader"


def test_one_failing_trader_does_not_hold_up_the_others_and_a_refusing_invo_gets_backoff(fake, tmp_path):
    """Owner request 2026-10-11 (copies in < 3 s): no pause between rounds, so a trader whose read fails must not end
    the round for the traders after it, and when Invo refuses every read the watcher waits longer each round."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    fake.add_user("aaa")
    pid = fake.add_user("zzz")[0]
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"aaa", "zzz"}, 0, 180,
                     threading.Event())
    assert w.round() == 0                                   # both baselines read, next round at once
    [out.get_nowait() for _ in range(out.qsize())]
    fake.broken_users.add(fake.users["aaa"])                # "aaa" (read first) now fails
    cid = fake.open_call(pid, "ETH")
    assert w.round() == 0                                   # one trader failing is no reason to wait
    assert [(k, c.id) for k, _, c in (out.get_nowait() for _ in range(out.qsize()))] == [("invo_open", cid)]
    invo.SLOW_MAX_S, slow0 = 0.0, invo.SLOW_MAX_S             # (no extra spacing here: only the round backoff)
    fake.throttle = True                                    # Invo refuses everything: 2 s, 4 s, ... at most 60 s
    assert [w.round() for _ in range(7)] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    invo.SLOW_MAX_S = slow0
    fake.throttle = False
    fake.broken_users.clear()
    assert w.round() == 0 and w.fails == 0


def test_a_call_missing_from_one_read_is_closed_only_when_invo_confirms_it(fake, tmp_path):
    """Audit 2026-10-11: one incomplete answer (no portfolio list, or a call missing) used to close our copy at once
    and the call, back a moment later, was too old to copy again. Now a close needs the call in the portfolio's closed
    calls (which also gives Invo's closing price and reason); a call that never shows there (deleted) closes after
    VANISH_S."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_user("akira")[0]
    t = [time.time() * 1000]
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"akira"}, 0, 180,
                     threading.Event(), now=lambda: t[0])
    drain = lambda: [out.get_nowait() for _ in range(out.qsize())]
    w.round()
    drain()
    a = fake.open_call(pid, "ETH", created_ms=int(t[0]))
    b = fake.open_call(pid, "SOL", created_ms=int(t[0]))
    w.round()
    assert sorted(c.id for k, _, c in drain() if k == "invo_open") == sorted([a, b])
    fake.blank_users.add(fake.users["akira"])               # 200 without the portfolio list: a failed read
    w.round()
    assert drain() == [] and w.known["akira"].keys() == {a, b}
    fake.blank_users.clear()
    fake.delete_call(pid, a)                                 # gone, never closed
    for dt in (0, 5_000):
        t[0] += dt
        w.round()
        assert drain() == []                                 # not yet: maybe Invo answered incompletely
    t[0] += 6_000
    w.round()
    (k, _, c), = drain()
    assert (k, c.id, c.reason_closed) == ("invo_close", a, "call deleted by the trader")
    fake.close_call(pid, b, price=161.5, reason="target_hit")
    w.round()
    (k, _, c), = drain()                                     # confirmed: at once, with Invo's numbers
    assert (k, c.id, c.closing_price, c.reason_closed, c.is_open) == ("invo_close", b, 161.5, "target_hit", False)


def test_open_calls_past_the_first_page_and_unread_portfolios_never_look_deleted(fake, tmp_path):
    """Live 2026-10-13: a prateek BTC short (Casino portfolio, 1% calls) was closed as "deleted by the trader" while
    the call was still open (he closed it an hour later, in profit). Only the first page of open calls (50) and of
    portfolios (20) was read, and 10 s missing was enough. Now every page is read, and a call whose portfolio was not
    fully read waits VANISH_UNREAD_S."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pids = fake.add_user("prateek", n_portfolios=22)
    pid, far = pids[0], pids[21]                             # far: on the second page of portfolios
    t = [time.time() * 1000]
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"prateek"}, 0, 180,
                     threading.Event(), now=lambda: t[0])
    drain = lambda: [out.get_nowait() for _ in range(out.qsize())]
    w.round()
    drain()
    btc = fake.open_call(pid, "BTC", long=False, created_ms=int(t[0]))
    eth = fake.open_call(far, "ETH", created_ms=int(t[0]))
    w.round()
    assert sorted(c.id for k, _, c in drain() if k == "invo_open") == sorted([btc, eth])
    for i in range(55):                                      # BTC falls to the second page of open calls
        fake.open_call(pid, "SOL", created_ms=int(t[0]))
    for _ in range(3):
        t[0] += 100_000
        w.round()
    assert [k for k, *_ in drain() if k == "invo_close"] == [] and btc in w.known["prateek"]
    fake.port_meta[far] = {"active": False}                  # a portfolio we cannot read for a while
    fake.delete_call(far, eth)
    for _ in range(3):
        t[0] += 300_000
        w.round()
    assert drain() == [] and eth in w.known["prateek"]
    t[0] += invo.VANISH_UNREAD_S * 1000
    w.round()
    assert [(k, c.id, c.reason_closed) for k, _, c in drain()] == [("invo_close", eth, invo.DELETED)]
    fake.close_call(pid, btc, price=83435.0, reason="stop_loss_hit")
    w.round()
    (k, _, c), = drain()
    assert (k, c.id, c.closing_price, c.reason_closed) == ("invo_close", btc, 83435.0, "stop_loss_hit")


def test_a_call_invo_lists_under_a_new_id_is_the_same_call(fake, tmp_path):
    """Live 2026-10-13: after prateek edited his BTC short, Invo listed it under a new id (same creation time): the
    old id vanished (never in the closed list) and the new one looked like a 20-minute-old new call. Our copy was
    closed as "deleted" and the new id ignored; he closed it an hour later in profit. Now it is one call."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_user("prateek")[0]
    t = [time.time() * 1000]
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"prateek"}, 0, 180,
                     threading.Event(), now=lambda: t[0])
    drain = lambda: [out.get_nowait() for _ in range(out.qsize())]
    w.round()
    drain()
    old = fake.open_call(pid, "BTC", long=False, leverage=40, size=0.01, entry=83963.0, created_ms=int(t[0]))
    w.round()
    assert [k for k, *_ in drain()] == ["invo_open"]
    t[0] += 1200_000                                         # 20 minutes later he sets a stop: a new id
    new = fake.reid_call(pid, old, stopLoss=83435.0)
    for _ in range(3):
        t[0] += 30_000
        w.round()
    (k, _, a, b), = drain()
    assert (k, a.id, b.id, b.stop) == ("invo_replace", old, new, 83435.0)
    fresh = fake.open_call(pid, "BTC", long=False, created_ms=int(t[0]))   # a real new call on the same coin: a new call
    gone = fake.open_call(pid, "ETH", created_ms=int(t[0]))
    w.round()
    assert sorted(c.id for k, _, c in drain()) == sorted([fresh, gone])
    fake.delete_call(pid, gone)                                  # deleted, no twin: still closed after VANISH_S
    t[0] += 11_000
    w.round()
    t[0] += 11_000
    w.round()
    assert [(k, c.id) for k, _, c in drain()] == [("invo_close", gone)]
    fake.close_call(pid, new, price=83435.0, reason="stop_loss_hit")
    w.round()
    assert [(k, c.id, c.closing_price) for k, _, c in drain()] == [("invo_close", new, 83435.0)]


def test_a_call_counted_before_it_is_listed_is_still_copied(fake, tmp_path):
    """Audit 2026-10-11: Invo's open-call count can move before its list; the portfolio used to be marked as read and
    the call was only seen at the next change (too old to copy by then)."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_user("akira")[0]
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"akira"}, 0, 180,
                     threading.Event())
    w.round()
    out.get_nowait()
    cid = fake.open_call(pid, "ETH")
    fake.hidden.add(cid)
    w.round()
    assert out.empty()
    fake.hidden.clear()
    w.round()
    assert [(k, c.id) for k, _, c in (out.get_nowait() for _ in range(out.qsize()))] == [("invo_open", cid)]


def test_a_rotated_token_that_cannot_be_saved_is_kept_and_saved_later(fake, tmp_path):
    """Audit 2026-10-11: the old refresh token is spent once Invo rotates it; if the file write failed the bot kept
    only the spent one and lost the login at the next refresh."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    (tmp_path / "invo.tmp").mkdir()                          # the temporary file cannot be written
    fake.add_user("x")
    c = invo.InvoClient(fake.url, str(tok), min_interval_s=0)
    assert c.user("x") and c.save_failed
    assert tok.read_text(encoding="utf-8") == "REFRESH0"
    fake.expire_access()
    assert c.user("x")                                       # the next refresh uses the token kept in memory
    (tmp_path / "invo.tmp").rmdir()
    assert c.save_token() and not c.save_failed
    assert tok.read_text(encoding="utf-8") == fake.valid_refresh


# ---- real-money ("live") portfolios and Invo's score (owner request 2026-10-12) -----------------------------------
def test_live_portfolio_and_score_answers_are_parsed_from_the_recorded_shapes():
    p = invo.parse_portfolios(fixture("invo_users_portfolios.json"))[0]
    live = invo.parse_live_overview(fixture("invo_live_overview.json"), invo.Portfolio(**{**p.__dict__, "kind": "live"}))
    assert (round(live.win_rate, 1), live.closed, round(live.pnl_pct), live.open_count) == (77.9, 656, 1548, 10)
    calls, cursor = invo.parse_live_positions(fixture("invo_live_positions.json"), "pf")
    ldo, tao = calls
    assert (ldo.ticker, ldo.long, ldo.leverage, ldo.is_open, ldo.units) == ("LDO", True, 5.0, True, 12350.7)
    assert ldo.committed == pytest.approx(0.004454, rel=1e-3)       # size.pct is a % of the trader's equity
    assert (tao.is_open, tao.closing_price, tao.reason_closed) == (False, pytest.approx(294.9612), "manual")
    assert cursor.startswith("2026-10-10")
    top, = invo.parse_score_top(fixture("invo_score_top.json"))
    assert (top["user"], top["kind"], top["score"]) == ("luxora", "paper", 9.86)
    assert invo.parse_portfolio_score(fixture("invo_score_portfolio.json")) == {"score": 9.86, "capped": False,
                                                                               "caps": []}
    assert invo.parse_portfolio_score({"status": "pending", "detail": None}) == {}


def test_watcher_copies_a_real_money_portfolio_and_follows_its_size_by_coins(fake, tmp_path):
    """Live portfolios are empty in /v1_0: the watcher reads their open positions from the read-only /dex/ view. Their
    size.pct moves with the price, so adds and trims are seen in the coin amount."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_live("realguy", accuracy=78.0, pnl=1500.0, closed=650)
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"realguy"}, 0, 180,
                     threading.Event())
    drain = lambda: [out.get_nowait() for _ in range(out.qsize())]
    w.round()
    assert [k for k, *_ in drain()] == ["invo_baseline"]
    assert w.stats["realguy"][0].win_rate == 78.0 and w.stats["realguy"][0].closed == 650   # real numbers
    cid = fake.live_open(pid, "ETH", long=False, leverage=5, pct=2.0, entry=3000.0, coins=1.0)
    w.round()
    (k, _, c), = drain()
    assert (k, c.id, c.long, c.committed) == ("invo_open", cid, False, pytest.approx(0.02))
    fake.live_set(pid, cid, pct=2.6)                       # the price moved: not a resize
    w.round()
    assert drain() == []
    fake.live_set(pid, cid, coins=1.5, pct=3.9)            # 1.5x the coins: an add
    w.round()
    (k, _, old, new), = drain()
    assert k == "invo_resize" and new.committed == pytest.approx(0.03)
    fake.live_close(pid, cid, price=2900.0)
    w.round()
    (k, _, c), = drain()                                   # confirmed in its closed positions at once
    assert (k, c.closing_price, c.reason_closed) == ("invo_close", 2900.0, "manual")


def test_the_bot_never_calls_an_invo_trading_endpoint():
    """Invo's /dex/ API also places real trades (/dex/position/create, /dex/mimic/copy ...): only the read-only
    portfolio views may appear in our code."""
    import re
    from pathlib import Path
    for f in Path("src/copybot").rglob("*.py"):
        for path in re.findall(r"/dex/[A-Za-z_/]+", f.read_text(encoding="utf-8")):
            assert path.startswith("/dex/portfolio/"), f"{f.name}: {path}"


def test_a_refused_real_money_read_alerts_once_and_never_closes_a_copy(fake, tmp_path):
    """Owner request 2026-10-12: the bot's own Invo account may not be allowed to see real-money portfolios. A refusal
    is reported once (not as a lost login), keeps what we knew, and the first read waits for a real answer."""
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = fake.add_live("realguy")
    out = queue.Queue()
    w = invo.Watcher(invo.InvoClient(fake.url, str(tok), min_interval_s=0), out, lambda: {"realguy"}, 0, 180,
                     threading.Event())
    drain = lambda: [out.get_nowait() for _ in range(out.qsize())]
    fake.refuse_live = True
    w.round()
    w.round()
    got = drain()
    assert [(k, ok) for k, _, ok, _ in got] == [("invo_live", False)] and "403" in got[0][3]   # once, not "auth"
    assert "realguy" not in w.known and w.auth_ok                         # no baseline from a refused read
    fake.refuse_live = False
    w.round()
    assert [k for k, *_ in drain()] == ["invo_live", "invo_baseline"]
    cid = fake.live_open(pid, "ETH")
    w.round()
    assert [k for k, *_ in drain()] == ["invo_open"]
    fake.refuse_live = True
    for _ in range(3):
        w.round()
    assert [k for k, *_ in drain()] == ["invo_live"]                       # refused again: no close of the call
    assert cid in w.known["realguy"]


def test_real_money_calls_are_sized_with_their_own_multiplier():
    """Owner request 2026-10-12: real-money traders bet ~1% of their account; invo.live_size_mult scales those calls."""
    from types import SimpleNamespace
    from copybot.runner import Bot
    cfg = config.load("config", env={})
    calls, _ = invo.parse_live_positions(fixture("invo_live_positions.json"), "pf")
    paper = invo.parse_calls(fixture("invo_investments_open.json"))[0]
    b = SimpleNamespace(cfg=cfg, invo_cap=lambda eq: Bot.invo_cap(b, eq))
    for call, mult in ((calls[0], cfg.invo.live_size_mult), (paper, cfg.invo.size_mult)):
        b._invo_call = call
        want = max(min(call.exposure * mult * 300, 300 * cfg.invo.max_risk_pct / 100 / 0.03), 10.5)
        assert Bot.invo_size(b, None, 2.0, 300.0) * 2.0 == pytest.approx(want), call.ticker
    b._invo_call = calls[0]                                # LDO: 0.445% x 5x = 2.2% exposure, x10 of 300$ = 66.8$
    assert Bot.invo_size(b, None, 2.0, 300.0) * 2.0 == pytest.approx(66.8, abs=0.1)     # (x1 would be 6.7$ -> 10$)


# ---- the bot end to end -----------------------------------------------------------------------------------
@pytest.fixture
def invo_env(env, tmp_path):
    hl, dc, data, cdir = env
    f = FakeInvo()
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    # the daily Invo search has its own tests (test_invo_search.py): off here so it does not share the fake's login
    (cdir / "invo.toml").write_text(f'api_base = "{f.url}"\npoll_s = 1.0\nsearch = false\n', encoding="utf-8")
    yield hl, dc, data, cdir, f, tok
    f.close()


def start_with_invo(env, tok, monkeypatch):
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    return start_bot(env)


def test_bot_copies_an_invo_call_and_closes_it_when_the_trader_does(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert bot.invo is not None
        dc.say("/invofollow @nicush")
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
        # ... and every size again with an 8% stop and with the trader's exit (owner request 2026-10-12)
        risk = [f"invo_risk_{r:g}pct" for r in (1, 2, 5, 10, 20)]
        assert {w.name for w in bot.invo_extra} == {*risk, *(f"{n}_{v}" for n in ["invo_calls", *risk]
                                                            for v in ("stop8", "trader"))}
        assert len(bot.invo_wallets()) == 18
        assert wait_for(lambda: all("ETH" in w.st.positions for w in bot.invo_extra), timeout=10)
        for w in bot.invo_extra:
            q = w.st.positions["ETH"]
            base = w.name.rsplit("_", 1)[0] if w.name.endswith(("_stop8", "_trader")) else w.name
            # trader size: the same copy at every stop; r% risk: r% of the wallet lost at ITS stop (owner request
            # 2026-10-13, "A"): 3% -> 300 x r / 3%, 8% -> 300 x r / 8%, trader's exit -> at its 30% guard (>= 10.5$)
            stop = 8.0 if w.name.endswith("_stop8") else 30.0 if w.name.endswith("_trader") else 3.0
            want = 150.0 if base == "invo_calls" else max(300 * float(base.split("_")[2][:-3]) / stop, 10.5)
            assert q.size * q.entry_px == pytest.approx(want, rel=0.12), w.name
            assert "invo:nicush" in w.st.followed
        dc.say("/invowallets")
        assert wait_for(lambda: any("Invo wallets" in m["text"] and "18 combinations" in m["text"]
                                    and "By stop" in m["text"] for m in dc.sent + dc.edits))
        text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
        assert wait_for(lambda: "Invo call by @nicush" in text() and "<b>ETH</b>" in text())   # its live trade card
        for cmd, needle in (("/invo", "Invo calls"), ("/invotrades", "Invo trades"), ("/invotraders", "Invo traders")):
            dc.say(cmd)
            assert wait_for(lambda: needle in text()), cmd
        assert "@nicush" in text() and "✅ $100-$1000" in text() and "95% win" in text()   # its portfolios, copied
        n_edits = len(dc.edits)
        hl.mids["ETH"] = 3015.0                                                     # the price moves: cards are edited
        assert wait_for(lambda: len(dc.edits) > n_edits, timeout=10)
        cid = f.calls[pid][0]["id"]
        f.close_call(pid, cid)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
        assert bot.invo.st.closed[-1]["leader"] == "invo:nicush"
        assert wait_for(lambda: all("ETH" not in w.st.positions for w in bot.invo_extra), timeout=10)
        assert wait_for(lambda: "closed by the trader" in text())               # the card became the summary
        assert "REFRESH" not in " ".join(m["text"] for m in dc.sent)               # no token ever shown
        dc.say("/invounfollow nicush")
        assert wait_for(lambda: "invo:nicush" not in bot.invo.st.followed)
        assert set(bot.st.followed) == {LEADER}                                   # the main wallet untouched
    finally:
        stop_bot(bot, th)


def test_a_huge_call_is_capped_at_max_risk_pct_also_after_the_traders_add(invo_env, monkeypatch):
    """The STRK case: 25% x 5x = 125% of the portfolio. The copy is capped at invo.max_risk_pct (1%) of our 300$
    at our 3% stop = 100$, and the trader's later add does not push it past the cap."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("lazy")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow lazy")
        assert wait_for(lambda: bot.invo_watch.known.get("lazy") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=False, leverage=5, size=0.25, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        p = bot.invo.st.positions["ETH"]
        cap = 300 * bot.cfg.invo.max_risk_pct / 100 / 0.03
        assert p.size * p.entry_px == pytest.approx(cap, rel=0.05)                        # 100$, not 375$
        f.resize_call(pid, cid, 0.40)                                                       # they add 60%
        time.sleep(3)
        p = bot.invo.st.positions["ETH"]
        assert p.size * p.entry_px <= cap * 1.05                                            # still at the cap
        f.close_call(pid, cid)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
    finally:
        stop_bot(bot, th)


def test_without_a_token_file_invo_is_off_and_says_how_to_turn_it_on(env, monkeypatch):
    hl, dc, data, cdir = env
    monkeypatch.delenv("INVO_TOKEN_FILE", raising=False)
    bot, th = start_bot(env)
    try:
        assert bot.invo is None and all(not w.own_leaders for w in bot.sides)
        dc.say("/invofollow nicush")
        assert wait_for(lambda: any("Invo is off" in m["text"] for m in dc.sent))
    finally:
        stop_bot(bot, th)


def test_our_stop_closes_an_invo_copy_and_its_card_becomes_the_summary(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("akira")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow akira")
        assert wait_for(lambda: bot.invo_watch.known.get("akira") is not None, timeout=10)
        f.open_call(pid, "SOL", long=True, leverage=5, size=0.05, entry=150.0)
        assert wait_for(lambda: "SOL" in bot.invo.st.positions, timeout=10)
        hl.mids["SOL"] = 140.0                                    # -6.7%: through our 3% stop
        assert wait_for(lambda: "SOL" not in bot.invo.st.positions, timeout=10)
        assert bot.invo.st.closed[-1]["reason"] == "stop"
        text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
        assert wait_for(lambda: "stop-loss hit" in text() and "Invo call by @akira" in text())
        assert "closed by the trader" not in text()
    finally:
        stop_bot(bot, th)


def test_a_trader_whose_copies_lose_too_much_is_dropped_at_once(invo_env, monkeypatch):
    """The wallet leader pause rules (copy drawdown > 10% of equity / 7, or 5 losses in a row) also apply to Invo
    traders: the first breach drops the trader from every Invo wallet; /invofollow brings it back."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("lazy")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow lazy")
        assert wait_for(lambda: bot.invo_watch.known.get("lazy") is not None, timeout=10)
        f.open_call(pid, "SOL", long=True, leverage=5, size=0.25, entry=150.0)     # capped at 100$
        assert wait_for(lambda: "SOL" in bot.invo.st.positions, timeout=10)
        time.sleep(1.2)                                           # side wallets reuse an order book for 1 s
        hl.mids["SOL"] = 130.0                                    # our stop: about -13$ > 4% of 300$
        assert wait_for(lambda: "invo:lazy" not in bot.invo.st.followed, timeout=10)
        assert wait_for(lambda: all("invo:lazy" not in w.st.followed for w in bot.invo_extra))
        text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
        assert wait_for(lambda: "copying it lost too much" in text() and "/invofollow lazy brings it back" in text())
        dc.say("/invofollow lazy")
        assert wait_for(lambda: "invo:lazy" in bot.invo.st.followed and "invo:lazy" not in bot.invo.st.paused_leaders)
    finally:
        stop_bot(bot, th)


def test_an_unfollowed_traders_exit_still_closes_our_copy_and_invo_does_not_trail(invo_env, monkeypatch):
    """Owner request 2026-10-11: a dropped or unfollowed trader is still read while we hold its copy, so its close is
    copied (it used to stay open until our stop); its new calls are not. The Invo wallets do not trail (invo.toml)."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("henderson")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert all(w.cfg.risk.trail_after_pct == 0 for w in bot.invo_wallets())
        assert bot.cfg.risk.trail_after_pct == 3.0                       # the Hyperliquid wallets still trail
        dc.say("/invofollow henderson")
        assert wait_for(lambda: bot.invo_watch.known.get("henderson") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=False, leverage=5, size=0.05, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        dc.say("/invounfollow henderson")
        assert wait_for(lambda: "invo:henderson" not in bot.invo.st.followed)
        assert "henderson" in bot.invo_watched                           # still read: we hold its copy
        f.open_call(pid, "SOL", long=True, leverage=5, size=0.05, entry=150.0)      # a new call: not copied
        time.sleep(3)
        assert "SOL" not in bot.invo.st.positions
        f.close_call(pid, cid)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
        assert bot.invo.st.closed[-1]["reason"] == "leader_close"
        assert wait_for(lambda: all("ETH" not in w.st.positions for w in bot.invo_extra), timeout=10)
        assert wait_for(lambda: "henderson" not in bot.invo_watched)     # nothing held any more: no longer read
    finally:
        stop_bot(bot, th)


def test_a_call_closed_while_the_bot_was_off_closes_our_copy_at_the_restart(invo_env, monkeypatch):
    """Code audit 2026-10-10 #5: the first read after a start is a baseline; a copy whose call is no longer open
    there is closed (it used to run on our stop forever). A copy whose call is still open is kept."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("akira")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow akira")
        assert wait_for(lambda: bot.invo_watch.known.get("akira") is not None, timeout=10)
        eth = f.open_call(pid, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)
        f.open_call(pid, "SOL", long=False, leverage=5, size=0.05, entry=150.0)
        assert wait_for(lambda: {"ETH", "SOL"} <= set(bot.invo.st.positions), timeout=10)
    finally:
        stop_bot(bot, th)
        bot.lock.f.close()                                               # as the process exit would
    f.close_call(pid, eth)                                               # while the bot is off
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=15)
        assert bot.invo.st.closed[-1]["coin"] == "ETH" and "SOL" in bot.invo.st.positions
        assert wait_for(lambda: all("ETH" not in w.st.positions and "SOL" in w.st.positions for w in bot.invo_extra))
        assert any(k[1] != eth and v == "SOL" for k, v in bot.invo_calls.items())     # SOL matched to its call again
    finally:
        stop_bot(bot, th)


def test_a_call_we_skipped_never_moves_the_copy_of_another_call_on_that_coin(invo_env, monkeypatch):
    """Code audit 2026-10-10 #4: call B on a coin we already hold for call A is skipped; B's close must not close A's
    copy (it used to, matched by the coin alone)."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow nicush")
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
        a = f.open_call(pid, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        b = f.open_call(pid, "ETH", long=False, leverage=5, size=0.05, entry=3000.0)
        time.sleep(2)
        f.close_call(pid, b)
        time.sleep(3)
        assert "ETH" in bot.invo.st.positions and bot.invo.st.positions["ETH"].side == 1
        f.close_call(pid, a)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
    finally:
        stop_bot(bot, th)


def test_memecoins_listed_per_1000_on_hyperliquid_are_copied_under_their_k_name():
    """Owner request 2026-10-11: Hyperliquid lists PEPE, BONK, SHIB, FLOKI ... per 1,000 coins as kPEPE, kBONK ...; a
    PEPE call trades kPEPE with the trader's prices x1000. The Invo search keeps the same-unit names only."""
    from types import SimpleNamespace
    from copybot.runner import Bot
    assets = {a["name"]: None for a in fixture("meta_and_asset_ctxs.json")[0]["universe"]}
    b = SimpleNamespace(assets=assets, cfg=SimpleNamespace(invo=SimpleNamespace(aliases={"matic": "POL"})))
    b.invo_coin = lambda t: Bot.invo_coin(b, t)
    for ticker, want in (("PEPE", ("kPEPE", 1000.0)), ("bonk", ("kBONK", 1000.0)), ("SHIB", ("kSHIB", 1000.0)),
                         ("ETH", ("ETH", 1.0)), ("FARTCOIN", ("FARTCOIN", 1.0)), ("NOPE123", (None, 1.0)),
                         ("MATIC", ("POL", 1.0))):                  # invo.aliases (audit 2026-10-11)
        assert Bot.invo_trade_coin(b, ticker) == want, ticker
    assert Bot.invo_coin(b, "PEPE") is None


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
    assert out.get_nowait()[:2] == ("invo_baseline", "nicush")
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
    time.sleep(0.01)
    fake.resize_call(pid, cid, 0.1025)                     # ... but small changes add up (owner request 2026-10-13)
    w.poll("nicush")
    k, name, old, new = out.get_nowait()
    assert k == "invo_resize" and old.committed == pytest.approx(0.10) and new.committed == pytest.approx(0.1025)
    with fake.lock:                                        # an edit Invo does not flag (updatedAt unchanged) ...
        fake.calls[pid][0].update(entrySize=5.0, positionSize=0.05)
    w.poll("nicush")
    assert out.empty()
    w.read_at.clear()                                      # ... is read at the next RECHECK_S
    w.poll("nicush")
    k, name, old, new = out.get_nowait()
    assert k == "invo_resize" and new.committed == pytest.approx(0.05)


def test_bot_mirrors_the_traders_add_and_partial_close(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("glitty")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow glitty")
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
        text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
        assert wait_for(lambda: "resized by the trader 6.0% → 3.0%" in text())
        f.close_call(pid, cid)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
    finally:
        stop_bot(bot, th)


def test_a_copy_follows_its_call_when_invo_gives_it_a_new_id(invo_env, monkeypatch):
    """Live 2026-10-13 (prateek's BTC short): an edited call re-listed under a new id keeps our copy open in every
    wallet, a size change in the edit is mirrored, and the new id's close closes the copy."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("prateek")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow prateek")
        assert wait_for(lambda: bot.invo_watch.known.get("prateek") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=False, leverage=5, size=0.04, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions and all(
            "ETH" in w.st.positions for w in bot.invo_extra), timeout=10)
        size0 = bot.invo.st.positions["ETH"].size
        pos_id = bot.invo.st.positions["ETH"].pos_id
        new = f.reid_call(pid, cid, stopLoss=2900.0)                                 # edited: new id, same call
        assert wait_for(lambda: bot.invo.st.invo_links[pos_id]["call"] == new, timeout=10)
        time.sleep(1.5)
        assert all("ETH" in w.st.positions for w in bot.invo_wallets())             # nothing closed
        assert bot.invo.st.positions["ETH"].size == size0
        new2 = f.reid_call(pid, new, entrySize=2.0, positionSize=0.02)               # edited again: half off
        assert wait_for(lambda: bot.invo.st.positions["ETH"].size < size0 * 0.6, timeout=10)
        assert bot.invo.st.positions["ETH"].size == pytest.approx(size0 * 0.5, rel=0.03)
        new3 = f.reid_call(pid, new2, entrySize=3.0, positionSize=0.03)              # and up again: +50%
        assert wait_for(lambda: bot.invo.st.positions["ETH"].size > size0 * 0.7, timeout=10)
        assert bot.invo.st.positions["ETH"].size == pytest.approx(size0 * 0.75, rel=0.03)
        new2 = new3
        f.close_call(pid, new2, price=2950.0)
        assert wait_for(lambda: all("ETH" not in w.st.positions for w in bot.invo_wallets()), timeout=10)
        assert bot.invo.st.invo_links[pos_id]["closed_by"] == "confirmed"
    finally:
        stop_bot(bot, th)


def test_a_second_trader_on_the_same_coin_and_side_adds_size(invo_env, monkeypatch):
    """Owner request 2026-10-13 ("B"): when another followed trader calls the coin and side we already hold, the
    copy gets extra size; its close takes it off; the first trader's exit closes everything (no hand-over); the
    opposite side is still skipped; a second trader's close while the bot is off is caught at the restart."""
    hl, dc, data, cdir, f, tok = invo_env
    pa, pb = f.add_user("aaa")[0], f.add_user("bbb")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow aaa")
        dc.say("/invofollow bbb")
        assert wait_for(lambda: all(bot.invo_watch.known.get(n) is not None for n in ("aaa", "bbb")), timeout=10)
        risk = next(w for w in bot.invo_extra if w.name == "invo_risk_5pct")
        a1 = f.open_call(pa, "ETH", long=True, leverage=5, size=0.04, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions and "ETH" in risk.st.positions, timeout=10)
        s0, r0 = bot.invo.st.positions["ETH"].size, risk.st.positions["ETH"].size
        f.open_call(pb, "ETH", long=False, leverage=5, size=0.04, entry=3000.0)          # the other side: skipped
        time.sleep(2)
        assert bot.invo.st.positions["ETH"].size == s0 and bot.invo.st.positions["ETH"].side == 1
        b1 = f.open_call(pb, "ETH", long=True, leverage=5, size=0.04, entry=3010.0)      # agrees: extra size
        assert wait_for(lambda: risk.st.positions["ETH"].size > r0 * 1.3, timeout=10)
        assert risk.st.positions["ETH"].size == pytest.approx(r0 * 1.5, rel=0.05)      # half a copy more
        assert "invo:bbb" in risk.st.positions["ETH"].backers
        assert wait_for(lambda: bot.invo.st.positions["ETH"].size > s0 * 1.05, timeout=10)
        text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
        assert wait_for(lambda: "@bbb also called it" in text())
        f.close_call(pb, b1)                                                              # bbb out: extra size off
        assert wait_for(lambda: risk.st.positions["ETH"].size < r0 * 1.05, timeout=10)
        assert risk.st.positions["ETH"].size == pytest.approx(r0, rel=0.05)
        assert "invo:bbb" not in risk.st.positions["ETH"].backers
        b2 = f.open_call(pb, "ETH", long=True, leverage=5, size=0.04, entry=3005.0)      # agrees again
        assert wait_for(lambda: "invo:bbb" in risk.st.positions["ETH"].backers, timeout=10)
    finally:
        stop_bot(bot, th)
        bot.lock.f.close()
    f.close_call(pb, b2)                                                                  # while the bot is off
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        risk = next(w for w in bot.invo_extra if w.name == "invo_risk_5pct")
        assert wait_for(lambda: "invo:bbb" not in risk.st.positions["ETH"].backers, timeout=10)
        assert risk.st.positions["ETH"].size == pytest.approx(r0, rel=0.05)
        b3 = f.open_call(pb, "ETH", long=True, leverage=5, size=0.04, entry=3005.0)
        assert wait_for(lambda: "invo:bbb" in risk.st.positions["ETH"].backers, timeout=10)
        f.close_call(pa, a1)                                                              # aaa out: all closed
        assert wait_for(lambda: all("ETH" not in w.st.positions for w in bot.invo_wallets()), timeout=10)
        f.close_call(pb, b3)
        time.sleep(1.5)
        assert all("ETH" not in w.st.positions for w in bot.invo_wallets())
    finally:
        stop_bot(bot, th)


def test_each_book_controls_only_its_own_wallets(invo_env, monkeypatch):
    """Owner request 2026-10-10: /invopause, /invoresume, /invoflatten, /invoreset act on the Invo wallets only, and the
    /hyper ones no longer touch them."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("akira")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        dc.say("/hyperpause")
        assert wait_for(lambda: bot.st.entries_paused)
        assert not any(w.st.entries_paused for w in bot.invo_wallets())          # Hyperliquid only
        dc.say("/hyperresume")
        assert wait_for(lambda: not bot.st.entries_paused)
        dc.say("/invopause")
        assert wait_for(lambda: all(w.st.entries_paused for w in bot.invo_wallets()))
        assert not bot.st.entries_paused and not any(w.st.entries_paused for w in bot.sides if not w.own_leaders)
        dc.say("/invoresume")
        assert wait_for(lambda: not any(w.st.entries_paused for w in bot.invo_wallets()))
        dc.say("/invofollow akira")
        assert wait_for(lambda: bot.invo_watch.known.get("akira") is not None, timeout=10)
        f.open_call(pid, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        dc.say("/invoreset 0000")
        assert wait_for(lambda: "Wrong or missing PIN. Usage: /invoreset" in text())
        dc.say(f"/invoreset {PIN}")
        assert wait_for(lambda: "Invo reset refused" in text())
        dc.say(f"/invoflatten {PIN}")
        assert wait_for(lambda: not any(w.st.positions for w in bot.invo_wallets()), timeout=10)
        assert wait_for(lambda: "Invo flattened" in text())
        dc.say("/invoprogress")
        assert wait_for(lambda: "🧾" in text() and "Progress" in text())
        dc.say(f"/invoreset {PIN}")
        assert wait_for(lambda: "Invo reset done" in text() and bot.restart_at > 0)
        arch = next((data / "archive").glob("invo-reset-*"))
        assert (arch / "wallets" / "invo_calls" / "ledger.jsonl").exists()
        assert (data / "wallets" / "risk_20pct" / "ledger.jsonl").exists()              # Hyperliquid untouched
        assert (data / "ledger.jsonl").exists()
    finally:
        stop_bot(bot, th)


def test_weak_portfolios_are_skipped_and_the_owner_can_block_one(invo_env, monkeypatch):
    """Owner request 2026-10-10: calls are only copied from a trader's good portfolios (win rate >= 80%, return >= 100%,
    20+ closed calls); /invoblock blocks one by hand, /invounblock undoes it."""
    hl, dc, data, cdir, f, tok = invo_env
    good, weak = f.add_user("nicush", n_portfolios=2)
    f.port_meta[good] = {"title": "Kitchen", "winRate": 93.0, "closedPositions": 929, "plSnapshot": 140.0}
    f.port_meta[weak] = {"title": "Probably nothing", "winRate": 77.0, "closedPositions": 39, "plSnapshot": 12.0}
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        dc.say("/invofollow nicush")
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
        f.open_call(weak, "SOL", long=True, leverage=5, size=0.05, entry=150.0)          # weak portfolio: skipped
        f.open_call(good, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)         # good one: copied
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        time.sleep(2.5)
        assert "SOL" not in bot.invo.st.positions
        dc.say("/invotraders")
        assert wait_for(lambda: "✅ Kitchen" in text() and "+140%" in text())
        assert "Probably nothing" not in text()                                           # skipped ones: not listed
        dc.say("/invoblock nicush kit")                                                    # by the start of its name
        assert wait_for(lambda: good in bot.invo.st.blocked.get("invo:nicush", {}))
        assert wait_for(lambda: "Blocked" in text() and "Kitchen" in text())
        f.open_call(good, "BTC", long=True, leverage=5, size=0.05, entry=100_000.0)
        time.sleep(2.5)
        assert "BTC" not in bot.invo.st.positions                                        # blocked: not copied
        cid = f.calls[good][0]["id"]
        f.close_call(good, cid)                                                           # but exits still follow
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
        dc.say("/invounblock nicush Kitchen")
        assert wait_for(lambda: not bot.invo.st.blocked.get("invo:nicush"))
        dc.say("/invoblock nicush nothing-like-this")
        assert wait_for(lambda: "Its portfolios: Kitchen, Probably nothing" in text())
    finally:
        stop_bot(bot, th)


def test_after_a_restart_only_the_copied_call_moves_our_copy(invo_env, monkeypatch):
    """Audit 2026-10-11: after a restart a copy was matched to its call by coin and side only; with the same coin open
    in a portfolio we do not copy, that portfolio's close closed our copy. The call id is now saved with the copy."""
    hl, dc, data, cdir, f, tok = invo_env
    good, weak = f.add_user("prateek", n_portfolios=2)
    f.port_meta[weak] = {"title": "Casino", "winRate": 60.0, "closedPositions": 300, "plSnapshot": -20.0}
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow prateek")
        assert wait_for(lambda: bot.invo_watch.known.get("prateek") is not None, timeout=10)
        a = f.open_call(good, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        b = f.open_call(weak, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)     # skipped (weak portfolio)
        pos_id = bot.invo.st.positions["ETH"].pos_id
        assert bot.invo.st.invo_links[pos_id]["call"] == a
        assert "their entry" in bot.invo.st.invo_links[pos_id]["info"]
    finally:
        stop_bot(bot, th)
        bot.lock.f.close()
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert bot.invo_calls[("invo_calls", a)] == "ETH" and "their entry" in bot.invo_info[pos_id]
        assert wait_for(lambda: bot.invo_watch.known.get("prateek") is not None, timeout=10)
        f.close_call(weak, b)
        time.sleep(3)
        assert "ETH" in bot.invo.st.positions                                # the other portfolio's close: ignored
        f.close_call(good, a)
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions, timeout=10)
    finally:
        stop_bot(bot, th)


def test_a_resize_or_a_new_id_while_the_bot_was_off_is_applied_at_the_restart(invo_env, monkeypatch):
    """Owner request 2026-10-13 ("if the trader resizes it, resize it, always"): an add or a trim made while the bot
    was off is mirrored when it starts again, also when Invo re-ided the call meanwhile."""
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("prateek")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow prateek")
        assert wait_for(lambda: bot.invo_watch.known.get("prateek") is not None, timeout=10)
        a = f.open_call(pid, "ETH", long=True, leverage=5, size=0.04, entry=3000.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        size0 = bot.invo.st.positions["ETH"].size
        r1 = bot.invo_extra[0]
        assert wait_for(lambda: "ETH" in r1.st.positions, timeout=10)
        size1 = r1.st.positions["ETH"].size
    finally:
        stop_bot(bot, th)
        bot.lock.f.close()
    b = f.reid_call(pid, a, entrySize=6.0, positionSize=0.06)                         # while off: new id, +50%
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert wait_for(lambda: bot.invo.st.positions.get("ETH") is not None
                        and bot.invo.st.positions["ETH"].size > size0 * 1.4, timeout=10)
        assert bot.invo.st.positions["ETH"].size == pytest.approx(size0 * 1.5, rel=0.03)
        r1 = next(w for w in bot.invo_extra if w.name == r1.name)
        assert wait_for(lambda: r1.st.positions["ETH"].size > size1 * 1.4, timeout=10)
        assert bot.invo_calls[("invo_calls", b)] == "ETH"
        f.close_call(pid, b)
        assert wait_for(lambda: all("ETH" not in w.st.positions for w in bot.invo_wallets()), timeout=10)
    finally:
        stop_bot(bot, th)


def test_a_restart_before_hyperliquid_answers_does_not_close_the_copies(invo_env, monkeypatch):
    """Audit 2026-10-11: without Hyperliquid's coin list (network still coming up) no call mapped to a coin and the
    first read closed EVERY Invo copy as "closed while the bot was not watching". It now waits for the list."""
    from copybot import hl as hlmod
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("akira")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow akira")
        assert wait_for(lambda: bot.invo_watch.known.get("akira") is not None, timeout=10)
        eth = f.open_call(pid, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)
        f.open_call(pid, "SOL", long=False, leverage=5, size=0.05, entry=150.0)
        assert wait_for(lambda: {"ETH", "SOL"} <= set(bot.invo.st.positions), timeout=10)
    finally:
        stop_bot(bot, th)
        bot.lock.f.close()
    f.close_call(pid, eth)                                               # while the bot is off
    real_meta, held = hlmod.Info.meta, {"bot": None, "waited": False}

    def meta(self, *a, **k):                                             # Hyperliquid unreachable until the Invo
        b = held["bot"]                                                  # baseline had to wait for it
        if b is None or not b.invo_baseline_wait:
            raise OSError("network down")
        held["waited"] = True
        return real_meta(self, *a, **k)
    monkeypatch.setattr(hlmod.Info, "meta", meta)
    monkeypatch.setattr("copybot.runner.Bot.boot", _remember(held))
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert wait_for(lambda: held["waited"] and "ETH" not in bot.invo.st.positions, timeout=20)
        assert "SOL" in bot.invo.st.positions and bot.invo.st.closed[-1]["coin"] == "ETH"
    finally:
        stop_bot(bot, th)


def _remember(held):
    from copybot.runner import Bot
    boot = Bot.boot

    def wrapped(self):
        held["bot"] = self
        return boot(self)
    return wrapped


def test_one_normal_stop_does_not_drop_an_invo_trader(invo_env, monkeypatch):
    """Audit 2026-10-11: the shared leader-pause limit (10% of equity / 7 = 4.29$) dropped a trader on one 3% stop of a
    140$+ copy. The Invo limit is invo.pause_dd_pct (4%) of the wallet."""
    hl, dc, data, cdir, f, tok = invo_env
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        r = bot.invo.cfg.risk
        assert bot.invo.st.equity0 * r.leader_pause_dd_pct / 100 / r.max_leaders == pytest.approx(12.0)
        assert bot.cfg.risk.leader_pause_dd_pct == 10.0                  # the Hyperliquid wallets keep their rule
    finally:
        stop_bot(bot, th)


def test_every_size_runs_with_every_stop_and_only_the_exit_differs(invo_env, monkeypatch):
    """Owner request 2026-10-12: 3%, 8% and "stop whenever the trader stops" for every size, on the same call; a 4%
    drop stops only the 3% version, the trader's close closes the others. Since 2026-10-13 ("A") an r% wallet loses
    r% of itself at ITS stop, so the wider the stop the smaller the copy."""
    from copybot import runner
    monkeypatch.setattr(invo, "VANISH_S", 10.0)              # (the deleted call below, without a 2-minute wait)
    hl, dc, data, cdir, f, tok = invo_env
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        dc.say("/invofollow nicush")
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
        w = {x.name: x for x in bot.invo_extra}
        s3, s8, tr = w["invo_risk_1pct"], w["invo_risk_1pct_stop8"], w["invo_risk_1pct_trader"]
        cid = f.open_call(pid, "ETH", long=True, leverage=10, size=0.05, entry=3000.0)
        assert wait_for(lambda: all("ETH" in x.st.positions for x in (s3, s8, tr)), timeout=10)
        e = {x.name: x.st.positions["ETH"].entry_px for x in (s3, s8, tr)}
        assert s3.st.positions["ETH"].stop_px == pytest.approx(e["invo_risk_1pct"] * 0.97)
        assert s8.st.positions["ETH"].stop_px == pytest.approx(e["invo_risk_1pct_stop8"] * 0.92)
        assert tr.st.positions["ETH"].stop_px == pytest.approx(e["invo_risk_1pct_trader"] * 0.70)  # a guard only
        loss = [x.st.positions["ETH"].size * abs(x.st.positions["ETH"].entry_px - x.st.positions["ETH"].stop_px)
                for x in (s3, s8)]
        assert loss[0] == pytest.approx(3.0, rel=0.05) and loss[1] == pytest.approx(3.0, rel=0.05)   # 1% of 300$
        assert wait_for(lambda: "ETH" in w["invo_risk_20pct_trader"].st.positions, timeout=10)
        big = w["invo_risk_20pct_trader"].st.positions["ETH"]                     # 20% at its 30% guard: 200$
        assert big.size * big.entry_px == pytest.approx(200.0, rel=0.05)
        assert big.stop_px == pytest.approx(big.entry_px * 0.70)
        assert runner.invo_emergency_stop(20 / 3) == pytest.approx(11.2, abs=0.1)   # (trader size: before liquidation)
        hl.mids["ETH"] = 3000.0 * 0.96
        assert wait_for(lambda: "ETH" not in s3.st.positions, timeout=10)
        assert s3.st.closed[-1]["reason"] == "stop"
        time.sleep(1.5)
        assert "ETH" in s8.st.positions and "ETH" in tr.st.positions
        f.close_call(pid, cid, price=2880.0)
        assert wait_for(lambda: "ETH" not in s8.st.positions and "ETH" not in tr.st.positions, timeout=10)
        assert tr.st.closed[-1]["reason"] == "leader_close"
        # each copy keeps its portfolio and how the trader's close was known (analysis 2026-10-12)
        link = tr.st.invo_links[tr.st.closed[-1]["pos_id"]]
        assert (link["call"], link["portfolio"], link["closed_by"]) == (cid, pid, "confirmed")
        assert "closed_by" not in s3.st.invo_links[s3.st.closed[-1]["pos_id"]]      # our stop closed that one
        # a call its usual-stop version did not copy is not copied by the 8% / trader-exit versions either
        s3.rec({"ev": "pause", "reason": "test"})
        sol = f.open_call(pid, "SOL", long=True, leverage=10, size=0.05, entry=150.0)
        assert wait_for(lambda: "SOL" in w["invo_risk_2pct_trader"].st.positions, timeout=10)
        assert all("SOL" not in x.st.positions for x in (s3, s8, tr))
        f.delete_call(pid, sol)                                                    # gone without a close
        assert wait_for(lambda: "SOL" not in w["invo_risk_2pct_trader"].st.positions, timeout=20)
        x = w["invo_risk_2pct_trader"]
        assert x.st.invo_links[x.st.closed[-1]["pos_id"]]["closed_by"] == "vanished"
        dc.say("/invowallets")
        assert wait_for(lambda: any("Best" in m["text"] and "Worst" in m["text"] and "By stop" in m["text"]
                                    for m in dc.sent + dc.edits))
    finally:
        stop_bot(bot, th)


def test_with_main_stop_0_the_main_wallets_hold_until_the_trader_closes(invo_env, monkeypatch):
    """Owner request 2026-10-12: "keep the trades open until the traders close it (the 3% and 8% will still work)".
    invo.main_stop_pct = 0: invo_calls and invo_risk_<r>pct have only the liquidation guard, the 3% versions are
    invo_*_stop3 and copy the same calls."""
    hl, dc, data, cdir, f, tok = invo_env
    with open(cdir / "invo.toml", "a", encoding="utf-8") as fh:
        fh.write("main_stop_pct = 0.0\n")
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        names = {w.name for w in bot.invo_wallets()}
        assert bot.invo.name == "invo_calls" and bot.invo.stop_label == "trader's exit"
        assert {"invo_calls_stop3", "invo_calls_stop8", "invo_risk_1pct_stop3"} <= names and len(names) == 18
        assert not any(n.endswith("_trader") for n in names)
        dc.say("/invofollow nicush")
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
        w = {x.name: x for x in bot.invo_wallets()}
        main, s3 = w["invo_calls"], w["invo_calls_stop3"]
        cid = f.open_call(pid, "ETH", long=True, leverage=10, size=0.05, entry=3000.0)
        assert wait_for(lambda: all("ETH" in x.st.positions for x in w.values()), timeout=10)
        assert main.st.positions["ETH"].stop_px == pytest.approx(main.st.positions["ETH"].entry_px * 0.70)
        assert s3.st.positions["ETH"].stop_px == pytest.approx(s3.st.positions["ETH"].entry_px * 0.97)
        assert main.st.positions["ETH"].size == pytest.approx(s3.st.positions["ETH"].size, rel=0.02)
        hl.mids["ETH"] = 3000.0 * 0.96                                    # our 3% stop, not the trader's exit
        assert wait_for(lambda: "ETH" not in s3.st.positions and "ETH" not in w["invo_risk_5pct_stop3"].st.positions,
                        timeout=10)
        time.sleep(1.5)
        assert "ETH" in main.st.positions and "ETH" in w["invo_risk_5pct"].st.positions
        f.close_call(pid, cid, price=2880.0)
        assert wait_for(lambda: all("ETH" not in x.st.positions for x in w.values()), timeout=10)
        assert main.st.closed[-1]["reason"] == "leader_close"
        text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
        assert wait_for(lambda: "closed by the trader" in text())         # the main wallet's card
    finally:
        stop_bot(bot, th)


def test_the_wallets_card_shows_the_best_and_worst_combinations():
    from copybot import cardfmt
    from copybot.ledger import State
    rows = []
    combos = [(a, b) for a in ("1% risk", "20% risk", "trader size") for b in ("3% stop", "8% stop", "trader's exit")]
    for i, (size, stop) in enumerate(combos):
        st = State(equity0=300.0, realized=(i - 4) * 5.0)
        rows.append((size, stop, st))
    text = cardfmt.invo_wallets_card(rows, {}, 0)
    assert "9 combinations (3 sizes × 3 stops)" in text and "🏆 <b>Best</b>" in text and "🔻 <b>Worst</b>" in text
    assert "🥇 <b>trader size · trader&#x27;s exit</b>" in text or "🥇 <b>trader size · trader's exit</b>" in text
    assert "9. <b>1% risk · 3% stop</b>" in text
    assert "20% risk · 3% stop" not in text.split("By stop")[0]               # the middle is left out
    assert text.count("biggest drop") == 6 and "<b>By stop</b>" in text
