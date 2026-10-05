from dataclasses import asdict

import pytest

from copybot import config, hl
from copybot.ledger import Position, State
from copybot.risk import Health, Order, RiskGate, liq_distance
from tests.fakes import fixture

META = hl.parse_meta(fixture("meta_and_asset_ctxs.json"))
BTC = META["BTC"]
NOW = 1_800_000_000_000


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def healthy():
    return Health(now_ms=NOW, mids_age_s=0.2, clock_ok=True, clock_offset_ms=0, feed_age_s=1, feed_connected=True)


def state(equity=300.0):
    st = State(equity0=equity)
    st.followed = {"0xl": 0, "0xm": 0}
    st.marks = {"day": {"key": "d", "equity": equity}, "week": {"key": "w", "equity": equity}}
    return st


def gate():
    return RiskGate(config.load("config", env={}), clock=Clock())


def open_order(px=100_000.0, size=1.0, side=1, leader="0xl", coin="BTC", fill_time=NOW - 1000):
    return Order("open", coin, side, size, px, leader=leader, fill_time_ms=fill_time)


def add_pos(st, coin="BTC", side=1, size=0.001, entry=100_000.0, stop=97_000.0, leader="0xl", lev=9):
    st.positions[coin] = Position(pos_id=coin, coin=coin, side=side, size=size, entry_px=entry, stop_px=stop,
                                  leverage=lev, leader=leader, k=1, open_oid=1, opened_ms=0)


def test_open_is_sized_from_our_risk_not_leader_size():
    g, st = gate(), state()
    d = g.check(open_order(size=1e9), st, healthy(), {}, BTC)
    assert d.ok
    risk = abs(100_000 - d.stop_px) * d.size
    assert risk == pytest.approx(3.0, rel=0.01)          # 1% of $300
    assert d.size * 100_000 == pytest.approx(100, rel=0.01)  # $100 notional with a 3% stop
    assert d.stop_px == pytest.approx(97_000)


def test_short_stop_is_above_entry():
    d = gate().check(open_order(side=-1), state(), healthy(), {}, BTC)
    assert d.ok and d.stop_px == pytest.approx(103_000)


def test_leverage_capped_and_liquidation_at_least_3x_stop_away():
    g = gate()
    for a in META.values():
        lev = g.leverage_for(a)
        assert lev <= 10 and lev <= a.max_leverage
        if lev >= 1:
            assert liq_distance(lev, a.max_leverage) >= 3 * 0.03


def test_min_notional_skip():
    g, st = gate(), state(equity=20.0)   # 1% of $20 at 3% stop = $6.67 notional < $10
    d = g.check(open_order(), st, healthy(), {}, BTC)
    assert not d.ok and d.reason.startswith("below_min_notional")


@pytest.mark.parametrize("mutate,reason", [
    (lambda h, st: setattr(h, "mids_age_s", 30), "stale_prices"),
    (lambda h, st: setattr(h, "clock_ok", False), "clock_in_doubt"),
    (lambda h, st: setattr(h, "feed_connected", False), "leader_feed_in_doubt"),
    (lambda h, st: setattr(h, "feed_age_s", 500), "leader_feed_in_doubt"),
    (lambda h, st: st.uncertain.append("x"), "uncertain_state"),
    (lambda h, st: setattr(st, "entries_paused", True), "entries_paused"),
    (lambda h, st: st.paused_leaders.update({"0xl": "streak"}), "leader_paused"),
    (lambda h, st: st.followed.pop("0xl"), "leader_not_followed"),
    (lambda h, st: st.marks.pop("day"), "no_day_mark"),
])
def test_entries_fail_closed(mutate, reason):
    g, st, h = gate(), state(), healthy()
    mutate(h, st)
    d = g.check(open_order(), st, h, {}, BTC)
    assert not d.ok and d.reason.startswith(reason)


def test_old_leader_fill_refused_with_clock_tolerance():
    g, st = gate(), state()
    assert g.check(open_order(fill_time=NOW - 10_400), st, healthy(), {}, BTC).ok      # 10.4 s - 0.5 s tolerance
    d = g.check(open_order(fill_time=NOW - 11_000), st, healthy(), {}, BTC)
    assert not d.ok and d.reason.startswith("leader_fill_too_old")


def test_daily_and_weekly_loss_limits():
    g, st = gate(), state()
    st.realized = -15.0   # -5% today
    assert g.check(open_order(), st, healthy(), {}, BTC).reason == "day_loss_limit"
    st.marks["day"]["equity"] = 285.0
    st.marks["week"]["equity"] = 320.0
    st.realized = -32.0 + 20  # equity 288 = -10% on the week mark of 320
    assert g.check(open_order(), st, healthy(), {}, BTC).reason == "week_loss_limit"


def test_max_positions_and_symbol_taken():
    g, st = gate(), state()
    add_pos(st, leader="0xm")
    assert g.check(open_order(), st, healthy(), {}, BTC).reason == "symbol_taken"
    for i in range(9):
        add_pos(st, coin=f"C{i}", size=0.0, entry=1, stop=0.97)
    assert len(st.positions) == 10
    assert g.check(open_order(coin="ETH"), st, healthy(), {}, META["ETH"]).reason == "max_positions"


def test_total_and_symbol_risk_clamp_adds():
    g, st = gate(), state()
    add_pos(st, size=0.001)   # $3 risk (1%)
    d = g.check(Order("add", "BTC", 1, 1.0, 100_000.0, leader="0xl"), st, healthy(), {}, BTC)
    assert d.ok
    assert (0.001 + d.size) * 3000 <= 300 * 0.015 + 1e-9   # symbol cap 1.5%


def test_total_risk_cap():
    g, st = gate(), state()
    for i in range(3):  # 3 x $9.96 = $29.88 risk (cap $30)
        add_pos(st, coin=f"C{i}", size=3.32, entry=100, stop=97)
    d = g.check(open_order(), st, healthy(), {}, BTC)
    assert not d.ok  # only $0.12 of risk left -> $10 notional impossible


def test_orders_per_minute_blocks_entries_never_exits():
    g, st = gate(), state()
    add_pos(st, coin="ETH", size=0.05, entry=3000, stop=2910)
    for _ in range(30):
        g.record_order()
    assert g.check(open_order(), st, healthy(), {}, BTC).reason == "order_rate_limit"
    d = g.check(Order("close", "ETH", 1, 0, 3000.0), st, Health(), {}, None)  # even with every input stale
    assert d.ok and d.size == 0.05
    g.clock.t += 61
    assert g.check(open_order(), st, healthy(), {}, BTC).ok


def test_exits_pass_with_everything_stale_and_partial_dust_is_upgraded():
    g, st = gate(), state()
    st.entries_paused = True
    st.uncertain.append("x")
    add_pos(st, coin="ETH", size=0.01, entry=3000, stop=2910)   # $30 notional
    d = g.check(Order("reduce", "ETH", 1, 0.005, 3000.0), st, Health(), {}, None)
    assert d.ok and d.size == 0.005 and d.reason == "exit"            # $15 out, $15 left
    d = g.check(Order("reduce", "ETH", 1, 0.007, 3000.0), st, Health(), {}, None)
    assert d.ok and d.size == 0.01 and d.reason == "exit_upgraded_to_close"   # $9 would be left
    d = g.check(Order("reduce", "ETH", 1, 0.002, 3000.0), st, Health(), {}, None)
    assert not d.ok and d.reason.startswith("below_min_notional")     # a $6 partial mirror is skipped
