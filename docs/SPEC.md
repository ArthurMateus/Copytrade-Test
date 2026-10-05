# Original specification (as given by the owner)

This is the brief the bot was built from. Keep it as the reference for scope: features not listed here should
not be added without the owner asking.

---

# Prompt: build a minimal Hyperliquid copy-trading bot (paper mode first)

You are a senior Python engineer. Build a SMALL, RELIABLE copy-trading bot for Hyperliquid, in PAPER MODE ONLY (no real orders, no private keys, ever). Prefer the simplest design that works. Priorities in order: (1) never lose track of a position, (2) correctness over features, (3) visibility (always log what you are doing), (4) speed. Write tests for every rule below and run them. Do not add features that are not listed.

## Goal
Prove that copying a few well-chosen wallets can profit after fees, funding and slippage. $300 simulated wallet. Run unattended for 2-4 weeks on a Windows PC (Python 3.11, uv), controlled and monitored from Telegram.

## Core behaviour
1. Pick leaders automatically. Every cycle (hourly rescoring, a daily and weekly review), choose at most 7 wallets to follow from Hyperliquid's public leaderboard, ranked by a deterministic scoring function over each wallet's VERIFIED FILLS (never trust leaderboard numbers for the final ranking).
2. Follow every trade of a followed wallet: open, add, reduce, close, flip; long and short. Copy size = a small % of OUR wallet, computed from our risk limits, not the leader's size.
3. Mirror closes and reductions when the leader reduces/closes. Our own stop-loss is always placed at entry.
4. Re-score and kick out underperformers (daily/weekly), replace them from the ranked list, with hysteresis so wallets do not flip-flop (join at rank <= 8 for 2 consecutive cycles, drop at rank > 15 for 2 cycles, minimum 24 h followed, one swap per cycle). Pause a leader after a bad streak (copy drawdown > 10% or 5 consecutive losses).
5. Lag target: open/close our paper copy within 5 s of the leader's fill (less is better). Measure and log the lag of every copy (leader fill time -> our order time, p50/p95).
6. Telegram: commands /status /positions /leaders /progress /pause /resume /flatten <PIN>; automatic messages for every copy open/close, leader followed/dropped, errors. Only the owner's chat id may command it. The bot token and PIN come from environment variables only and must never be logged.
7. Restart safety: on restart reload all open positions and the followed set from an append-only ledger (JSON lines, fsync on write). Never double-open, never forget a stop. If anything is uncertain at restart: pause entries, alert, keep managing exits.

## Risk rules (hard limits, enforced in ONE place that every order must pass through)
- Risk per trade 1-2% of wallet (stop distance x size); daily loss limit 5% of wallet -> block new entries for the day; weekly loss limit 10%.
- Leverage up to 10x on any coin, but the stop must always sit well before liquidation (at least 3x the stop distance away).
- Max 7 leaders, max 10 open positions, max total open risk 10% of wallet, max 1.5% per symbol, max 30 orders per minute.
- Hyperliquid minimum order is $10 notional: a copy below $10 is skipped and LOGGED (with $300 this will happen).
- Fail closed: if the clock, price feed or leader data is stale or in doubt, refuse NEW entries; exits and stops keep working.
- Paper broker only: simulate fills at the current best bid/ask from the order book with a slippage and fee model (taker fee 0.045%, funding applied hourly). Never import a signing library or read a wallet key.

## Hyperliquid facts you MUST respect (all learned the hard way)
- Info endpoint: POST https://api.hyperliquid.xyz/info. Leaderboard: GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard (about 47,000 rows, NOT sorted by quality: rows have ethAddress, accountValue, windowPerformances [day, week, month, allTime -> pnl, roi, vlm]; the roi denominator is undocumented: do not rank on roi).
- Rate limit: about 1,200 request-weight per minute per IP. Weights: most info calls 20 (l2Book, allMids, clearinghouseState 2), userRole 60, portfolio 20; userFillsByTime/userFunding add 1 per 20 items returned (a full 2,000-fill page costs about 120); candleSnapshot adds 1 per 60 bars. Keep a budget class for exits/stops that scoring can never starve. HTTP 429 -> back off, do not hammer.
- userFillsByTime: max 2,000 fills per call, oldest first from startTime; only a wallet's latest 10,000 fills are retrievable in total. Page forward with the last fill time as the next startTime (inclusive: dedupe by tid). A first page that is full and spans less than one day = a high-frequency account: reject it at once.
- candleSnapshot: at most 5,000 bars per request (use 30-day chunks for 1h bars); the pseudo-coins named '#0', '#140', '#7521' ... are NOT perps and answer HTTP 500: ignore every coin starting with '#', '@' (spot) or containing ':' or '/'.
- Fills contain: coin, px, sz, side (B/A), time (ms), startPosition, dir, closedPnl, fee, crossed (false = maker), oid, tid, hash. Detect leader opens/adds/reduces/flips from startPosition and dir; use the websocket (userFills) for the leaders you follow, not polling.
- From Brazil the round trip is 0.3-0.5 s: clock offset uncertainty can reach 340 ms; tolerate up to 500 ms for entry freshness checks but never let an estimate of the exchange clock block exits.
- Never block the trading loop on a slow request: run leaderboard download, backfill and clock estimates on worker threads with a hard per-call timeout (2 s on the trading path).

