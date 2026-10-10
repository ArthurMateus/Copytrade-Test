// Daily FOMO picks, step 1 (runs INSIDE a fomo.family tab where the owner is signed in; FOMO's API refuses scripts).
// Paste the whole file into the browser's javascript tool. It reads FOMO's 24h / 7d / 30d leaderboards, then the last
// 100 swaps of the best `MAX` traders (FOMO returns at most 100 per trader, no paging), turns USDC/USDT swaps into
// buy/sell legs exactly like the bot's on-chain parser (SOL-priced and token-to-token swaps are skipped), and keeps
// only traders that are not clearly failing (a LOOSE pre-screen: the real rules run in tools/fomo_picks.py).
// Result: window.__fomoPicks = {generated, checked, kept: {handle: {id, pnl24h, pnl7d, pnl30d, swapCount, tokens,
// legs: [[unix_s, token_index, 1=buy|0=sell, amount, usd], ...]}}}. It runs in the background (2-4 minutes): poll
// window.__fomoProgress until it says done (or error); then, with `uv run python tools/fomo_receive.py` running,
// call fomoSend(): the tab navigates to the local receiver with the data in the #fragment and it is saved to disk.
window.__fomoProgress = { phase: "starting", checked: 0 };
window.__fomoRun = (async () => {
  const MAX = 150;                     // traders whose swaps are read
  const GAP_MS = 700;                  // spacing between requests (be gentle with FOMO)
  const H = { "app-language": "en", "x-supported-chains": "1399811149" };
  const SOLNET = 1399811149;
  const USD = new Set(["EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"]);
  const SOLQ = new Set(["So11111111111111111111111111111111111111112", "11111111111111111111111111111111"]);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const get = async url => {
    const r = await fetch(url, { credentials: "include", headers: H });
    if (r.status === 401 || r.status === 403) throw new Error("FOMO refused (" + r.status + "): sign in at fomo.family");
    if (!r.ok) throw new Error("FOMO http " + r.status + " for " + url);
    return (await r.json()).responseObject || {};
  };
  const lb = {};
  for (const w of ["24h", "7d", "30d"]) {
    const rows = (await get(`https://prod-api.fomo.family/v2/leaderboard/${w}`)).leaderboard || [];
    for (const x of rows) {
      const e = lb[x.id] || (lb[x.id] = { id: x.id, handle: x.userHandle, swapCount: x.swapCount || 0 });
      e["pnl" + w] = Math.round(x["pnl" + w] || 0);
    }
    await sleep(GAP_MS);
  }
  const all = Object.values(lb);
  // best first: 30-day profit, then 7-day, then 24h (a trader on several boards counts once)
  all.sort((a, b) => (b.pnl30d || 0) - (a.pnl30d || 0) || (b.pnl7d || 0) - (a.pnl7d || 0) || (b.pnl24h || 0) - (a.pnl24h || 0));
  const kept = {};
  let checked = 0;
  for (const e of all.slice(0, MAX)) {
    let swaps = [];
    try { swaps = (await get(`https://prod-api.fomo.family/v2/users/${e.id}/swaps?limit=100`)).swaps || []; }
    catch (err) { if (/refused/.test(String(err))) throw err; await sleep(GAP_MS); continue; }
    checked++;
    window.__fomoProgress = { phase: "reading swaps", checked, of: Math.min(MAX, all.length) };
    const tokens = [], ti = t => { let i = tokens.indexOf(t); if (i < 0) { i = tokens.length; tokens.push(t); } return i; };
    const legs = [];
    for (const s of swaps) {
      if (s.networkId !== SOLNET) continue;
      const i = s.inTokenAddress, o = s.outTokenAddress, t = Math.floor(Date.parse(s.createdAt) / 1000);
      if (USD.has(i) && !USD.has(o) && !SOLQ.has(o)) legs.push([t, ti(o), 1, +s.outHumanAmount, +(+s.inHumanAmount).toFixed(4)]);
      else if (USD.has(o) && !USD.has(i) && !SOLQ.has(i)) legs.push([t, ti(i), 0, +s.inHumanAmount, +(+s.outHumanAmount).toFixed(4)]);
    }
    legs.sort((a, b) => a[0] - b[0]);
    // loose pre-screen: average-cost round trips like the bot's scoring; keep if it could possibly pass
    const pos = {}, nets = [];
    for (const [t, k, buy, amt, usd] of legs) {
      let p = pos[k];
      if (buy) { if (!p) p = pos[k] = { amt: 0, peak: 0, cost: 0, tot: 0, proc: 0 }; p.amt += amt; p.peak = Math.max(p.peak, p.amt); p.cost += usd; p.tot += usd; continue; }
      if (!p || p.amt <= 1e-12) continue;
      const f = Math.min(1, amt / p.amt); p.cost -= p.cost * f; p.amt -= Math.min(amt, p.amt); p.proc += usd;
      if (p.amt <= p.peak * 0.01) { nets.push(p.proc - p.tot); delete pos[k]; }
    }
    const pnl = nets.reduce((a, b) => a + b, 0), gp = nets.filter(x => x > 0).reduce((a, b) => a + b, 0);
    const gl = -nets.filter(x => x < 0).reduce((a, b) => a + b, 0), best = Math.max(0, ...nets);
    const ok = nets.length >= 10 && pnl > 0 && (gl === 0 || gp / gl >= 1.2) && best / pnl <= 0.5;
    if (ok) kept[e.handle] = { id: e.id, pnl24h: e.pnl24h || 0, pnl7d: e.pnl7d || 0, pnl30d: e.pnl30d || 0, swapCount: e.swapCount, tokens, legs };
    await sleep(GAP_MS);
  }
  window.__fomoPicks = { generated: new Date().toISOString(), leaderboard: all.length, checked, kept };
  window.fomoSend = () => { location.href = "http://127.0.0.1:8765/recv#" + encodeURIComponent(JSON.stringify(window.__fomoPicks)); return "sent"; };
  window.__fomoProgress = { phase: "done", leaderboard: all.length, checked, kept: Object.keys(kept).length,
                            chunks: Math.ceil(Object.keys(kept).length / 6) };
})().catch(err => { window.__fomoProgress = { phase: "error", error: String(err) }; });
"started: poll window.__fomoProgress"

