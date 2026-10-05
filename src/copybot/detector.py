"""Leader fill detector: turns websocket userFills into leader position moves.

The leader's position before/after comes from the fill's startPosition (+ signed size), not from guessing.
Fills of one coin that arrive together are aggregated into ONE move (a market order that sweeps 10 levels
is one trade, not ten). Duplicate fills (same tid) are dropped; websocket snapshots never trigger trades.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass, field

from copybot.hl import Fill, is_core_perp

EPS = 1e-12


@dataclass
class Move:
    leader: str
    coin: str
    start_pos: float
    end_pos: float
    time_ms: int          # last fill time (exchange clock)
    first_time_ms: int
    oid: int              # order id of the first fill
    oids: tuple
    px: float             # leader's vwap
    n_fills: int
    tids: tuple = field(default=(), repr=False)

    @property
    def kind(self) -> str:
        s, e = self.start_pos, self.end_pos
        if abs(s) < EPS and abs(e) < EPS:
            return "none"
        if abs(s) < EPS:
            return "open"
        if abs(e) < EPS:
            return "close"
        if (s > 0) != (e > 0):
            return "flip"
        if abs(e) > abs(s) + EPS:
            return "add"
        if abs(e) < abs(s) - EPS:
            return "reduce"
        return "none"

    @property
    def new_side(self) -> int:
        return 1 if self.end_pos > 0 else -1 if self.end_pos < 0 else 0


class Detector:
    def __init__(self, max_seen: int = 200_000):
        self.seen: collections.OrderedDict[int, None] = collections.OrderedDict()
        self.max_seen = max_seen

    def _new(self, f: Fill) -> bool:
        if f.tid in self.seen:
            return False
        self.seen[f.tid] = None
        if len(self.seen) > self.max_seen:
            self.seen.popitem(last=False)
        return True

    def on_fills(self, leader: str, fills, snapshot: bool = False) -> list[Move]:
        fresh = [f for f in fills if self._new(f)]
        if snapshot:
            return []  # history: remember tids, never trade on it
        moves: list[Move] = []
        groups: dict[str, list[Fill]] = {}
        order: list[str] = []
        for f in sorted(fresh, key=lambda x: (x.time, x.tid)):
            if not is_core_perp(f.coin):
                continue
            if f.coin not in groups:
                groups[f.coin] = []
                order.append(f.coin)
            groups[f.coin].append(f)
        for coin in order:
            fs = groups[coin]
            # one move per contiguous chain of positions; if the chain breaks (a fill whose start does not
            # continue the previous end), split there so nothing is double counted
            chain = [fs[0]]
            for f in fs[1:]:
                if abs(f.start_pos - chain[-1].end_pos) > 1e-9 * max(1.0, abs(f.start_pos)):
                    moves.append(_move(leader, chain))
                    chain = []
                chain.append(f)
            moves.append(_move(leader, chain))
        return [m for m in moves if m.kind != "none"]


def _move(leader: str, fs: list[Fill]) -> Move:
    qty = sum(f.sz for f in fs)
    return Move(leader=leader, coin=fs[0].coin, start_pos=fs[0].start_pos, end_pos=fs[-1].end_pos,
                time_ms=fs[-1].time, first_time_ms=fs[0].time, oid=fs[0].oid,
                oids=tuple(dict.fromkeys(f.oid for f in fs)), px=sum(f.px * f.sz for f in fs) / qty,
                n_fills=len(fs), tids=tuple(f.tid for f in fs))
