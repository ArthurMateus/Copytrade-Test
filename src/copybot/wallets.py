"""The Invo wallets: paper wallets that copy the same Invo calls at different sizes and stops (runner.Bot.invo_set).

Each has its own $start_equity, its own ledger (data/wallets/<name>/ledger.jsonl), State, RiskGate and
PositionManager, so it is restart-safe on its own. Every limit is scaled with its risk level (`config.scaled`).
An error in one never touches the others.
"""
from __future__ import annotations

from pathlib import Path

from copybot import config, log
from copybot.broker import PaperBroker
from copybot.ledger import Ledger, State
from copybot.positions import PositionManager
from copybot.risk import RiskGate


class SideWallet:
    def __init__(self, base: config.Config, risk_pct: float, data_dir: Path, broker: PaperBroker, health,
                 mids: dict, assets: dict, name: str, label: str, sizer=None, notify=None, cap=None,
                 pause_leaders: bool = False, margin_pct: float | None = None):
        """`sizer`: its own copy sizes (equity, price -> size); `risk_pct` then only sets the limits that clamp them.
        `cap`: equity -> the most notional one copy may hold (the trader-size wallet's invo.max_risk_pct)."""
        self.risk_pct = risk_pct
        self.name = name
        self.label = label
        self.cfg = config.scaled(base, risk_pct)
        path = Path(data_dir) / "wallets" / self.name
        path.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(path / "ledger.jsonl")
        self.st: State = self.ledger.replay()
        self.gate = RiskGate(self.cfg)
        manager, extra = PositionManager, {}
        if margin_pct is not None:
            from copybot.allocations import MarginPositionManager
            manager, extra = MarginPositionManager, {"margin_pct": margin_pct}
        self.pm = manager(self.cfg, self.st, self.ledger, self.gate, broker, health, mids, assets,
                                  pause_leaders=pause_leaders, sizer=sizer, cap=cap,
                                  **extra, **({"notify": notify} if notify else {}))

    def rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    def max_drop_pct(self, mids: dict) -> float:
        return max_drop_pct(self.st, mids)


def max_drop_pct(st: State, mids: dict) -> float:
    """Biggest fall from a peak of the wallet, over closed trades and the current value (percent)."""
    eq, peak, worst = st.equity0, st.equity0, 0.0
    for t in st.closed:
        eq += t["pnl"]
        peak = max(peak, eq)
        worst = max(worst, (peak - eq) / peak if peak > 0 else 0.0)
    now = st.equity(mids)
    peak = max(peak, now)
    worst = max(worst, (peak - now) / peak if peak > 0 else 0.0)
    return worst * 100


def boot_repair(st: State, rec, gate: RiskGate, name: str, assets: dict | None = None) -> list[str]:
    """Restart safety for one wallet: abort dangling intents, give every position a valid stop, and pause
    entries when anything was uncertain. Returns the uncertainties (to alert on)."""
    problems = list(st.uncertain)
    for iid in list(st.open_intents):
        rec({"ev": "intent_abort", "intent": iid})
    for p in list(st.positions.values()):  # never a stop-less position
        # a stop on the winning side of the entry is valid: the trailing stop moved it there to lock in profit
        # (code audit 2026-10-10 #3: it used to be reset to entry - 3% at every restart; same rule as Ledger.replay)
        bad = not (0 < p.stop_px < p.entry_px * 10)
        if bad:
            stop = gate.stop_for(p.side, p.entry_px, p.leverage, assets.get(p.coin) if assets else None)
            rec({"ev": "stop_set", "coin": p.coin, "pos_id": p.pos_id, "stop_px": stop})
            log.error("stop_repaired", wallet=name, coin=p.coin, stop=stop)
    if problems:
        st.uncertain = problems
        rec({"ev": "pause", "reason": "uncertain restart"})
        log.error("uncertain_restart", wallet=name, problems=" | ".join(problems))
    return problems
