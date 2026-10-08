"""Solana risk gate, paper broker, detector and trader, wired with the REAL components against fake network."""
import pytest

from copybot import config
from copybot.ledger import Ledger, now_ms
from copybot.sol.fomo import Leg
from copybot.sol.market import NoQuote, PaperBroker, Prices
from copybot.sol.risk import Health, SolGate
from copybot.sol.trader import Detector, Trader
from tests.fakes_sol import FakeDex

LEADER, OTHER = "LeaderWallet1", "LeaderWallet2"
TOK, TOK2 = "TokMint111", "TokMint222"


class Rig:
    def __init__(self, tmp_path, equity=300.0):
        self.dex = FakeDex()
        self.cfg = config.load("config", env={})
        self.c = self.cfg.sol
        self.c.dex_url, self.c.start_equity = self.dex.url, equity
        self.path = tmp_path / "ledger.jsonl"
        self.healthy = True
        self.events: list = []
        self.n = 0
        self.dex.set(TOK, 0.01, 500_000.0, "MEME")
        self.dex.set(TOK2, 2.0, 400_000.0, "TWO")
        self.boot()
        self.rec({"ev": "genesis", "equity0": equity, "btc_px0": 0})
        self.rec({"ev": "mark", "kind": "day", "key": "d", "equity": equity})
        self.rec({"ev": "mark", "kind": "week", "key": "w", "equity": equity})
        self.rec({"ev": "follow", "leader": LEADER})
        self.rec({"ev": "follow", "leader": OTHER})

    def boot(self):
        self.ledger = Ledger(self.path)
        self.st = self.ledger.replay()
        self.prices = Prices(self.dex.url)
        self.det = Detector()
        self.gate = SolGate(self.c)
        self.trader = Trader(self.c, self.st, self.ledger, self.gate, PaperBroker(self.c), self.prices, self.health,
                             self.det, notify=lambda kind, **kw: self.events.append((kind, kw)))

    def health(self):
        if not self.healthy:
            return Health(now_ms=now_ms())
        return Health(now_ms=now_ms(), price_age_s=0.5, leader_feed_age_s=1.0, auth_ok=True)

    def rec(self, ev):
        self.st.apply(self.ledger.append(ev))

    def price(self, token, px, liq=500_000.0, sym="MEME"):
        self.dex.set(token, px, liq, sym)
        self.prices.fetch([token])

    def swap(self, leader, side, token, amount, usd=None, age_s=1.0):
        self.n += 1
        usd = usd if usd is not None else amount * self.dex.tokens[token]["px"]
        leg = Leg(f"leg{self.n}", now_ms() - int(age_s * 1000), token, side, amount, usd)
        for m in self.det.on_legs(leader, [leg]):
            self.trader.on_move(m)

    def close(self):
        self.ledger.close()
        self.dex.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def skips(rig):
    return [kw["reason"] for k, kw in rig.events if k == "skip"]


# ---- sizing, fills, stop -------------------------------------------------------------------------------
def test_open_sizes_from_our_risk_and_places_the_stop_at_the_fill(rig):
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    p = rig.st.positions[TOK]
    assert p.side == 1 and p.leverage == 1.0 and p.sym == "MEME" and p.leader == LEADER
    assert p.entry_px == pytest.approx(0.01 * (1 + 0.005 + 2 * 10 / 500_000), rel=0.001)    # impact + extra slippage
    assert p.size == pytest.approx(3.0 / (0.01 * 0.30 * 1.05))          # 1% risk / 30% stop, 5% slippage margin
    assert p.stop_px == pytest.approx(p.entry_px * 0.7)
    assert p.risk_usd() < 3.0
    assert p.k == pytest.approx(p.size / 5_000_000)
    assert rig.st.realized == pytest.approx(-p.size * p.entry_px * 0.01, rel=0.01)     # 1% swap fee
    assert rig.st.lags_ms and rig.st.lags_ms[0] >= 1000


def test_price_impact_grows_with_size_and_shrinks_with_liquidity():
    b = PaperBroker(config.Sol())
    deep = b.market(True, 1000, 1.0, 1_000_000.0)
    thin = b.market(True, 1000, 1.0, 20_000.0)
    assert thin.px > deep.px > 1.0 and thin.impact_pct == pytest.approx(10.0)
    sell = b.market(False, 1000, 1.0, 1_000_000.0)
    assert sell.px < 1.0 and sell.fee == pytest.approx(sell.px * 1000 * 0.01)
    with pytest.raises(NoQuote):
        b.market(True, 10, None, 0.0)
    fb = b.market(False, 10, None, 0.0, exit=True, last_px=2.0)
    assert fb.source == "fallback" and fb.px == pytest.approx(1.4)


def test_add_follows_the_leader_proportionally(rig):
    rig.c.min_notional_usd = 1.0
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    p = rig.st.positions[TOK]
    size0, k = p.size, p.k
    rig.swap(LEADER, "buy", TOK, 2_500_000)                   # leader adds 50% of its position
    assert rig.st.positions[TOK].size == pytest.approx(size0 + k * 2_500_000, rel=0.01)


