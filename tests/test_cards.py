"""Card rendering (pure functions in copybot/cardfmt.py)."""
from copybot import cardfmt
from copybot.ledger import Position, State


def test_trade_card_has_every_field():
    p = Position(pos_id="x", coin="BTC", side=1, size=0.001, entry_px=100_000, stop_px=97_000, leverage=9,
                 leader="0x1234567890abcdef1234567890abcdef12345678", k=1, open_oid=1, opened_ms=0,
                 realized=-0.05, entry_notional=100, open_lag_ms=1234)
    s = cardfmt.trade_card(p, 101_000, 1_790_000_000_000)
    # one P&L after fees (+1.00 price - 0.05 fees), green because it makes money; side shown with an arrow
    for needle in ("🟢 <b>BTC</b> ⬆️ LONG · +0.95$ (+0.95%)", "100,000.0 → 101,000.0", "Value: <b>101.00$ · your margin 11.22$ (9x)",
                   "97,000.0 · 4.0% away · −3.09$ if hit", "when 0x1234…5678 exits", "copied in 1.2s", "updated"):
        assert needle in s, needle
    assert "uPnL" not in s and "rPnL" not in s and "<pre>" not in s
    assert cardfmt.trade_card(p, 99_000, 0).startswith("🔴")       # losing money: red
    t = {"coin": "BTC", "side": -1, "entry": 100.0, "exit": 90.0, "pnl": 9.9, "fees": 0.1, "notional": 100.0,
         "reason": "leader_close", "opened_ms": 0, "closed_ms": 3_600_000, "leader": p.leader}
    c = cardfmt.closed_card(t)
    assert "✅ WIN" in c and "⬇️ SHORT" in c and "+9.90$" in c and "+9.90%" in c and "the trader closed" in c
    assert "❌ LOSS" in cardfmt.closed_card({**t, "pnl": -1.0})


def test_progress_render():
    st = State(equity0=300, btc_px0=100_000, genesis_ms=0)
    st.followed = {"invo:bob": 0}
    assert "too early" in cardfmt.progress_text(st, {}, None, 0)


def test_trades_and_traders_cards():
    a, b = "invo:alice", "invo:bob"
    st = State(equity0=300, btc_px0=100_000, genesis_ms=0)
    st.followed = {a: 0, b: 0}
    st.positions["BTC"] = Position(pos_id="BTC-1", coin="BTC", side=1, size=0.001, entry_px=100_000.0,
                                   stop_px=97_000.0, leverage=5, leader=a, k=0.001, open_oid=1, opened_ms=0,
                                   realized=-0.05, entry_notional=100.0, backers={b: {"lpos": 1, "oid": 2, "frac": 0}})
    st.closed = [{"pos_id": "ETH-1", "coin": "ETH", "side": 1, "leader": a, "pnl": 2.0},
                 {"pos_id": "SOL-1", "coin": "SOL", "side": -1, "leader": a, "pnl": -0.5}]
    st.realized = 1.5 - 0.05
    mids = {"BTC": 102_000.0}
    t = cardfmt.trades_card(st, mids, 3_600_000)
    assert "💼 <b>Trades</b> · 1 open" in t and "🟢 <b>BTC</b> ⬆️ LONG · +1.95$" in t and "🤝 +1 agree" in t
    assert "🟢 <b>Total: +3.45$ (+1.15%)</b>" in t        # wallet: 1.45 realized + 2.00 open, vs $300
    assert "2 trades (1 won, 1 lost)" in t and "1h00m" in t
    assert "No open trades" in cardfmt.trades_card(State(equity0=300), {}, 0)
    tr = cardfmt.traders_card(st, mids, 3_600_000, extra={a: [("✅ Main", "80% win")]})
    assert "🧾 <b>Invo traders</b> · 2 followed" in tr and "<code>@alice</code>" in tr and "Main: <b>80% win" in tr
    assert "2 trades · 1 won, 1 lost (50%)" in tr and "backs 1" in tr
    assert tr.index("@alice") < tr.index("@bob")         # in the order they were followed
    assert "Made for you: <b>🟢 +3.45$" in tr             # 1.50 closed + 1.95 open
    assert "🟢 <b>Copying them made: +3.45$" in tr
    assert "None followed" in cardfmt.traders_card(State(equity0=300), {}, 0)


def test_invo_trade_card_is_short_and_shows_leverage():
    p = Position(pos_id="x", coin="BTC", side=1, size=0.001, entry_px=100_000, stop_px=91_700, leverage=10,
                 leader="invo:bob", k=1, open_oid=1, opened_ms=0, realized=-0.05, entry_notional=100)
    s = cardfmt.invo_trade_card(p, 101_000, 1_790_000_000_000)
    for needle in ("🟢 <b>BTC</b> ⬆️ LONG · 10x · +0.95$ (+0.95%)", "Price: <b>100,000.0 → 101,000.0",
                   "Size: <b>101.00$ (margin 10.10$)", "Exit: <b>with @bob", "Stop: <b>91,700.0 (9.2% away", "Open for"):
        assert needle in s, needle
    assert "Take profit" not in s and "<pre>" not in s


def test_invo_trades_card_uses_the_short_layout_per_copy():
    p = Position(pos_id="x", coin="BTC", side=1, size=0.001, entry_px=100_000, stop_px=91_700, leverage=10,
                 leader="invo:bob", k=1, open_oid=1, opened_ms=0, entry_notional=100)
    st = State(equity0=300, btc_px0=100_000, genesis_ms=0)
    st.positions["BTC"] = p
    s = cardfmt.trades_card(st, {"BTC": 101_000}, 3_600_000, title="🧾 <b>Invo trades</b>",
                            infos={"x": "🧾 <b>@bob</b> · 1.0% x 40x\n⏱️ In 3.3s after them · same price as theirs\n🎯 target 1"})
    for needle in ("🧾 <b>Invo trades</b> · 1 open", "LONG · 10x", "Size: <b>101.00$ (margin 10.10$)", "⏱️ In 3.3s"):
        assert needle in s, needle
    assert "Take profit" not in s and "🎯" not in s
