import pytest

from copybot import hl
from copybot.detector import Detector
from copybot.ledger import Ledger
from tests.fakes import make_fill
from tests.rig import LEADER, OTHER, Rig


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def test_open_long_places_stop_at_entry_and_sizes_from_risk(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 5.0, "B"))   # leader buys $500k
    p = rig.st.positions["BTC"]
    assert p.side == 1 and p.leader == LEADER
    assert p.stop_px == pytest.approx(p.entry_px * 0.97)
    assert p.risk_usd() == pytest.approx(3.0, rel=0.02)            # 1% of $300, not the leader's size
    assert p.k == pytest.approx(p.size / 5.0)
    assert rig.st.lags_ms and 0 <= rig.st.lags_ms[-1] < 5000
    assert ("opened", {"coin": "BTC"}) in rig.events
    # durable: a fresh replay sees the same position and stop
    st2 = Ledger(rig.path).replay()
    assert st2.positions["BTC"].stop_px == p.stop_px and not st2.uncertain


def test_open_short(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 10, "A"))
    p = rig.st.positions["ETH"]
    assert p.side == -1 and p.stop_px == pytest.approx(p.entry_px * 1.03)


def test_split_opening_order_rebases_instead_of_adding(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 0.1, "B", oid=777))
    size = rig.st.positions["BTC"].size
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 4.9, "B", oid=777))   # rest of the same order
    p = rig.st.positions["BTC"]
    assert p.size == size and p.k == pytest.approx(size / 5.0)


def test_multi_fill_message_is_one_move(rig):
    f1 = rig.fill(LEADER, "SOL", 100, "B", oid=5)
    f2 = make_fill("SOL", rig.mids["SOL"], 50, "B", 100.0, oid=5)
    rig.leader_trades(LEADER, f1, f2)
    p = rig.st.positions["SOL"]
    assert p.k == pytest.approx(p.size / 150)
    assert rig.st.counters["orders"] == 1


def test_add_reduce_close_mirror_proportionally(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 100, "B"))
    p = rig.st.positions["ETH"]
    s0 = p.size
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 25, "B"))          # +25% -> +25% (within 1.5% symbol risk)
    assert p.size == pytest.approx(s0 * 1.25, rel=0.01)
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 100, "B"))         # way more -> capped at 1.5% risk
    assert p.risk_usd() <= 300 * 0.015 + 1e-6
    held = p.size
    lp = rig.lpos(LEADER, "ETH")
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", lp / 2, "A"))      # leader halves -> we halve
    assert p.size == pytest.approx(held / 2, rel=0.02)
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", rig.lpos(LEADER, "ETH"), "A"))
    assert "ETH" not in rig.st.positions
    assert rig.st.closed[-1]["reason"] == "leader_close"


def test_flip_closes_and_opens_fresh_copy(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 2, "B"))
    first = rig.st.positions["BTC"].pos_id
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 5, "A"))   # long 2 -> short 3
    p = rig.st.positions["BTC"]
    assert p.side == -1 and p.pos_id != first
    assert p.k == pytest.approx(p.size / 3)
    assert rig.st.closed[-1]["reason"] == "leader_flip"


def test_stop_closes_position(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 1, "B"))
    stop = rig.st.positions["BTC"].stop_px
    rig.price("BTC", stop * 1.001)
    rig.pm.check_stops()
    assert "BTC" in rig.st.positions
    rig.price("BTC", stop * 0.999)
    rig.pm.check_stops()
    assert "BTC" not in rig.st.positions
    t = rig.st.closed[-1]
    assert t["reason"] == "stop" and t["pnl"] < 0
    assert t["pnl"] == pytest.approx(-3.0, rel=0.1)   # about the 1% planned risk


def test_exits_work_when_everything_is_stale_and_book_is_down(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 10, "B"))
    rig.healthy = False
    rig.fake.book_fail = True
    rig.rec({"ev": "pause", "reason": "test"})
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 10, "A"))
    assert "ETH" not in rig.st.positions
    # but entries are refused
    rig.leader_trades(LEADER, rig.fill(LEADER, "SOL", 10, "B"))
    assert "SOL" not in rig.st.positions


def test_other_leader_cannot_take_a_held_symbol(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 1, "B"))
    rig.leader_trades(OTHER, rig.fill(OTHER, "BTC", 1, "A"))
    assert rig.st.positions["BTC"].leader == LEADER
    rig.leader_trades(OTHER, rig.fill(OTHER, "BTC", 1, "B"))   # OTHER closes: must not touch our LEADER copy
    assert rig.st.positions["BTC"].side == 1


