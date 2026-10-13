"""The daily Invo search (copybot/invo_scorer.py) and the daily picks report (copybot/picks.py): pure scoring of call
histories with the Hyperliquid rules, the report's day-to-day comparison, and the bot end to end (FakeHL +
FakeDiscord + FakeInvo)."""
import copy
import time

import pytest

from copybot import config, invo, invo_scorer, picks, scoring
from tests.fakes import fixture
from tests.fakes_invo import FakeInvo
from tests.test_e2e import env, start_bot, stop_bot, wait_for  # noqa: F401 (env is a fixture)

DAY = 86_400_000
NOW = 1_800_000_000_000
CFG = config.load("config", env={})
PARAMS = scoring.ScoreParams(stop_pct=3.0, cost_bps=13.0, min_win_rate=CFG.selection.min_win_rate,
                             min_score=CFG.selection.min_score, min_profit_factor=CFG.selection.min_profit_factor,
                             max_dd_cap=CFG.selection.max_drawdown, max_open_loss=CFG.selection.max_open_loss,
                             max_loss_7d=CFG.selection.max_loss_7d, max_loss_24h=CFG.selection.max_loss_24h,
                             max_loss_streak=CFG.selection.max_loss_streak,
                             min_recent_win_rate=CFG.selection.min_recent_win_rate, max_dd_7d=CFG.selection.max_dd_7d,
                             max_best_trade_share=CFG.selection.max_best_trade_share)


def call(ticker="ETH", long=True, lev=5.0, size=0.03, entry=3000.0, close=3060.0, start=NOW - 10 * DAY,
         hours=4.0, i=0) -> invo.Call:
    """A closed call built from the REAL recorded one (fixture), with the given numbers."""
    raw = copy.deepcopy(fixture("invo_investments_closed.json")["investmentsTicker"][0])
    end = start + int(hours * 3_600_000)
    raw.update(id=f"c{i}", ticker=ticker, directionLong=long, leverage=lev, entrySize=size * 100, positionSize=size,
               entryPrice=entry, closingPrice=close, isOpen=False,
               createdAt=f"{_iso(start)}", closedAt=f"{_iso(end)}")
    return invo.parse_calls({"investmentsTicker": [raw]})[0]


def _iso(ms: int) -> str:
    from tests.fakes_invo import iso
    return iso(ms)


def history(n=60, days=150, win=0.02, loss=-0.01, ticker="ETH"):
    """n calls spread over `days`: two of three win `win`, one loses `loss` (price moves, long)."""
    out = []
    for i in range(n):
        start = NOW - days * DAY + int(i * days * DAY / n)
        move = loss if i % 3 == 2 else win
        out.append(call(ticker=ticker, entry=3000.0, close=3000.0 * (1 + move), start=start, i=i))
    return out


def score(closed, opened=(), coin_of=lambda t: t if t in ("ETH", "BTC", "SOL") else None):
    return invo_scorer.score_trader("t", list(closed), list(opened), coin_of, lambda c, s, e: [], {"ETH": 3000.0},
                                    NOW, PARAMS, 0.5, 15.0, 14.0)


# ---- scoring (pure) --------------------------------------------------------------------------------------------
def test_each_closed_call_is_one_round_trip_with_its_own_pnl():
    fills = invo_scorer.calls_to_fills([("ETH", call(close=3060.0)), ("ETH", call(long=False, close=3060.0, i=1))])
    trips = scoring.round_trips(fills)
    assert len(trips) == 2                                   # overlapping calls on one coin never merge
    long_, short = sorted(trips, key=lambda t: t.side, reverse=True)
    # 3% of the portfolio x 5x = 15% exposure of the 10,000 base; +2% price = +30
    assert long_.net == pytest.approx(30.0) and short.net == pytest.approx(-30.0)
    assert long_.ret == pytest.approx(0.02)


