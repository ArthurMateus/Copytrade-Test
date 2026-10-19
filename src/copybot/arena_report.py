"""The Invo arena report page (data/arena/index.html): live season standings, the backtest ranking, and two line
charts (the season's wallets, the best recipes run season after season). One self-contained file: inline SVG, a few
lines of JS for the hover readout, light and dark colors. Pure: res (arena.run's result) -> HTML."""
from __future__ import annotations

import html
import json
import math
import time

WINDOW_DAYS = 14
DAY = 86_400_000
SERIES = ["--s1", "--s2", "--s3"]        # the top three get the first three categorical slots; the rest are gray

CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--card:#ffffff;--line:#e4e3df;--grid:#efeee9;--t1:#0b0b0b;--t2:#52514e;
--t3:#7a7975;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--gray:#a8a7a1;--pos:#256abf;--neg:#d03b3b;--chip:#f3f2ee}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1a1a19;--card:#21211f;
--line:#383835;--grid:#2c2c2a;--t1:#f0efec;--t2:#c3c2b7;--t3:#8f8e88;--s1:#3987e5;--s2:#d95926;--s3:#199e70;
--gray:#6b6a65;--pos:#5598e7;--neg:#e66767;--chip:#2c2c2a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--card:#21211f;--line:#383835;--grid:#2c2c2a;--t1:#f0efec;
--t2:#c3c2b7;--t3:#8f8e88;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--gray:#6b6a65;--pos:#5598e7;--neg:#e66767;
--chip:#2c2c2a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t1);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}p{margin:4px 0;color:var(--t2)}
.note{font-size:13px;color:var(--t3)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-top:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:16px}
.tile .v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}.tile .k{font-size:12px;color:var(--t2)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;font-weight:600;color:var(--t2)}td.n,th.n{text-align:right;white-space:nowrap}
tr.base td{color:var(--t2)}.pos{color:var(--pos)}.neg{color:var(--neg)}
.picks{font-size:12px;color:var(--t2);max-width:420px}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:0 6px;margin:1px 2px;font-size:12px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--t2);margin:4px 0 8px}
svg{display:block;width:100%;height:auto}svg text{fill:var(--t3);font-size:11px}
.tip{position:fixed;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:6px;
padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);display:none;z-index:9;max-width:320px}
details summary{cursor:pointer;color:var(--t2);font-size:13px}
"""

JS = """
document.querySelectorAll('svg[data-series]').forEach(svg=>{
  const S=JSON.parse(svg.dataset.series), xs=JSON.parse(svg.dataset.xs), fmt=svg.dataset.fmt;
  const tip=document.getElementById('tip'), cross=svg.querySelector('.cross');
  const W=+svg.dataset.w, L=+svg.dataset.l, R=+svg.dataset.r;
  svg.addEventListener('mousemove',e=>{
    const b=svg.getBoundingClientRect(), x=(e.clientX-b.left)*W/b.width;
    let i=0,best=1e9; xs.forEach((v,j)=>{const d=Math.abs(v-x); if(d<best){best=d;i=j}});
    cross.setAttribute('x1',xs[i]);cross.setAttribute('x2',xs[i]);cross.style.display='';
    let h='<b>'+S.labels[i]+'</b>';
    S.rows.map(r=>[r,r.v[i]]).filter(p=>p[1]!=null).sort((a,b)=>b[1]-a[1]).forEach(([r,v])=>{
      h+='<div><span class="sw" style="background:var('+r.c+')"></span>'+r.n+': <b>$'+v.toFixed(0)+'</b></div>'});
    tip.innerHTML=h;tip.style.display='block';
    tip.style.left=Math.min(e.clientX+14,innerWidth-330)+'px';tip.style.top=(e.clientY+14)+'px';
  });
  svg.addEventListener('mouseleave',()=>{tip.style.display='none';cross.style.display='none'});
});
"""


def esc(x) -> str:
    return html.escape(str(x))


def pct(x, signed=True) -> str:
    if x is None:
        return "–"
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<span class="{cls}">{x * 100:+.1f}%</span>' if signed else f"{x * 100:.0f}%"


def day(ms: int) -> str:
    return time.strftime("%d %b", time.gmtime(ms / 1000))


def chips(names) -> str:
    return "".join(f'<span class="chip">{esc(n)}</span>' for n in names)


def line_chart(series: list[dict], labels: list[str], title: str) -> str:
    """series: [{"n": name, "c": css var, "v": [equity or None per x], "dash": bool}] on one $ axis."""
    vals = [v for s in series for v in s["v"] if v is not None]
    if not vals or len(labels) < 2:
        return '<p class="note">Not enough data for a chart yet.</p>'
    W, H, L, R, T, B = 900, 300, 52, 16, 12, 28
    lo, hi = min(vals + [300.0]), max(vals + [300.0])
    pad = (hi - lo) * 0.08 or 10
    lo, hi = lo - pad, hi + pad
    xs = [L + (W - L - R) * i / (len(labels) - 1) for i in range(len(labels))]
    y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    step = 10 ** math.floor(math.log10(max(hi - lo, 1) / 4))
    step *= 5 if (hi - lo) / step > 20 else 2 if (hi - lo) / step > 8 else 1
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}" data-w="{W}" data-l="{L}" data-r="{R}" '
           f'data-xs="{esc(json.dumps([round(x, 1) for x in xs]))}" '
           f"data-series='{esc(json.dumps({'labels': labels, 'rows': series}))}'>"]
    g = math.ceil(lo / step) * step
    while g <= hi:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(g):.1f}" y2="{y(g):.1f}" style="stroke:var(--grid)" '
                   f'stroke-width="1"/><text x="{L - 6}" y="{y(g) + 4:.1f}" text-anchor="end">${g:,.0f}</text>')
        g += step
    out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(300):.1f}" y2="{y(300):.1f}" style="stroke:var(--t3)" '
               'stroke-width="1" stroke-dasharray="3 3"/>')
    every = max(1, len(labels) // 8)
    for i in range(0, len(labels), every):
        out.append(f'<text x="{xs[i]:.1f}" y="{H - 8}" text-anchor="middle">{esc(labels[i])}</text>')
    for s in sorted(series, key=lambda s: s["c"] != "--gray"):      # grays below the colored lines
        pts = [(xs[i], y(v)) for i, v in enumerate(s["v"]) if v is not None]
        if len(pts) < 2:
            continue
        d = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in pts)
        dash = ' stroke-dasharray="6 4"' if s.get("dash") else ""
        out.append(f'<path d="{d}" fill="none" style="stroke:var({s["c"]})" stroke-width="2" stroke-linejoin="round" '
                   f'stroke-linecap="round"{dash}/>')
        if s["c"] != "--gray":
            a, b = pts[-1]
            out.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="4" '
                       f'style="fill:var({s["c"]});stroke:var(--card)" stroke-width="2"/>')
    out.append(f'<line class="cross" x1="0" x2="0" y1="{T}" y2="{H - B}" style="stroke:var(--t3);display:none" '
               'stroke-width="1"/></svg>')
    legend = "".join(f'<span><span class="sw" style="background:var({s["c"]})"></span>{esc(s["n"])}</span>'
                     for s in series if s["c"] != "--gray")
    grays = [s["n"] for s in series if s["c"] == "--gray"]
    if grays:
        legend += (f'<span title="{esc(", ".join(grays))}"><span class="sw" style="background:var(--gray)"></span>'
                   f'{"others" if len(grays) > 1 else esc(grays[0])} (gray{", dashed = baselines" if any(s.get("dash") for s in series) else ""})</span>')
    return f'<div class="legend">{legend}</div>' + "".join(out)


def season_section(s: dict | None, now_ms: int) -> str:
    if not s:
        return '<h2>Live season</h2><p>No season yet: one starts after the first arena run with traders.</p>'
    st = s.get("standings") or []
    d = min(WINDOW_DAYS, max(1, math.ceil((min(now_ms, s["end"]) - s["start"]) / DAY)))
    lead = st[0] if st else None
    tiles = [("Season", f"day {d}/{WINDOW_DAYS}"), ("Ends", day(s["end"]))]
    if lead:
        tiles += [("Leader", f"${lead['equity']:.0f}"), ("Leader's recipe", lead["label"].split(" · ")[0])]
    out = ['<h2>Live season</h2>',
           f'<p>{day(s["start"])} → {day(s["end"])}. Each wallet starts at $300 and copies only calls opened since the '
           'season started, with the traders frozen at the start.</p>',
           '<div class="tiles">' + "".join(f'<div class="card tile"><div class="k">{esc(k)}</div>'
                                         f'<div class="v">{esc(v)}</div></div>' for k, v in tiles) + "</div>"]
    # chart: equity after each exit, on a daily grid
    days = max(2, d + 1)
    labels = [day(s["start"] + i * DAY) for i in range(days)]
    series = []
    for i, r in enumerate(st):
        curve = r.get("curve") or [[s["start"], 300.0]]
        v = []
        for j in range(days):
            t = s["start"] + j * DAY
            if t > min(now_ms, s["end"]):
                v.append(None)
                continue
            e = 300.0
            for ct, ce in curve:
                if ct <= t:
                    e = ce
            v.append(round(e, 2))
        if v and v[-1] is not None:
            v[-1] = round(r["equity"], 2)       # today: open copies at today's price
        base = r["key"] in ("random", "follows")
        series.append({"n": r["label"].split(" · ")[0] + ("" if base else f" ({r['key']})"),
                       "c": SERIES[i] if i < 3 else "--gray", "v": v, "dash": base})
    out.append('<div class="card">' + line_chart(series, labels, "Season wallets' equity") +
               '<p class="note">Realized equity at each day\'s end; the last point counts open copies at the latest '
               'price. Dashed = baselines.</p></div>')
    rows = "".join(
        f'<tr class="{"base" if r["key"] in ("random", "follows") else ""}"><td class="n">{i}</td>'
        f'<td>{esc(r["label"])}<div class="picks">{chips(r["picks"])}</div></td>'
        f'<td class="n">${r["equity"]:.2f}</td><td class="n">{pct(r["ret"])}</td><td class="n">{r["trades"]}</td>'
        f'<td class="n">{pct(r["win_rate"], False)}</td><td class="n">{r["max_dd"] * 100:.1f}%</td>'
        f'<td class="n">{pct(r.get("backtest_mean"))}</td></tr>'
        for i, r in enumerate(st, 1))
    out.append('<div class="card scroll"><table><tr><th class="n">#</th><th>Wallet and its traders</th>'
               '<th class="n">Equity</th><th class="n">Return</th><th class="n">Copies</th><th class="n">Wins</th>'
               f'<th class="n">Worst drop</th><th class="n">Backtest avg</th></tr>{rows}</table></div>')
    return "".join(out)


def backtest_section(bt: dict) -> str:
    rk = bt.get("ranking") or []
    if not rk:
        return ""
    recipes = [r for r in rk if r["recipe"] not in ("random", "follows", "hindsight")]
    base = {r["recipe"]: r for r in rk if r["recipe"] in ("random", "follows", "hindsight")}
    out = ['<h2>Backtest</h2>',
           f'<p>{bt["traders"]} traders, {bt["calls"]} calls, {len(bt["starts"])} two-week windows (one starting every '
           f'{bt["step_days"]} days). In each window a recipe picks its traders from the calls BEFORE the window, then '
           'copies the calls inside it.</p>']
    # chart: top 3 recipes + baselines, back-to-back windows ending today
    starts = bt["starts"]
    stepw = max(1, bt["window_days"] // bt["step_days"])
    idx = list(range(len(starts) - 1, -1, -stepw))[::-1]
    if len(idx) >= 1:
        labels = ["start"] + [day(starts[i] + bt["window_days"] * DAY) for i in idx]

        def chained(r):
            e, v = 300.0, [300.0]
            for i in idx:
                e *= 1 + r["windows"][i]["ret"]
                v.append(round(e, 2))
            return v
        series = [{"n": r["label"].split(" · ")[0] + f" ({r['key']})", "c": SERIES[i], "v": chained(r)}
                  for i, r in enumerate(recipes[:3])]
        series += [{"n": r["label"], "c": "--gray", "v": chained(r), "dash": True} for k, r in base.items()
                   if k != "hindsight"]
        out.append('<div class="card"><p>$300 run season after season (back-to-back 2-week windows ending today)</p>'
                   + line_chart(series, labels, "Backtest: best recipes season after season") + "</div>")
    head = ('<tr><th class="n">#</th><th>Recipe</th><th class="n">Avg 2 weeks</th><th class="n">Median</th>'
            '<th class="n">Worst</th><th class="n">Best</th><th class="n">Windows up</th>'
            '<th class="n">$300 back-to-back</th><th>Picks now</th></tr>')

    def row(i, r, cls=""):
        now = r["windows"][-1]["picks"] if r["windows"] else []
        return (f'<tr class="{cls}"><td class="n">{i}</td><td>{esc(r["label"])}</td><td class="n">{pct(r["mean"])}</td>'
                f'<td class="n">{pct(r["median"])}</td><td class="n">{pct(r["worst"])}</td>'
                f'<td class="n">{pct(r["best"])}</td><td class="n">{pct(r["positive"], False)}</td>'
                f'<td class="n">${300 * r["chained"]:.0f}</td><td class="picks">{chips(now)}</td></tr>')
    rows = "".join(row(i, r) for i, r in enumerate(recipes[:15], 1))
    rows += "".join(row("–", r, "base") for r in base.values())
    out.append(f'<div class="card scroll"><table>{head}{rows}</table>'
               '<p class="note">"Picks now" = the traders the recipe picked for the latest window. Gray rows are '
               'baselines: random = the middle of 20 random wallets of eligible traders (what luck gives); '
               'hindsight = the best 10 of each window, known only afterwards.</p></div>')
    if len(recipes) > 15:
        more = "".join(row(i, r) for i, r in enumerate(recipes[15:], 16))
        out.append(f'<details class="card"><summary>All {len(recipes)} recipes</summary><div class="scroll">'
                   f'<table>{head}{more}</table></div></details>')
    return "".join(out)


def past_section(past: list) -> str:
    if not past:
        return ""
    rows = []
    for p in past:
        st = p.get("standings") or []
        if not st:
            continue
        w = st[0]
        fav = max((r for r in st if r.get("backtest_mean") is not None), key=lambda r: r["backtest_mean"],
                  default=None)
        rows.append(f'<tr><td>{day(p["start"])} → {day(p["end"])}</td><td>{esc(w["label"])}</td>'
                    f'<td class="n">{pct(w["ret"])}</td><td>{esc(fav["label"]) if fav else "–"}</td>'
                    f'<td class="n">{pct(fav["ret"]) if fav else "–"}</td></tr>')
    return ('<h2>Past seasons</h2><div class="card scroll"><table><tr><th>Season</th><th>Winner</th>'
            '<th class="n">Return</th><th>Backtest favourite</th><th class="n">Its return</th></tr>'
            + "".join(rows) + "</table></div>")


def render(res: dict, now_ms: int) -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1"><title>Invo Arena</title>'
            f"<style>{CSS}</style></head><body><main>"
            "<h1>Invo arena</h1><p>Which set of at most 10 Invo traders makes a $300 paper wallet the most money in "
            f"2 weeks. Updated {time.strftime('%d %b %Y %H:%M UTC', time.gmtime(now_ms / 1000))}.</p>"
            + season_section(res.get("season"), now_ms) + backtest_section(res.get("backtest") or {})
            + past_section(res.get("past") or [])
            + '<h2>How to read this</h2><p class="note">Copies are replayed like the main Invo wallet: the trader\'s '
              "size × leverage of our equity (at most 1/3 of it), closed at the trader's exit or the liquidation "
              "guard, minus our costs and funding. Not modelled: our copy delay, the wallet's risk limits. The backtest "
              "is flattered: traders come from today's rankings and deleted calls are gone. The live season has "
              "neither problem, so trust it over the backtest. Paper only: nothing here trades.</p>"
            + f'</main><div id="tip" class="tip"></div><script>{JS}</script></body></html>')
