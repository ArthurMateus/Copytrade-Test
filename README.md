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

Discord (optional, runs next to Telegram): create a bot at https://discord.com/developers/applications (scopes
`bot` + `applications.commands`; permissions View Channels, Send Messages, Embed Links, Read Message History),
then set `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` and `DISCORD_OWNER_ID` the same way. The same commands appear
as slash commands; only the owner can use them, and replies to commands are private.

Keep the PC from sleeping. State lives in `data/ledger.jsonl` (append-only, fsync on every write) and the
download cache lives in `data/cache/`. Logs go to `logs/copybot.log` (rotating, key=value, no secrets).
Settings are in `config/*.toml`. Every limit has a documented range, and the bot refuses to start when a
value is outside it.

Run the tests: `uv run pytest` (about 2 minutes; this includes real-process kill -9 restarts).

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

Commands (Telegram `/x`; Discord shows the same names as slash commands). The bot has two books that run at the same
time in one process, each with its own family of commands:

| Hyperliquid | FOMO (Solana) | What it does |
|---|---|---|
| `/hyperstatus` | `/fomo` | wallet, P&L, health (live card) |
| `/hypertrades` | `/fomotrades` | open trades at live prices and P&L against the $300 start (live) |
| `/hypertraders` | `/fomotraders` | followed traders: score, copied trades, wins/losses, money made (live) |
| `/hyperwallet` | `/fomowallet` | Hyperliquid: the same copies at 1/2/5/10/20% risk, compared. FOMO: cash, invested, fees, loss limits |
| `/hyperpositions` | `/fomopositions` | short list of open trades |
| `/hyperleaders` | `/fomoleaders` | followed wallets, one line each (live) |
| `/hyperprogress` | `/fomoprogress` | success metrics (Hyperliquid also shows longs and shorts separately) |
| `/hypersearch` | `/fomosearch` | look for new traders now |
| `/hyperadd 0x…` | `/fomoadd <wallet>` | check ONE wallet you found with the same strict rules now; followed at once if it passes and fewer than 7 are followed, else the bot says which rules failed. A FOMO wallet added this way is re-checked at every later search |
| `/hyperpause`, `/hyperresume` | `/fomopause`, `/fomoresume` | stop / allow new entries (exits always run) |
| `/hyperflatten <PIN>` | `/fomoflatten <PIN>` | close everything of that book and pause it |
| `/hyperreset <PIN>` | `/fomoreset <PIN>` | that book back to the start ($300, no history, traders kept, old history archived; refused while a trade is open) |

Plus `/help` and `/restart`. The short Hyperliquid names (`/status`, `/trades`, `/reset`, ...) still work on Telegram.
Each open trade gets one message, which is edited until it becomes the final ✅/❌ summary. A reset restarts the
process, so run the bot in the restart loop below. `/hyperreset` never touches FOMO and `/fomoreset` never touches Hyperliquid.

**Is the trader losing right now?** Besides the long history, a Hyperliquid wallet is only eligible if it is not in a bad
stretch: at most 3% of its account lost in 7 days (open losses count), 1.5% in 24 hours, no more than 4 losing round
trips in a row, at least 45% of its last 15 round trips won, and no drop above 10% of its hourly equity within 7 days
(`config/selection.toml`, with hard ranges).

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
- **Live account check:** every time a wallet is scored (followed leaders every hour), its open positions are read. A losing position it keeps open counts as a lost trade in its win rate and profit factor, so a trader cannot look good by never closing losers. A wallet whose open losses exceed `max_open_loss` (15%) of its account, or whose perp account is empty, is not eligible, and a followed one leaves after 2 cycles.
- **Reviews:** the scorer rescores the followed leaders and the top 15 every hour. A daily review downloads the leaderboard again and screens up to 400 pre-screened wallets (it stops once 100 are scored). A screen result is kept for 7 days.
- **Restart with doubt:** if the ledger has a torn line, an order intent with no result, or a position without a valid stop, the bot pauses entries, sends a Telegram alert and keeps running stops and exits. `/resume` acknowledges the problem.

## Solana memecoin book (FOMO traders, followed on-chain)

A second **paper-only** book copies FOMO traders (Solana memecoins), next to the Hyperliquid one. It has its own $300,
ledger (`data/sol/ledger.jsonl`), risk gate and scoring. It never signs, never holds a key and never sends a
transaction: fills are simulated from the real DexScreener pool price and liquidity. It needs no FOMO login.

