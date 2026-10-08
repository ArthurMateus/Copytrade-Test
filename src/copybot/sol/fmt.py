"""FOMO/Solana message rendering (Telegram HTML, converted for Discord). Same look as tgfmt: 🟢/🔴 = money only,
labelled lines, one P&L number always AFTER fees, an "updated" footer."""
from __future__ import annotations

import statistics

from copybot.ledger import Position, State
from copybot.tgfmt import (by_rank, dot, dur, esc, fpct, fpx, fusd, lag_line, money, pctl, pf_text, pre, short,
                           updated)

FEE_PCT = 1.0     # the paper swap fee (config sol.swap_fee_pct); used for the "if the stop hits" estimate

REASONS = {"leader_close": "the trader sold everything", "leader_reduce": "the trader sold part",
           "stop": "🛑 stop-loss hit", "reconcile_leader_flat": "the trader's balance is gone (caught by check)",
           "flatten": "/fomoflatten", "leader_reopened": "stale copy"}


def _n(k: int, word: str) -> str:
    return f"{k} {word}" + ("" if k == 1 else "s")


def search_line(progress: dict | None, now_ms: float) -> str:
    """Where the trader search is. `now_ms` 0 = no elapsed times (the card is only edited when something changed)."""
    pr = progress or {}
    phase = pr.get("phase", "idle")
    ago = lambda t: f" · {dur(now_ms - t)} ago" if now_ms and t else ""
    if phase == "discovering":
        return "looking at FOMO's recent trades for active traders" + ago(pr.get("started", 0))
    if phase == "scoring":
        return (f"scoring {pr.get('done', 0)}/{pr.get('todo', 0)} FOMO traders · {pr.get('eligible', 0)} pass so far"
                + (f" · started {dur(now_ms - pr['started'])} ago" if now_ms and pr.get("started") else ""))
    if pr.get("finished"):
        return "last search done" + ago(pr["finished"]) + " · /fomosearch to search again"
    return "no search yet · /fomosearch"


def who(addr: str, handle: str = "") -> str:
    return f"{handle} ({short(addr)})" if handle else short(addr)


def net_pnl(p: Position, mark: float | None) -> float:
    return (p.upnl(mark) if mark else 0.0) + p.realized


def loss_if_stopped(p: Position) -> float:
    return (p.stop_px - p.entry_px) * p.size + p.realized - p.stop_px * p.size * FEE_PCT / 100


def sym(p: Position) -> str:
    return p.sym or p.coin[:6]


# ---- one open trade -------------------------------------------------------------------------------------
def trade_head(p: Position, mark: float | None) -> str:
    net = net_pnl(p, mark)
    notional = p.entry_notional or p.size * p.entry_px
    return f"{dot(net)} <b>{esc(sym(p))}</b> ⬆️ BUY · {fusd(net)} ({fpct(net / notional * 100)})"


def _rows(p: Position, mark: float | None, now_ms: float, handle: str) -> list:
    value = p.size * (mark or p.entry_px)
    to_stop = abs(p.stop_px / mark - 1) * 100 if mark else None
    rows = [
        ("Entry → now", f"{fpx(p.entry_px)} → {fpx(mark) if mark else '-'}"),
        ("Value", f"{fusd(value, sign=False)} · {p.size:,.0f} tokens"),
        ("Stop-loss", f"{fpx(p.stop_px)}" + (f" · {to_stop:.1f}% away" if to_stop is not None else "")
         + f" · {fusd(loss_if_stopped(p))} if hit"),
        ("Sells when", f"{who(p.leader, handle)} sells"),
    ]
    if now_ms:
        lag = f" · copied in {p.open_lag_ms / 1000:.1f}s" if p.open_lag_ms is not None else ""
        rows.append(("Open for", dur(now_ms - p.opened_ms) + lag))
    return rows


def trade_card(p: Position, mark: float | None, now_ms: float, handle: str = "") -> str:
    return trade_head(p, mark) + "\n" + pre(_rows(p, mark, now_ms, handle)) + ("\n" + updated(now_ms) if now_ms else "")


