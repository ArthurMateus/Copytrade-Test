# copybot: Hyperliquid copy-trading, paper mode

This bot copies up to 7 automatically selected Hyperliquid wallets on a **simulated** $300 wallet.
**It runs in PAPER MODE ONLY.** It has no signing library and no key handling, and it never calls an order
endpoint. A test enforces this (`tests/test_safety.py`).

## Run (Windows, Python 3.11, uv)

```powershell
setx TELEGRAM_BOT_TOKEN "<token from @BotFather>"
setx TELEGRAM_CHAT_ID   "<your numeric chat id>"
setx COPYBOT_PIN        "<a PIN for /flatten>"
# open a NEW terminal so the variables are visible, then:
uv run copybot
```

For unattended runs, restart the bot automatically if it crashes. It is designed to be killed at any moment:

```powershell
while ($true) { uv run copybot; Start-Sleep 10 }
```

Keep the PC from sleeping. State lives in `data/ledger.jsonl` (append-only, fsync on every write) and the
download cache lives in `data/cache/`. Logs go to `logs/copybot.log` (rotating, key=value, no secrets).
Settings are in `config/*.toml`. Every limit has a documented range, and the bot refuses to start when a
value is outside it.

Run the tests: `uv run pytest` (about 1 minute; this includes real-process kill -9 restarts).

## What it does

| Part | Module |
|---|---|
| Append-only ledger; restart = replay of the same events | `ledger.py` |
| The one risk gate every order passes | `risk.py` |
| Paper fills that walk the real L2 book: 0.045% taker fee, slippage, hourly funding | `broker.py` |
| Open/add/reduce/close/flip mirroring, stop at entry, reconcile, leader pause | `positions.py` |
| Leader fill detection from `startPosition` (websocket `userFills`) | `detector.py` |
| Websocket feed (15-user cap) and exchange-clock estimate | `feed.py` |
| Pre-screen, first-page screen, 180-day score (deterministic) | `scoring.py` |
| Hysteresis, background scorer, disk cache | `selection.py` |
| Telegram commands and in-place edited cards | `tg.py`, `tgfmt.py` |
| Threads and the trading loop | `runner.py` |

Telegram commands: `/status` and `/leaders` (live cards with rank and score, edited in place), `/positions`, `/progress`, `/pause`,
`/resume`, and `/flatten <PIN>`. Each open trade gets one message, which is edited until it becomes the final
✅/❌ summary.

### Design choices the spec left open
- **Copy size:** each open risks 1% of equity: size = 1% × equity / (3% stop distance), which is about $100 notional at $300.
  After that our target is `k × leader position`, so adds, reduces and closes stay proportional to the leader. Adds are clamped by the 1.5% per-symbol and 10% total risk caps.
  More fills of the order that opened the position do not add; they only re-anchor `k`.
- **Main coins only:** BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX, LINK, LTC (`config/selection.toml`, `main_coins`). Wallets are scored only on these coins, and the risk gate refuses entries on anything else. Exits always work. **Exception 🎲:** a wallet that makes money in at least 3 coins, with no coin above half of its profit, is scored on all its perp trades and copied in every Hyperliquid perp, memecoins included (`alt_min_coins`, `alt_max_coin_share`).
- **One net position per coin, as on the exchange.** When a second followed leader opens the **same side** on a coin we hold, we treat it as agreement 🤝: we add 0.5% extra risk (still capped at 1.5% per coin), and if the first leader exits while the second still holds, the position is handed over instead of closed. When a leader opens the **opposite side** ⚔️, the leader with the higher wallet score wins: we switch only if the newcomer scores strictly higher.
- **Stops are a fixed 3% from the fill price.** Leverage is the highest value (≤10x and ≤ the coin's maximum) that still keeps liquidation at least 3× the stop distance away.
- **Exits are never refused.** Stale prices, a paused bot, uncertain state, or the 30 orders/min limit all block entries only. If the book cannot be fetched, an exit fills at mid ± 20 bps.
- **Missed websocket fills** are caught by reconciling against `clearinghouseState` every 60 s and right after any reconnect.
- **Leader pause:** a leader is paused after 5 consecutive losing copies, or when its copy drawdown exceeds 10% of its allocation (equity / 7). The next cycle drops it, and it cannot rejoin for 7 days.
- **Wallet score 1–100:** a daily review screens pre-screened wallets until the top 100 are fully scored. Wallets that cannot be copied are rejected; every other wallet gets points out of 100 (edge after costs 25, profit factor 15, monthly consistency 15, number of trades 15, win rate 10, max drawdown 10, current drawdown 5, concentration 5). The top 7 are followed (rank ≤ 7 for 2 hourly cycles; drop at rank > 15 for 2 cycles).
- **Reviews:** the scorer rescores the followed leaders and the top 15 every hour. A daily review downloads the leaderboard again and screens up to 400 pre-screened wallets (it stops once 100 are scored). A screen result is kept for 7 days.
- **Restart with doubt:** if the ledger has a torn line, an order intent with no result, or a position without a valid stop, the bot pauses entries, sends a Telegram alert and keeps running stops and exits. `/resume` acknowledges the problem.

## Findings from the real API (recorded in `tests/fixtures`, re-record with `tools/record_samples.py`)
- One websocket can track at most **15 users** (`"Cannot track more than 15 total users."`).
- Live `userFills` messages have no `isSnapshot` key, and `hash` can be all zeros.
- Spot fills (`@107`) have `dir` set to `Buy`/`Sell`. Builder perps like `xyz:TSLA` show up in fills and are ignored.
- The `#140` candles request answers HTTP 500.
- This PC's clock was about **1.4 s ahead** of the exchange (±0.15 s). The offset is measured and corrected.
- Most of the top pre-screened wallets fail the first-page screen as too fast or HFT. Zero eligible wallets is a valid outcome: the bot alerts and follows nobody.

## Success metrics (fixed before running)
- 50–100 copied trades in 2–4 weeks.
- 0 missed exits and 0 unexplained position mismatches. Exits caught by reconcile are counted separately.
- Median copy lag ≤ 5 s, measured from the leader's fill time on the exchange clock to our paper fill. Every copy is logged, and `/status` shows p50/p95.
- Paper P&L after fees, funding and slippage, compared with holding BTC from the first start.

**Kill criterion:** P&L clearly negative after costs over ≥ 50 trades, or any unexplained missed exit or
stop-less position. `/progress` shows all of these.

A few dozen paper trades show that the bot works and that a profit is plausible. They do not prove an edge exists.
