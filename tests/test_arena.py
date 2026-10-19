"""The Invo arena (copybot/arena.py): replay of a wallet copying calls, recipes picking only from the past, the
walk-forward backtest, live seasons, the saved calls, the report and the /arena card."""
import json
import math

import pytest

from copybot import arena, invo, runner
from copybot.hl import Candle
from copybot.invo import Call, Portfolio

DAY = arena.DAY
HOUR = arena.HOUR
NOW = 1_800_000_000_000 // HOUR * HOUR
P = arena.Params(start_equity=300.0, size_mult=1.0, live_size_mult=10.0, cap_frac=1 / 3, min_notional=10.0,
                 cost_bps=0.0)


def mk(i, trader="a", t=NOW - 10 * DAY, hours=10, entry=100.0, close=110.0, long=True, lev=1.0, size=0.1,
       portfolio="p1", is_open=False, ticker="ETH") -> Call:
    return Call(f"{trader}{i}", portfolio, trader, ticker, long, lev, size, entry, None, None, t, is_open,
                None if is_open else t + hours * HOUR, None if is_open else close, None, size)


def flat(price=100.0):
    """Candles that never move (no guard hit, marks at `price`)."""
    return lambda coin, s, e: [Candle(t, price, price, price, price, 1.0) for t in range(s // HOUR * HOUR, e, HOUR)]


def prices(candles_of=None):
    return arena.Prices(candles_of or flat(), None, NOW - 400 * DAY, NOW + DAY)


def copies(calls):
    by = {}
    for c in calls:
        by.setdefault(c.owner, []).append(c)
    return arena.prepare({n: ({}, cs) for n, cs in by.items()}, lambda t: t)


# ---- replay -------------------------------------------------------------------------------------------------------
def test_a_copy_is_sized_like_the_main_invo_wallet_and_closed_at_the_traders_exit():
    # 10% x 2x of 300$ = 60$ notional, +10% -> +6$
    r = arena.simulate(copies([mk(0, lev=2.0, size=0.1)])["a"], NOW - 20 * DAY, NOW, P, prices())
    assert r.trades == 1 and r.pnl == pytest.approx(6.0)
    # a 50% x 5x call is capped at 1/3 of equity (100$): +10% -> +10$
    r = arena.simulate(copies([mk(0, lev=5.0, size=0.5)])["a"], NOW - 20 * DAY, NOW, P, prices())
    assert r.pnl == pytest.approx(10.0)


def test_costs_come_off_and_equity_compounds():
    p = arena.Params(cost_bps=100.0)
    cs = copies([mk(0, t=NOW - 10 * DAY), mk(1, t=NOW - 5 * DAY)])["a"]
    r = arena.simulate(cs, NOW - 20 * DAY, NOW, p, prices())
    first = 30 * 0.10 - 30 * 0.01                      # 10% of 300 = 30$ notional
    eq = 300 + first
    assert r.equity == pytest.approx(eq + eq * 0.1 * 0.10 - eq * 0.1 * 0.01)


def test_the_liquidation_guard_closes_a_copy_before_the_traders_exit():
    def crash(coin, s, e):            # one hour 40% down in the middle of the call
        mid = NOW - 10 * DAY + 5 * HOUR
        return [Candle(t, 100, 100, 60 if t == mid else 100, 100, 1) for t in range(s // HOUR * HOUR, e, HOUR)]
    r = arena.simulate(copies([mk(0, close=120.0)])["a"], NOW - 20 * DAY, NOW, P, prices(crash))
    assert r.pnl == pytest.approx(-30 * arena.guard_pct(1.0) / 100)        # stopped at -30%, not +20%


@pytest.mark.parametrize("lev", [1, 2, 3, 5, 10, 40])
def test_guard_is_the_bots_emergency_stop_at_the_calls_leverage(lev):
    assert arena.guard_pct(lev) == pytest.approx(runner.invo_emergency_stop(min(lev, 10)))


def test_a_copy_open_at_the_end_counts_at_that_hours_price_and_never_sees_later_prices():
    c = mk(0, t=NOW - 3 * DAY, close=200.0, hours=24 * 10)            # the trader closes AFTER the window: +100%
    r = arena.simulate(copies([c])["a"], NOW - 4 * DAY, NOW - 2 * DAY, P, prices(flat(105.0)))
    assert r.open_left == 1 and r.pnl == pytest.approx(30 * 0.05)       # marked at 105, not closed at 200


def test_calls_on_coins_hyperliquid_does_not_list_are_dropped_and_k_coins_are_rescaled():
    got = arena.prepare({"a": ({}, [mk(0, ticker="XYZ"), mk(1, ticker="PEPE", entry=0.00001, close=0.000011)])},
                        lambda t: ("kPEPE", 1000.0) if t == "PEPE" else None)
    assert [(c.coin, c.entry) for c in got["a"]] == [("kPEPE", pytest.approx(0.01))]


# ---- recipes and backtest --------------------------------------------------------------------------------------------
def steady(name, win, n=40, start=NOW - 120 * DAY, every=2):
    """A trader posting a call every `every` days that ends +`win` (or `-win` when win < 0)."""
    return [mk(i, trader=name, t=start + i * every * DAY, close=100 * (1 + win)) for i in range(n)
            if start + i * every * DAY < NOW - DAY]


def test_recipes_pick_from_the_past_only():
    # "late" lost before day -30 and wins after: a recipe picking at day -30 must not pick it
    late = [mk(i, trader="late", t=NOW - 60 * DAY + i * DAY, close=95.0) for i in range(30)]
    late += [mk(100 + i, trader="late", t=NOW - 29 * DAY + i * DAY, close=130.0) for i in range(25)]
    cs = copies(steady("good", 0.02, start=NOW - 70 * DAY) + late)
    pk = arena.Picker(cs, P, prices())
    assert pk.picks(arena.Strategy("top_return", 10, 30), NOW - 30 * DAY) == ["good"]
    assert "late" in pk.picks(arena.Strategy("top_return", 10, 14), NOW)


def test_losing_portfolios_are_not_copied():
    good = [mk(i, trader="x", t=NOW - 50 * DAY + i * DAY, portfolio="win") for i in range(20)]
    bad = [mk(100 + i, trader="x", t=NOW - 50 * DAY + i * DAY, close=90.0, portfolio="lose") for i in range(20)]
    cs = copies(good + bad)
    assert arena.kept_portfolios(cs["x"], NOW - 60 * DAY, NOW, P, prices()) == {"win"}


def test_backtest_ranks_recipes_and_beats_the_random_baseline_on_a_clear_case():
    calls = []
    for i in range(6):
        calls += steady(f"win{i}", 0.03 + 0.005 * i)
    for i in range(8):
        calls += steady(f"lose{i}", -0.02)
    bt = arena.backtest(copies(calls), P, prices(), NOW, follows=["lose0", "lose1"])
    keys = {r["key"] for r in bt["ranking"]}
    assert {"random", "follows", "hindsight"} <= keys and len(bt["starts"]) >= 5
    best = bt["ranking"][0]
    base = {r["key"]: r for r in bt["ranking"]}
    assert best["recipe"] not in arena.BASELINES and best["mean"] > base["random"]["mean"] > base["follows"]["mean"]
    assert all(set(w["picks"]) <= {f"win{i}" for i in range(6)} for w in best["windows"] if w["picks"])
    assert base["hindsight"]["mean"] >= best["mean"]            # nobody beats hindsight
    assert all(len(w["picks"]) <= arena.MAX_TRADERS for r in bt["ranking"] for w in r["windows"])


# ---- seasons --------------------------------------------------------------------------------------------------------
def test_a_season_freezes_its_picks_and_only_counts_calls_after_its_start():
    cs = copies(steady("a", 0.05, start=NOW - 60 * DAY, n=200, every=1))
    bt = arena.backtest(cs, P, prices(), NOW - 7 * DAY)
    s = arena.new_season(cs, P, prices(), bt, NOW - 7 * DAY, follows=["a"], seed=1)
    assert s["end"] - s["start"] == 14 * DAY and all(w["picks"] == ["a"] for w in s["wallets"])
    s = arena.score_season(s, cs, P, prices(), NOW)
    top = s["standings"][0]
    assert top["trades"] == 6 and top["equity"] > 300 and not s["done"]    # days -7..-1, not the earlier ones
    assert arena.score_season(s, cs, P, prices(), NOW + 8 * DAY)["done"]


def test_run_ends_a_season_keeps_it_and_starts_the_next(tmp_path):
    for n in ("a", "b"):
        arena.save_calls(tmp_path, n, [(Portfolio("p1", "Main", True, "paper", 0, (), 80, 10),
                                        steady(n, 0.03, n=150, start=NOW - 140 * DAY, every=1), [])], NOW)
    out = tmp_path / "arena"
    r1 = arena.run(tmp_path, out, lambda t: t, flat(), P, NOW - 20 * DAY)
    assert r1["started"] and not r1["ended"]
    r2 = arena.run(tmp_path, out, lambda t: t, flat(), P, NOW)
    assert r2["ended"] and r2["started"] and r2["past"][0]["start"] == r1["season"]["start"]
    assert (out / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert json.loads((out / "arena.json").read_text(encoding="utf-8"))["season"]["start"] == NOW


def test_saved_calls_merge_and_keep_history(tmp_path):
    port = Portfolio("p1", "Main", True, "paper", 1, ("ETH",), 80, 10)
    o = mk(0, is_open=True)
    arena.save_calls(tmp_path, "Al", [(port, [mk(1)], [o])], NOW)
    arena.save_calls(tmp_path, "Al", [(port, [mk(0)], [])], NOW + 1)       # 0 closed since, 1 no longer listed
    ports, calls = arena.load_calls(tmp_path)["Al"]
    assert ports["p1"] == port
    assert {c.id: c.is_open for c in calls} == {"a0": False, "a1": False}


def test_the_card_shows_the_season_and_the_backtest(tmp_path):
    assert "no run yet" in arena.card(None, NOW)
    for n in ("a", "b"):
        arena.save_calls(tmp_path, n, [(Portfolio("p1", "Main", True, "paper", 0, (), 80, 10),
                                        steady(n, 0.03, n=150, start=NOW - 140 * DAY, every=1), [])], NOW)
    res = arena.run(tmp_path, tmp_path / "arena", lambda t: t, flat(), P, NOW)
    text = arena.card(res, NOW + 3 * DAY, report="data/arena/index.html")
    assert "Live season" in text and "day 3/14" in text and "Backtest" in text and "random traders" in text
    assert "🥇" in text and "data/arena/index.html" in text


# ---- the bot end to end (FakeHL + FakeDiscord + FakeInvo) ------------------------------------------------------------
from tests.test_e2e import env, start_bot, stop_bot, wait_for  # noqa: E402,F401 (env is a fixture)
from tests.test_invo_search import add_trader, search_env  # noqa: E402,F401 (search_env is a fixture)


def test_the_arena_runs_after_the_invo_search_and_answers_slash_arena(search_env, monkeypatch):
    from copybot import invo_scorer
    hl, dc, data, cdir, f, tok = search_env
    for n in ("steady", "steady2", "steady3"):
        add_trader(f, n, good=True)
    add_trader(f, "coinflip", good=False)
    f.ranked = ["steady", "steady2", "steady3", "coinflip"]
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    monkeypatch.setattr(invo_scorer, "REQ_GAP_S", 0.0)
    bot, th = start_bot((hl, dc, data, cdir))
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        assert wait_for(lambda: "New arena season" in text(), timeout=120)
        saved = arena.load_calls(data / "cache")
        assert {"steady", "coinflip"} <= set(saved) and len(saved["steady"][1]) == 45     # no extra Invo reads
        assert (data / "arena" / "index.html").exists() and bot.arena_res["season"]["wallets"]
        n = len(dc.sent)
        dc.say("/arena")
        assert wait_for(lambda: any("Invo arena" in m["text"] and "Backtest" in m["text"] for m in dc.sent[n:]))
        assert "coinflip" not in bot.arena_res["season"]["standings"][0]["picks"]
        assert "REFRESH" not in text()
    finally:
        stop_bot(bot, th)
