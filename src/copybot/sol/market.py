"""Prices and pool liquidity (DexScreener, free) and the paper broker that fills against them.

DexScreener: GET {dex}/tokens/v1/solana/{mint,mint,...} (up to 30) -> list of pairs with baseToken, priceUsd and,
for most pools, liquidity.usd. Bonding-curve pools can come back WITHOUT liquidity: liquidity is then unknown
(0.0) and the risk gate refuses to enter (fail closed). Exits still fill.

Paper fill model: price = mark +/- (price impact + extra slippage); price impact of a constant-product pool
with total liquidity L (USD, both sides) for a trade of N USD is about 2N/L. A swap fee is charged on top.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from copybot.config import Sol


@dataclass
class Quote:
    px: float
    liq_usd: float            # 0.0 = unknown
    symbol: str
    ts: float                 # time.time() when fetched


def parse_pairs(pairs: list) -> dict[str, Quote]:
    """Best pair (highest liquidity) per base token."""
    best: dict[str, tuple[float, Quote]] = {}
    now = time.time()
    for p in pairs or []:
        try:
            mint = p["baseToken"]["address"]
            px = float(p.get("priceUsd") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if px <= 0:
            continue
        liq = float(((p.get("liquidity") or {}).get("usd")) or 0.0)
        if mint not in best or liq > best[mint][0]:
            best[mint] = (liq, Quote(px, liq, str(p["baseToken"].get("symbol") or mint[:4]), now))
    return {m: q for m, (_, q) in best.items()}


class Prices:
    """Thread-safe quote cache. `refresh` is called from a worker; the trading loop only reads."""

    def __init__(self, dex_url: str):
        self.base = dex_url.rstrip("/")
        self.q: dict[str, Quote] = {}
        self.last_ok = 0.0
        self.lock = threading.Lock()

    def fetch(self, mints: list[str], timeout: float = 5.0) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        mints = sorted(set(mints))
        for i in range(0, len(mints), 30):
            chunk = ",".join(mints[i:i + 30])
            req = urllib.request.Request(f"{self.base}/tokens/v1/solana/{chunk}",
                                         headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (copybot paper)"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    out.update(parse_pairs(json.loads(r.read())))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
        if out:
            with self.lock:
                self.q.update(out)
                self.last_ok = time.time()
        return out

    def get(self, mint: str) -> Quote | None:
        with self.lock:
            return self.q.get(mint)

    def age_s(self, mint: str) -> float:
        q = self.get(mint)
        return time.time() - q.ts if q else 1e9

    def marks(self) -> dict[str, float]:
        with self.lock:
            return {m: q.px for m, q in self.q.items()}


@dataclass
class Fill:
    px: float
    fee: float                # USD
    impact_pct: float
    source: str               # "quote" | "fallback"


class NoQuote(Exception):
    pass


class PaperBroker:
    def __init__(self, cfg: Sol):
        self.cfg = cfg

    def impact_pct(self, notional: float, liq_usd: float) -> float:
        return min(50.0, 2.0 * notional / liq_usd * 100) if liq_usd > 0 else 0.0

    def market(self, buy: bool, size: float, mark: float | None, liq_usd: float, exit: bool = False,
               last_px: float = 0.0) -> Fill:
        """Fill `size` tokens. Entries need a mark; an exit with no mark fills at the last known price minus the
        configured penalty (an exit must always complete)."""
        c = self.cfg
        if not mark:
            if not exit or last_px <= 0:
                raise NoQuote("no price")
            px = last_px * (1 - c.exit_fallback_penalty_pct / 100)
            return Fill(px, size * px * c.swap_fee_pct / 100, c.exit_fallback_penalty_pct, "fallback")
        imp = self.impact_pct(size * mark, liq_usd)
        slip = (imp + c.extra_slippage_pct) / 100
        px = mark * (1 + slip) if buy else mark * (1 - slip)
        return Fill(px, size * px * c.swap_fee_pct / 100, imp, "quote")
