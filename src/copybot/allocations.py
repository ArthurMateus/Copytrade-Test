"""Paper margin allocations: one net position, independently tracked trader contributions."""
from __future__ import annotations

import math
import uuid

from copybot.broker import NoBook
from copybot.positions import PositionManager, Move
from copybot.risk import Order
from copybot.ledger import State, now_ms


class MarginPositionManager(PositionManager):
    def __init__(self, *args, margin_pct: float, **kwargs):
        super().__init__(*args, **kwargs)
        self.margin_pct = margin_pct
        self.cap = None
        self.sizer = self.margin_size
        self.handover = True

    def margin_size(self, m, px, equity):
        asset = self.assets.get(m.coin)
        lev = self.gate.leverage_for(asset) if asset else 1
        return equity * self.margin_pct / 100 * lev / px if px > 0 else 0

    @staticmethod
    def share(size, exposure, oid=0):
        return dict(size=size, exposure=exposure, k=size / exposure if exposure > 0 else 0, oid=oid)

    def seed(self, p):
        if p.shares:
            return
        shares = {a: self.share(p.size * b["frac"], b["lpos"], b.get("oid", 0))
                  for a, b in p.backers.items()}
        own = max(0, p.size - sum(v["size"] for v in shares.values()))
        exposure = p.size / p.k if p.k > 0 else 1
        shares[p.leader] = self.share(own, exposure, p.open_oid)
        for s in shares.values():
            fraction = s["size"] / p.size
            s.update(entry_px=p.entry_px, realized=p.realized * fraction, fees=p.fees * fraction,
                     funding=p.funding * fraction, notional=p.entry_notional * fraction,
                     opened_ms=p.opened_ms)
        link = self.st.invo_links.get(p.pos_id, {})
        shares[p.leader].update({k: link[k] for k in ('call', 'portfolio', 'title', 'live', 'coin') if k in link})
        self.record_shares(p, shares)

    def record_shares(self, p, shares, owner=None):
        self._rec(dict(ev="shares", coin=p.coin, pos_id=p.pos_id, shares=shares,
                       share_owner=owner or p.leader))

    def link_share(self, p, leader, call, metadata=None):
        self.seed(p)
        if leader not in p.shares:
            return
        shares = {a: dict(v) for a, v in p.shares.items()}
        shares[leader].update(call=call.id, portfolio=call.portfolio_id)
        if metadata:
            shares[leader].update(metadata)
        self.record_shares(p, shares)

    def on_move(self, m: Move) -> str | None:
        p = self.st.positions.get(m.coin)
        if p is None:
            reason = super().on_move(m)
            p = self.st.positions.get(m.coin)
            if p is not None:
                self.seed(p)
            return reason
        self.seed(p)
        if m.leader not in p.shares:
            if m.kind not in ("open", "flip") or m.new_side != p.side:
                return self._skip("symbol_held_for_other_leader", coin=m.coin, leader=m.leader)
            return self.enter_share(p, m)
        if m.kind in ("close", "flip"):
            shares = {a: dict(v) for a, v in p.shares.items()}
            shares[m.leader]["closing"] = True
            self.record_shares(p, shares)
            return self.flush_closing(p, m)
        if m.kind not in ("add", "reduce"):
            return
        own = p.shares[m.leader]
        if own.get("closing"):
            return self._skip("share_exit_pending", coin=m.coin, leader=m.leader)
        target = own["k"] * abs(m.end_pos)
        return self.resize_share(p, m, target)

    def enter_share(self, p, m):
        shares = {a: dict(v) for a, v in p.shares.items()}
        shares[m.leader] = self.share(0, abs(m.end_pos), m.oid)
        shares[m.leader].update(entry_px=self.mids.get(p.coin) or m.px, realized=0,
                               fees=0, funding=0, notional=0, opened_ms=m.time_ms)
        self.record_shares(p, shares)
        px = self.mids.get(p.coin) or m.px
        budget = self.st.equity(self.mids) * self.margin_pct / 200
        reason = self.resize_share(p, m, budget * p.leverage / px)
        self.notify("consensus", coin=p.coin, leader=m.leader, size=p.shares[m.leader]["size"])
        return reason

    def resize_share(self, p, m, target):
        before = p.shares[m.leader]["size"]
        delta = target - before
        if math.isclose(delta, 0, abs_tol=1e-12):
            return
        px = self.mids.get(p.coin) or m.px or p.entry_px
        adding = delta > 0
        kind = ("add" if m.leader == p.leader else "boost") if adding else "reduce"
        order = Order(kind, p.coin, p.side, abs(delta), px, leader=m.leader,
                      fill_time_ms=m.time_ms if adding else 0, keep_remainder=not adding)
        d = self.gate.check(order, self.st, self.health(), self.mids, self.assets.get(p.coin))
        reason = None
        changed = False
        if not d:
            reason = self._skip(d.reason, coin=p.coin, leader=m.leader, action=kind)
        else:
            iid = uuid.uuid4().hex[:12]
            self._rec(dict(ev="intent", intent=iid, action=kind, coin=p.coin))
            try:
                f = self.broker.market(p.coin, p.side > 0 if adding else p.side < 0, d.size,
                                       **({} if adding else dict(exit=True, mid=px)))
            except NoBook as exc:
                self._rec(dict(ev="intent_abort", intent=iid))
                reason = self._skip("no_book", coin=p.coin, leader=m.leader, err=str(exc))
            else:
                shares = {a: dict(v) for a, v in p.shares.items()}
                s = shares[m.leader]
                if adding:
                    s["entry_px"] = (before * s["entry_px"] + d.size * f.px) / (before + d.size)
                    s["notional"] += d.size * f.px
                    s["realized"] -= f.fee
                else:
                    s["realized"] += p.side * (f.px - s["entry_px"]) * d.size - f.fee
                s["fees"] += f.fee
                s["size"] = max(0, before + (d.size if adding else -d.size))
                s["exposure"] = abs(m.end_pos)
                s["k"] = s["size"] / abs(m.end_pos) if m.end_pos else 0
                event = dict(ev="add" if adding else "reduce", intent=iid, coin=p.coin,
                             pos_id=p.pos_id, sz=d.size, px=f.px, fee=f.fee,
                             lag_ms=self._lag(self.health(), m), shares=shares,
                             reason="trader_share_resize")
                self._rec(event)
                self.gate.record_order()
                self.notify("updated", coin=p.coin)
                changed = True
        # As in the regular engine, don't make later trims target rejected adds.
        tiny = adding and reason and reason.startswith("below_min_notional") and delta * px < self.cfg.risk.min_notional_usd
        if not changed and adding and not tiny:
            shares = {a: dict(v) for a, v in p.shares.items()}
            s = shares[m.leader]
            s["exposure"] = abs(m.end_pos)
            s["k"] = s["size"] / abs(m.end_pos) if m.end_pos else 0
            self.record_shares(p, shares)
        return reason

    def flush_closing(self, p, move=None):
        closing = [a for a, s in p.shares.items() if s.get("closing")]
        if not closing:
            return
        remaining = {a: dict(s) for a, s in p.shares.items() if a not in closing}
        size = sum(p.shares[a]["size"] for a in closing)
        if not any(s["size"] > 1e-12 for s in remaining.values()):
            return self.close(p.coin, "leader_close", move=move)
        owner = p.leader if p.leader in remaining else next(a for a, s in remaining.items() if s["size"] > 1e-12)
        if size <= 1e-12:
            ts = now_ms()
            trades = [State.allocation_trade(p, a, p.shares[a], self.mids.get(p.coin) or p.entry_px,
                                             0, ts, "trader_share_close")
                      for a in closing if p.shares[a].get("notional", 0) > 0]
            self._rec(dict(ev="shares", coin=p.coin, pos_id=p.pos_id, shares=remaining,
                           share_owner=owner, share_closes=trades, ts=ts))
            return
        px = self.mids.get(p.coin) or p.entry_px
        d = self.gate.check(Order("reduce", p.coin, p.side, size, px, keep_remainder=True),
                            self.st, self.health(), self.mids, None)
        if not d:
            if any(p.shares[a].get("exit_refused") != d.reason.split(":")[0] for a in closing):
                shares = {a: dict(v) for a, v in p.shares.items()}
                for a in closing:
                    shares[a]["exit_refused"] = d.reason.split(":")[0]
                self.record_shares(p, shares)
                return self._skip(d.reason, coin=p.coin, action="share_exit")
            return d.reason
        iid = uuid.uuid4().hex[:12]
        self._rec(dict(ev="intent", intent=iid, action="reduce", coin=p.coin))
        f = self.broker.market(p.coin, p.side < 0, d.size, exit=True, mid=px)
        ts = now_ms()
        trades = [State.allocation_trade(p, a, p.shares[a], f.px,
                                         f.fee * p.shares[a]["size"] / size, ts, "trader_share_close")
                  for a in closing if p.shares[a].get("notional", 0) > 0]
        self._rec(dict(ev="reduce", intent=iid, coin=p.coin, pos_id=p.pos_id, sz=d.size,
                       px=f.px, fee=f.fee, reason="trader_share_close", shares=remaining,
                       share_owner=owner, lag_ms=self._lag(self.health(), move), share_closes=trades, ts=ts))
        self.gate.record_order()
        self.notify("updated", coin=p.coin)
        for leader in closing:
            self._check_leader(leader)

    def close(self, coin, reason, move=None):
        p = self.st.positions.get(coin)
        leaders = list(p.shares) if p else []
        super().close(coin, reason, move=move)
        for leader in leaders:
            if p is not None and leader != p.leader:
                self._check_leader(leader)

    def check_stops(self):
        super().check_stops()
        for p in list(self.st.positions.values()):
            if p.shares:
                self.flush_closing(p)