def test_leader_partial_sell_reduces_to_the_same_fraction_and_full_sell_closes(rig):
    rig.c.min_notional_usd = 1.0
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    size0 = rig.st.positions[TOK].size
    rig.swap(LEADER, "sell", TOK, 2_000_000)                  # 40% of the leader's balance
    assert rig.st.positions[TOK].size == pytest.approx(size0 * 0.6, rel=0.001)
    rig.price(TOK, 0.02)                                      # up 2x
    rig.swap(LEADER, "sell", TOK, 3_000_000)
    assert TOK not in rig.st.positions
    t = rig.st.closed[-1]
    assert t["reason"] == "leader_close" and t["pnl"] > 0 and t["sym"] == "MEME"
    assert t["exit"] == pytest.approx(0.02 * (1 - 0.005 - 0.0004), rel=0.02)
    assert ("closed", {"token": TOK, "trade": t}) in rig.events


def test_a_trim_too_small_to_trade_is_caught_up_by_the_next_one(rig):
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    size0 = rig.st.positions[TOK].size
    rig.swap(LEADER, "sell", TOK, 1_000_000)                  # 20% of ~$9.5 = $1.9: under the $5 minimum
    assert rig.st.positions[TOK].size == size0 and skips(rig)[0].startswith("below_min_notional:$1.9")
    rig.swap(LEADER, "sell", TOK, 1_500_000)                  # the leader now holds 50%: our target is 50%
    assert TOK not in rig.st.positions                        # ... which would leave < $5: closed, never dust
    assert rig.st.closed[-1]["reason"] == "leader_reduce"


def test_stop_closes_when_the_price_falls_through_it(rig):
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    rig.price(TOK, 0.0075)                                    # -25%: stop is 30% under the fill
    rig.trader.check_stops()
    assert TOK in rig.st.positions
    rig.price(TOK, 0.0060)                                    # -40%
    rig.trader.check_stops()
    assert TOK not in rig.st.positions and rig.st.closed[-1]["reason"] == "stop" and rig.st.closed[-1]["pnl"] < 0


# ---- the gate refuses entries, never exits ---------------------------------------------------------------
def test_entries_fail_closed(rig):
    rig.price(TOK2, 2.0, liq=10_000.0, sym="TWO")
    rig.swap(LEADER, "buy", TOK2, 100)
    rig.dex.set("NOLIQ", 1.0, None, "NOL")
    rig.swap(LEADER, "buy", "NOLIQ", 100, usd=100.0)
    rig.swap(LEADER, "buy", TOK, 1_000_000, age_s=120)       # a swap older than 30 s
    assert skips(rig)[:3] == ["illiquid:$10,000", "liquidity_unknown", "leader_swap_too_old:120s"]
    assert not rig.st.positions
    rig.healthy = False                                       # prices/feed/auth in doubt
    rig.dex.set("FRESH", 1.0, 500_000.0, "FR")
    rig.swap(LEADER, "buy", "FRESH", 100, usd=100.0)
    assert skips(rig)[-1] == "fomo_session_in_doubt"
    assert not rig.st.positions


def test_paused_entries_unfollowed_and_paused_leaders_are_refused(rig):
    rig.rec({"ev": "pause", "reason": "test"})
    rig.swap(LEADER, "buy", TOK, 1_000_000)
    rig.rec({"ev": "resume"})
    rig.rec({"ev": "leader_pause", "leader": LEADER, "reason": "x"})
    rig.swap(LEADER, "buy", TOK2, 1_000_000)
    rig.swap("Stranger", "buy", TOK, 1_000_000)
    assert skips(rig) == ["entries_paused:test", "leader_paused", "leader_not_followed"]


def test_loss_limit_position_cap_and_one_holder_per_token(rig):
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    rig.swap(OTHER, "buy", TOK, 5_000_000)
    assert skips(rig) == ["token_held_for_other_leader"]
    rig.rec({"ev": "mark", "kind": "day", "key": "d2", "equity": 330.0})     # we are 9% below today's start
    rig.swap(LEADER, "buy", TOK2, 1_000_000)
    assert skips(rig)[-1] == "day_loss_limit"


def test_max_positions(rig):
    rig.c.max_positions = 2
    for i in range(3):
        rig.dex.set(f"T{i}", 1.0, 500_000.0, f"S{i}")
        rig.swap(LEADER, "buy", f"T{i}", 100, usd=100.0)
    assert len(rig.st.positions) == 2 and skips(rig) == ["max_positions"]


def test_exits_still_work_when_everything_is_in_doubt(rig):
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    rig.healthy = False
    rig.rec({"ev": "pause", "reason": "paused"})
    rig.dex.down = True                                       # no price source at all
    rig.prices.q.clear()
    rig.trader.last_px[TOK] = 0.01
    rig.swap(LEADER, "sell", TOK, 5_000_000)
    assert TOK not in rig.st.positions
    t = rig.st.closed[-1]
    assert t["exit"] == pytest.approx(0.01 * 0.7) and t["pnl"] < 0         # the documented fallback fill


