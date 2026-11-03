"""The live wallet (copybot/live.py) against a loopback Hyperliquid (tests/fakes_live.py) that checks every signature:
orders, partial fills, the stop resting on the exchange, reconcile, the floor, /livekill, and the bot end to end."""
import pytest
from eth_account import Account

from copybot import config, live, log
from copybot.ledger import now_ms
from copybot.positions import Move
from copybot.risk import Health
from copybot.wallets import SideWallet
from tests.fakes_live import FakeExchange
from tests.test_e2e import env  # noqa: F401 (fixture)
from tests.test_invo import invo_env  # noqa: F401 (fixture)

ACCOUNT = "0x" + "ab" * 20
AGENT = Account.create()                       # a throwaway API wallet made for the tests
LEADER = "invo:nicush"


def healthy():
    return Health(now_ms=now_ms(), mids_age_s=0.1, clock_ok=True, clock_offset_ms=0, feed_age_s=0.5,
                  feed_connected=True)


class Desk:
    """A live wallet, its broker and its desk on the fake exchange (the bot wires the same parts: runner.live_setup)."""

    def __init__(self, tmp_path, fx: FakeExchange, key=None, **live_cfg):
        self.fx = fx
        cfg = config.load("config", env={})
        for k, v in live_cfg.items():
            setattr(cfg.live, k, v)
        self.ex = live.Exchange("testnet", ACCOUNT, key or AGENT.key.hex(), base=fx.base)
        self.broker = live.LiveBroker(self.ex, cfg.live, cfg.broker.taker_fee_pct)
        self.said: list[str] = []
        self.desk = live.Desk(self.ex, cfg.live, self.broker, lambda t, k, e=0: self.said.append(t), {})
        self.w = SideWallet(cfg, 5.0, tmp_path, self.broker, healthy, self.desk.mids, self.desk.assets, name="live",
                            label="LIVE", margin_pct=5.0, notify=self.desk.on_notify)
        r = self.w.cfg.risk
        r.stop_pct, r.liq_buffer_mult, r.max_entry_age_s = 8.0, 1.0, 60
        r.max_symbol_risk_pct = r.max_total_risk_pct
        for ev in ({"ev": "genesis", "equity0": 300.0, "btc_px0": 100_000}, {"ev": "follow", "leader": LEADER},
                   {"ev": "mark", "kind": "day", "key": "d", "equity": 300.0},
                   {"ev": "mark", "kind": "week", "key": "w", "equity": 300.0}):
            self.w.rec(ev)
        self.desk.attach(self.w, self.w)
        self.desk.boot()

    def call(self, coin="ETH", side=1, exposure=0.5, start=0.0):
        t = now_ms()
        return self.w.pm.on_move(Move(LEADER, coin, start, side * exposure, t, t, 7, (), self.fx.mids[coin], 1))

    def close(self, coin="ETH", side=1, exposure=0.5):
        t = now_ms()
        return self.w.pm.on_move(Move(LEADER, coin, side * exposure, 0.0, t, t, 7, (), self.fx.mids[coin], 1))


@pytest.fixture
def fx():
    f = FakeExchange(ACCOUNT, AGENT.address)
    yield f
    f.close()


def test_a_copy_is_a_signed_ioc_order_at_isolated_leverage_with_its_stop_resting_on_the_exchange(tmp_path, fx):
    d = Desk(tmp_path, fx)
    d.call()
    p = d.w.st.positions["ETH"]
    assert fx.positions["ETH"] == pytest.approx(p.size)                     # the ledger holds what the account holds
    assert p.size * p.entry_px == pytest.approx(300 * 0.05 * p.leverage, rel=0.02)   # 5% margin x the gate's leverage
    assert fx.leverage["ETH"] == (int(p.leverage), False)                   # isolated, at the gate's leverage
    [stop] = fx.stops("ETH")
    assert stop["reduceOnly"] and stop["side"] == "A" and float(stop["sz"]) == pytest.approx(p.size)
    assert float(stop["triggerPx"]) == pytest.approx(p.entry_px * 0.92, rel=1e-4)    # our 8% stop
    assert [a["type"] for a in fx.actions] == ["updateLeverage", "order", "order"]    # (the fake checked each signature)


def test_the_traders_close_sells_reduce_only_and_takes_the_stop_order_away(tmp_path, fx):
    d = Desk(tmp_path, fx)
    d.call()
    d.close()
    assert "ETH" not in d.w.st.positions and "ETH" not in fx.positions
    assert not fx.stops("ETH")
    assert d.w.st.closed[-1]["exit"] == pytest.approx(3000 * 0.9999)


