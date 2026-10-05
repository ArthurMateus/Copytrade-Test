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
    raw, _ = trader(NOW, trips=100)
    assert scoring.fill_screen(parse(raw), NOW, 100).reason == "round_trips<150"


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
    assert s.copy_edge_bps > 0 and 0 < s.shrink < 1 and s.score > 0


def test_full_score_losing_trader_is_not():
    raw, cs = trader(NOW, trips=500, win=0.35, seed=3)
    s = scoring.full_score("0xbad", parse(raw), cand(cs), 200_000, NOW)
    assert not s.eligible and "pnl<=0" in s.reasons


def test_full_score_concentration():
    raw, cs = trader(NOW, trips=300, win=0.55, seed=5, big_winner=400)
    s = scoring.full_score("0xluck", parse(raw), cand(cs), 200_000, NOW)
    assert "one_trade>25%_of_pnl" in s.reasons or s.concentration <= 0.25


def test_full_score_short_history_fails_consistency():
    raw, cs = trader(NOW, trips=400, win=0.65, first_day=120)   # active only the last 60 days
    s = scoring.full_score("0xnew", parse(raw), cand(cs), 200_000, NOW)
    assert "positive_blocks<4" in s.reasons


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
