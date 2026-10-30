"""Independent paper margin shares: actual fills through the real gate, broker and ledger."""
import pytest
import threading

from copybot.allocations import MarginPositionManager
from copybot.ledger import Ledger, now_ms
from copybot.positions import Move
from tests.rig import Rig, LEADER, OTHER
from tests.test_e2e import env, stop_bot, wait_for  # noqa: F401
from tests.test_invo import invo_env, start_with_invo  # noqa: F401

THIRD = "0x3333333333333333333333333333333333333333"


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.cfg.risk.risk_per_trade_pct = 5
    r.cfg.risk.max_symbol_risk_pct = r.cfg.risk.max_total_risk_pct = 50
    r.cfg.risk.consensus_risk_pct = 2.5
    r.cfg.risk.trail_after_pct = 0
    r.rec(dict(ev="follow", leader=THIRD))
    r.pm = manager(r)
    yield r
    r.close()


def manager(r):
    return MarginPositionManager(r.cfg, r.st, r.ledger, r.gate, r.broker, r.health,
                                 r.mids, r.assets, margin_pct=5,
                                 notify=lambda k, **kw: r.events.append((k, kw)))


def move(r, who, old, new, side=1):
    return r.pm.on_move(Move(who, "ETH", side * old, side * new, now_ms(), now_ms(),
                            int(new * 10000) + 17, (), r.mids["ETH"], 1))


def conserved(p):
    assert sum(s["size"] for s in p.shares.values()) == pytest.approx(p.size)
    assert p.k == p.shares[p.leader]["k"]
    assert set(p.backers) == set(p.shares) - {p.leader}


def stop_with_watcher(bot, thread):
    # A restart must not race the previous simulated client's rotating authentication.
    workers = [t for t in threading.enumerate()
               if getattr(getattr(t, '_target', None), '__self__', None) is bot.invo_watch]
    stop_bot(bot, thread)
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize("side", [1, -1])
def test_each_trader_owns_a_margin_share_and_only_their_exit_removes_it(rig, side):
    move(rig, LEADER, 0, .4, side)
    p = rig.st.positions["ETH"]
    own = p.size
    assert p.size * p.entry_px / p.leverage == pytest.approx(15, rel=.02)
    move(rig, OTHER, 0, .2, side)
    second = p.shares[OTHER]["size"]
    assert second * rig.mids["ETH"] / p.leverage == pytest.approx(7.5, rel=.02)
    move(rig, THIRD, 0, .3, side)
    third = p.shares[THIRD]["size"]
    assert third == pytest.approx(second, rel=.02)
    conserved(p)
    move(rig, LEADER, .4, 0, side)
    assert "ETH" in rig.st.positions and LEADER not in p.shares
    assert p.size == pytest.approx(second + third)
    assert not rig.st.closed
    conserved(p)
    move(rig, OTHER, .2, .1, side)             # this trader halves only their own allocation
    assert p.shares[OTHER]["size"] == pytest.approx(second / 2)
    assert p.shares[THIRD]["size"] == third
    conserved(p)
    move(rig, OTHER, .1, 0, side)
    assert p.leader == THIRD and p.size == pytest.approx(third)
    conserved(p)
    move(rig, THIRD, .3, 0, side)
    assert not rig.st.positions and len(rig.st.closed) == 1
    assert rig.st.realized == pytest.approx(rig.st.closed[0]["pnl"])


def test_owner_resizes_do_not_resize_backers_and_replay_keeps_all_shares(rig):
    move(rig, LEADER, 0, .4)
    p = rig.st.positions["ETH"]
    own = p.size
    move(rig, OTHER, 0, .2)
    second = p.shares[OTHER]["size"]
    move(rig, LEADER, .4, .2)
    assert p.shares[LEADER]["size"] == pytest.approx(own / 2)
    assert p.shares[OTHER]["size"] == second
    move(rig, LEADER, .2, .4)
    assert p.shares[LEADER]["size"] == pytest.approx(own, rel=.02)
    assert p.shares[OTHER]["size"] == second
    replay = Ledger(rig.path).replay()
    assert replay.positions["ETH"].shares == p.shares
    assert not replay.uncertain
    rig.st = replay
    rig.pm = manager(rig)
    move(rig, LEADER, .4, 0)
    assert rig.st.positions["ETH"].size == pytest.approx(second)
    conserved(rig.st.positions["ETH"])


