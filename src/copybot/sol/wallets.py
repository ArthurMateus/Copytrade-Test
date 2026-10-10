"""Side wallets of the FOMO / Solana book (owner request 2026-10-09): the same leader moves as the main 1% wallet,
at other fixed risk levels (`sol.side_wallets_risk_pct`, default 2/5/10/20% of each wallet's own $300 at the 30%
stop). Like the Hyperliquid side wallets: own ledger (data/sol/wallets/<name>/), own SolGate whose limits all scale
with the level (allowed above the CEILINGS on purpose: paper comparison only), the main wallet's followed traders
and pauses, no trade cards, and an error in one never reaches the main wallet.
"""
from __future__ import annotations

import copy
from pathlib import Path

from copybot.config import Sol
from copybot.ledger import Ledger, State
from copybot.sol.market import PaperBroker
from copybot.sol.risk import SolGate
from copybot.sol.trader import Trader


def scaled(c: Sol, risk_pct: float) -> Sol:
    """A copy of the book's config at another risk per trade: every risk limit scales by the same factor (loss stops
    and position caps at most 100%); the stop distance, fees and entry filters do not change."""
    s = copy.deepcopy(c)
    f = risk_pct / c.risk_per_trade_pct
    s.risk_per_trade_pct = risk_pct
    s.max_position_pct = min(100.0, c.max_position_pct * f)
    s.max_total_risk_pct = min(100.0, c.max_total_risk_pct * f)
    s.daily_loss_pct = min(100.0, c.daily_loss_pct * f)
    s.weekly_loss_pct = min(100.0, c.weekly_loss_pct * f)
    s.side_wallets_risk_pct = []
    return s


class SolSide:
    def __init__(self, c: Sol, risk_pct: float, data_dir: Path, prices, health, det):
        self.risk_pct = risk_pct
        self.name = f"risk_{risk_pct:g}pct"
        self.label = f"{risk_pct:g}% risk"
        self.c = scaled(c, risk_pct)
        path = Path(data_dir) / "wallets" / self.name
        path.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(path / "ledger.jsonl")
        self.st: State = self.ledger.replay()
        self.gate = SolGate(self.c)
        self.trader = Trader(self.c, self.st, self.ledger, self.gate, PaperBroker(self.c), prices, health, det)

    def rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    def sync(self, main: State) -> None:
        """Follow exactly the main wallet's traders, with its pauses."""
        for a, t in main.followed.items():
            if a not in self.st.followed:
                self.rec({"ev": "follow", "leader": a, "ts": t})
        for a in list(self.st.followed):
            if a not in main.followed:
                self.rec({"ev": "unfollow", "leader": a, "reason": "dropped by the main FOMO wallet"})
        for a, why in main.paused_leaders.items():
            if a in self.st.followed and a not in self.st.paused_leaders:
                self.rec({"ev": "leader_pause", "leader": a, "reason": why})
        for a in list(self.st.paused_leaders):     # the owner lifted a pause (re-added it): lift it here too
            if a in main.followed and a not in main.paused_leaders:
                self.rec({"ev": "follow", "leader": a, "ts": main.followed[a]})