## Wallet scoring and filters (script, deterministic, unit-tested)
Pre-screen from the leaderboard row alone: accountValue >= $10,000; active this week; month volume >= 2x account value but <= 500x (day <= 50x); positive pnl in the last month and before it; at least 10 bps pnl per dollar traded in both periods; month pnl <= 100% of account value; rank by pnl-per-dollar edge (cap 50 bps) then all-time pnl. Then screen the first 2,000 fills of each candidate: not empty, not too fast (< 56 fills/day on a full page), core-perp share of notional >= 50%, maker share <= 70%, history >= 60 days, median holding time >= 15 minutes, >= 150 closed round trips, at least half of its trades large enough to copy at our size. Then full history (180 days of fills + 1h candles per coin) and score: win rate, profit factor, pnl, number of trades, consistency across 30-day blocks (at least 4 of 6 positive), max drawdown <= 35%, current drawdown <= 20%, concentration (no single trade > 25% of pnl), copy edge after costs, a shrinkage on small trade counts. Followed set = top ranked eligible, max 7. Zero eligible wallets is a valid result: alert, do not follow anyone.
Start following as soon as 12 candidates are fully scored; keep scoring the rest in the background; cache all fetched data on disk so a restart resumes instead of repeating hours of downloads.

## Architecture (keep it small)
Single process, a few threads: trading loop (decisions + paper broker), worker threads (downloads, websocket, Telegram polling). One ledger. One risk gate module. One config folder of TOML files with documented ceilings validated at start. Structured logs to console and a rotating file (readable key=value text, no secrets). Everything testable against a loopback fake of the Hyperliquid REST/WS and Telegram APIs.

## How to work (to avoid bugs)
1. Write the data-contract tests FIRST using recorded real responses (record real samples of leaderboard, userFillsByTime, candleSnapshot, l2Book, clearinghouseState and websocket userFills before writing parsers). Synthetic fixtures hid every bug we had.
2. Build and test in this order: ledger + restart replay -> risk gate -> paper broker -> position manager (open/add/reduce/close/flip, stops) -> leader fill detector -> Telegram -> scoring script -> selection loop -> runner. After each step run all tests.
3. Kill -9 the process at random points in tests with an open position and prove the stop and position survive the restart.
4. Never mock your own code in tests; fake only the network.
5. Define success metrics before running: 50-100 trades in 2-4 weeks, 0 missed exits, 0 unexplained position mismatches, median copy lag <= 5 s, paper P&L after costs vs holding BTC. Kill criterion: clearly negative after costs on >= 50 trades, or any unexplained missed exit/stop-less position.
6. Be honest in reports: a few dozen paper trades show the bot works and profit is plausible, not that an edge exists.

---

## Telegram UI requirement (added by the owner)

TELEGRAM UI MUST BE EXTREMELY READABLE: short messages with clear emoji markers (for example 🟢 long, 🔴 short, ✅ win, ❌ loss, ⚠️ warning, 🛑 stop, ⏸️ paused), aligned fields, and one message per open trade that the bot keeps EDITING in place (editMessageText) so the numbers stay current instead of spamming new messages: coin, side, leverage, entry price, live mark price, size, stop, unrealised and realised P&L in $ and %, the leader's short address, the copy lag in seconds, and the time of the last update. When the trade closes, edit that same message into a final summary (✅/❌, exit price, final P&L in $ and %, reason). /status and /leaders are also single messages that are edited on refresh (price and P&L values update without new messages), rate-limited to respect Telegram limits (for example at most one edit per message every 5 seconds, and skip an edit when nothing changed). New messages only for new events (a trade opened, a leader followed or dropped, an error).