def test_add_or_close_without_our_copy_is_skipped(rig):
    rig.fake.positions[LEADER.lower()] = {"ETH": 5.0}   # leader had this before we followed
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 1, "B"))
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 6, "A"))
    assert "ETH" not in rig.st.positions and rig.st.counters.get("orders", 0) == 0


def test_min_notional_skip_is_counted(tmp_path):
    r = Rig(tmp_path, equity=20.0)
    try:
        r.leader_trades(LEADER, r.fill(LEADER, "BTC", 1, "B"))
        assert not r.st.positions and r.st.counters["skipped_min_notional"] == 1
    finally:
        r.close()


def test_snapshot_and_duplicate_fills_never_trade(rig):
    f = rig.fill(LEADER, "BTC", 1, "B")
    rig.leader_trades(LEADER, f, snapshot=True)
    assert not rig.st.positions
    rig.leader_trades(LEADER, f)            # same tid again, live
    assert not rig.st.positions


def test_reconcile_closes_when_leader_is_flat(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 2, "B"))
    rig.fake.positions[LEADER.lower()] = {}            # the close fill was missed by the websocket
    rig.pm.reconcile(LEADER, rig.info.positions(LEADER, 2))
    assert "BTC" not in rig.st.positions
    assert rig.st.closed[-1]["reason"] == "reconcile_leader_flat"


def test_reconcile_reduces_when_leader_reduced(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "ETH", 100, "B"))
    s = rig.st.positions["ETH"].size
    rig.fake.positions[LEADER.lower()] = {"ETH": 40.0}
    rig.pm.reconcile(LEADER, rig.info.positions(LEADER, 2))
    assert rig.st.positions["ETH"].size == pytest.approx(s * 0.4, rel=0.01)


def test_leader_paused_after_consecutive_losses(rig):
    for i in range(5):
        rig.leader_trades(LEADER, rig.fill(LEADER, "SOL", 10, "B"))
        rig.price("SOL", rig.mids["SOL"] * 0.995)
        rig.leader_trades(LEADER, rig.fill(LEADER, "SOL", 10, "A"))
    assert LEADER in rig.st.paused_leaders
    assert any(k == "leader_paused" for k, _ in rig.events)
    rig.leader_trades(LEADER, rig.fill(LEADER, "SOL", 10, "B"))
    assert "SOL" not in rig.st.positions   # no new entries from a paused leader


def test_leader_paused_on_copy_drawdown(rig):
    rig.cfg.risk.leader_pause_losses = 5
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 1, "B"))
    rig.price("BTC", rig.st.positions["BTC"].stop_px * 0.99)
    rig.pm.check_stops()   # ~ -$3 = 7% of a $43 allocation: not yet
    assert LEADER not in rig.st.paused_leaders
    rig.price("BTC", 100_000)
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 1, "A"))   # leader flat again (no copy, skipped)
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 1, "B"))
    rig.price("BTC", rig.st.positions["BTC"].stop_px * 0.99)
    rig.pm.check_stops()   # ~ -$6 > 10% of the allocation
    assert LEADER in rig.st.paused_leaders


def test_funding_applied(rig):
    rig.leader_trades(LEADER, rig.fill(LEADER, "BTC", 1, "B"))
    before = rig.st.realized
    rig.pm.apply_funding(rig.assets, "h1")
    p = rig.st.positions["BTC"]
    expected = -p.size * rig.mids["BTC"] * rig.assets["BTC"].funding
    assert rig.st.realized - before == pytest.approx(expected)


def test_detector_splits_broken_chains_and_ignores_non_perps():
    d = Detector()
    fs = hl.parse_fills([
        make_fill("BTC", 100, 1, "B", 0.0, oid=1),
        make_fill("BTC", 100, 1, "B", 5.0, oid=2),     # does not continue from 1.0: separate move
        make_fill("@107", 1, 10, "B", 0.0, oid=3),
        make_fill("xyz:TSLA", 1, 10, "B", 0.0, oid=4),
    ])
    moves = d.on_fills("0xa", fs)
    assert [(m.kind, m.start_pos, m.end_pos) for m in moves] == [("open", 0, 1), ("add", 5, 6)]


def test_detector_on_real_recorded_fills():
    from tests.fakes import fixture
    fills = hl.parse_fills(fixture("user_fills_full_page.json")["fills"])
    moves = Detector().on_fills("0xa", fills)
    kinds = {m.kind for m in moves}
    assert {"open", "add", "close"} <= kinds or {"open", "add", "reduce"} <= kinds
    assert all(hl.is_core_perp(m.coin) for m in moves)