def test_a_steady_trader_passes_the_same_rules_as_a_wallet():
    s = score(history())
    assert s["eligible"], s["reasons"]
    assert s["trades"] == 60 and s["win_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert s["score"] >= CFG.selection.min_score and s["hl_share"] == 1.0
    assert s["exposure"] == pytest.approx(0.15)


def test_one_lucky_call_carrying_the_profit_fails():
    calls = history(n=45, win=0.004, loss=-0.006)            # roughly flat ...
    calls.append(call(close=3000 * 1.30, start=NOW - 3 * DAY, i=99))    # ... plus one +30% call
    s = score(calls)
    assert not s["eligible"]
    assert any(r.startswith("one_trade>") for r in s["reasons"])


def test_invo_gates_coins_not_on_hyperliquid_short_calls_and_silence():
    s = score(history(ticker="XYZ"))
    assert "not_on_hyperliquid" in s["reasons"] and not s["eligible"]
    fast = [call(start=NOW - 100 * DAY + i * DAY, hours=0.05, close=3000 * (1.02 if i % 3 else 0.99), i=i)
            for i in range(60)]
    assert "too_fast" in score(fast)["reasons"]
    old = [call(start=NOW - 170 * DAY + i * DAY // 3, close=3000 * (1.02 if i % 3 else 0.99), i=i) for i in range(60)]
    assert "inactive" in score(old)["reasons"]


def test_open_calls_losing_now_count_against_it():
    o = call(entry=4000.0, close=None, start=NOW - DAY, i=500)          # long from 4000, ETH now 3000: -25% x 15%
    o = invo.Call(**{**o.__dict__, "is_open": True, "closed_ms": None, "closing_price": None})
    s = score(history(), opened=[o])
    assert s["open_calls"] == 1 and s["open_loss_pct"] > 0.03
    assert not s["eligible"]


def test_rankings_are_read_structurally():
    body = {"portfolios": fixture("invo_users_portfolios.json")["portfolios"]}
    assert invo.usernames(body) == ["prateek"]
    assert invo.usernames({"data": {"users": [{"username": "A"}, {"user": {"username": "b"}}, {"username": "a"}]}}) \
        == ["a", "b"]


def test_a_win_smaller_than_our_cost_counts_as_a_loss():
    """Audit 2026-10-11: Invo counts ETH 2472.8 -> 2473.1 (+0.01%) as a win; a copy loses our 13 bps on it."""
    tiny = [call(entry=3000.0, close=3000.3, start=NOW - 100 * DAY + i * DAY, i=i) for i in range(40)]
    s = score(tiny)
    assert s["win_rate"] == 0.0 and s["pnl"] < 0 and not s["eligible"]


def test_pepe_calls_are_scored_on_kpepe_prices():
    """Audit 2026-10-11: PEPE calls are copied on kPEPE (x1000) but the search left them out as not on Hyperliquid."""
    seen = []

    def candles(coin, s, e):
        seen.append(coin)
        return []
    calls = [call(ticker="PEPE", entry=0.00001, close=0.0000102 if i % 3 else 0.0000099,
                  start=NOW - 150 * DAY + i * 2 * DAY, i=i) for i in range(60)]
    s = invo_scorer.score_trader("t", calls, [], lambda t: ("kPEPE", 1000.0) if t == "PEPE" else (None, 1.0),
                                 candles, {}, NOW, PARAMS, 0.5, 15.0, 14.0)
    assert s["hl_share"] == 1.0 and set(seen) == {"kPEPE"}
    assert s["win_rate"] == pytest.approx(2 / 3, abs=0.01)


def test_a_trader_is_judged_on_every_portfolio_and_needs_one_we_would_copy():
    """Audit 2026-10-11: scoring only the portfolios that already show 80%+ wins selected on the outcome. The search
    now reads every portfolio; the floors only decide what is copied."""
    s = invo_scorer.score_trader("t", history(), [], lambda t: t if t == "ETH" else None, lambda c, a, b: [],
                                 {"ETH": 3000.0}, NOW, PARAMS, 0.5, 15.0, 14.0, portfolios=(3, 0, 2))
    assert "no_copyable_portfolio" in s["reasons"] and not s["eligible"]
    assert (s["portfolios"], s["copyable_portfolios"], s["losing_portfolios"]) == (3, 0, 2)


def test_funding_paid_while_a_call_is_held_comes_off_its_result():
    """Owner request 2026-10-12: calls held for weeks pay funding; the search now charges the real hourly rates."""
    from copybot import hl
    rates = hl.parse_funding(fixture("funding_history.json"))
    assert len(rates) == 4 and rates[0][1] == pytest.approx(0.0000125)
    assert invo_scorer.funding_paid(rates, 1, rates[0][0], rates[-1][0]) == pytest.approx(4 * 0.0000125)
    assert invo_scorer.funding_paid(rates, -1, rates[0][0], rates[-1][0]) == pytest.approx(-4 * 0.0000125)
    hourly = [(NOW - 200 * DAY + h * 3_600_000, 0.0001) for h in range(200 * 24)]     # 0.01%/h: longs pay
    calls = [call(close=3000.0, start=NOW - 10 * DAY, hours=100.0)]                  # flat price, held 100 h
    fills = invo_scorer.calls_to_fills([("ETH", c) for c in calls], 0.0, lambda coin: hourly)
    trip, = scoring.round_trips(fills)
    assert trip.ret == pytest.approx(-0.01, rel=0.02)                     # 100 h x 0.01% = 1% of the notional
    s0 = score(history())
    s1 = invo_scorer.score_trader("t", history(), [], lambda t: t, lambda c, a, b: [], {"ETH": 3000.0}, NOW, PARAMS,
                                  0.5, 15.0, 14.0, funding_of=lambda coin, a, b: hourly)
    assert s1["pnl"] < s0["pnl"]


# ---- the report (pure) --------------------------------------------------------------------------------------------
def test_report_compares_with_the_previous_day_and_says_why_one_left(tmp_path):
    st = picks.Store(tmp_path / "picks.json")
    e = lambda i, f=False: picks.Entry(f"id{i}", f"name{i}", 90 - i, "stats", f"/hyperadd id{i}", f)
    b1 = picks.Book("hyper", "🔷 Hyperliquid", [e(1), e(2), e(3)], "300 scored")
    first = picks.render(b1, st.prev("hyper"), "2026-10-10", 7)
    assert "fewer than 7 pass" in first and "🆕" not in first and "<code>/hyperadd id1</code>" in first
    st.save_day("2026-10-10", [b1])
    st2 = picks.Store(tmp_path / "picks.json")                       # survives a restart
    b2 = picks.Book("hyper", "🔷 Hyperliquid", [e(1, True), e(4)], "", {"id2": "fails: win rate<45%"})
    text = picks.render(b2, st2.prev("hyper"), "2026-10-11", 7)
    assert "compared with 2026-10-10" in text
    assert "✅ <b>name1</b>" in text and "⭐ followed" in text and "/hyperadd id1" not in text
    assert "🆕 <b>name4</b>" in text
    assert "❌ name2 · fails: win rate&lt;45%" in text and "❌ name3 · not checked again yet" in text
    assert "🆕 1 new · ✅ 1 still in · ❌ 2 left" in text


def test_daily_report_is_due_once_after_the_hour_local_time(tmp_path):
    st = picks.Store(tmp_path / "p.json")
    # 2026-10-10 15:59 UTC = 12:59 in Brazil (UTC-3): not yet; 16:00 UTC = 13:00: due
    t1259 = 1_791_647_940
    assert picks.local_day(t1259, -3) == "2026-10-10" and not picks.due(st, t1259, -3, 13)
    assert picks.due(st, t1259 + 60, -3, 13)
    st.save_day("2026-10-10", [])
    assert not picks.due(st, t1259 + 3600, -3, 13)


# ---- the bot end to end ----------------------------------------------------------------------------------------
@pytest.fixture
def search_env(env, tmp_path):
    hl, dc, data, cdir = env
    f = FakeInvo()
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    (cdir / "invo.toml").write_text(f'api_base = "{f.url}"\npoll_s = 1.0\ndiscover_pages = 1\n', encoding="utf-8")
    yield hl, dc, data, cdir, f, tok
    f.close()


def add_trader(f: FakeInvo, name: str, good: bool) -> str:
    pid = f.add_user(name)[0]
    now = int(time.time() * 1000)
    for i in range(45):
        start = now - 150 * DAY + int(i * 145 * DAY / 45)
        move = (-0.01 if i % 3 == 2 else 0.02) if good else (-0.02 if i % 2 else 0.01)
        f.add_closed(pid, "ETH", True, 5, 0.03, 3000.0, 3000.0 * (1 + move), start, start + 4 * 3_600_000)
    return pid


def test_the_search_asks_before_changing_anything_and_never_drops_a_pick(search_env, monkeypatch):
    """Owner request 2026-10-12 (a 3 AM search dropped every followed trader): /invosearch PROPOSES its changes and a
    followed trader whose portfolios are all skipped is PROPOSED for a drop; the owner answers with a button. A
    /invofollow pick is never proposed. Failed searches count only for traders read in that search."""
    hl, dc, data, cdir, f, tok = search_env
    pid = add_trader(f, "steady", good=True)
    add_trader(f, "coinflip", good=False)
    f.ranked = ["steady", "coinflip"]
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    monkeypatch.setattr(invo_scorer, "REQ_GAP_S", 0.0)
    monkeypatch.setattr(invo_scorer, "FAIL_GAP_MS", 0)
    bot, th = start_bot((hl, dc, data, cdir))
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        assert bot.invo_search is not None
        dc.say("/invofollow coinflip")
        assert wait_for(lambda: "invo:coinflip" in bot.invo.st.followed)
        assert "invo:coinflip" in bot.invo.st.picked                          # the owner's pick
        # the first search (at start) is shown as soon as it ends; it only reports
        assert wait_for(lambda: "Daily picks · 🧾 Invo" in text(), timeout=60)
        assert bot.invo_scores["steady"]["eligible"], bot.invo_scores["steady"]["reasons"]
        assert not bot.invo_scores["coinflip"]["eligible"]
        assert "<code>/invofollow steady</code>" in text() and "invo:steady" not in bot.invo.st.followed
        dc.say("/invosearch")
        assert wait_for(lambda: dc.asked("Apply?"), timeout=90)
        q = dc.asked("Apply?")
        assert "➕ follow: @steady" in dc.text(q) and "drop" not in dc.text(q)  # the pick stays
        assert "invo:steady" not in bot.invo.st.followed                       # nothing changes before the answer
        dc.click(q, "✅ Apply")
        assert wait_for(lambda: "invo:steady" in bot.invo.st.followed)
        assert wait_for(lambda: all("invo:steady" in w.st.followed for w in bot.invo_extra))
        assert wait_for(lambda: "Done" in dc.text(q) and not dc.buttons[q])   # the question shows the answer
        assert "invo:coinflip" in bot.invo.st.followed
        # steady starts losing: after drop_after_fails (2) searches with nothing copyable, the owner is asked
        f.closed[pid] = []
        now = int(time.time() * 1000)
        for i in range(45):
            start = now - 150 * DAY + int(i * 145 * DAY / 45)
            f.add_closed(pid, "ETH", True, 5, 0.03, 3000.0, 3000.0 * (0.98 if i % 2 else 1.01), start,
                         start + 4 * 3_600_000)
        for n in (1, 2):
            bot.invo_search.req.set()
            assert wait_for(lambda: bot.invo_search.fails.get("steady") == n, timeout=90)
        assert wait_for(lambda: dc.asked("Drop @steady?"), timeout=30)
        q = dc.asked("Drop @steady?")
        assert "invo:steady" in bot.invo.st.followed and dc.asked("Drop @coinflip?") is None
        dc.click(q, "🗑", user="12345")              # not the owner: refused
        assert wait_for(lambda: any("Only the owner" in r.get("data", {}).get("content", "") for r in dc.replies))
        assert "invo:steady" in bot.invo.st.followed
        dc.click(q, "🗑")
        assert wait_for(lambda: "invo:steady" not in bot.invo.st.followed)
        assert wait_for(lambda: "Dropped" in dc.text(q) and not dc.buttons[q])
        assert "invo:coinflip" in bot.invo.st.followed
        n = len(dc.sent)
        dc.say("/picks")
        assert wait_for(lambda: any("Daily picks · 🧾 Invo" in m["text"] for m in dc.sent[n:]))
        assert not (data / "picks.json").exists()                              # /picks never replaces the baseline
        dc.say("/invofollow steady")                                            # no blacklist: it can come back
        assert wait_for(lambda: "invo:steady" in bot.invo.st.followed)
        assert "REFRESH" not in text()
    finally:
        stop_bot(bot, th)


def test_invoadd_checks_a_trader_now_and_follows_it_if_it_passes(search_env, monkeypatch):
    hl, dc, data, cdir, f, tok = search_env
    add_trader(f, "steady", good=True)
    add_trader(f, "coinflip", good=False)
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    monkeypatch.setattr(invo_scorer, "REQ_GAP_S", 0.0)
    bot, th = start_bot((hl, dc, data, cdir))
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        dc.say("/invoadd coinflip")
        assert wait_for(lambda: "/invoadd @coinflip fails the strict rules" in text(), timeout=60)
        assert "invo:coinflip" not in bot.invo.st.followed
        dc.say("/invoadd steady")
        assert wait_for(lambda: "invo:steady" in bot.invo.st.followed, timeout=60)
        assert wait_for(lambda: "passes every rule" in text())
        assert wait_for(lambda: all("invo:steady" in w.st.followed for w in bot.invo_extra))
    finally:
        stop_bot(bot, th)


def test_each_portfolio_is_scored_and_only_passing_ones_are_copied():
    """Owner request 2026-10-12: a trader is judged per portfolio. Its best COPYABLE portfolio gives its score; a
    trader whose portfolios together lose fails even when one looks good (the bait pattern)."""
    good = invo_scorer.score_trader("t/A", history(), [], lambda t: t, lambda c, a, b: [], {"ETH": 3000.0}, NOW,
                                    PARAMS, 0.5, 15.0, 14.0)
    bad = invo_scorer.score_trader("t/B", history(win=0.004, loss=-0.02), [], lambda t: t, lambda c, a, b: [],
                                   {"ETH": 3000.0}, NOW, PARAMS, 0.5, 15.0, 14.0)
    per = {"A": {"copy": True, "title": "A"}, "B": {"copy": False, "title": "B"}}
    overall = {"trades": 120, "pnl": 10.0, "win_rate": 0.6, "profit_factor": 1.5, "address": "t"}
    r = invo_scorer.trader_result(overall, {"A": good, "B": bad}, per)
    assert r["eligible"] and r["best_portfolio"] == "A" and r["score"] == good["score"] and r["address"] == "t"
    assert r["copyable_portfolios"] == 1 and r["by_portfolio"] is per
    lost = invo_scorer.trader_result({**overall, "pnl": -5.0}, {"A": good, "B": bad}, per)
    assert not lost["eligible"] and lost["reasons"] == ["loses_overall"] and lost["score"] == 0.0
    none = invo_scorer.trader_result(overall, {"A": good, "B": bad}, {**per, "A": {"copy": False}})
    assert not none["eligible"] and "no_copyable_portfolio" in none["reasons"]
    # copied (no hard failure) but below the quality points: kept and copied, not picked by the search
    weak = invo_scorer.trader_result(overall, {"A": {**good, "eligible": False, "score": 0.0,
                                                     "reasons": ["profit_factor<2", "score<70"]}, "B": bad}, per)
    assert not weak["eligible"] and weak["copyable_portfolios"] == 1
    assert weak["reasons"] == ["profit_factor<2", "score<70"]


def test_only_a_loss_after_costs_bait_or_a_low_profit_factor_stops_a_portfolios_copies():
    """Owner request 2026-10-12 ("it skipped a bunch of portfolios that were good"): quality points no longer block a
    portfolio; losing after our costs, one call making the profit or minute-long calls still do, and (owner request
    later that day, after limpan96's PF 1.73 portfolio won 3 of 11 copies) a profit factor under the floor."""
    from types import SimpleNamespace
    from copybot.runner import Bot
    assert invo_scorer.copy_blockers(["score<70", "win_rate<45%", "max_drawdown>30%"]) == []
    assert invo_scorer.copy_blockers(["profit_factor<2", "score<70"]) == ["profit_factor<2"]
    assert invo_scorer.copy_blockers(["copy_edge<=0", "one_trade>30%_of_profit", "too_fast", "trades<30"]) == \
        ["copy_edge<=0", "one_trade>30%_of_profit", "too_fast"]
    cfg = config.load("config", env={})
    per = {"good": {"reasons": ["score<70"]}, "bait": {"reasons": ["one_trade>30%_of_profit"]},
           "weak": {"reasons": ["profit_factor<2", "score<70"]},
           "live": {"reasons": ["score<70"]}}
    b = SimpleNamespace(cfg=cfg, invo=None, invo_scores={"t": {"by_portfolio": per, "reasons": ["loses_overall"]}})
    b.invo_portfolio_score = lambda n, pid: Bot.invo_portfolio_score(b, n, pid)
    b.invo_floors = lambda p: Bot.invo_floors(b, p)
    port = lambda pid, kind="paper", win=90.0: SimpleNamespace(id=pid, kind=kind, closed=100, win_rate=win,
                                                               pnl_pct=300.0)
    assert Bot.invo_skip(b, "t", port("good")) is None
    assert "one call made most of the profit" in Bot.invo_skip(b, "t", port("bait"))
    assert "profit factor<2" in Bot.invo_skip(b, "t", port("weak"))
    assert Bot.invo_skip(b, "t", port("live", "live", win=10.0)) is None      # a live one that passes: no floors
    assert "win rate" in Bot.invo_skip(b, "t", port("new", win=50.0))         # not scored yet: the owner's floors
    # owner request 2026-10-13: an always_copy trader skips the floors and our rules
    b.cfg.invo.always_copy = ["@T"]
    assert Bot.invo_skip(b, "t", port("weak")) is None
    assert Bot.invo_skip(b, "t", port("new", win=50.0)) is None


def test_failed_searches_count_once_a_day_and_reset_when_something_is_copied():
    from types import SimpleNamespace
    t = [1_000_000_000]
    sc = SimpleNamespace(fails={}, fail_at={}, now=lambda: t[0])
    none, some = {"copyable_portfolios": 0}, {"copyable_portfolios": 1}
    for _ in range(3):                                        # three restarts re-searching within the hour
        invo_scorer.InvoScorer.count_fail(sc, "x", none)
    assert sc.fails["x"] == 1
    t[0] += invo_scorer.FAIL_GAP_MS
    invo_scorer.InvoScorer.count_fail(sc, "x", none)
    assert sc.fails["x"] == 2
    invo_scorer.InvoScorer.count_fail(sc, "x", some)
    assert sc.fails["x"] == 0


def test_a_real_money_portfolio_is_found_on_invos_leaderboard_scored_and_copied_alone(search_env, monkeypatch):
    """End to end: Invo's score leaderboard brings a trader with a paper portfolio that fails our rules and a real-money
    one that passes; /invofollow copies the real-money portfolio only."""
    hl, dc, data, cdir, f, tok = search_env
    paper = f.add_user("realguy")[0]
    live = f.add_live("realguy", accuracy=70.0, pnl=900.0, closed=400)
    now = int(time.time() * 1000)
    for i in range(45):
        start = now - 150 * DAY + int(i * 145 * DAY / 45)
        f.add_closed(paper, "ETH", True, 2, 0.01, 3000.0, 3000.0 * (0.985 if i % 2 else 1.004), start,
                     start + 4 * 3_600_000)
        cid = f.live_open(live, "ETH", True, 5, 10.0, 3000.0, 1.0, created_ms=start)
        f.live_close(live, cid, 3000.0 * (0.99 if i % 3 == 2 else 1.02), closed_ms=start + 4 * 3_600_000)
    f.score_top = [{"rank": 1, "user": {"id": f.users["realguy"], "username": "realguy"},
                    "topPortfolio": {"id": live, "title": "Live Portfolio", "type": "live", "score": 9.1}}]
    f.invo_score[live] = 9.1
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    monkeypatch.setattr(invo_scorer, "REQ_GAP_S", 0.0)
    bot, th = start_bot((hl, dc, data, cdir))
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        assert wait_for(lambda: "realguy" in bot.invo_scores, timeout=60)
        s = bot.invo_scores["realguy"]
        assert s["eligible"], s["reasons"]
        assert s["best_portfolio"] == live and s["by_portfolio"][live]["copy"]
        assert s["by_portfolio"][live]["invo_score"] == 9.1 and s["by_portfolio"][live]["kind"] == "live"
        assert not s["by_portfolio"][paper]["copy"]
        assert wait_for(lambda: "real money, Invo 9.1/10" in text(), timeout=30)     # in the picks
        dc.say("/invofollow realguy")
        assert wait_for(lambda: "copying Live Portfolio" in text(), timeout=60)
        assert wait_for(lambda: bot.invo_watch.known.get("realguy") is not None, timeout=10)
        f.open_call(paper, "SOL", long=True, leverage=5, size=0.05, entry=150.0)   # fails our rules: not copied
        f.live_open(live, "ETH", True, 5, 10.0, 3000.0, 1.0)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        time.sleep(2)
        assert "SOL" not in bot.invo.st.positions
    finally:
        stop_bot(bot, th)
