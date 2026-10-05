"""Paper broker: simulated market (taker) fills against the REAL current order book.

No signing library, no keys, no order endpoint: this module only reads books.
"""
from __future__ import annotations

from dataclasses import dataclass

from copybot.config import Broker as BrokerCfg
from copybot.hl import Book


@dataclass
class PaperFill:
    px: float          # average fill price incl. slippage
    size: float
    fee: float
    slippage_bps: float
    source: str        # "book" | "mid_fallback"


class NoBook(Exception):
    pass


class PaperBroker:
    def __init__(self, cfg: BrokerCfg, get_book):
        """get_book(coin) -> Book, raising on failure/timeout (the caller enforces the hard timeout)."""
        self.cfg = cfg
        self.get_book = get_book

    def walk(self, book: Book, is_buy: bool, size: float) -> float:
        levels = book.asks if is_buy else book.bids
        left, cost = size, 0.0
        for px, sz in levels:
            take = min(left, sz)
            cost += take * px
            left -= take
            if left <= 1e-15:
                break
        if left > 1e-15:  # deeper than the visible book: price the rest 1% beyond the last level
            last = levels[-1][0]
            cost += left * last * (1.01 if is_buy else 0.99)
        return cost / size

    def market(self, coin: str, is_buy: bool, size: float, exit: bool = False, mid: float | None = None) -> PaperFill:
        """Entries need a real book (else NoBook). Exits never fail: without a book they fill at mid +/- a
        penalty slippage."""
        try:
            book = self.get_book(coin)
            ref = book.mid
            px = self.walk(book, is_buy, size)
            source = "book"
            px *= 1 + (self.cfg.extra_slippage_bps / 1e4) * (1 if is_buy else -1)
        except Exception as e:
            if not exit or not mid:
                raise NoBook(f"{coin}: {e}") from None
            ref = mid
            px = mid * (1 + (self.cfg.no_book_slippage_bps / 1e4) * (1 if is_buy else -1))
            source = "mid_fallback"
        fee = px * size * self.cfg.taker_fee_pct / 100
        slip = (px - ref) / ref * 1e4 * (1 if is_buy else -1)
        return PaperFill(px, size, fee, slip, source)


def funding_payment(side: int, size: float, mark: float, rate: float) -> float:
    """Hourly funding received (+) or paid (-). Positive rate: longs pay shorts."""
    return -side * size * mark * rate
