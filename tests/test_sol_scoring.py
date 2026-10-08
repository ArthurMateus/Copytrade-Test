"""Strict Solana wallet scoring: a good wallet passes, and each rule rejects its own kind of bad wallet."""
from dataclasses import replace

import pytest

from copybot.config import Sol
from copybot.sol import scoring
from copybot.sol.fomo import Leg, LbRow

DAY = 86_400_000
NOW = 1_800_000_000_000
C = Sol()


def trade(i, token, open_ms, hold_s, cost, ret):
    """A closed round trip: one buy and one sell."""
    amt = 1000.0
    return [Leg(f"b{i}", open_ms, token, "buy", amt, cost),
            Leg(f"s{i}", open_ms + int(hold_s * 1000), token, "sell", amt, cost * (1 + ret))]


def good_trades(n=90, days=60, hold_s=1200, cost=200.0):
    """Wins +40% (3 of 5), losses -15% (2 of 5), a different token for (almost) every trade."""
    legs = []
    pattern = [0.40, 0.40, -0.15, 0.40, -0.15]
    for i in range(n):
        t = NOW - days * DAY + int(i * (days * DAY - 3 * 3600_000) / n)
        legs += trade(i, f"TOK{i % 37}", t, hold_s, cost, pattern[i % 5])
    return legs


def score(legs, c=C):
    return scoring.full_score("W", legs, NOW, c)


def test_a_consistent_wallet_is_eligible():
    s = score(good_trades())
    assert s.eligible, s.reasons
    assert s.trades == 90 and s.win_rate == pytest.approx(0.6) and s.profit_factor > 3
    assert s.positive_weeks == 4 and s.copy_edge_pct > 5 and s.median_hold_s == 1200


def test_round_trips_use_average_cost_and_partial_sells():
    legs = [Leg("1", 1000, "T", "buy", 100, 100.0), Leg("2", 2000, "T", "buy", 100, 300.0),
            Leg("3", 3000, "T", "sell", 100, 250.0), Leg("4", 4000, "T", "sell", 100, 150.0)]
    b = scoring.build(legs)
    assert len(b.trips) == 1 and b.trips[0].cost == 400.0 and b.trips[0].proceeds == 400.0
    assert b.open_cost == 0 and b.trips[0].hold_s == 3.0


def test_open_positions_and_orphan_sells_are_tracked():
    legs = [Leg("1", 1000, "T", "buy", 100, 100.0), Leg("2", 2000, "T", "sell", 40, 60.0),
            Leg("3", 3000, "X", "sell", 5, 9.0)]
    b = scoring.build(legs)
    assert b.trips == [] and b.open_cost == pytest.approx(60.0) and b.orphan_sells == 1


def test_too_few_trades_and_short_history_are_rejected():
    s = score(good_trades(n=30))
    assert "trades<40" in s.reasons
    s = score(good_trades(n=60, days=15))
    assert "history<21d" in s.reasons and "active_days<14" in s.reasons or "history<21d" in s.reasons


def test_a_wallet_losing_recently_is_rejected():
    legs = good_trades()
    for i in range(12):   # the last days: all stopped out
        legs += trade(1000 + i, f"LOS{i}", NOW - int((3 - i * 0.2) * DAY), 1200, 400.0, -0.4)
    s = score(legs)
    assert not s.eligible and ("not_profitable_7d" in s.reasons or "losing_today" in s.reasons
                               or "current_drawdown" in s.reasons)


def test_losing_today_alone_is_rejected():
    legs = good_trades()
    legs += trade(2000, "BAD", NOW - 3 * 3600_000, 1200, 3000.0, -0.9)
    s = score(legs)
    assert "losing_today" in s.reasons and not s.eligible


def test_holding_a_big_open_bag_is_rejected():
    legs = good_trades()
    legs += [Leg("bag", NOW - 5 * DAY, "BAG", "buy", 1e6, 6000.0)]          # bought, never sold
    s = score(legs)
    assert not s.eligible and ("holding_a_lot" in s.reasons or "open_bag_vs_pnl" in s.reasons)


