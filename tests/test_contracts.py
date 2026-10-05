"""Data-contract tests over REAL recorded Hyperliquid responses (tests/fixtures, see tools/record_samples.py)."""
import time

import pytest

from copybot import hl
from tests.fakes import FakeHL, fixture, make_fill


def test_leaderboard_rows_parse():
    rows = hl.parse_leaderboard(fixture("leaderboard_sample.json"))
    assert len(rows) >= 200
    r = rows[0]
    assert r.address.startswith("0x") and r.address == r.address.lower()
    assert r.account_value > 0 and r.month.vlm > 0 and r.all_time.vlm >= r.month.vlm


def test_fills_parse_and_position_arithmetic():
    fills = hl.parse_fills(fixture("user_fills_full_page.json")["fills"])
    assert len(fills) == 2000
    assert fills == sorted(fills, key=lambda f: f.time)  # oldest first
    for f in fills:
        assert f.side in ("B", "A") and f.sz > 0 and f.px > 0 and f.tid > 0
    # dir agrees with startPosition/side for perps
    for f in fills:
        if not hl.is_core_perp(f.coin):
            continue
        if f.dir == "Open Long":
            assert f.side == "B" and f.start_pos >= 0
        if f.dir == "Open Short":
            assert f.side == "A" and f.start_pos <= 0
        if f.dir == "Close Long":
            assert f.side == "A" and f.start_pos > 0 and f.end_pos >= -1e-9
        if f.dir == "Close Short":
            assert f.side == "B" and f.start_pos < 0 and f.end_pos <= 1e-9
    assert any(not f.crossed for f in fills) or any(f.crossed for f in fills)


def test_spot_and_builder_coins_are_not_core_perps():
    small = hl.parse_fills(fixture("user_fills_by_time.json")["fills"])
    spot = [f for f in small if f.dir in ("Buy", "Sell")]
    assert spot and all(f.coin.startswith("@") for f in spot)
    assert all(not hl.is_core_perp(f.coin) for f in spot)
    big = hl.parse_fills(fixture("user_fills_full_page.json")["fills"])
    assert any(":" in f.coin for f in big)  # e.g. xyz:TSLA (HIP-3) is in real data
    for c in ("#140", "@107", "xyz:TSLA", "PURR/USDC"):
        assert not hl.is_core_perp(c)
    for c in ("BTC", "ETH", "kPEPE", "HYPE"):
        assert hl.is_core_perp(c)


def test_book_parse():
    b = hl.parse_book(fixture("l2book_btc.json"))
    assert b.bids[0][0] < b.asks[0][0]
    assert b.bids == sorted(b.bids, reverse=True) and b.asks == sorted(b.asks)
    assert b.time > 1_700_000_000_000
    with pytest.raises(ValueError):
        hl.parse_book(None)


def test_candles_parse():
    cs = hl.parse_candles(fixture("candle_snapshot_eth_1h.json"))
    assert len(cs) > 10
    assert all(c.l <= min(c.o, c.c) <= max(c.o, c.c) <= c.h for c in cs)
    assert all(b.t - a.t == 3_600_000 for a, b in zip(cs, cs[1:]))


def test_hash_coin_candles_answer_500():
    assert fixture("candle_snapshot_hash_coin_error.json")["status"] == 500


def test_clearinghouse_positions_parse():
    pos = hl.parse_positions(fixture("clearinghouse_state.json"))
    assert pos and all(isinstance(v, float) and v != 0 for v in pos.values())


def test_meta_parse():
    meta = hl.parse_meta(fixture("meta_and_asset_ctxs.json"))
    assert meta["BTC"].sz_decimals == 5 and meta["BTC"].max_leverage >= 10
    assert meta["BTC"].mark > 0
    assert any(a.delisted for a in meta.values())


def test_ws_messages_parse():
    msgs = fixture("ws_messages.json")
    evs = [hl.parse_ws(m) for m in msgs]
    kinds = {e.kind for e in evs}
    assert {"fills", "sub", "error", "pong"} <= kinds
    snaps = [e for e in evs if e.kind == "fills" and e.snapshot]
    live = [e for e in evs if e.kind == "fills" and not e.snapshot]
    assert snaps and live
    assert all(isinstance(f, hl.Fill) for e in live for f in e.fills)
    assert any("15 total users" in e.text for e in evs if e.kind == "error")


def test_fake_responses_match_real_shapes():
    """The fake's synthetic objects must parse with the same parsers as the recordings."""
    fake = FakeHL()
    try:
        f = make_fill("BTC", 100000, 0.01, "B", 0.0)
        real = fixture("user_fills_full_page.json")["fills"][0]
        assert set(f) == set(real) - {"cloid"}
        info = hl.Info(fake.info_url, hl.RateBudget(1200, 300))
        assert info.book("BTC", 2).mid == pytest.approx(100000, rel=1e-3)
        fake.positions["0xa"] = {"ETH": -2.0}
        assert info.positions("0xa", 2) == {"ETH": -2.0}
        assert "BTC" in info.meta()
    finally:
        fake.close()


def test_fills_paging_inclusive_dedupe_by_tid():
    fake = FakeHL()
    try:
        t0 = 1_700_000_000_000
        # 4500 fills, with several sharing the page-boundary timestamp
        fake.fills["0xu"] = [make_fill("BTC", 100, 0.1, "B", 0.0, t=t0 + (i // 3) * 1000, tid=i + 1)
                             for i in range(4500)]
        info = hl.Info(fake.info_url, hl.RateBudget(100000, 0))
        fills, complete = hl.fetch_fills_history(info, "0xu", t0, t0 + 10**10)
        assert complete
        assert len(fills) == 4500 and len({f.tid for f in fills}) == 4500
    finally:
        fake.close()


def test_429_backs_off_bulk_but_not_critical_forever():
    fake = FakeHL()
    try:
        budget = hl.RateBudget(1200, 300)
        info = hl.Info(fake.info_url, budget)
        fake.fail_429 = 1
        with pytest.raises(hl.HttpError):
            info.post({"type": "metaAndAssetCtxs"}, hl.BULK, 2)
        assert not budget.try_take(1, hl.BULK)  # bulk paused ~60 s
        time.sleep(2.1)
        assert info.book("BTC", 2)  # critical recovers after a short pause
    finally:
        fake.close()


def test_bulk_can_never_use_the_critical_reserve():
    b = hl.RateBudget(1200, 300)
    taken = 0
    while b.try_take(20, hl.BULK):
        taken += 20
    assert taken <= 900
    assert b.try_take(2, hl.CRITICAL)


def test_hard_timeout_on_slow_request():
    fake = FakeHL()
    try:
        fake.delay = 1.5
        info = hl.Info(fake.info_url, hl.RateBudget(1200, 300))
        t = time.monotonic()
        with pytest.raises(TimeoutError):
            info.book("BTC", 0.3)
        assert time.monotonic() - t < 0.6
    finally:
        fake.close()