def test_sell_of_an_unknown_balance_closes_our_copy_and_is_skipped_if_we_hold_nothing(rig):
    rig.swap(LEADER, "sell", TOK, 1000)                      # we hold nothing: nothing to do
    assert skips(rig) == ["no_copy_for_close"]
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    rig.det.bal.pop((LEADER, TOK))                           # lost track of the balance
    rig.swap(LEADER, "sell", TOK, 10)
    assert TOK not in rig.st.positions


def test_reconcile_closes_a_copy_the_leader_no_longer_holds(rig):
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    rig.det.bal[(LEADER, TOK)] = 0.0                         # the sell was never seen
    rig.trader.reconcile(LEADER)
    assert TOK not in rig.st.positions and rig.st.closed[-1]["reason"] == "reconcile_leader_flat"
    assert rig.st.counters["reconcile_exits"] == 1


def test_leader_is_paused_after_a_losing_streak(rig):
    for i in range(4):
        rig.price(TOK, 0.01)
        rig.swap(LEADER, "buy", TOK, 5_000_000)
        rig.price(TOK, 0.008)
        rig.swap(LEADER, "sell", TOK, 5_000_000)
    assert LEADER in rig.st.paused_leaders        # copy drawdown (or the 4-loss streak) pauses it
    assert ("copy drawdown" in rig.st.paused_leaders[LEADER] or "consecutive" in rig.st.paused_leaders[LEADER])
    assert any(k == "leader_paused" for k, _ in rig.events)


def test_order_rate_limit_applies_to_entries_only(rig):
    rig.c.max_orders_per_min = 1
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    rig.swap(LEADER, "buy", TOK2, 100, usd=200.0)
    assert skips(rig) == ["order_rate_limit"]
    rig.swap(LEADER, "sell", TOK, 5_000_000)
    assert TOK not in rig.st.positions


# ---- the detector --------------------------------------------------------------------------------------------
def test_detector_classifies_moves_and_dedupes():
    d = Detector()
    legs = [Leg("1", 1, "T", "buy", 100, 10.0), Leg("2", 2, "T", "buy", 50, 6.0), Leg("3", 3, "T", "sell", 50, 7.0),
            Leg("4", 4, "T", "sell", 100, 14.0), Leg("5", 5, "X", "sell", 1, 1.0)]
    kinds = [(m.kind, m.start, m.end) for m in d.on_legs("L", legs)]
    assert kinds == [("open", 0, 100), ("add", 100, 150), ("reduce", 150, 100), ("close", 100, 0), ("close", 0, 0)]
    assert d.on_legs("L", legs) == []                                       # same legs again: nothing new


def test_detector_seed_folds_history_and_returns_only_later_legs():
    d = Detector()
    legs = [Leg("1", 10, "T", "buy", 100, 10.0), Leg("2", 20, "T", "sell", 40, 5.0), Leg("3", 30, "T", "sell", 60, 9.0)]
    later = d.seed("L", legs, cursor_ms=20)
    assert d.balance("L", "T") == 60 and [g.id for g in later] == ["3"]
    assert [m.kind for m in d.on_legs("L", later)] == ["close"]


# ---- restart safety -----------------------------------------------------------------------------------------------
def test_restart_restores_positions_stops_and_cursors_without_double_opening(rig, tmp_path):
    rig.swap(LEADER, "buy", TOK, 5_000_000)
    rig.rec({"ev": "cursor", "leader": LEADER, "t": 12345})
    before = {k: (p.size, p.entry_px, p.stop_px, p.sym, p.leader) for k, p in rig.st.positions.items()}
    realized = rig.st.realized
    rig.ledger.close()                                                       # "kill -9": no clean shutdown logic
    rig.boot()
    assert not rig.st.uncertain
    assert {k: (p.size, p.entry_px, p.stop_px, p.sym, p.leader) for k, p in rig.st.positions.items()} == before
    assert rig.st.realized == pytest.approx(realized) and rig.st.cursors[LEADER] == 12345
    # the leader's buy is replayed from history after the restart: its id is already folded, so no second open
    legs = [Leg("legX", now_ms() - 1000, TOK, "buy", 5_000_000, 50_000.0)]
    rig.det.seed(LEADER, legs, cursor_ms=now_ms())
    assert rig.det.on_legs(LEADER, legs) == []
    rig.price(TOK, 0.001)                                                    # the stop survives the restart
    rig.trader.check_stops()
    assert TOK not in rig.st.positions and rig.st.closed[-1]["reason"] == "stop"


def test_a_stopless_position_in_the_ledger_is_flagged_uncertain(rig):
    from dataclasses import asdict
    from copybot.ledger import Position
    p = Position(pos_id="x", coin=TOK, side=1, size=10, entry_px=1.0, stop_px=0.0, leverage=1.0, leader=LEADER,
                 k=1.0, open_oid=0, opened_ms=1, sym="M")
    rig.rec({"ev": "open", "pos": asdict(p), "fee": 0.1})
    rig.ledger.close()
    rig.boot()
    assert any("no valid stop" in u for u in rig.st.uncertain)
