import dataclasses
import time

import pytest

from copybot import hl, scoring
from copybot.hl import LbRow, Window
from tests.fakes import fixture, make_fill
from tests.gen import DAY, trader

NOW = (int(time.time() * 1000) // 3_600_000) * 3_600_000


def row(av=100_000, day=(1000, 0, 200_000), week=(3000, 0, 1_000_000), month=(10_000, 0, 4_000_000),
        all_time=(60_000, 0, 20_000_000), addr="0xabc"):
    return LbRow(addr, av, Window(*day), Window(*week), Window(*month), Window(*all_time))


def parse(raw):
    return hl.parse_fills(raw)


def cand(cs):
    return {c: hl.parse_candles(v) for c, v in cs.items()}


# ---- prescreen ------------------------------------------------------------------------------------
def test_prescreen_accepts_good_row():
    p = scoring.prescreen(row())
    assert p.ok and p.edge_bps == pytest.approx(min(25.0, 50_000 / 16_000_000 * 1e4))


@pytest.mark.parametrize("kw,reason", [
    ({"av": 9_000}, "account_value<10k"),
    ({"week": (0, 0, 0)}, "inactive_this_week"),
    ({"month": (10_000, 0, 100_000)}, "month_volume<2x"),
    ({"month": (10_000, 0, 60_000_000)}, "month_volume>500x"),
    ({"day": (0, 0, 6_000_000)}, "day_volume>50x"),
    ({"month": (-1, 0, 4_000_000)}, "month_pnl<=0"),
    ({"all_time": (5_000, 0, 20_000_000)}, "pnl_before_month<=0"),
    ({"month": (1_000, 0, 4_000_000)}, "edge<10bps"),
    ({"month": (150_000, 0, 40_000_000), "all_time": (900_000, 0, 100_000_000)}, "month_pnl>100%_of_account"),
])
def test_prescreen_rejections(kw, reason):
    assert scoring.prescreen(row(**kw)).reason == reason


def test_prescreen_rank_caps_edge_then_all_time_pnl():
    a = row(addr="0xa", month=(30_000, 0, 4_000_000), all_time=(330_000, 0, 24_000_000))   # edge 75/150 -> 50
    b = row(addr="0xb", month=(40_000, 0, 4_000_000), all_time=(540_000, 0, 24_000_000))   # edge 100/250 -> 50
    c = row(addr="0xc")
    ranked = [r.address for r, _ in scoring.rank_prescreened([c, a, b])]
    assert ranked == ["0xb", "0xa", "0xc"]


def test_prescreen_on_real_leaderboard_is_deterministic():
    rows = hl.parse_leaderboard(fixture("leaderboard_sample.json"))
    r1 = [r.address for r, _ in scoring.rank_prescreened(rows)]
    r2 = [r.address for r, _ in scoring.rank_prescreened(list(reversed(rows)))]
    assert r1 == r2 and 0 < len(r1) < len(rows)


# ---- first page screen ----------------------------------------------------------------------------
def test_screen_passes_a_good_trader():
    raw, _ = trader(NOW, trips=400, days=180)
    s = scoring.fill_screen(parse(raw)[:2000], NOW, 100.0)
    assert s.ok, s


def test_screen_rejects_real_fast_wallet():
    s = scoring.fill_screen(parse(fixture("user_fills_full_page.json")["fills"]), NOW, 100.0)
    assert s.reason == "too_fast"


def test_screen_rejects_real_spot_wallet():
    s = scoring.fill_screen(parse(fixture("user_fills_by_time.json")["fills"]), NOW, 100.0)
    assert s.reason == "core_perp_share<50%"


def test_screen_hft_full_page_within_a_day():
    fl = [make_fill("BTC", 100, 1, "B" if i % 2 == 0 else "A", 0.0 if i % 2 == 0 else 1.0, t=NOW - 3_600_000 + i * 1000)
          for i in range(2000)]
    assert scoring.fill_screen(parse(fl), NOW, 100.0).reason == "high_frequency"


def test_screen_empty_maker_history_trips_hold():
    assert scoring.fill_screen([], NOW, 100).reason == "no_fills"
    raw, _ = trader(NOW, maker=True)
    assert scoring.fill_screen(parse(raw), NOW, 100).reason == "maker_share>70%"
    raw, _ = trader(NOW, days=50, trips=300)
    assert scoring.fill_screen(parse(raw), NOW, 100).reason == "history<60d"
    raw, _ = trader(NOW, trips=20)
    assert scoring.fill_screen(parse(raw), NOW, 100).reason == "round_trips<30"
    raw, _ = trader(NOW, trips=100)      # under the old 150 floor: now passes, fewer trips only cost points
    assert scoring.fill_screen(parse(raw), NOW, 100).ok


def test_screen_median_hold():
    fl, t = [], NOW - 100 * DAY
    for i in range(200):
        fl.append(make_fill("BTC", 100, 1, "B", 0.0, t=t, oid=2 * i))
        fl.append(make_fill("BTC", 100, 1, "A", 1.0, t=t + 5 * 60_000, oid=2 * i + 1))
        t += 6 * 3_600_000
    assert scoring.fill_screen(parse(fl), NOW, 100).reason == "median_hold<15min"


def test_screen_too_small_to_copy():
    """A leader that trims out in many 5% clips: our proportional reduces would be < $10."""
    fl, t, oid = [], NOW - 100 * DAY, 0
    for i in range(160):
        oid += 1
        fl.append(make_fill("ETH", 3000, 10, "B", 0.0, t=t, oid=oid))
        pos = 10.0
        for j in range(20):
            oid += 1
            fl.append(make_fill("ETH", 3000, 0.5, "A", pos, t=t + 3_600_000 + j * 1000, oid=oid))
            pos -= 0.5
        t += 12 * 3_600_000
    s = scoring.fill_screen(parse(fl), NOW, 100)
    assert s.reason == "too_small_to_copy", s
    assert s.metrics["copyable"] == pytest.approx(2 / 21, abs=0.01)


# ---- full score -----------------------------------------------------------------------------------
def test_full_score_good_trader_is_eligible():
    raw, cs = trader(NOW, trips=500, win=0.75)
    s = scoring.full_score("0xgood", parse(raw), cand(cs), 200_000, NOW)
    assert s.eligible, s.reasons
    assert s.trades >= 150 and s.win_rate > 0.55 and s.profit_factor > 1 and s.positive_blocks >= 4
    assert s.copy_edge_bps > 0 and 0 < s.shrink < 1
    assert 1 <= s.score <= 100 and s.score == pytest.approx(sum(s.points.values()), abs=0.01)
    assert s.points["trades"] == scoring.WEIGHTS["trades"] and s.points["consistency"] >= 10


def test_full_score_losing_trader_is_not():
    raw, cs = trader(NOW, trips=500, win=0.35, seed=3)
    s = scoring.full_score("0xbad", parse(raw), cand(cs), 200_000, NOW)
    assert not s.eligible and "pnl<=0" in s.reasons and s.score == 0


def test_full_score_concentration():
    raw, cs = trader(NOW, trips=300, win=0.55, seed=5, big_winner=400)
    s = scoring.full_score("0xluck", parse(raw), cand(cs), 200_000, NOW)
    # concentration is no longer a reject: it costs points instead
    assert "one_trade>25%_of_pnl" not in s.reasons
    assert s.points["concentration"] < scoring.WEIGHTS["concentration"] or s.concentration <= 0.25


def test_full_score_short_history_costs_consistency_points():
    raw, cs = trader(NOW, trips=400, win=0.65, first_day=120)   # active only the last 60 days
    s = scoring.full_score("0xnew", parse(raw), cand(cs), 200_000, NOW)
    raw, cs = trader(NOW, trips=400, win=0.65)
    full = scoring.full_score("0xold", parse(raw), cand(cs), 200_000, NOW)
    assert s.eligible and s.positive_blocks <= 2
    assert s.points["consistency"] <= scoring.WEIGHTS["consistency"] * 2 / 6
    assert s.points["consistency"] < full.points["consistency"]


def test_drawdown_uses_mark_to_market():
    curve = [100, 120, 90, 110]
    mdd, cur = scoring.drawdowns(curve)
    assert mdd == pytest.approx(0.25) and cur == pytest.approx(10 / 120)


def test_copy_return_hits_our_stop():
    t = scoring.Trip("BTC", 1, 0, 3 * 3_600_000, 100.0, 1000.0, 50.0, 0.0)   # leader +5% at the end
    cs = [hl.Candle(0, 100, 101, 99, 100, 1), hl.Candle(3_600_000, 100, 100, 96.5, 97, 1),
          hl.Candle(7_200_000, 97, 106, 97, 105, 1)]
    assert scoring.copy_return(t, cs, 0.03) == -0.03
    assert scoring.copy_return(t, cs, 0.05) == pytest.approx(0.05)


def test_ranking_is_deterministic():
    a = scoring.Score("0xa", True, score=1.0)
    b = scoring.Score("0xb", True, score=1.0)
    c = scoring.Score("0xc", True, score=2.0)
    d = scoring.Score("0xd", False, score=9.0)
    assert scoring.ranking([d, b, a, c]) == ["0xc", "0xa", "0xb"]


def test_score_is_reproducible():
    raw, cs = trader(NOW, trips=300, seed=9)
    s1 = scoring.full_score("0x9", parse(raw), cand(cs), 100_000, NOW)
    s2 = scoring.full_score("0x9", parse(list(reversed(raw))), cand(cs), 100_000, NOW)
    assert dataclasses.asdict(s1) == dataclasses.asdict(s2)


# ---- 0-100 points and main coins ------------------------------------------------------------------
def test_weights_add_up_to_100_and_a_perfect_wallet_scores_100():
    assert sum(scoring.WEIGHTS.values()) == 100
    p = scoring.ScoreParams()
    best = scoring.Score("0xbest", True, trades=500, win_rate=0.9, profit_factor=5, positive_blocks=6,
                         max_dd=0.0, cur_dd=0.0, concentration=0.05, copy_edge_bps=80)
    assert sum(scoring.points(best, p).values()) == 100
    worst = scoring.Score("0xworst", True, trades=30, win_rate=0.3, profit_factor=1.0, positive_blocks=0,
                          max_dd=0.9, cur_dd=0.9, concentration=1.0, copy_edge_bps=0)
    assert sum(scoring.points(worst, p).values()) == 0


def test_better_trader_scores_higher():
    raw, cs = trader(NOW, trips=500, win=0.85)
    good = scoring.full_score("0xa", parse(raw), cand(cs), 200_000, NOW)
    raw, cs = trader(NOW, trips=500, win=0.65)
    meh = scoring.full_score("0xb", parse(raw), cand(cs), 200_000, NOW)
    assert good.eligible and meh.eligible and good.score > meh.score
    assert scoring.ranking([meh, good]) == ["0xa", "0xb"]


def test_only_main_coins_are_scored():
    raw, cs = trader(NOW, trips=450, win=0.7, coins=("BTC", "ETH", "SOL"))
    s = scoring.full_score("0xa", parse(raw), cand(cs), 200_000, NOW,
                           scoring.ScoreParams(coins=("BTC",), alt_min_coins=1000))   # alts unlock disabled
    trips_btc = [t for t in scoring.round_trips(parse(raw)) if t.coin == "BTC"]
    assert s.trades == len(trips_btc) and 0 < s.trades < 200


def test_screen_judges_only_main_coin_trades():
    raw, _ = trader(NOW, trips=400, coins=("HYPE", "BTC"), size_usd=20_000)
    fl = parse(raw)
    assert scoring.fill_screen(fl, NOW, 100, coins=("ETH",)).reason == "no_main_coin_trades"
    # half its trading is in an off-list coin: that is ignored, its ~200 BTC round trips are what counts
    s = scoring.fill_screen(fl, NOW, 100, coins=("BTC",))
    assert s.ok and s.metrics["main_share"] == pytest.approx(0.5, abs=0.1)
    assert s.metrics["round_trips"] == len([t for t in scoring.round_trips(fl) if t.coin == "BTC"])


def test_ranking_ignores_scores_of_an_older_version():
    old = scoring.Score("0xold", True, score=99.0, v=scoring.VERSION - 1)
    new = scoring.Score("0xnew", True, score=10.0)
    assert scoring.ranking([old, new]) == ["0xnew"]


def test_diversification():
    T = lambda coin, net: scoring.Trip(coin, 1, 0, 1, 1.0, 1.0, net, 0.0)
    ok, top = scoring.diversification([T("BTC", 30), T("PUMP", 40), T("WIF", 30), T("ETH", -10)], 3, 0.5)
    assert ok and top["PUMP"] == 40
    assert not scoring.diversification([T("BTC", 10), T("PUMP", 80), T("WIF", 10)], 3, 0.5)[0]   # one coin carries it
    assert not scoring.diversification([T("BTC", 30), T("PUMP", 40)], 3, 0.5)[0]                 # too few coins


def test_diversified_wallet_is_scored_on_all_its_coins():
    raw, cs = trader(NOW, trips=450, win=0.8, coins=("BTC", "DOGE", "SOL"))
    p = scoring.ScoreParams(coins=("BTC",))
    s = scoring.full_score("0xa", parse(raw), cand(cs), 200_000, NOW, p)
    assert s.diversified and set(s.coin_pnl) == {"BTC", "DOGE", "SOL"}
    assert s.trades == len(scoring.round_trips(parse(raw)))
    narrow = scoring.full_score("0xa", parse(raw), cand(cs), 200_000, NOW,
                                scoring.ScoreParams(coins=("BTC",), alt_min_coins=1000))
    assert not narrow.diversified and narrow.trades < s.trades


def test_screen_unlocks_alts_for_a_diversified_page():
    raw, _ = trader(NOW, trips=400, win=0.8, coins=("HYPE", "DOGE", "SOL"))
    s = scoring.fill_screen(parse(raw), NOW, 100, coins=("ETH",))
    assert s.ok and s.metrics["diversified"]
    assert scoring.fill_screen(parse(raw), NOW, 100, coins=("ETH",), alt_min_coins=1000).reason == "no_main_coin_trades"


def test_win_rate_and_score_floors():
    raw, cs = trader(NOW, trips=500, win=0.65)
    fl, c = parse(raw), cand(cs)
    base = scoring.full_score("0xa", fl, c, 200_000, NOW)
    assert base.eligible and base.win_rate < 0.70
    s = scoring.full_score("0xa", fl, c, 200_000, NOW, scoring.ScoreParams(min_win_rate=0.70))
    assert not s.eligible and s.reasons == ["win_rate<70%"] and s.score == 0
    s = scoring.full_score("0xa", fl, c, 200_000, NOW, scoring.ScoreParams(min_score=base.score + 1))
    assert not s.eligible and s.reasons == [f"score<{base.score + 1:g}"]


def test_profit_factor_floor():
    raw, cs = trader(NOW, trips=500, win=0.75)
    fl, c = parse(raw), cand(cs)
    base = scoring.full_score("0xa", fl, c, 200_000, NOW)
    assert base.eligible and base.rules == scoring.ScoreParams().rules()
    s = scoring.full_score("0xa", fl, c, 200_000, NOW, scoring.ScoreParams(min_profit_factor=base.profit_factor + 0.5))
    assert not s.eligible and s.reasons == [f"profit_factor<{base.profit_factor + 0.5:g}"]


def test_max_drawdown_cap():
    raw, cs = trader(NOW, trips=500, win=0.75)
    fl, c = parse(raw), cand(cs)
    base = scoring.full_score("0xa", fl, c, 200_000, NOW)
    assert base.eligible and 0 < base.max_dd
    s = scoring.full_score("0xa", fl, c, 200_000, NOW, scoring.ScoreParams(max_dd_cap=base.max_dd / 2))
    assert not s.eligible and s.reasons == [f"max_drawdown>{base.max_dd / 2 * 100:.0f}%"]
    assert "max_dd_cap" in s.rules


def test_open_losers_count_as_lost_trades():
    raw, cs = trader(NOW, trips=500, win=0.75)
    fl, c = parse(raw), cand(cs)
    base = scoring.full_score("0xa", fl, c, 200_000, NOW)
    live = hl.Account(200_000, (hl.OpenPos("BTC", 1.0, 100_000, -2_000), hl.OpenPos("ETH", -5.0, 15_000, 300),
                                hl.OpenPos("WIF", 100.0, 5_000, -500)))   # WIF: not a scored coin
    main = scoring.ScoreParams(coins=("BTC", "ETH", "SOL"), alt_min_coins=1000)   # main coins only: WIF not scored
    s = scoring.full_score("0xa", fl, c, 200_000, NOW, main, live)
    wins = round(base.win_rate * base.trades)
    assert s.live and s.open_losers == 1 and s.trades == base.trades
    assert s.win_rate == pytest.approx(wins / (base.trades + 1)) and s.profit_factor < base.profit_factor
    assert s.open_loss_pct == pytest.approx(2_500 / 200_000)   # the account-level figure counts every coin
    assert s.eligible


def test_open_loss_cap_and_empty_account():
    raw, cs = trader(NOW, trips=500, win=0.75)
    fl, c = parse(raw), cand(cs)
    # the losers sit in a coin it is not scored on, so only the account-level cap can reject it
    p = scoring.ScoreParams(max_open_loss=0.15, coins=("BTC", "ETH", "SOL"), alt_min_coins=1000)
    assert "max_open_loss" in p.rules()
    bag = hl.Account(50_000, (hl.OpenPos("WIF", 1.0, 90_000, -10_000),))   # 10k of max(50k, 200k) = 5%: ok
    assert scoring.full_score("0xa", fl, c, 200_000, NOW, p, bag).eligible
    bag = hl.Account(50_000, (hl.OpenPos("WIF", 1.0, 90_000, -40_000),))   # 20% of the account
    s = scoring.full_score("0xa", fl, c, 200_000, NOW, p, bag)
    assert not s.eligible and s.reasons == ["open_losses>15%"] and s.score == 0
    s = scoring.full_score("0xa", fl, c, 200_000, NOW, p, hl.Account(0.0, ()))   # moved its money out
    assert not s.eligible and s.reasons == ["account_empty"]
    assert scoring.full_score("0xa", fl, c, 200_000, NOW, p, hl.Account(50.0, (hl.OpenPos("BTC", 1.0, 900, 5),))).eligible


# ---- "is it losing right now?" gates ----------------------------------------------------------------
STRICT = dict(max_loss_7d=0.03, max_loss_24h=0.015, max_loss_streak=4, min_recent_win_rate=0.45, max_dd_7d=0.10)
ACCOUNT = 200_000


def with_losing_tail(n_losses: int, loss_usd: float, hours_ago_first: float = 20.0, wins_after: int = 0):
    """A good wallet whose latest round trips are `n_losses` losers, `hours_ago_first` hours ago and later."""
    raw, cs = trader(NOW, trips=500, win=0.75, seed=11)
    coin, px = "DOGE", 0.2          # a coin the generator does not trade, so nothing overlaps
    t0 = NOW - int(hours_ago_first * 3_600_000)
    oid = 9_000_000
    raw = [f for f in raw if f["time"] < t0 - 5 * 3_600_000]      # the generator's own latest trades must not follow
    for i in range(n_losses + wins_after):
        loss = i < n_losses
        t = t0 + i * 600_000
        oid += 2
        size = 100_000.0
        pnl = -loss_usd if loss else loss_usd
        raw.append(make_fill(coin, px, size, "B", 0.0, t=t, oid=oid, closed_pnl=0.0))
        raw.append(make_fill(coin, px, size, "A", size, t=t + 120_000, oid=oid + 1, closed_pnl=pnl))
    raw.sort(key=lambda f: (f["time"], f["tid"]))
    return raw, cs


def score_with(raw, cs, **kw):
    p = scoring.ScoreParams(**{**STRICT, **kw})
    return scoring.full_score("0xw", parse(raw), cand(cs), ACCOUNT, NOW, p)


def test_a_clean_wallet_passes_the_recent_gates_and_reports_its_week():
    raw, cs = trader(NOW, trips=500, win=0.75, seed=11)
    s = score_with(raw, cs)
    assert s.eligible, s.reasons
    assert s.loss_streak <= 2 and s.recent_win_rate >= 0.45 and s.loss_24h_pct <= 0.015


def test_losing_too_much_this_week_is_rejected_even_with_a_great_history():
    raw, cs = with_losing_tail(2, 5_000.0, hours_ago_first=50)       # -10k on 200k = 5% this week
    s = score_with(raw, cs, max_loss_streak=99, min_recent_win_rate=0.0)
    assert any(r.startswith("lost>3%_this_week") for r in s.reasons) and s.loss_7d_pct > 0.03
    assert not s.eligible


def test_losing_today_is_rejected():
    raw, cs = with_losing_tail(1, 4_000.0, hours_ago_first=3)         # -2% today
    s = score_with(raw, cs, max_loss_7d=1.0, max_loss_streak=99, min_recent_win_rate=0.0)
    assert any("_today" in r for r in s.reasons) and s.loss_24h_pct == pytest.approx(0.02, abs=0.001)


def test_a_losing_streak_is_rejected_and_a_win_breaks_it():
    raw, cs = with_losing_tail(5, 100.0)
    s = score_with(raw, cs, max_loss_7d=1.0, max_loss_24h=1.0, min_recent_win_rate=0.0)
    assert s.loss_streak == 5 and "5+_losses_in_a_row" in s.reasons
    raw, cs = with_losing_tail(5, 100.0, wins_after=1)                # the latest trade won
    s = score_with(raw, cs, max_loss_7d=1.0, max_loss_24h=1.0, min_recent_win_rate=0.0)
    assert s.loss_streak == 0 and not any("in_a_row" in r for r in s.reasons)


def test_a_cold_recent_stretch_is_rejected_by_the_recent_win_rate():
    raw, cs = with_losing_tail(10, 50.0)                              # 10 of the last 15 trips lost
    s = score_with(raw, cs, max_loss_7d=1.0, max_loss_24h=1.0, max_loss_streak=99)
    assert s.recent_win_rate < 0.45 and any(r.startswith("recent_win_rate<45%") for r in s.reasons)


def test_open_losses_count_towards_the_week():
    raw, cs = trader(NOW, trips=500, win=0.75, seed=11)
    acct = hl.Account(value=ACCOUNT, positions=[hl.OpenPos("BTC", 1.0, 100_000.0, -8_000.0)]) \
        if hasattr(hl, "OpenPos") else None
    if acct is None:
        pytest.skip("live account type not available")
    p = scoring.ScoreParams(**{**STRICT, "max_open_loss": 1.0})
    s = scoring.full_score("0xw", parse(raw), cand(cs), ACCOUNT, NOW, p, live=acct)
    # open losses are netted against this week's realized gains, and still push it over the 3% line
    assert 0.03 < s.loss_7d_pct <= 0.04 and any("_this_week" in r for r in s.reasons)


def test_the_default_params_leave_the_new_gates_off():
    raw, cs = with_losing_tail(5, 5_000.0)
    s = scoring.full_score("0xw", parse(raw), cand(cs), ACCOUNT, NOW)
    assert not any(k in " ".join(s.reasons) for k in ("_this_week", "_today", "in_a_row", "recent_win_rate", "week_drawdown"))


def test_repo_config_turns_the_gates_on_and_changing_them_redoes_cached_scores():
    from copybot import config
    c = config.load("config", env={})
    assert (c.selection.max_loss_7d, c.selection.max_loss_streak) == (0.03, 4)
    a = scoring.ScoreParams(**STRICT).rules()
    b = scoring.ScoreParams(**{**STRICT, "max_loss_7d": 0.05}).rules()
    assert a != b                      # a score made under other floors is recomputed


def test_one_big_win_carrying_the_profit_is_rejected_when_the_owner_caps_it():
    """Owner request 2026-10-09: a lower win-rate floor, but no wallet whose profit is one lucky trade."""
    raw, cs = trader(NOW, trips=300, win=0.55, seed=5, big_winner=400)
    p = scoring.ScoreParams(max_best_trade_share=0.30)
    s = scoring.full_score("0xluck", parse(raw), cand(cs), 200_000, NOW, p)
    assert s.concentration > 0.30 and "one_trade>30%_of_profit" in s.reasons and not s.eligible
    good, gcs = trader(NOW, trips=500, win=0.75)
    g = scoring.full_score("0xgood", parse(good), cand(gcs), 200_000, NOW, p)
    assert g.concentration <= 0.30 and g.eligible, g.reasons
    assert p.rules()["max_best_trade_share"] == 0.30          # part of the rules: cached scores are redone
