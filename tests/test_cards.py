"""Card rendering (pure functions in copybot/cardfmt.py)."""
from copybot import cardfmt
from copybot.ledger import Position, State
from copybot.risk import Health


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


def test_status_leaders_progress_render():
    st = State(equity0=300, btc_px0=100_000, genesis_ms=0)
    st.followed = {"0x" + "a" * 40: 0}
    st.marks = {"day": {"key": "d", "equity": 300}}
    h = Health(now_ms=0, mids_age_s=0.2, clock_ok=True, feed_age_s=1, feed_connected=True)
    s = cardfmt.status_card(st, {}, h, 0, 101_000)
    assert "Wallet" in s and "300.00$" in s and "▶️ copying" in s and "+1.00%" in s
    st.entries_paused, st.pause_reason = True, "daily loss"
    assert "⏸️" in cardfmt.status_card(st, {}, h, 0, None)
    assert "⚪ – <code>0xaaaa…aaaa</code>" in cardfmt.leaders_card(st, {}, 0)
    assert "too early" in cardfmt.progress_text(st, {}, None, 0)


def test_trades_and_traders_cards():
    a, b = "0x" + "a" * 40, "0x" + "b" * 40
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
    tr = cardfmt.traders_card(st, mids, {a: 1}, {a: {"score": 94.5, "diversified": True, "win_rate": 0.77,
                                                    "profit_factor": 2.1}}, 3_600_000)
    assert "👥 <b>Traders</b> · 2 followed" in tr and "#1 · 94/100 🎲" in tr
    assert "2 trades · 1 won, 1 lost (50%)" in tr and "backs 1" in tr
    assert tr.index(a[-4:]) < tr.index(b[-4:])            # ranked before unranked
    assert "Made for you: <b>🟢 +3.45$" in tr             # 1.50 closed + 1.95 open
    assert "🟢 <b>Copying them made: +3.45$" in tr
    assert "None followed" in cardfmt.traders_card(State(equity0=300), {}, {}, {}, 0)


def test_traders_are_listed_by_rank_with_history_and_pf():
    a, b, c = ("0x" + x * 40 for x in "abc")
    st = State(equity0=300)
    st.followed = {a: 0, b: 0, c: 0}                      # followed in this order ...
    sc = {a: {"score": 60, "trades": 153, "win_rate": 0.77, "profit_factor": 2.5},
          b: {"score": 90, "trades": 40, "win_rate": 1.0, "profit_factor": 99.0}}
    tr = cardfmt.traders_card(st, {}, {b: 1, a: 2}, sc, 0)
    assert tr.index("bbbb") < tr.index("aaaa") < tr.index("cccc")   # ... listed by rank, unranked last
    assert "153 trades · 77% win" in tr and "2.50 (wins 2.50$ per 1$ lost)" in tr and "no losing trade" in tr
