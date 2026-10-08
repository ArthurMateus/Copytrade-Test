"""Solana message rendering (HTML like tgfmt; the Discord UI converts it to markdown)."""
from __future__ import annotations

import statistics

from copybot.ledger import Position, State
from copybot.tgfmt import dur, esc, fpct, fpx, fusd, hhmmss, lag_line, pre, short

REASONS = {"leader_close": "leader sold everything", "leader_reduce": "leader sold part", "stop": "🛑 stop hit",
           "reconcile_leader_flat": "leader's balance is gone", "flatten": "flatten", "leader_reopened": "stale copy"}


def who(addr: str, handle: str = "") -> str:
    return f"{handle} ({short(addr)})" if handle else short(addr)


def trade_card(p: Position, mark: float | None, now_ms: float, handle: str = "") -> str:
    u = p.upnl(mark) if mark else 0.0
    notional = p.entry_notional or p.size * p.entry_px
    lag = f" · lag {p.open_lag_ms / 1000:.1f}s" if p.open_lag_ms is not None else ""
    return f"🟢 <b>{esc(p.sym or p.coin[:6])}</b> · spot{lag}\n" + pre([
        ("Entry", fpx(p.entry_px)),
        ("Mark", fpx(mark) if mark else "-"),
        ("Size", f"{p.size:,.0f} ({fusd(p.size * p.entry_px, sign=False)})"),
        ("Stop", f"{fpx(p.stop_px)} 🛑"),
        ("uPnL", f"{fusd(u)}  {fpct(u / notional * 100)}"),
        ("rPnL", f"{fusd(p.realized)}  {fpct(p.realized / notional * 100)}"),
        ("Leader", who(p.leader, handle)),
        ("Upd", hhmmss(now_ms)),
    ])


def closed_card(t: dict, handle: str = "") -> str:
    win = t["pnl"] >= 0
    notional = t["notional"] or 1
    return f"{'✅ WIN' if win else '❌ LOSS'} <b>{esc(t.get('sym') or t['coin'][:6])}</b> · closed\n" + pre([
        ("Entry", fpx(t["entry"])),
        ("Exit", fpx(t["exit"])),
        ("P&L", f"{fusd(t['pnl'])}  {fpct(t['pnl'] / notional * 100)}"),
        ("Fees", fusd(-t["fees"])),
        ("Reason", REASONS.get(t["reason"], t["reason"])),
        ("Held", dur(t["closed_ms"] - t["opened_ms"])),
        ("Leader", who(t["leader"], handle)),
    ])


def status_card(st: State, marks: dict, h, now_ms: float, auth_ok: bool) -> str:
    eq = st.equity(marks)
    pnl = eq - st.equity0
    day = st.marks.get("day", {}).get("equity") or st.equity0
    if st.entries_paused or st.uncertain:
        state = f"⏸️ entries paused: {st.pause_reason or 'uncertain state'}"
    elif not auth_ok:
        state = "⚠️ FOMO session expired: no new wallets"
    else:
        state = "▶️ copying"
    return f"🪙 <b>Solana (paper)</b> · {esc(state)}\n" + pre([
        ("Equity", fusd(eq, sign=False)),
        ("P&L", f"{fusd(pnl)}  {fpct(pnl / st.equity0 * 100)}"),
        ("Today", f"{fusd(eq - day)}  {fpct((eq - day) / day * 100)}"),
        ("Open", f"{len(st.positions)} pos · risk {fusd(st.total_risk(), sign=False)}"),
        ("Leaders", f"{len(st.followed)} ({len(st.paused_leaders)} paused)"),
        ("Trades", str(len(st.closed))),
        ("Lag", lag_line(st)),
        ("Feeds", f"prices {'ok' if h.price_age_s < 15 else '⚠️ ' + format(h.price_age_s, '.0f') + 's'} · "
                  f"leaders {'ok' if h.leader_feed_age_s < 30 else '⚠️ ' + format(h.leader_feed_age_s, '.0f') + 's'}"),
        ("Upd", hhmmss(now_ms)),
    ])


def leaders_card(st: State, ranks: dict[str, int], handles: dict[str, str], now_ms: float) -> str:
    lines = []
    leaders = list(st.followed) + [p.leader for p in st.positions.values() if p.leader not in st.followed]
    for a in dict.fromkeys(leaders):
        s = st.leader_stats.get(a)
        mark = "⏸️" if a in st.paused_leaders else ("🟢" if a in st.followed else "⏳")
        r = ranks.get(a)
        lines.append((f"{mark} {handles.get(a) or short(a)}", f"#{r if r else '-':<3} {s.trades if s else 0:>3}t "
                                                              f"{fusd(s.cum if s else 0.0)}"))
    if not lines:
        return "👥 <b>Solana leaders</b>\nNone followed yet (no wallet passed the strict scoring).\n<i>upd " + hhmmss(now_ms) + "</i>"
    return "👥 <b>Solana leaders</b>  rank · trades · copy P&amp;L\n" + pre(lines) + f"\n<i>upd {hhmmss(now_ms)}</i>"


def positions_text(st: State, marks: dict) -> str:
    if not st.positions:
        return "📭 No open Solana positions."
    lines = []
    for p in st.positions.values():
        m = marks.get(p.coin)
        u = p.upnl(m) if m else 0.0
        lines.append((f"🟢 {p.sym or p.coin[:6]}", f"{fusd(u):>9}  stop {fpx(p.stop_px)}"))
    return f"📂 <b>Solana positions</b> ({len(st.positions)})\n" + pre(lines)


def progress_text(st: State, marks: dict, now_ms: float) -> str:
    n = len(st.closed)
    eq = st.equity(marks)
    pnl = eq - st.equity0
    wins = sum(1 for t in st.closed if t["pnl"] > 0)
    days = (now_ms - st.genesis_ms) / 86_400_000 if st.genesis_ms else 0
    med = statistics.median(st.lags_ms) / 1000 if st.lags_ms else None
    missed = st.counters.get("missed_exits", 0)
    kill = (n >= 50 and pnl < 0) or missed > 0
    verdict = "🛑 KILL criterion met" if kill else ("🟡 too early to judge" if n < 50 else "🟢 on track")
    return f"🎯 <b>Solana progress</b> · day {days:.1f} · {verdict}\n" + pre([
        ("Trades", f"{n} (target 50-100) · {wins}W/{n - wins}L"),
        ("P&L", f"{fusd(pnl)}  {fpct(pnl / st.equity0 * 100)} after costs"),
        ("Med lag", (f"{med:.1f}s") if med is not None else "-"),
        ("Skipped<min", str(st.counters.get("skipped_min_notional", 0))),
        ("Reconcile", f"{st.counters.get('reconcile_exits', 0)} exits caught"),
    ]) + "\n<i>Memecoins can gap through a stop; paper fills use the pool price when seen.</i>"


SOL_HELP = ("🪙 <b>Solana (paper)</b>\n"
            "/sol – book, P&amp;L, health (live)\n"
            "/solpositions · /solleaders · /solprogress\n"
            "/solpause · /solresume – new entries (exits always run)\n"
            "/solflatten &lt;PIN&gt; – close all Solana copies and pause")
