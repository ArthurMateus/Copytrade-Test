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


def add_trader(f: FakeInvo, name: str, good: bool) -> None:
    pid = f.add_user(name)[0]
    now = int(time.time() * 1000)
    for i in range(45):
        start = now - 150 * DAY + int(i * 145 * DAY / 45)
        move = (-0.01 if i % 3 == 2 else 0.02) if good else (-0.02 if i % 2 else 0.01)
        f.add_closed(pid, "ETH", True, 5, 0.03, 3000.0, 3000.0 * (1 + move), start, start + 4 * 3_600_000)


def test_the_search_finds_a_good_trader_reports_it_and_drops_a_followed_one_that_keeps_failing(search_env,
                                                                                              monkeypatch):
    hl, dc, data, cdir, f, tok = search_env
    add_trader(f, "steady", good=True)
    add_trader(f, "coinflip", good=False)
    f.ranked = ["steady", "coinflip"]
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    monkeypatch.setattr(invo_scorer, "REQ_GAP_S", 0.0)
    bot, th = start_bot((hl, dc, data, cdir))
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        assert bot.invo_search is not None
        dc.say("/invofollow coinflip")
        assert wait_for(lambda: "invo:coinflip" in bot.invo.st.followed)
        # the first search (at start) is shown as soon as it ends
        assert wait_for(lambda: "Daily picks · 🧾 Invo" in text(), timeout=60)
        assert bot.invo_scores["steady"]["eligible"], bot.invo_scores["steady"]["reasons"]
        assert not bot.invo_scores["coinflip"]["eligible"]
        assert "<code>/invofollow steady</code>" in text() and "@steady" in text()
        assert "invo:steady" not in bot.invo.st.followed                       # it only reports
        assert "invo:coinflip" in bot.invo.st.followed                         # 1 failed search: kept
        bot.invo_search.req.set()                                              # a second search
        assert wait_for(lambda: "invo:coinflip" not in bot.invo.st.followed, timeout=60)
        assert wait_for(lambda: "fails the rules 2 searches in a row" in text()
                        and "/invofollow coinflip brings it back" in text())
        n = len(dc.sent)
        dc.say("/picks")
        assert wait_for(lambda: any("Daily picks · 🔷 Hyperliquid" in m["text"] for m in dc.sent[n:]))
        assert wait_for(lambda: any("Daily picks · 🧾 Invo" in m["text"] for m in dc.sent[n:]))
        assert not (data / "picks.json").exists()                              # /picks never replaces the baseline
        dc.say("/invofollow coinflip")                                          # no blacklist: it can come back
        assert wait_for(lambda: "invo:coinflip" in bot.invo.st.followed)
        assert "REFRESH" not in text()
    finally:
        stop_bot(bot, th)


def test_invosearch_searches_now_and_follows_the_best(search_env, monkeypatch):
    hl, dc, data, cdir, f, tok = search_env
    add_trader(f, "steady", good=True)
    add_trader(f, "coinflip", good=False)
    f.ranked = ["steady", "coinflip"]
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    monkeypatch.setattr(invo_scorer, "REQ_GAP_S", 0.0)
    bot, th = start_bot((hl, dc, data, cdir))
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        dc.say("/invofollow coinflip")
        assert wait_for(lambda: "invo:coinflip" in bot.invo.st.followed)
        dc.say("/invosearch")
        assert wait_for(lambda: "Invo search finished" in text(), timeout=90)
        assert "invo:steady" in bot.invo.st.followed and "invo:coinflip" not in bot.invo.st.followed
        assert wait_for(lambda: "@steady" in text() and "➖ dropped: @coinflip" in text())
        assert wait_for(lambda: all("invo:steady" in w.st.followed for w in bot.invo_extra))
    finally:
        stop_bot(bot, th)