def closed_card(t: dict, handle: str = "") -> str:
    win = t["pnl"] >= 0
    notional = t["notional"] or 1
    head = (f"{'✅ WIN' if win else '❌ LOSS'} · <b>{esc(t.get('sym') or t['coin'][:6])}</b> ⬆️ BUY · "
            f"{fusd(t['pnl'])} ({fpct(t['pnl'] / notional * 100)})")
    return head + "\n" + pre([
        ("Entry → exit", f"{fpx(t['entry'])} → {fpx(t['exit'])}"),
        ("Result", f"{fusd(t['pnl'])} after fees"),
        ("Why", REASONS.get(t["reason"], t["reason"])),
        ("Held", dur(t["closed_ms"] - t["opened_ms"])),
        ("Trader", who(t["leader"], handle)),
    ])


# ---- wallet-level cards -----------------------------------------------------------------------------------
def status_card(st: State, marks: dict, h, now_ms: float, auth_ok: bool, progress: dict | None = None) -> str:
    eq = st.equity(marks)
    pnl = eq - st.equity0
    day = st.marks.get("day", {}).get("equity") or st.equity0
    if st.entries_paused or st.uncertain:
        state = f"⏸️ new copies paused: {st.pause_reason or 'uncertain state'}"
    elif not auth_ok:
        state = "⚠️ Solana data refused: no new copies, exits and stops still run"
    else:
        state = "▶️ copying"
    ok = lambda good, bad: "✅" if good else f"⚠️ {bad}"
    conn = (f"prices {ok(h.price_age_s < 15, f'{h.price_age_s:.0f}s old')} · "
            f"traders feed {ok(h.leader_feed_age_s < 120, f'{h.leader_feed_age_s:.0f}s')} · "
            f"Solana data {ok(auth_ok, 'refused')}")
    return f"🪙 <b>FOMO (paper)</b> · {esc(state)}\n" + pre([
        ("Wallet", f"{fusd(eq, sign=False)} of {fusd(st.equity0, sign=False)} · {money(pnl, st.equity0)}"),
        ("Today", money(eq - day, day)),
        ("Open trades", f"{len(st.positions)} · at most {fusd(st.total_risk(), sign=False)} at risk"),
        ("Traders", f"{len(st.followed)} followed" + (f" ({len(st.paused_leaders)} paused)" if st.paused_leaders else "")),
        ("Closed trades", str(len(st.closed))),
        ("Copy speed", lag_line(st)),
        ("Search", search_line(progress, now_ms)),
        ("Connection", conn),
    ]) + "\n" + updated(now_ms)


def wallet_card(st: State, marks: dict, cfg, now_ms: float) -> str:
    """/fomowallet: where the paper money is."""
    eq = st.equity(marks)
    invested = sum(p.size * (marks.get(p.coin) or p.entry_px) for p in st.positions.values())
    open_net = sum(net_pnl(p, marks.get(p.coin)) for p in st.positions.values())
    realized_closed = sum(t["pnl"] for t in st.closed)
    fees = sum(t["fees"] for t in st.closed) + sum(p.fees for p in st.positions.values())
    day = st.marks.get("day", {}).get("equity") or st.equity0
    week = st.marks.get("week", {}).get("equity") or st.equity0
    d_used = max(0.0, (day - eq) / day * 100)
    w_used = max(0.0, (week - eq) / week * 100)
    return f"💰 <b>FOMO wallet</b> (paper)\n{money(eq - st.equity0, st.equity0)} on {fusd(st.equity0, sign=False)}\n" + pre([
        ("Wallet now", fusd(eq, sign=False)),
        ("Cash", fusd(eq - invested, sign=False)),
        ("In open trades", f"{fusd(invested, sign=False)} ({invested / eq * 100:.0f}% of the wallet)" if eq > 0 else "-"),
        ("Open trades", f"{money(open_net)} · {_n(len(st.positions), 'trade')} (max {cfg.max_positions})"),
        ("Closed trades", f"{money(realized_closed)} · {_n(len(st.closed), 'trade')}"),
        ("Fees paid", fusd(-fees)),
        ("Today", f"{money(eq - day, day)} · stops new copies at −{cfg.daily_loss_pct:g}% ({d_used:.1f}% used)"),
        ("This week", f"{money(eq - week, week)} · stops new copies at −{cfg.weekly_loss_pct:g}% ({w_used:.1f}% used)"),
    ]) + "\n" + updated(now_ms)


