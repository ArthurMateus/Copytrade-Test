"""Telegram message rendering (pure functions, HTML parse mode). Short, emoji-marked, aligned in <pre>."""
from __future__ import annotations

import html
import statistics
import time

from copybot.ledger import Position, State


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def fpx(x: float) -> str:
    if x is None:
        return "-"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.1f}"
    if a >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.6g}"


def fusd(x: float, sign: bool = True) -> str:
    s = f"{abs(x):,.2f}$"
    return (("+" if x >= 0 else "−") + s) if sign else s


def fpct(x: float) -> str:
    return ("+" if x >= 0 else "−") + f"{abs(x):.2f}%"


def hhmmss(ms: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000)) + "Z"


def dur(ms: float) -> str:
    m = int(ms // 60000)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h{m:02d}m"
    return f"{h // 24}d{h % 24}h"


def rows(pairs) -> str:
    w = max(len(k) for k, _ in pairs)
    return "\n".join(f"{k.ljust(w)}  {v}" for k, v in pairs)


def pre(pairs) -> str:
    return "<pre>" + esc(rows(pairs)) + "</pre>"


def side_tag(side: int) -> str:
    return "🟢 LONG" if side > 0 else "🔴 SHORT"


def trade_card(p: Position, mark: float | None, now_ms: float) -> str:
    u = p.upnl(mark) if mark else 0.0
    notional = p.entry_notional or p.size * p.entry_px
    lag = f" · lag {p.open_lag_ms / 1000:.1f}s" if p.open_lag_ms is not None else ""
    head = f"{side_tag(p.side)} <b>{esc(p.coin)}</b> · {p.leverage:g}x{lag}"
    return head + "\n" + pre([
        ("Entry", fpx(p.entry_px)),
        ("Mark", fpx(mark) if mark else "-"),
        ("Size", f"{p.size:g} ({fusd(p.size * p.entry_px, sign=False)})"),
        ("Stop", f"{fpx(p.stop_px)} 🛑"),
        ("uPnL", f"{fusd(u)}  {fpct(u / notional * 100)}"),
        ("rPnL", f"{fusd(p.realized)}  {fpct(p.realized / notional * 100)}"),
        ("Leader", short(p.leader)),
        ("Upd", hhmmss(now_ms)),
    ])


REASONS = {"leader_close": "leader closed", "leader_flip": "leader flipped", "stop": "🛑 stop hit",
           "leader_reduce": "leader reduced", "reconcile_leader_flat": "leader flat (reconcile)",
           "flatten": "/flatten", "leader_reopened": "stale copy", "reconcile_leader_reduced": "reconcile"}


def closed_card(t: dict) -> str:
    win = t["pnl"] >= 0
    notional = t["notional"] or 1
    head = f"{'✅ WIN' if win else '❌ LOSS'} <b>{esc(t['coin'])}</b> {'LONG' if t['side'] > 0 else 'SHORT'} · closed"
    return head + "\n" + pre([
        ("Entry", fpx(t["entry"])),
        ("Exit", fpx(t["exit"])),
        ("P&L", f"{fusd(t['pnl'])}  {fpct(t['pnl'] / notional * 100)}"),
        ("Fees", fusd(-t["fees"])),
        ("Reason", REASONS.get(t["reason"], t["reason"])),
        ("Held", dur(t["closed_ms"] - t["opened_ms"])),
        ("Leader", short(t["leader"])),
    ])


def pctl(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def lag_line(st: State) -> str:
    p50, p95 = pctl(st.lags_ms, 0.5), pctl(st.lags_ms, 0.95)
    if p50 is None:
        return "-"
    return f"p50 {p50 / 1000:.1f}s · p95 {p95 / 1000:.1f}s"


def status_card(st: State, mids: dict, health, now_ms: float, btc_px: float | None) -> str:
    eq = st.equity(mids)
    pnl = eq - st.equity0
    day = st.marks.get("day", {}).get("equity") or st.equity0
    bh = (btc_px / st.btc_px0 - 1) * 100 if btc_px and st.btc_px0 else None
    if st.entries_paused or st.uncertain:
        state = f"⏸️ entries paused: {st.pause_reason or 'uncertain state'}"
    else:
        state = "▶️ copying"
    feed = "ok" if health.feed_connected and health.feed_age_s < 30 else f"⚠️ {health.feed_age_s:.0f}s"
    prices = "ok" if health.mids_age_s < 5 else f"⚠️ {health.mids_age_s:.0f}s"
    clock = "ok" if health.clock_ok else f"⚠️ {health.clock_why}"
    return f"📊 <b>Status</b> · {esc(state)}\n" + pre([
        ("Equity", f"{fusd(eq, sign=False)}"),
        ("P&L", f"{fusd(pnl)}  {fpct(pnl / st.equity0 * 100)}"),
        ("Today", f"{fusd(eq - day)}  {fpct((eq - day) / day * 100)}"),
        ("BTC hold", fpct(bh) if bh is not None else "-"),
        ("Open", f"{len(st.positions)} pos · risk {fusd(st.total_risk(), sign=False)}"),
        ("Leaders", f"{len(st.followed)} ({len(st.paused_leaders)} paused)"),
        ("Trades", str(len(st.closed))),
        ("Lag", lag_line(st)),
        ("Feed", f"{feed} · px {prices} · clock {clock}"),
        ("Upd", hhmmss(now_ms)),
    ])


def leaders_card(st: State, ranks: dict[str, int], now_ms: float) -> str:
    lines = []
    leaders = list(st.followed) + [p.leader for p in st.positions.values() if p.leader not in st.followed]
    for a in dict.fromkeys(leaders):
        s = st.leader_stats.get(a)
        mark = "⏸️" if a in st.paused_leaders else ("🟢" if a in st.followed else "⏳")
        r = ranks.get(a)
        lines.append((f"{mark} {short(a)}", f"#{r if r else '-':<3} {s.trades if s else 0:>3}t "
                                            f"{fusd(s.cum if s else 0.0)}"))
    if not lines:
        return "👥 <b>Leaders</b>\nNone followed (no eligible wallet yet).\n<i>upd " + hhmmss(now_ms) + "</i>"
    return "👥 <b>Leaders</b>  rank · trades · copy P&amp;L\n" + pre(lines) + f"\n<i>upd {hhmmss(now_ms)}</i>"


def positions_text(st: State, mids: dict) -> str:
    if not st.positions:
        return "📭 No open positions."
    lines = []
    for p in st.positions.values():
        m = mids.get(p.coin)
        u = p.upnl(m) if m else 0.0
        lines.append((f"{'🟢' if p.side > 0 else '🔴'} {p.coin}", f"{fusd(u):>9}  stop {fpx(p.stop_px)}"))
    return f"📂 <b>Positions</b> ({len(st.positions)})\n" + pre(lines)


def progress_text(st: State, mids: dict, btc_px: float | None, now_ms: float) -> str:
    n = len(st.closed)
    eq = st.equity(mids)
    pnl = eq - st.equity0
    bh = (btc_px / st.btc_px0 - 1) * st.equity0 if btc_px and st.btc_px0 else None
    med = statistics.median(st.lags_ms) / 1000 if st.lags_ms else None
    wins = sum(1 for t in st.closed if t["pnl"] > 0)
    days = (now_ms - st.genesis_ms) / 86_400_000 if st.genesis_ms else 0
    missed = st.counters.get("missed_exits", 0)
    unexpl = st.counters.get("unexplained_mismatch", 0)
    kill = (n >= 50 and pnl < 0) or missed > 0 or unexpl > 0
    verdict = "🛑 KILL criterion met" if kill else ("🟡 too early to judge" if n < 50 else "🟢 on track")
    return f"🎯 <b>Progress</b> · day {days:.1f} · {verdict}\n" + pre([
        ("Trades", f"{n} (target 50-100) · {wins}W/{n - wins}L"),
        ("P&L", f"{fusd(pnl)}  {fpct(pnl / st.equity0 * 100)} after costs"),
        ("BTC hold", fusd(bh) if bh is not None else "-"),
        ("Missed exit", f"{missed} {'✅' if not missed else '❌'}"),
        ("Mismatch", f"{unexpl} {'✅' if not unexpl else '❌'} (+{st.counters.get('reconcile_exits', 0)} caught)"),
        ("Med lag", (f"{med:.1f}s {'✅' if med <= 5 else '⚠️'}") if med is not None else "-"),
        ("Skipped<$10", str(st.counters.get("skipped_min_notional", 0))),
    ]) + "\n<i>A few dozen paper trades show the bot works, not that an edge exists.</i>"