def test_one_lucky_trade_is_rejected():
    legs = []
    for i in range(80):          # many break-even-ish trades ...
        legs += trade(i, f"T{i % 40}", NOW - 59 * DAY + i * 15 * 3600_000 // 10, 1200, 100.0, 0.05 if i % 2 else -0.02)
    legs += trade(999, "LUCKY", NOW - 20 * DAY, 3600, 100.0, 80.0)           # ... and one 80x
    s = score(legs)
    assert "one_trade_too_big" in s.reasons and not s.eligible


def test_one_token_carrying_the_pnl_is_rejected():
    legs = []
    for i in range(80):
        legs += trade(i, "SAME", NOW - 59 * DAY + i * 3 * 3600_000 // 2 * 10, 1200, 200.0, 0.40 if i % 5 < 3 else -0.15)
    assert "one_token_too_big" in score(legs).reasons


def test_snipers_with_second_long_holds_are_rejected():
    s = score(good_trades(hold_s=8))
    assert "sniper_holds_too_short" in s.reasons and not s.eligible


def test_low_win_rate_and_weak_profit_factor_are_rejected():
    legs = []
    for i in range(90):   # win +60% 1 of 3 times, lose -25% otherwise: pnl positive but win rate 33%
        t = NOW - 60 * DAY + int(i * 59 * DAY / 90)
        legs += trade(i, f"T{i % 37}", t, 1200, 200.0, 0.60 if i % 3 == 0 else -0.25)
    s = score(legs)
    assert "win_rate<40%" in s.reasons


def test_copy_edge_after_costs_and_lag_is_required():
    legs = []
    pattern = [0.04, 0.04, -0.03, 0.04, -0.03]      # a thin edge that our 3% cost + lag eats
    for i in range(90):
        legs += trade(i, f"T{i % 37}", NOW - 60 * DAY + int(i * 59 * DAY / 90), 300, 200.0, pattern[i % 5])
    s = score(legs)
    assert "copy_edge_too_low" in s.reasons and not s.eligible


def test_stricter_config_rejects_what_the_default_accepts():
    legs = good_trades()
    assert score(legs).eligible
    assert not score(legs, replace(C, min_trades=200)).eligible


def test_ranking_is_deterministic_and_only_eligible():
    a = score(good_trades())
    b = score(good_trades(n=120))
    bad = score(good_trades(hold_s=5))
    a.address, b.address, bad.address = "A", "B", "C"
    r1 = scoring.ranking([a, b, bad])
    assert r1 == scoring.ranking([bad, b, a]) and "C" not in r1 and set(r1) == {"A", "B"}


def row(pnl=10_000.0, window="30d", swaps=200, volume=60_000.0, holdings=0.0, hpnl=0.0, private=False):
    return LbRow("u", "A", "h", 100, swaps, swaps // 2, volume, pnl, window, 0, private, False, holdings, hpnl, 0)


def test_prescreen_rules():
    r30, r7, r24 = row(), row(2000.0, "7d"), row(100.0, "24h")
    assert scoring.prescreen(r30, r7, r24, C).ok
    assert scoring.prescreen(row(500.0), r7, r24, C).reason == "pnl_30d_too_low"
    assert scoring.prescreen(row(swaps=10), r7, r24, C).reason == "too_few_swaps"
    assert scoring.prescreen(row(volume=10_000.0), r7, r24, C).reason == "pnl_vs_volume_one_shot"
    assert scoring.prescreen(r30, None, r24, C).reason == "not_profitable_7d"
    assert scoring.prescreen(r30, row(-5.0, "7d"), r24, C).reason == "not_profitable_7d"
    assert scoring.prescreen(r30, r7, row(-5000.0, "24h"), C).reason == "losing_today"
    assert scoring.prescreen(row(holdings=50_000.0), r7, r24, C).reason == "holding_a_lot"
    assert scoring.prescreen(row(holdings=100.0, hpnl=8000.0), r7, r24, C).reason == "profit_mostly_unrealised"
    assert scoring.prescreen(row(private=True), r7, r24, C).reason == "private_or_restricted"