def test_a_partial_fill_books_only_what_filled(tmp_path, fx):
    d = Desk(tmp_path, fx)
    fx.fill_ratio = 0.5
    d.call()
    p = d.w.st.positions["ETH"]
    assert p.size == pytest.approx(fx.positions["ETH"])
    assert float(fx.stops("ETH")[0]["sz"]) == pytest.approx(p.size)


def test_an_entry_that_finds_no_liquidity_is_skipped_and_nothing_is_booked(tmp_path, fx):
    d = Desk(tmp_path, fx)
    fx.no_fill = True
    assert d.call() == "no_book"
    assert not d.w.st.positions and not fx.positions and not d.w.st.open_intents


def test_one_coin_never_grows_past_the_live_position_cap(tmp_path, fx):
    d = Desk(tmp_path, fx, max_position_usd=50.0)
    assert d.call() == "no_book"                            # a ~150$ copy is over the 50$ cap
    assert not fx.positions


def test_a_stop_the_exchange_fired_is_booked_at_its_real_fill(tmp_path, fx):
    d = Desk(tmp_path, fx)
    d.call()
    trigger = float(fx.stops("ETH")[0]["triggerPx"])
    fx.mids["ETH"] = trigger
    fx.fire_stop("ETH")                                     # the bot was not looking: the exchange closed it
    d.desk.reconcile()
    assert "ETH" not in d.w.st.positions
    t = d.w.st.closed[-1]
    assert t["reason"] == "exchange_closed" and t["exit"] == pytest.approx(trigger)
    assert not d.w.st.entries_paused


def test_a_position_the_bot_did_not_open_pauses_live_entries(tmp_path, fx):
    d = Desk(tmp_path, fx)
    fx.positions["SOL"] = 2.0                               # someone traded the account by hand
    d.desk.reconcile()
    assert d.w.st.entries_paused and d.w.st.pause_reason == "live_mismatch"
    assert any("does not match" in t and "SOL" in t for t in d.said)
    d.desk.resume()
    d.desk.reconcile()                                      # still different: paused again
    assert d.w.st.entries_paused
    fx.positions.pop("SOL")
    d.desk.reconcile()
    d.desk.resume()
    d.desk.reconcile()
    assert not d.w.st.entries_paused and any("matches" in t for t in d.said)


def test_an_exit_the_exchange_refuses_keeps_the_position_and_its_stop(tmp_path, fx):
    d = Desk(tmp_path, fx)
    d.call()
    fx.down = True
    with pytest.raises(live.LiveError):
        d.close()
    assert "ETH" in d.w.st.positions and fx.stops("ETH")    # still held, still protected on the exchange
    fx.down = False
    d.close()
    assert "ETH" not in d.w.st.positions and "ETH" not in fx.positions


def test_the_floor_pauses_new_entries_once(tmp_path, fx):
    d = Desk(tmp_path, fx, floor_usd=299.0)
    d.call()
    fx.mids["ETH"] = 2900.0                                 # the open copy is down > 1$
    d.desk.mids["ETH"] = 2900.0
    d.desk.check_floor()
    assert d.w.st.entries_paused and d.w.st.pause_reason == "live_floor"
    assert any("floor" in t for t in d.said)
    d.desk.resume()
    d.desk.check_floor()                                    # the owner resumed below the floor: it holds
    assert not d.w.st.entries_paused


def test_livekill_closes_the_ledger_and_the_account_and_cancels_every_order(tmp_path, fx):
    d = Desk(tmp_path, fx)
    d.call()
    fx.positions["SOL"] = -1.0                              # something the bot did not open, too
    text = d.desk.kill()
    assert not d.w.st.positions and not fx.positions and not fx.orders
    assert d.w.st.entries_paused and "LIVE kill" in text


def test_the_main_wallet_key_and_an_unapproved_api_wallet_are_refused(tmp_path, fx):
    main = Account.create()
    with pytest.raises(live.LiveError, match="MAIN wallet"):
        live.Exchange("testnet", main.address.lower(), main.key.hex(), base=fx.base)
    with pytest.raises(live.LiveError, match="not approved"):
        Desk(tmp_path, fx, key=Account.create().key.hex())


def test_credentials_come_from_the_environment_and_the_key_is_redacted():
    assert "must both be set" in live.credentials({})
    assert "not an address" in live.credentials({live.ENV_ACCOUNT: "nope", live.ENV_KEY: "0x1234"})
    key = AGENT.key.hex()
    acct, k = live.credentials({live.ENV_ACCOUNT: ACCOUNT.upper().replace("0X", "0x"), live.ENV_KEY: key})
    assert acct == ACCOUNT and k == key
    assert key.removeprefix("0x") not in log.redact(f"key={key}")


