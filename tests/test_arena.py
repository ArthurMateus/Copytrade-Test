"""The Invo arena (copybot/arena.py): replay of a wallet copying calls in every size x stop with the delay and the costs
kept apart, recipes picking only from the past, the walk-forward backtest and its honest check, the live groups (their
own wallets in the bot), the saved calls and the /arena card."""
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
    assert bt["variants"] == 18 and len(bt["best"]) == 3 and len(bt["worst"]) == 3
    assert set(bt["by_stop"]) == {"3% stop", "8% stop", "trader's exit"} and len(bt["by_size"]) == 6
    # the arena's own choice: chosen only from windows that had ENDED before the window it is measured on
    ch = base["chooser"]
    assert ch["windows"]
    for w in ch["windows"]:
        assert sum(1 for t in bt["starts"] if t + arena.WINDOW_DAYS * DAY <= w["start"]) >= arena.CHOOSER_MIN
        assert len(w["picks"]) == arena.LIVE_GROUPS and all(k in {r["key"] for r in bt["ranking"]} for k in w["picks"])


# ---- every size x stop, the delay, the costs --------------------------------------------------------------------------
def test_every_size_and_stop_of_the_invo_book_is_a_variant_main_first():
    vs = P.variants()
    assert len(vs) == 18 and vs[0] == P.main == arena.Variant(None, 0.0)
    assert {v.stop_label for v in vs} == {"3% stop", "8% stop", "trader's exit"}
    assert {v.size_label for v in vs} == {"trader size", "1% risk", "2% risk", "5% risk", "10% risk", "20% risk"}