def test_a_tiny_share_exit_waits_instead_of_closing_other_traders(rig):
    move(rig, LEADER, 0, .4)
    p = rig.st.positions["ETH"]
    move(rig, OTHER, 0, .2)
    other = p.shares[OTHER]["size"]
    move(rig, LEADER, .4, .01)                 # leave about $2.60 of the original position
    move(rig, LEADER, .01, 0)
    assert p.shares[LEADER]["closing"]
    assert p.shares[OTHER]["size"] == other
    assert not rig.st.closed
    conserved(p)
    move(rig, OTHER, .2, 0)                   # the final exit can close the whole position
    assert not rig.st.positions


def test_a_backer_rejected_add_then_trim_leaves_the_main_contribution_unchanged(rig):
    move(rig, LEADER, 0, .4)
    p = rig.st.positions['ETH']
    main = p.shares[LEADER]['size']
    move(rig, OTHER, 0, .2)
    other = p.shares[OTHER]['size']
    rig.healthy = False
    move(rig, OTHER, .2, .8)
    assert p.shares[OTHER]['size'] == other
    assert p.shares[OTHER]['k'] == pytest.approx(other / .8)
    move(rig, OTHER, .8, .4)
    assert p.shares[OTHER]['size'] == pytest.approx(other / 2)
    assert p.shares[LEADER]['size'] == main
    conserved(p)


def test_individual_profit_fees_and_funding_reconcile_before_and_after_final_stop(rig):
    from copybot.cardfmt import trader_closed, trader_live, traders_card
    move(rig, LEADER, 0, .4)
    p = rig.st.positions['ETH']
    rig.price('ETH', 2950)
    move(rig, OTHER, 0, .2)
    rig.rec(dict(ev='funding', coin='ETH', pos_id=p.pos_id, amount=-.25))
    rig.price('ETH', 2960)
    move(rig, LEADER, .4, 0)
    first = trader_closed(rig.st, LEADER)
    assert len(first) == 1 and len(trader_closed(rig.st, OTHER)) == 0
    assert first[0]['pnl'] < 0
    assert rig.st.leader_stats[LEADER].cum == pytest.approx(first[0]['pnl'])
    attributed = sum(t['pnl'] for t in rig.st.allocation_closed) + trader_live(p, OTHER, 2960)
    assert attributed == pytest.approx(rig.st.realized + p.upnl(2960))
    text = traders_card(rig.st, rig.mids, now_ms())
    assert '1 trade · 0 won, 1 lost' in text
    rig.price('ETH', p.stop_px * .99)
    rig.pm.check_stops()
    assert not rig.st.positions and len(rig.st.allocation_closed) == 2
    assert sum(t['pnl'] for t in rig.st.allocation_closed) == pytest.approx(rig.st.realized)
    assert sum(t['fees'] for t in rig.st.allocation_closed) == pytest.approx(rig.st.closed[0]['fees'])
    assert sum(t['funding'] for t in rig.st.allocation_closed) == pytest.approx(-.25)
    replay = Ledger(rig.path).replay()
    assert replay.allocation_closed == rig.st.allocation_closed
    assert not replay.uncertain


def test_existing_positions_migrate_without_buying_more_or_resetting_their_balances(rig):
    from copybot.positions import PositionManager
    rig.pm = PositionManager(rig.cfg, rig.st, rig.ledger, rig.gate, rig.broker,
                             rig.health, rig.mids, rig.assets)
    move(rig, LEADER, 0, .4)
    p = rig.st.positions['ETH']
    move(rig, OTHER, 0, .2)
    before = p.size
    orders = rig.st.counters['orders']
    expected = p.size * p.backers[OTHER]['frac']
    rig.pm = manager(rig)
    rig.pm.seed(p)
    assert p.size == before and rig.st.counters['orders'] == orders
    conserved(p)
    move(rig, LEADER, .4, 0)
    assert p.size == pytest.approx(expected)
    move(rig, OTHER, .2, 0)
    assert sum(t['pnl'] for t in rig.st.allocation_closed) == pytest.approx(rig.st.realized)