def test_prices_follow_hyperliquids_tick_rules():
    assert live.px_wire(3012.3456, 4) == 3012.3            # 5 significant figures
    assert live.px_wire(0.123456789, 0) == 0.12346         # 6 - szDecimals decimals
    assert live.px_wire(123456.7, 5) == 123457.0           # whole numbers are always allowed


# ---- the bot end to end --------------------------------------------------------------------------------
@pytest.mark.parametrize("layout", ["", "margin_allocations = true\nmain_stop_pct = 0.0\n"],
                         ids=["risk_sizes", "owner_config_margin"])
def test_the_bot_copies_an_invo_call_with_real_orders_next_to_its_paper_twin(invo_env, monkeypatch, layout):
    from tests.test_e2e import wait_for, stop_bot
    from tests.test_invo import start_with_invo
    hl, dc, data, cdir, f, tok = invo_env
    (cdir / "invo.toml").write_text((cdir / "invo.toml").read_text(encoding="utf-8") + layout, encoding="utf-8")
    fx = FakeExchange(ACCOUNT, AGENT.address)
    try:
        (cdir / "live.toml").write_text(f'enabled = true\napi_base = "{fx.base}"\nreconcile_s = 5.0\n',
                                        encoding="utf-8")
        monkeypatch.setenv(live.ENV_ACCOUNT, ACCOUNT)
        monkeypatch.setenv(live.ENV_KEY, AGENT.key.hex())
        pid = f.add_user("nicush")[0]
        bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
        try:
            assert bot.live is not None and bot.live.base_name == "invo_risk_5pct"
            assert bot.live.cfg.risk.daily_loss_pct == 15 and bot.live.cfg.risk.stop_pct == 8
            assert wait_for(lambda: any("paper + LIVE testnet" in m["text"] for m in dc.sent))
            assert len(bot.invo_paper_wallets()) == 18 and bot.invo_wallets()[-1] is bot.live
            dc.say("/follow nicush")
            assert wait_for(lambda: LEADER in bot.live.st.followed)
            assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
            twin = next(w for w in bot.invo_wallets() if w.name == "invo_risk_5pct_stop8")
            cid = f.open_call(pid, "ETH", long=True, leverage=10, size=0.05, entry=2990.0)
            assert wait_for(lambda: "ETH" in bot.live.st.positions and "ETH" in twin.st.positions, timeout=10)
            p, q = bot.live.st.positions["ETH"], twin.st.positions["ETH"]
            assert p.size * p.entry_px == pytest.approx(q.size * q.entry_px, rel=0.05)    # the twin's size
            assert fx.positions["ETH"] == pytest.approx(p.size)
            assert wait_for(lambda: fx.stops("ETH"))                # (placed right after the fill is booked)
            dc.say("/livestatus")
            assert wait_for(lambda: any("Live wallet" in m["text"] and "stop on the exchange" in m["text"]
                                        for m in dc.sent))
            f.close_call(pid, cid)
            assert wait_for(lambda: "ETH" not in bot.live.st.positions and "ETH" not in fx.positions, timeout=10)
            dc.say("/livekill wrong")
            assert wait_for(lambda: any("Wrong or missing PIN" in m["text"] for m in dc.sent))
            from tests.test_e2e import PIN
            dc.say(f"/livekill {PIN}")
            assert wait_for(lambda: bot.live.st.entries_paused)
            logs = "".join(p.read_text(encoding="utf-8") for p in (data / "logs").glob("*.log*"))
            assert AGENT.key.hex().removeprefix("0x") not in logs + " ".join(m["text"] for m in dc.sent)
        finally:
            stop_bot(bot, th)
    finally:
        fx.close()


def test_without_the_keys_live_stays_off_and_says_why(invo_env, monkeypatch):
    from tests.test_e2e import wait_for, stop_bot
    from tests.test_invo import start_with_invo
    hl, dc, data, cdir, f, tok = invo_env
    (cdir / "live.toml").write_text("enabled = true\n", encoding="utf-8")
    monkeypatch.delenv(live.ENV_KEY, raising=False)
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert bot.live is None
        assert wait_for(lambda: any("LIVE is off" in m["text"] and live.ENV_KEY in m["text"] for m in dc.sent))
        dc.say("/livestatus")
        assert wait_for(lambda: any("Live trading is off" in m["text"] for m in dc.sent))
    finally:
        stop_bot(bot, th)
