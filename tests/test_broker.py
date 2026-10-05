import pytest

from copybot import config, hl
from copybot.broker import NoBook, PaperBroker, funding_payment
from tests.fakes import FakeHL, fixture

REAL_BOOK = hl.parse_book(fixture("l2book_btc.json"))
CFG = config.load("config", env={}).broker


def broker(book=REAL_BOOK):
    def get(coin):
        if book is None:
            raise TimeoutError("slow")
        return book
    return PaperBroker(CFG, get)


def test_buy_fills_at_best_ask_plus_slippage_and_taker_fee():
    f = broker().market("BTC", True, 0.001)
    best_ask = REAL_BOOK.asks[0][0]
    assert f.px == pytest.approx(best_ask * (1 + 1e-4))
    assert f.fee == pytest.approx(f.px * 0.001 * 0.00045)
    assert f.source == "book" and f.slippage_bps > 0


def test_sell_fills_at_best_bid():
    f = broker().market("BTC", False, 0.001)
    assert f.px == pytest.approx(REAL_BOOK.bids[0][0] * (1 - 1e-4))


def test_large_order_walks_the_book():
    total = sum(sz for _, sz in REAL_BOOK.asks[:3])
    f = broker().market("BTC", True, total)
    assert REAL_BOOK.asks[0][0] < f.px / (1 + 1e-4) <= REAL_BOOK.asks[2][0]


def test_entry_without_book_fails_but_exit_falls_back_to_mid():
    with pytest.raises(NoBook):
        broker(None).market("BTC", True, 0.001)
    f = broker(None).market("BTC", False, 0.001, exit=True, mid=100_000)
    assert f.source == "mid_fallback" and f.px == pytest.approx(100_000 * (1 - 0.002))


def test_with_fake_network_book():
    fake = FakeHL()
    try:
        info = hl.Info(fake.info_url, hl.RateBudget(1200, 300))
        b = PaperBroker(CFG, lambda c: info.book(c, 2.0))
        f = b.market("ETH", True, 0.1)
        assert f.px == pytest.approx(3000, rel=1e-3)
    finally:
        fake.close()


def test_funding_sign():
    assert funding_payment(+1, 1.0, 100.0, 0.0001) == pytest.approx(-0.01)   # long pays positive funding
    assert funding_payment(-1, 1.0, 100.0, 0.0001) == pytest.approx(0.01)