def test_a_risk_wallet_loses_its_risk_at_its_own_stop():
    def crash(coin, s, e):            # one hour 40% down in the middle of the call
        mid = NOW - 10 * DAY + 5 * HOUR
        return [Candle(t, 100, 100, 60 if t == mid else 100, 100, 1) for t in range(s // HOUR * HOUR, e, HOUR)]
    cs = copies([mk(0, close=120.0)])["a"]
    r = arena.simulate(cs, NOW - 20 * DAY, NOW, P, prices(crash), arena.Variant(2.0, 3.0))
    assert r.pnl == pytest.approx(-300 * 0.02)                     # 200$ copy, 3% stop: 2% of the wallet
    r = arena.simulate(cs, NOW - 20 * DAY, NOW, P, prices(crash), arena.Variant(2.0, 8.0))
    assert r.pnl == pytest.approx(-300 * 0.02)                     # 75$ copy, 8% stop
    r = arena.simulate(cs, NOW - 20 * DAY, NOW, P, prices(), arena.Variant(2.0, 0.0))
    assert r.pnl == pytest.approx(20 * 0.2)                        # 20$ copy (2% at the 30% guard), +20%
    # the same call in the trader-size wallet with our 3% stop: 30$ copy, -3%
    r = arena.simulate(cs, NOW - 20 * DAY, NOW, P, prices(crash), arena.Variant(None, 3.0))
    assert r.pnl == pytest.approx(-30 * 0.03)


def test_the_delay_makes_entry_and_the_traders_exit_worse_and_is_counted_apart():
    p = arena.Params(cost_bps=0.0, fee_bps=0.0, delay_bps=50.0)
    r = arena.simulate(copies([mk(0)])["a"], NOW - 20 * DAY, NOW, p, prices())
    ours, out = 100 * 1.005, 110 * 0.995                            # in 0.5% higher, out 0.5% lower
    assert r.pnl == pytest.approx(30 * (out / ours - 1))
    assert r.delay == pytest.approx(30 * 0.10 - r.pnl)
    short = mk(1, long=False, close=90.0)
    r = arena.simulate(copies([short])["a"], NOW - 20 * DAY, NOW, p, prices())
    assert r.pnl == pytest.approx(-30 * (90 * 1.005 / (100 * 0.995) - 1))
    # measured per coin where there are enough copies
    p = arena.Params(cost_bps=0.0, fee_bps=0.0, delay_bps=50.0, delay_by_coin=(("ETH", 0.0),))
    assert arena.simulate(copies([mk(0)])["a"], NOW - 20 * DAY, NOW, p, prices()).delay == pytest.approx(0.0)


def test_fees_slippage_and_funding_are_kept_apart():
    fund = lambda coin, s, e: [(t, 0.0001) for t in range(s // HOUR * HOUR, e, HOUR)]
    p = arena.Params(cost_bps=13.0, fee_bps=4.5)
    r = arena.simulate(copies([mk(0, hours=10)])["a"], NOW - 20 * DAY, NOW, p,
                       arena.Prices(flat(), fund, NOW - 400 * DAY, NOW + DAY))
    assert r.fees == pytest.approx(30 * 9 / 1e4) and r.slippage == pytest.approx(30 * 4 / 1e4)
    assert r.funding == pytest.approx(30 * 0.0001 * 10, rel=0.15)        # ~10 hours of 0.01% paid by a long
    assert r.pnl == pytest.approx(3.0 - r.fees - r.slippage - r.funding)


def test_delay_cost_is_measured_on_real_copies_with_a_margin():
    d = arena.delay_cost([("ETH", 0.2)] * 5, default_bps=10.0, margin_bps=5.0)
    assert d["default"] and d["bps"] == 15.0 and d["by_coin"] == {}       # too few: the configured default
    gaps = [("ETH", 0.1)] * 20 + [("BTC", -0.05)] * 10
    d = arena.delay_cost(gaps, default_bps=10.0, margin_bps=5.0)
    assert not d["default"] and d["measured"] == 30
    assert d["bps"] == pytest.approx((20 * 10 - 10 * 5) / 30 + 5, abs=0.01)
    assert d["by_coin"] == {"ETH": 15.0}                                   # BTC: not enough copies yet
    assert arena.delay_cost([("ETH", -0.3)] * 25, 10.0, 5.0)["bps"] == 5.0   # better than them: only the margin


# ---- live groups --------------------------------------------------------------------------------------------------
def test_best_groups_pick_now_are_distinct_and_carry_their_portfolios():
    calls = []
    for i in range(6):
        calls += steady(f"win{i}", 0.03 + 0.005 * i, n=60)          # active until yesterday
    cs = copies(calls)
    pk = arena.Picker(cs, P, prices())
    bt = arena.backtest(cs, P, prices(), NOW, picker=pk)
    gs = arena.best_groups(bt, pk, NOW)
    assert 1 <= len(gs) <= arena.LIVE_GROUPS
    assert len({tuple(sorted(g["picks"])) for g in gs}) == len(gs)
    assert all(g["portfolios"][n] == ["p1"] for g in gs for n in g["picks"])
    live = arena.new_live(gs, NOW)
    assert [g["slot"] for g in live["groups"]] == list(range(1, len(gs) + 1))
    assert arena.same_groups(live, gs) and not arena.same_groups(live, [{**gs[0], "picks": ["x"]}])


def test_run_saves_the_result_and_replays_the_live_groups_since_they_started(tmp_path):
    for n in ("a", "b"):
        arena.save_calls(tmp_path, n, [(Portfolio("p1", "Main", True, "paper", 0, (), 80, 10),
                                        steady(n, 0.03, n=150, start=NOW - 140 * DAY, every=1), [])], NOW)
    out = tmp_path / "arena"
    r1 = arena.run(tmp_path, out, lambda t: t, flat(), P, NOW - 20 * DAY)
    assert r1["next"] and not r1["live_replay"] and arena.load_result(out)["ts"] == NOW - 20 * DAY
    live = arena.new_live(r1["next"], NOW - 20 * DAY)
    arena.save_live(out, live)
    assert arena.load_live(out) == live
    r2 = arena.run(tmp_path, out, lambda t: t, flat(), P, NOW, live=live)
    rep = r2["live_replay"][0]
    assert rep["slot"] == 1 and 0 < rep["trades"] <= 2 * 20 and rep["pnl"] > 0   # only calls since it started
    assert json.loads((out / "arena.json").read_text(encoding="utf-8"))["v"] == arena.VERSION


def test_the_card_shows_the_live_wallets_and_the_backtest(tmp_path):
    assert "no run yet" in arena.card(None, None, NOW)
    for n in ("a", "b"):
        arena.save_calls(tmp_path, n, [(Portfolio("p1", "Main", True, "paper", 0, (), 80, 10),
                                        steady(n, 0.03, n=150, start=NOW - 140 * DAY, every=1), [])], NOW)
    res = arena.run(tmp_path, tmp_path / "arena", lambda t: t, flat(), P, NOW)
    assert "No live groups yet" in arena.card(res, None, NOW)
    live = arena.new_live(res["next"], NOW - 3 * DAY)
    ws = [{"slot": 1, "size": v.size_label, "stop": v.stop_label, "equity0": 300.0, "pnl": float(i - 9), "trades": 2,
           "wins": 1, "open": 0, "fees": 0.1, "funding": 0.02} for i, v in enumerate(P.variants())]
    text = arena.card(res, {**live, "wallets": ws}, NOW)
    assert "Live</b> since" in text and "(day 3)" in text and "🏆 <b>Best</b>" in text and "🔻 <b>Worst</b>" in text
    assert "+8.00$" in text and "−9.00$" in text and "By stop" in text and "By group" in text
    assert "Best combinations" in text and "random traders" in text
    assert "delay cost" in text and "fees" in text and len(text) < 4096


# ---- the bot end to end (FakeHL + FakeDiscord + FakeInvo) ------------------------------------------------------------
from tests.test_e2e import env, start_bot, stop_bot, wait_for  # noqa: E402,F401 (env is a fixture)
from tests.test_invo_search import add_trader, search_env  # noqa: E402,F401 (search_env is a fixture)


def test_the_arena_puts_its_best_groups_live_in_their_own_wallets(search_env, monkeypatch):
    """After the Invo search: the backtest, then its best groups copy live in 18 wallets each (owner request
    2026-10-20); a group trader's new call is copied there and NOT in the Invo book's wallets (it does not follow it);
    /arena shows them; the owner's switch archives the old wallets and starts the new groups at 300$."""
    from copybot import invo_scorer
    from copybot.chat import Command
    hl, dc, data, cdir, f, tok = search_env
    pids = {n: add_trader(f, n, good=True) for n in ("steady", "steady2", "steady3")}
    add_trader(f, "coinflip", good=False)
    f.ranked = ["steady", "steady2", "steady3", "coinflip"]
    monkeypatch.setenv("INVO_TOKEN_FILE", str(tok))
    monkeypatch.setattr(invo_scorer, "REQ_GAP_S", 0.0)
    bot, th = start_bot((hl, dc, data, cdir))
    text = lambda: " ".join(m["text"] for m in dc.sent + dc.edits)
    try:
        assert wait_for(lambda: "Arena live" in text(), timeout=120)
        saved = arena.load_calls(data / "cache")
        assert {"steady", "coinflip"} <= set(saved) and len(saved["steady"][1]) == 45     # no extra Invo reads
        g = bot.arena_groups[0]
        assert len(g["wallets"]) == 18 and (data / "arena" / "wallets" / "g1_invo_calls").is_dir()
        assert (data / "arena" / "live.json").exists()
        picks = g["group"]["picks"]
        assert "coinflip" not in picks and all("invo:" + n in g["primary"].st.followed for n in picks)
        assert not bot.invo.st.followed                                   # the Invo book follows nobody
        who = picks[0]
        assert wait_for(lambda: who in bot.invo_watch.known, timeout=30)   # watched (baseline read)
        f.open_call(pids[who][0] if isinstance(pids[who], list) else pids[who], "ETH", leverage=2, size=0.1)
        assert wait_for(lambda: all("ETH" in w.st.positions for w in g["wallets"]), timeout=30), \
            [(w.name, list(w.st.positions)) for w in g["wallets"]]
        assert not any(w.st.positions for w in bot.invo_wallets())
        link = next(iter(g["primary"].st.invo_links.values()))
        assert link["coin"] == "ETH" and "gap" in link
        n = len(dc.sent)
        dc.say("/arena")
        assert wait_for(lambda: any("Invo arena" in m["text"] and "Backtest" in m["text"] and "G1 · " in m["text"]
                                    for m in dc.sent[n:]))
        # the owner's switch (the button under /arena): open copies closed, old wallets archived, new ones at 300$
        other = [{**bot.arena_live["groups"][0], "picks": ["coinflip"], "portfolios": {"coinflip": []}}]
        bot.asks["t1"] = {"kind": "arena_switch", "groups": other, "text": "switch?"}
        bot.q.put(("cmd", Command("/button", "ask:t1:yes")))
        assert wait_for(lambda: bot.arena_live["groups"][0]["picks"] == ["coinflip"], timeout=30)
        assert wait_for(lambda: list((data / "archive").glob("arena-*/wallets/g1_invo_calls")))
        assert wait_for(lambda: len(bot.arena_wallets()) == 18)
        assert not any(w.st.positions for w in bot.arena_wallets())
        assert all(w.st.equity() == pytest.approx(300.0) for w in bot.arena_wallets())
        assert "REFRESH" not in text()
        stop_bot(bot, th)
        bot.lock.f.close()                               # as the process exit would
        bot, th = start_bot((hl, dc, data, cdir))       # a restart rebuilds the live groups' wallets from live.json
        assert len(bot.arena_wallets()) == 18
        assert all("invo:coinflip" in w.st.followed for w in bot.arena_wallets()) and "coinflip" in bot.invo_watched
    finally:
        stop_bot(bot, th)
