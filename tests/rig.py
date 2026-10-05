"""Wires the REAL components together against the fake network (used by several test files)."""
from __future__ import annotations

from copybot import config, hl
from copybot.broker import PaperBroker
from copybot.detector import Detector
from copybot.ledger import Ledger, now_ms
from copybot.positions import PositionManager
from copybot.risk import Health, RiskGate
from tests.fakes import FakeHL, make_fill

LEADER = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"


class Rig:
    def __init__(self, tmp_path, equity=300.0):
        self.fake = FakeHL()
        self.cfg = config.load("config", env={})
        self.cfg.risk.start_equity = equity
        self.info = hl.Info(self.fake.info_url, hl.RateBudget(1200, 300))
        self.path = tmp_path / "ledger.jsonl"
        self.healthy = True
        self.events: list = []
        self.scores: dict[str, float] = {}   # leader -> wallet score 0-100 (for conflicts)
        self.alts: set[str] = set()           # diversified leaders (may be copied outside the main coins)
        self.boot()
        self.rec({"ev": "genesis", "equity0": equity, "btc_px0": 100_000})
        self.rec({"ev": "mark", "kind": "day", "key": "d", "equity": equity})
        self.rec({"ev": "mark", "kind": "week", "key": "w", "equity": equity})
        self.rec({"ev": "follow", "leader": LEADER})
        self.rec({"ev": "follow", "leader": OTHER})

    def boot(self):
        self.ledger = Ledger(self.path)
        self.st = self.ledger.replay()
        self.mids = dict(self.fake.mids)
        self.assets = self.info.meta()
        self.gate = RiskGate(self.cfg, alts_ok=lambda a: a in self.alts)
        self.broker = PaperBroker(self.cfg.broker, lambda c: self.info.book(c, 2.0))
        self.pm = PositionManager(self.cfg, self.st, self.ledger, self.gate, self.broker, self.health, self.mids,
                                  self.assets, notify=lambda kind, **kw: self.events.append((kind, kw)),
                                  score_of=lambda a: self.scores.get(a, 0.0))
        self.det = Detector()

    def health(self):
        if not self.healthy:
            return Health(now_ms=now_ms())
        return Health(now_ms=now_ms(), mids_age_s=0.1, clock_ok=True, clock_offset_ms=0, feed_age_s=0.5,
                      feed_connected=True)

    def rec(self, ev):
        self.st.apply(self.ledger.append(ev))

    def price(self, coin, px):
        self.fake.mids[coin] = px
        self.mids[coin] = px

    def leader_trades(self, leader, *fills, snapshot=False):
        """Leader fills arrive through the detector and the manager, like from the websocket."""
        self.fake.push_fills(leader, list(fills)) if not snapshot else None
        parsed = hl.parse_fills(list(fills))
        for m in self.det.on_fills(leader, parsed, snapshot=snapshot):
            self.pm.on_move(m)

    def lpos(self, leader, coin):
        return self.fake.positions.get(leader.lower(), {}).get(coin, 0.0)

    def fill(self, leader, coin, sz, side, oid=None, px=None):
        start = self.lpos(leader, coin)
        make_fill.oid = getattr(make_fill, "oid", 0) + 1
        return make_fill(coin, px or self.mids[coin], sz, side, start, oid=oid or make_fill.oid)

    def close(self):
        self.ledger.close()
        self.fake.close()