**Where the traders come from (found 2026-10-08).** FOMO's own API refuses programs (Cloudflare answers
`{"authorization":false}` even for public pages), and the wallet address FOMO shows for a trader is not the wallet that
trades (listed addresses with thousands of FOMO swaps have no on-chain transactions). But FOMO pays the network fee of
every user swap, so each FOMO trade is a public Solana transaction co-signed by FOMO's fee payer
(`AgmLJBMD…zN51`, `sol.fomo_fee_payer`) and by the trader's **real** wallet. The bot samples that flow to find active
FOMO traders, reads their swaps from the chain, ranks them with the strict scoring, follows the best 7 and gets an
instant websocket alert when one of them trades. Copy tools such as CopyFomo also copy FOMO traders on-chain.

| Part | Module |
|---|---|
| Solana RPC client, swap parser, FOMO trader discovery, websocket alerts | `sol/chain.py` |
| Strict scoring from on-chain swaps | `sol/scoring.py` |
| Background scorer (discovery + history), disk cache | `sol/scorer.py` |
| DexScreener prices/liquidity + paper broker (price impact + 1% swap fee) | `sol/market.py` |
| The Solana risk gate | `sol/risk.py` |
| Leader swap detector + position manager (open/add/reduce/close, stop at entry) | `sol/trader.py` |
| Threads, selection, commands | `sol/runner.py`, `sol/fmt.py` |

**Data sources and cost.** With `HELIUS_API_KEY` set, everything goes to Helius (free plan: 1M credits a month,
10 requests a second): live alerts over its websocket, a safety poll every 120 s (`poll_leader_s`), and the search.
The search reads histories with Helius' `getTransactionsForAddress` (100 transactions per call, 10 credits); if your
plan refuses it, one by one (1 credit each), retrying bulk 6 hours later (log `sol_bulk_unavailable`). It may spend at
most `helius_daily_credits` (30,000) a day; past that it continues on the free public endpoint, so a search can never
use up the month. Without a key everything runs on the free public endpoint, which only allows about one transaction a
second: following works, but a search of 200 traders takes days.

**How a wallet is chosen.** A search (`/fomosearch`, at start, then daily) opens `discover_pages x discover_per_page`
(600) random FOMO transactions, keeps the `max_candidates` (200) traders that moved the most USD, and reads up to 30 days
of their swaps (`history_days`; a wallet whose newest 2,000 transactions cover less than 21 days is skipped as too busy,
which costs one cheap call). Every rule in `config/sol.toml` must pass (>= 40 round trips, win rate >= 40%, profit
factor >= 1.5, 3 of the last 4 weeks positive, no single trade > 20% or token > 30% of the profit, median hold >= 60 s so
it is not a sniper, open bag <= 30% of its buys, drawdown limits, and a positive copy edge after OUR fees, slippage and
entry lag). When a search ends after `/fomosearch` (or when nobody is followed yet) the best 7 are followed AT ONCE and
followed traders outside the new top 7 are dropped (their open copies still exit normally); an empty search drops
nobody. Otherwise the hourly cycle uses the usual hysteresis. `/fomoleaders` and `/fomo` show the search progress
("scoring 37/200 FOMO traders · 2 pass so far") and the best traders found but not followed. Swaps priced in SOL
instead of USDC (about 1 in 40 FOMO trades) are not seen.

**Risk (spot, no leverage).** 1% of the book per trade with a 30% stop = about $9.5 notional at $300; max 6% of the book in
one token; entries refused below $25k pool liquidity (or when DexScreener has no liquidity figure), when the price or
the leader feed is stale, or when the Helius key is refused. Exits are never refused. Memecoins can gap through a stop:
the paper fill uses the pool price when the stop is seen.

### Setup (Windows PowerShell)
1. **Helius key (recommended).** Free account at https://dashboard.helius.dev, copy the API key, then:
   ```powershell
   setx HELIUS_API_KEY "paste-key-here"
   ```
   Open a NEW terminal. Check it without showing it:
   `[Environment]::GetEnvironmentVariable("HELIUS_API_KEY", "User").Length` (about 36).
2. **Discord (optional).** Already set up for the Hyperliquid side (see above): the Solana commands show up as slash
   commands next to the others, and the Solana cards are posted and edited in place on Discord too.
3. `uv run copybot` (or the restart loop). The start message says where live trades come from.

Commands: the `/fomo...` column of the table above (only the owner can use them). `/fomosearch` runs a review now.

**Limits to know.** FOMO's fee payer address is FOMO's own choice and could change; if `/fomosearch` finds no traders,
it probably did (the log shows `sol_discovered traders=0`). Copy lag is the websocket alert plus one read (seconds); it is
measured and shown in `/fomoprogress`. Re-record the Solana test data with `tools/record_chain.py`.

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
