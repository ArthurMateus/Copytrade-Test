"""Chat message rendering (pure functions, Telegram HTML; Discord converts it, see discord.py).

Conventions, the same on every card:
  🟢 / 🔴  money only: making / losing money (⚪ flat)      ⬆️ LONG / ⬇️ SHORT  the side of a trade
  one P&L number, always AFTER fees and funding             "label: value" lines (no code blocks)
"""
from __future__ import annotations

import html
import statistics
import time

from copybot.ledger import Position, State

UTC_OFFSET_H = 0.0   # shown times are in the owner's time zone (telegram.utc_offset_hours)


def set_utc_offset(hours: float) -> None:
    global UTC_OFFSET_H
    UTC_OFFSET_H = float(hours)


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
    t = time.strftime("%H:%M:%S", time.gmtime(ms / 1000 + UTC_OFFSET_H * 3600))
    return t if UTC_OFFSET_H else t + " UTC"


def dur(ms: float) -> str:
    m = int(ms // 60000)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h{m:02d}m"
    return f"{h // 24}d{h % 24}h"


def dot(x: float) -> str:
    """Money colour: 🟢 making money, 🔴 losing money, ⚪ nothing yet."""
    return "🟢" if x > 0.005 else ("🔴" if x < -0.005 else "⚪")


_pnl_dot = dot


def money(x: float, base: float | None = None) -> str:
    """'🟢 +1.23$ (+0.41%)': the colour always says winning or losing."""
    pct = f" ({fpct(x / base * 100)})" if base else ""
    return f"{dot(x)} {fusd(x)}{pct}"


def pre(pairs) -> str:
    """Readable 'label: value' lines (no <pre>: Telegram shows a COPY CODE box on those)."""
    return "\n".join(f"{esc(k)}: <b>{esc(v)}</b>" for k, v in pairs)


def side_tag(side: int) -> str:
    return "⬆️ LONG" if side > 0 else "⬇️ SHORT"


def updated(now_ms: float) -> str:
    return f"<i>updated {hhmmss(now_ms)}</i>"


def _n(k: int, word: str) -> str:
    return f"{k} {word}" + ("" if k == 1 else "s")


# ---- one open trade ---------------------------------------------------------------------------
def net_pnl(p: Position, mark: float | None) -> float:
    """What the trade has made so far: price move, minus fees paid, plus/minus funding."""
    return (p.upnl(mark) if mark else 0.0) + p.realized


def loss_if_stopped(p: Position, fee_pct: float = 0.045) -> float:
    """The trade's total result if the stop-loss fills now (incl. fees so far and the exit fee)."""
    return p.side * (p.stop_px - p.entry_px) * p.size + p.realized - p.stop_px * p.size * fee_pct / 100


def _trade_rows(p: Position, mark: float | None, now_ms: float) -> list:
    value = p.size * (mark or p.entry_px)
    to_stop = abs(p.stop_px / mark - 1) * 100 if mark else None
    exit_by = f"when {short(p.leader)} exits" + (f" (🤝 +{len(p.backers)} agree)" if p.backers else "")
    rows = [
        ("Entry → now", f"{fpx(p.entry_px)} → {fpx(mark) if mark else '-'}"),
        ("Value", f"{fusd(value, sign=False)} · your margin {fusd(value / max(p.leverage, 1), sign=False)} "
                  f"({p.leverage:g}x)"),
        ("Stop-loss", f"{fpx(p.stop_px)}" + (f" · {to_stop:.1f}% away" if to_stop is not None else "")
         + f" · {fusd(loss_if_stopped(p))} if hit"),
        ("Take profit", exit_by),
    ]
    if now_ms:
        lag = f" · copied in {p.open_lag_ms / 1000:.1f}s" if p.open_lag_ms is not None else ""
        rows.append(("Open for", dur(now_ms - p.opened_ms) + lag))
    return rows


def trade_head(p: Position, mark: float | None) -> str:
    net = net_pnl(p, mark)
    notional = p.entry_notional or p.size * p.entry_px
    return f"{dot(net)} <b>{esc(p.coin)}</b> {side_tag(p.side)} · {fusd(net)} ({fpct(net / notional * 100)})"


def trade_card(p: Position, mark: float | None, now_ms: float) -> str:
    """The live card of one open trade (edited in place). P&L is after fees and funding."""
    return trade_head(p, mark) + "\n" + pre(_trade_rows(p, mark, now_ms)) + ("\n" + updated(now_ms) if now_ms else "")


REASONS = {"leader_close": "the trader closed", "leader_flip": "the trader reversed", "stop": "🛑 stop-loss hit",
           "leader_reduce": "the trader reduced", "reconcile_leader_flat": "the trader closed (caught by check)",
           "flatten": "/flatten", "leader_reopened": "stale copy", "reconcile_leader_reduced": "check",
           "conflict_better_leader": "⚔️ a better trader took the other side", "backer_close": "🤝 backer closed",
           "backer_flip": "🤝 backer reversed", "reconcile_backer_flat": "🤝 backer closed (caught by check)"}


def closed_card(t: dict) -> str:
    """The final summary the trade card turns into."""
    win = t["pnl"] >= 0
    notional = t["notional"] or 1
    side = side_tag(t["side"])
    head = (f"{'✅ WIN' if win else '❌ LOSS'} · <b>{esc(t['coin'])}</b> {side} · "
            f"{fusd(t['pnl'])} ({fpct(t['pnl'] / notional * 100)})")
    return head + "\n" + pre([
        ("Entry → exit", f"{fpx(t['entry'])} → {fpx(t['exit'])}"),
        ("Result", f"{fusd(t['pnl'])} after fees and funding"),
        ("Why", REASONS.get(t["reason"], t["reason"])),
        ("Held", dur(t["closed_ms"] - t["opened_ms"])),
        ("Trader", short(t["leader"])),
    ])


# ---- wallet-level cards -------------------------------------------------------------------------
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
    return f"usually {p50 / 1000:.1f}s · slowest {p95 / 1000:.1f}s"


def status_card(st: State, mids: dict, health, now_ms: float, btc_px: float | None) -> str:
    eq = st.equity(mids)
    pnl = eq - st.equity0
    day = st.marks.get("day", {}).get("equity") or st.equity0
    bh = (btc_px / st.btc_px0 - 1) * 100 if btc_px and st.btc_px0 else None
    if st.entries_paused or st.uncertain:
        state = f"⏸️ new copies paused: {st.pause_reason or 'uncertain state'}"
    else:
        state = "▶️ copying"
    ok = lambda good, bad: "✅" if good else f"⚠️ {bad}"
    conn = (f"prices {ok(health.mids_age_s < 5, f'{health.mids_age_s:.0f}s old')} · "
            f"trades feed {ok(health.feed_connected and health.feed_age_s < 30, f'{health.feed_age_s:.0f}s')} · "
            f"clock {ok(health.clock_ok, health.clock_why)}")
    return f"📊 <b>Status</b> · {esc(state)}\n" + pre([
        ("Wallet", f"{fusd(eq, sign=False)} of {fusd(st.equity0, sign=False)} · {money(pnl, st.equity0)}"),
        ("Today", money(eq - day, day)),
        ("Just holding BTC", fpct(bh) if bh is not None else "-"),
        ("Open trades", f"{len(st.positions)} · at most {fusd(st.total_risk(), sign=False)} at risk"),
        ("Traders", f"{len(st.followed)} followed" + (f" ({len(st.paused_leaders)} paused)" if st.paused_leaders else "")),
        ("Closed trades", str(len(st.closed))),
        ("Copy speed", lag_line(st)),
        ("Connection", conn),
    ]) + "\n" + updated(now_ms)


def trades_card(st: State, mids: dict, now_ms: float) -> str:
    """/trades: every open copy at the live price, plus the result against the starting wallet."""
    eq = st.equity(mids)
    total = eq - st.equity0
    open_net = sum(net_pnl(p, mids.get(p.coin)) for p in st.positions.values())
    wins = sum(1 for t in st.closed if t["pnl"] > 0)
    closed_pnl = sum(t["pnl"] for t in st.closed)
    at_risk = sum(loss_if_stopped(p) for p in st.positions.values())
    out = [f"💼 <b>Trades</b> · {len(st.positions)} open\n"
           f"{dot(total)} <b>Total: {fusd(total)} ({fpct(total / st.equity0 * 100)})</b> on {fusd(st.equity0, sign=False)}",
           pre([
               ("Wallet now", fusd(eq, sign=False)),
               ("Open trades", money(open_net)),
               ("Closed trades", f"{money(closed_pnl)} · {_n(len(st.closed), 'trade')} "
                                 f"({wins} won, {len(st.closed) - wins} lost)"),
               ("If every stop hits", fusd(at_risk) if st.positions else "-"),
           ])]
    if not st.positions:
        out.append("📭 No open trades.")
    for p in sorted(st.positions.values(), key=lambda p: p.opened_ms):
        mark = mids.get(p.coin)
        out.append("\n" + trade_head(p, mark) + "\n" + pre(_trade_rows(p, mark, now_ms)))
    out.append(updated(now_ms))
    return "\n".join(out)


def by_rank(leaders, ranks: dict[str, int]) -> list[str]:
    """Unique leaders, best rank first; unranked ones (no longer eligible) last."""
    return sorted(dict.fromkeys(leaders), key=lambda a: (ranks.get(a) or 10**6, a))


def pf_text(pf: float) -> str:
    """Profit factor = money won on winning trades / money lost on losing trades."""
    if pf >= 99:
        return "no losing trade"
    return f"{pf:.2f} (wins {fusd(pf, sign=False)} per 1$ lost)"


def leaders_card(st: State, ranks: dict[str, int], now_ms: float, scores: dict | None = None) -> str:
    lines = []
    leaders = list(st.followed) + [p.leader for p in st.positions.values() if p.leader not in st.followed]
    for a in by_rank(leaders, ranks):
        s = st.leader_stats.get(a)
        cum = s.cum if s else 0.0
        flag = " ⏸️" if a in st.paused_leaders else ("" if a in st.followed else " ⏳")
        r = ranks.get(a)
        sc = (scores or {}).get(a, {}).get("score")
        lines.append(f"{dot(cum)} {'#' + str(r) if r else '–'} <code>{short(a)}</code>{flag} · "
                     f"{f'{sc:.0f}/100' if sc else 'no score'} · {_n(s.trades if s else 0, 'trade')} · {fusd(cum)}")
    if not lines:
        return "👥 <b>Leaders</b>\nNone followed (no eligible wallet yet).\n" + updated(now_ms)
    return "👥 <b>Leaders</b> · rank · score · copied trades · made for you\n" + "\n".join(lines) + "\n" + updated(now_ms)


def traders_card(st: State, mids: dict, ranks: dict[str, int], scores: dict | None, now_ms: float) -> str:
    """/traders: every followed (or still held) leader with what copying it has earned us."""
    scores = scores or {}
    leaders = list(st.followed) + [a for p in st.positions.values() for a in (p.leader, *p.backers)
                                   if a not in st.followed]
    blocks, grand = [], 0.0
    for a in by_rank(leaders, ranks):
        mine = [t for t in st.closed if t["leader"] == a]
        wins = sum(1 for t in mine if t["pnl"] > 0)
        realized = sum(t["pnl"] for t in mine)
        held = [p for p in st.positions.values() if p.leader == a]
        backing = sum(1 for p in st.positions.values() if a in p.backers)
        live = sum(net_pnl(p, mids.get(p.coin)) for p in held)
        made = realized + live
        grand += made
        sc = scores.get(a, {})
        r = ranks.get(a)
        tag = (f"#{r} · " if r else "") + (f"{sc['score']:.0f}/100" if sc.get("score") else "no score")
        tag += " 🎲" if sc.get("diversified") else ""
        flag = " ⏸️ paused" if a in st.paused_leaders else ("" if a in st.followed else " ⏳ no longer followed")
        rows = [
            ("Made for you", money(made, st.equity0)),
            ("Copied", f"{_n(len(mine), 'trade')} · {wins} won, {len(mine) - wins} lost"
                       + (f" ({wins / len(mine) * 100:.0f}%)" if mine else "")),
            ("Open now", f"{_n(len(held), 'trade')} · {fusd(live)}" + (f" · backs {backing}" if backing else "")),
        ]
        if mine:
            rows.append(("Best", f"{fusd(max(t['pnl'] for t in mine))} · worst {fusd(min(t['pnl'] for t in mine))}"))
        if sc:
            rows.append(("Their record", f"{sc.get('trades', 0)} trades · {sc.get('win_rate', 0) * 100:.0f}% win"))
            rows.append(("Profit factor", pf_text(sc.get("profit_factor", 0))))
            if sc.get("open_losers"):
                rows.append(("Holding losers", f"{sc['open_losers']} open · {sc.get('open_loss_pct', 0) * 100:.0f}% of account"))
        if a in st.followed and now_ms:
            rows.append(("Following for", dur(now_ms - st.followed[a])))
        if a in st.paused_leaders:
            rows.append(("Paused because", st.paused_leaders[a]))
        blocks.append(f"\n{dot(made)} <code>{short(a)}</code> · {esc(tag)}{flag}\n" + pre(rows))
    if not blocks:
        return "👥 <b>Traders</b>\nNone followed yet.\n" + updated(now_ms)
    head = (f"👥 <b>Traders</b> · {len(st.followed)} followed\n"
            f"{dot(grand)} <b>Copying them made: {fusd(grand)} ({fpct(grand / st.equity0 * 100)})</b>")
    return "\n".join([head, *blocks, updated(now_ms)])


def wallets_card(wallets: list, mids: dict, now_ms: float) -> str:
    """/wallets: the same copies at different risk levels, best result first.
    `wallets` = [(risk_pct, State, is_main)]."""
    from copybot.wallets import max_drop_pct
    rows = []
    for risk, st, main in wallets:
        eq = st.equity(mids)
        rows.append((eq - st.equity0, risk, st, main, eq))
    rows.sort(key=lambda x: (-x[0], x[1]))
    medals = ["🥇", "🥈", "🥉"]
    out = ["💰 <b>Wallets</b> · same traders, different risk per trade"]
    for i, (pnl, risk, st, main, eq) in enumerate(rows):
        n = len(st.closed)
        wins = sum(1 for t in st.closed if t["pnl"] > 0)
        live = sum(net_pnl(p, mids.get(p.coin)) for p in st.positions.values())
        medal = medals[i] if i < 3 and abs(pnl) > 0.005 else "▫️"
        name = f"{risk:g}% risk" + (" (main)" if main else "")
        state = " ⏸️" if st.entries_paused or st.uncertain else ""
        body = [
            ("Result", money(pnl, st.equity0)),
            ("Wallet", fusd(eq, sign=False)),
            ("Trades", f"{n} · {wins} won, {n - wins} lost" + (f" ({wins / n * 100:.0f}%)" if n else "")),
            ("Open", f"{_n(len(st.positions), 'trade')} · {fusd(live)}"),
            ("Biggest drop", f"−{max_drop_pct(st, mids):.1f}%"),
        ]
        refused = st.counters.get("opens_refused", 0) + st.counters.get("skipped_min_notional", 0)
        if refused:
            body.append(("Not taken", f"{refused} (limits)"))
        out.append(f"\n{medal} <b>{esc(name)}</b>{state}\n" + pre(body))
    out.append(updated(now_ms))
    return "\n".join(out)


def positions_text(st: State, mids: dict) -> str:
    if not st.positions:
        return "📭 No open trades."
    lines = []
    for p in st.positions.values():
        net = net_pnl(p, mids.get(p.coin))
        lines.append(f"{dot(net)} <b>{esc(p.coin)}</b> {side_tag(p.side)} · {fusd(net)} · stop {fpx(p.stop_px)}")
    return f"📂 <b>Open trades</b> ({len(st.positions)})\n" + "\n".join(lines)


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
    longs = [t for t in st.closed if t["side"] > 0]
    shorts = [t for t in st.closed if t["side"] < 0]
    open_long = sum(1 for p in st.positions.values() if p.side > 0)
    open_short = len(st.positions) - open_long
    one_sided = (n + len(st.positions)) >= 8 and not shorts and not open_short
    return f"🎯 <b>Progress</b> · day {days:.1f} · {verdict}\n" + pre([
        ("Closed trades", f"{n} (target 50-100) · {wins} won, {n - wins} lost"),
        ("Longs", f"{len(longs)} closed · {fusd(sum(t['pnl'] for t in longs))} · {open_long} open"),
        ("Shorts", f"{len(shorts)} closed · {fusd(sum(t['pnl'] for t in shorts))} · {open_short} open"),
        ("Result", f"{money(pnl, st.equity0)} after all costs"),
        ("Just holding BTC", fusd(bh) if bh is not None else "-"),
        ("Missed exits", f"{missed} {'✅' if not missed else '❌'}"),
        ("Position mismatches", f"{unexpl} {'✅' if not unexpl else '❌'} "
                                f"({st.counters.get('reconcile_exits', 0)} caught by the checks)"),
        ("Copy speed", (f"{med:.1f}s {'✅' if med <= 5 else '⚠️'}") if med is not None else "-"),
        ("Too small to copy", str(st.counters.get("skipped_min_notional", 0))),
    ]) + ("\n⚠️ <i>Every copy so far was a long: check /hypertraders. The followed traders may all be long right now "
          "(shorts are copied the same way).</i>" if one_sided else "") \
        + "\n<i>A few dozen paper trades show the bot works, not that an edge exists.</i>"