def trades_card(st: State, marks: dict, now_ms: float, handles: dict | None = None) -> str:
    """/fomotrades: every open copy at the live price, plus the result against the starting wallet."""
    handles = handles or {}
    eq = st.equity(marks)
    total = eq - st.equity0
    open_net = sum(net_pnl(p, marks.get(p.coin)) for p in st.positions.values())
    wins = sum(1 for t in st.closed if t["pnl"] > 0)
    closed_pnl = sum(t["pnl"] for t in st.closed)
    at_risk = sum(loss_if_stopped(p) for p in st.positions.values())
    out = [f"💼 <b>FOMO trades</b> · {len(st.positions)} open\n"
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
        mark = marks.get(p.coin)
        out.append("\n" + trade_head(p, mark) + "\n" + pre(_rows(p, mark, now_ms, handles.get(p.leader, ""))))
    out.append(updated(now_ms))
    return "\n".join(out)


def leaders_card(st: State, ranks: dict[str, int], handles: dict[str, str], now_ms: float,
                 scores: dict | None = None, progress: dict | None = None) -> str:
    scores = scores or {}
    search = f"🔎 Search: {esc(search_line(progress, now_ms))}"
    best = []
    for a in by_rank([a for a in ranks if a not in st.followed], ranks)[:5]:
        sc = scores.get(a, {})
        best.append(f"#{ranks[a]} {esc(short(a))} · {sc.get('score', 0) * 100:.0f} pts · "
                    f"{sc.get('trades', 0)} trades · {sc.get('win_rate', 0) * 100:.0f}% win · "
                    f"PF {pf_text(sc.get('profit_factor', 0))}")
    found = ("\n<b>Best found, not followed</b>\n" + "\n".join(best)) if best else ""
    lines = []
    leaders = list(st.followed) + [p.leader for p in st.positions.values() if p.leader not in st.followed]
    for a in by_rank(leaders, ranks):
        s = st.leader_stats.get(a)
        cum = s.cum if s else 0.0
        flag = " ⏸️" if a in st.paused_leaders else ("" if a in st.followed else " ⏳")
        r = ranks.get(a)
        sc = scores.get(a, {}).get("score")
        lines.append(f"{dot(cum)} {'#' + str(r) if r else '–'} {esc(who(a, handles.get(a, '')))}{flag} · "
                     f"{f'{sc * 100:.0f} pts' if sc else 'no score'} · {_n(s.trades if s else 0, 'trade')} · {fusd(cum)}")
    if not lines:
        return (f"👥 <b>FOMO leaders</b>\nNone followed yet (no wallet passed the strict scoring).\n{search}{found}\n"
                + updated(now_ms))
    return ("👥 <b>FOMO leaders</b> · rank · score · copied trades · made for you\n" + "\n".join(lines)
            + f"\n{search}{found}\n" + updated(now_ms))


def traders_card(st: State, marks: dict, ranks: dict[str, int], scores: dict | None, handles: dict[str, str],
                 now_ms: float) -> str:
    """/fomotraders: every followed (or still held) trader with what copying it has earned us."""
    scores = scores or {}
    leaders = list(st.followed) + [p.leader for p in st.positions.values() if p.leader not in st.followed]
    blocks, grand = [], 0.0
    for a in by_rank(leaders, ranks):
        mine = [t for t in st.closed if t["leader"] == a]
        wins = sum(1 for t in mine if t["pnl"] > 0)
        held = [p for p in st.positions.values() if p.leader == a]
        live = sum(net_pnl(p, marks.get(p.coin)) for p in held)
        made = sum(t["pnl"] for t in mine) + live
        grand += made
        sc = scores.get(a, {})
        r = ranks.get(a)
        flag = " ⏸️ paused" if a in st.paused_leaders else ("" if a in st.followed else " ⏳ no longer followed")
        rows = [
            ("Made for you", money(made, st.equity0)),
            ("Copied", f"{_n(len(mine), 'trade')} · {wins} won, {len(mine) - wins} lost"
                       + (f" ({wins / len(mine) * 100:.0f}%)" if mine else "")),
            ("Open now", f"{_n(len(held), 'trade')} · {fusd(live)}"),
        ]
        if mine:
            rows.append(("Best", f"{fusd(max(t['pnl'] for t in mine))} · worst {fusd(min(t['pnl'] for t in mine))}"))
        if sc:
            rows.append(("Their record", f"{sc.get('trades', 0)} trades · {sc.get('win_rate', 0) * 100:.0f}% win"))
            rows.append(("Profit factor", pf_text(sc.get("profit_factor", 0))))
            rows.append(("Typical hold", f"{sc.get('median_hold_s', 0) / 60:.0f} min · "
                                         f"open bag {sc.get('open_buy_share', 0) * 100:.0f}% of their buys"))
            rows.append(("This week / today", f"{fusd(sc.get('pnl_7d', 0))} / {fusd(sc.get('pnl_24h', 0))}"))
        if a in st.followed and now_ms:
            rows.append(("Following for", dur(now_ms - st.followed[a])))
        if a in st.paused_leaders:
            rows.append(("Paused because", st.paused_leaders[a]))
        tag = (f"#{r} · " if r else "") + (f"{sc['score'] * 100:.0f} pts" if sc.get("score") else "no score")
        blocks.append(f"\n{dot(made)} <b>{esc(who(a, handles.get(a, '')))}</b> · {esc(tag)}{flag}\n" + pre(rows))
    if not blocks:
        return "👥 <b>FOMO traders</b>\nNone followed yet.\n" + updated(now_ms)
    head = (f"👥 <b>FOMO traders</b> · {len(st.followed)} followed\n"
            f"{dot(grand)} <b>Copying them made: {fusd(grand)} ({fpct(grand / st.equity0 * 100)})</b>")
    return "\n".join([head, *blocks, updated(now_ms)])


def positions_text(st: State, marks: dict) -> str:
    if not st.positions:
        return "📭 No open FOMO trades."
    lines = []
    for p in st.positions.values():
        net = net_pnl(p, marks.get(p.coin))
        lines.append(f"{dot(net)} <b>{esc(sym(p))}</b> ⬆️ BUY · {fusd(net)} · stop {fpx(p.stop_px)}")
    return f"📂 <b>Open FOMO trades</b> ({len(st.positions)})\n" + "\n".join(lines)


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
    stops = sum(1 for t in st.closed if t["reason"] == "stop")
    return f"🎯 <b>FOMO progress</b> · day {days:.1f} · {verdict}\n" + pre([
        ("Closed trades", f"{n} (target 50-100) · {wins} won, {n - wins} lost"),
        ("Result", f"{money(pnl, st.equity0)} after all costs"),
        ("Stopped out", f"{stops} of {n}"),
        ("Missed exits", f"{missed} {'✅' if not missed else '❌'}"),
        ("Caught by checks", f"{st.counters.get('reconcile_exits', 0)} exits"),
        ("Copy speed", (f"{med:.1f}s") if med is not None else "-"),
        ("Too small to copy", str(st.counters.get("skipped_min_notional", 0))),
    ]) + ("\n<i>Spot memecoins: only buys are possible, so every trade is a long. Memecoins can gap through a stop.</i>")


FOMO_HELP = ("🪙 <b>FOMO / Solana (paper)</b>\n"
             "/fomo – book, P&amp;L and connection (live)\n"
             "/fomotrades – open trades at live prices + P&amp;L vs the start (live)\n"
             "/fomotraders – followed traders and what copying them earned (live)\n"
             "/fomowallet – the paper wallet: cash, invested, fees, loss limits (live)\n"
             "/fomopositions · /fomoleaders · /fomoprogress\n"
             "/fomosearch – find active FOMO traders on-chain, rank them, follow the best 7\n"
             "/fomopause · /fomoresume – new entries (exits always run)\n"
             "/fomoflatten &lt;PIN&gt; – close every FOMO trade and pause\n"
             "/fomoreset &lt;PIN&gt; – the FOMO wallet back to the start (no open trades), traders kept")

SOL_HELP = FOMO_HELP     # old name
