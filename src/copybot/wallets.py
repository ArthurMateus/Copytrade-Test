"""Side wallets: extra paper wallets that copy the SAME leader moves as the main wallet at another risk level.

Each side wallet has its own $start_equity, its own ledger (data/wallets/<name>/ledger.jsonl), State, RiskGate
and PositionManager, so it is restart-safe exactly like the main wallet. Every limit is scaled with the risk
level (`config.scaled`). They follow the main wallet's leaders (followed and paused sets are synced from it),
never pause leaders on their own, post no trade cards, and an error in one never touches the main wallet.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from copybot import config, log
from copybot.broker import PaperBroker
from copybot.ledger import Ledger, State
from copybot.positions import PositionManager
from copybot.risk import RiskGate


def wallet_name(risk_pct: float) -> str:
    return f"risk_{risk_pct:g}pct"


class SideWallet:
    def __init__(self, base: config.Config, risk_pct: float, data_dir: Path, broker: PaperBroker, health,
                 mids: dict, assets: dict, alts_ok: Callable[[str], bool], score_of: Callable[[str], float]):
        self.risk_pct = risk_pct
        self.name = wallet_name(risk_pct)
        self.cfg = config.scaled(base, risk_pct)
        path = Path(data_dir) / "wallets" / self.name
        path.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(path / "ledger.jsonl")
        self.st: State = self.ledger.replay()
        self.gate = RiskGate(self.cfg, alts_ok=alts_ok)
        self.pm = PositionManager(self.cfg, self.st, self.ledger, self.gate, broker, health, mids, assets,
                                  score_of=score_of, pause_leaders=False)

    def rec(self, ev: dict) -> dict:
        ev = self.ledger.append(ev)
        self.st.apply(ev)
        return ev

    def sync_leaders(self, main: State) -> None:
        """Follow exactly the main wallet's leaders, with its pauses."""
        for a in main.followed:
            if a not in self.st.followed:
                self.rec({"ev": "follow", "leader": a})
        for a in list(self.st.followed):
            if a not in main.followed:
                self.rec({"ev": "unfollow", "leader": a, "reason": "dropped by the main wallet"})
        for a, why in main.paused_leaders.items():
            if a in self.st.followed and a not in self.st.paused_leaders:
                self.rec({"ev": "leader_pause", "leader": a, "reason": why})

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


def boot_repair(st: State, rec, gate: RiskGate, name: str) -> list[str]:
    """Restart safety for one wallet: abort dangling intents, give every position a valid stop, and pause
    entries when anything was uncertain. Returns the uncertainties (to alert on)."""
    problems = list(st.uncertain)
    for iid in list(st.open_intents):
        rec({"ev": "intent_abort", "intent": iid})
    for p in list(st.positions.values()):  # never a stop-less position
        bad = p.stop_px <= 0 or (p.side > 0 and p.stop_px >= p.entry_px) or (p.side < 0 and p.stop_px <= p.entry_px)
        if bad:
            stop = gate.stop_for(p.side, p.entry_px)
            rec({"ev": "stop_set", "coin": p.coin, "pos_id": p.pos_id, "stop_px": stop})
            log.error("stop_repaired", wallet=name, coin=p.coin, stop=stop)
    if problems:
        st.uncertain = problems
        rec({"ev": "pause", "reason": "uncertain restart"})
        log.error("uncertain_restart", wallet=name, problems=" | ".join(problems))
    return problems