def test_bot_margin_allocations_continue_after_the_first_trader_exits_and_restart(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    with (cdir / "invo.toml").open("a", encoding="utf8") as out:
        out.write("margin_allocations = true\nmain_stop_pct = 0.0\n")
    a = f.add_user("first")[0]
    b = f.add_user("second")[0]
    c = f.add_user("third")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        for name in ("first", "second", "third"):
            dc.say('/invofollow ' + name)
            assert wait_for(lambda: bot.invo_watch.known.get(name) is not None, timeout=10)
        ca = f.open_call(a, "ETH", size=.02, leverage=20, entry=3000)
        assert wait_for(lambda: "ETH" in bot.invo.st.positions, timeout=10)
        assert wait_for(lambda: all('ETH' in w.st.positions for w in bot.invo_wallets()), timeout=10)
        for w in bot.invo_wallets():
            q = w.st.positions['ETH']
            assert 'margin' in w.label
            assert q.size * q.entry_px / q.leverage == pytest.approx(300 * w.pm.margin_pct / 100, rel=.04)
        p = bot.invo.st.positions["ETH"]
        assert p.size * p.entry_px / p.leverage == pytest.approx(15, rel=.03)
        cb = f.open_call(b, "ETH", size=.01, leverage=10, entry=3000)
        assert wait_for(lambda: "invo:second" in p.shares, timeout=10)
        assert wait_for(lambda: p.shares["invo:second"]["size"] > 0, timeout=10)
        second = p.shares["invo:second"]["size"]
        cc = f.open_call(c, "ETH", size=.01, leverage=10, entry=3000)
        assert wait_for(lambda: p.shares.get("invo:third", {}).get("size", 0) > 0, timeout=10)
        third = p.shares["invo:third"]["size"]
        main = p.shares['invo:first']['size']
        cb = f.reid_call(b, cb, entrySize=2.0, positionSize=.02)
        assert wait_for(lambda: p.shares['invo:second']['size'] > second * 1.9, timeout=10)
        assert p.shares['invo:first']['size'] == main
        assert p.shares['invo:third']['size'] == third
        f.resize_call(b, cb, .01)
        assert wait_for(lambda: p.shares['invo:second']['size'] < second * 1.1, timeout=10)
        second = p.shares['invo:second']['size']
        f.close_call(a, ca)
        assert wait_for(lambda: "invo:first" not in p.shares, timeout=10)
        assert p.size == pytest.approx(second + third)
        assert not bot.invo.st.closed
        assert wait_for(lambda: bot.invo.st.invo_links[p.pos_id]["call"] in (cb, cc), timeout=10)
    finally:
        stop_with_watcher(bot, th)
        bot.lock.f.close()
    cb = f.reid_call(b, cb, entrySize=.5, positionSize=.005)  # resized and re-identified while offline
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert wait_for(lambda: all(bot.invo_watch.known.get(n) is not None for n in ('second', 'third')), timeout=10)
        p = bot.invo.st.positions["ETH"]
        assert wait_for(lambda: p.shares['invo:second']['size'] < second * .6, timeout=10)
        assert p.size == pytest.approx(second / 2 + third, rel=.02)
        assert p.shares['invo:second']['call'] == cb
        cb2 = f.reid_call(b, cb, entrySize=1.0, positionSize=.01)
        assert wait_for(lambda: p.shares['invo:second']['size'] > second * .9, timeout=10)
        assert p.shares['invo:third']['size'] == third
        f.close_call(b, cb2)
        assert wait_for(lambda: 'invo:second' not in p.shares, timeout=10)
        assert p.size == pytest.approx(third)
        f.close_call(c, cc)
        assert wait_for(lambda: 'ETH' not in bot.invo.st.positions, timeout=10)
    finally:
        stop_with_watcher(bot, th)
