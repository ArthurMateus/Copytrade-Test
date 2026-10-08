# CLAUDE.md: context for working on this repo

Hyperliquid copy-trading bot, **PAPER MODE ONLY**. It copies up to 7 automatically selected wallets on a
simulated $300 wallet. It is controlled from Telegram and runs unattended for 2–4 weeks on the owner's Windows PC.
The full original brief is in [docs/SPEC.md](docs/SPEC.md). It is the scope reference: do not add features
that are not listed there unless the owner asks. The README covers run instructions and design choices.

## Owner
- Runs Windows 11 with a Brazilian Portuguese locale (Windows error messages come out in Portuguese) and uses
  PowerShell.
- Not a Python developer. Give exact copy-paste commands, one per block, and explain results in plain words.
- GitHub: https://github.com/ArthurMateus/Copytrade-Test (branch `main`).

## Hard rules (never break)
- Never import a signing library, read a wallet key, or call an exchange/order endpoint.
  `tests/test_safety.py` enforces this.
- Secrets come from environment variables only: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `COPYBOT_PIN`,
  `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_OWNER_ID`, and for the Solana book `HELIUS_API_KEY` (optional).
  Never log or commit them. `log.py` redacts them.
- Every order goes through `RiskGate.check` (`risk.py`). Exits (reduce/close/stop) are never refused; entries
  fail closed.
- Every state change goes through the ledger: `ledger.append(ev)` then `state.apply(ev)`. A restart replays the
  same `apply`, so never mutate `State` outside an event.
- Tests fake only the network (`tests/fakes.py`: loopback HL REST + WS + Telegram). Never mock our own code.
  Parsers are tested against real recorded responses in `tests/fixtures` (re-record with
  `tools/record_samples.py`).
- Run `uv run pytest` after every change (about 2.5 minutes, 250+ tests at the time of writing, including real-process
  kill -9 restarts).

## Layout (`src/copybot/`)
| Module | Role |
|---|---|
| `config.py` | Loads TOML from `config/` (one file per section) and validates the `CEILINGS`; unknown keys are an error |
| `log.py` | key=value logs, rotating file, secret redaction |
| `ledger.py` | `Ledger` (append-only JSONL, fsync), `State`, `Position` and the event fold. Unresolved intents, torn tail lines and stop-less positions go into `state.uncertain` |
| `hl.py` | Data contracts/parsers, `RateBudget` (CRITICAL vs BULK classes, BULK can't touch the reserve), `Info` client with a hard timeout, fill paging |
| `risk.py` | The gate. Sizing: 1% risk at a 3% stop, about $100 notional. Leverage = highest ≤10x that keeps liquidation ≥ 3× the stop distance away. Entries only on `selection.main_coins`; `boost` = consensus extra size |
| `broker.py` | Paper fills that walk the real L2 book plus slippage; taker fee 0.045%; funding helper |
| `detector.py` | Websocket fills → leader `Move`s (open/add/reduce/close/flip) from `startPosition`; dedupe by tid; snapshots never trade |
| `positions.py` | Mirrors moves using `k = our size / leader position`; stop at entry; reconcile against `clearinghouseState`; leader pause rules; consensus (backers, boost, handover) and conflicts |
| `scoring.py` | Pure, deterministic: `prescreen` → `fill_screen` (first 2,000 fills) → `full_score` (180 d fills + 1h candles, 0–100 points) → `ranking`. `VERSION` invalidates cached results |
| `selection.py` | Pure `select()` hysteresis; `Scorer` thread with a disk cache in `data/cache` (resumes after a restart) |
| `feed.py` | Websocket (allMids + userFills, max 15 users) and the exchange `Clock` offset estimate |
| `tg.py`, `tgfmt.py` | Telegram: owner-only commands, outbox; cards edited in place, rate-limited; renderers (shared by Discord). Live cards: /status, /leaders, /trades, /traders, /wallets (`Bot.render_card`). Card conventions: 🟢/🔴 = money only, ⬆️/⬇️ = side, one P&L after fees+funding, `label: value` lines (no `<pre>`), times in `telegram.utc_offset_hours` |
| `discord.py` | Discord (owner request 2026-10-06), runs NEXT TO Telegram via `MultiUI`: `DiscordUI` subclasses `TelegramUI` (same outbox/card logic) over REST + Gateway (stdlib + `websockets.sync`), guild slash commands, owner-only, ephemeral replies (PIN never shown). Telegram HTML → markdown embeds coloured by money. Discord card ids live in the ledger as `dc:<key>` |
| `runner.py` | `Bot`: boot/repair, worker threads, trading loop, commands, selection application, heartbeat |
| `wallets.py` | Side wallets: same moves at other risk levels (`risk.side_wallets_risk_pct`), own ledger in `data/wallets/<name>/`, limits scaled by `config.scaled`; `boot_repair` shared with the main wallet |
| `sol/` | **Solana memecoin paper book** (own $300, own ledger `data/sol/`, own risk gate). `chain.py` Solana RPC client + swap parser + FOMO trader discovery + websocket alerts, `scoring.py` strict pure scoring, `scorer.py` thread + cache, `market.py` DexScreener + paper broker, `risk.py` gate, `trader.py` detector + positions, `runner.py` `SolBot` (threads inside the main process), `fmt.py` cards |

Runtime state: `data/ledger.jsonl` (the source of truth), `data/cache/`, `data/bot.lock` (single instance) and
`logs/copybot.log`. All of these are git-ignored. Moving the bot to another PC means copying `data/`. Never run two
instances on the same wallet.

## Decisions made where the spec was open
- One net position per coin (as on the exchange), owned by one leader. Owner decision (2026-10-05):
  - **Agreement:** a second followed leader opening the SAME side becomes a *backer*; we add `risk.consensus_risk_pct`
    (0.5%) extra risk, still capped by the 1.5% per-symbol limit. Backer adds/reduces only refresh its size; its
    close/flip trims its share (`frac`). If the OWNER exits while a backer holds, the position is handed over to
    the best-scored backer and trimmed back to a normal 1% copy (`handover` event) instead of closing.
  - **Conflict:** a followed leader opening the OPPOSITE side wins only if its wallet score is strictly higher than
    every leader on our side: we close (`conflict_better_leader`) and copy it. Otherwise (and on ties) it is skipped.
  - A handed-over position's whole P&L is credited to the new owner's leader stats.
- Fills of the order that opened a copy only re-anchor `k`; they never add. An add with no copy of ours is skipped.
- A partial mirror under $10 is skipped. A reduce that would leave under $10 becomes a full close.
- Missed websocket fills are caught by reconcile every 60 s and right after any reconnect.
- Leader pause: copy drawdown > 10% of (equity / 7) or 5 consecutive losses. A paused leader is dropped at the next cycle, then a 7-day cooldown.
- Followed-set changes (owner request 2026-10-05): bad leaders leave at once (all paused ones, and every leader no
  longer eligible for 2 cycles, even if followed < 24 h). New leaders join at most once per `change_cooldown_hours`
  (24 h, counted from the newest join); free slots fill together then. Rank-based swaps (eligible but rank > 15)
  only happen in that window, one per cycle, after `min_follow_hours`. Max 7 leaders.
- A screen result is cached for 7 days per wallet (rejected wallets get retried after that).
- Leaders are picked once `min_scored_to_start` (12) wallets are fully scored OR the first review has finished
  (`Scorer.ready`): real reviews of 400 candidates fully score only a handful, so waiting for 12 meant following nobody.
- `/resume` writes an `ack` event that clears acknowledged restart uncertainties.
- Owner commands added 2026-10-06: `/search` re-picks at once with `selection.rebalance` (top `max_leaders` of
  the ranking, no window/confirmation; followed ones outside it are dropped, their open copies managed until
  exit) and asks the scorer for a review (`search_req` → `("searched", ...)` → re-pick again; an empty ranking
  never drops anyone). `/reset <PIN>` (no open trade in any wallet) archives `data/ledger.jsonl` and
  `data/wallets/` to `data/archive/reset-<UTC>/`, writes a fresh ledger seeded with genesis + followed (original
  'since') + drop cooldowns + pauses + selection streaks, freezes the old ledgers and restarts. `/restart` stops
  the loop after 3 s; the owner's PowerShell `while` loop starts it again. `/reset`, `/restart`, `/flatten` sent
  before the current start are ignored (Telegram can redeliver them).
- Side wallets (owner request 2026-10-05): 2/5/10/20% risk, $300 each, compared by `/wallets`. EVERY risk limit
  scales with the level (per-symbol, total, consensus, daily/weekly loss stops capped at 100%); leverage, stop
  distance and liquidation buffer do not. They are allowed above the CEILINGS on purpose (paper comparison only).
  They follow the main wallet's followed/paused sets (`sync_leaders`), never pause leaders themselves, post no trade
  cards, and get the main wallet's moves after it (books reused for 1 s). A side-wallet error is logged/alerted and
  never reaches the main wallet. /pause, /resume and /flatten act on every wallet.
- Lag = exchange-clock time of our paper fill minus the leader's fill time.

## Solana / FOMO / Discord (added 2026-10-07 at the owner's request; not in the original spec)
- **On-chain since 2026-10-08 (FOMO's API dropped).** FOMO's API (`prod-api.fomo.family`) refuses every non-browser
  client with 403 `{"authorization":false}`, even for public pages without a cookie (it is Cloudflare, not the login), and
  the `address` it shows for a trader is NOT the trading wallet (verified: listed addresses with thousands of swaps have
  no on-chain transactions). FOMO pays every user swap's fee: each FOMO trade is co-signed by FOMO's fee payer
  `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51` (`sol.fomo_fee_payer`) and the trader's real wallet (the other signer).
  `tests/fixtures/chain_tx_fomo_buy.json` is the on-chain side of the first swap in `fomo_swaps.json` (same amount, +1 s,
  89.0483 USDC vs FOMO's 88.0983: FOMO's 0.95 fee). The scorer samples the fee payer's flow (`discover_pages` x
  `discover_per_page` random transactions), keeps the `max_candidates` traders by USD moved, reads their history and
  scores it (`sol/scoring.py`, unchanged rules; the leaderboard pre-screen is gone). Do not bypass FOMO's Cloudflare.
- Sources (measured 2026-10-08): the free public RPC allows only ~1 getTransaction/s and answers parallel reads with 429,
  so with `HELIUS_API_KEY` EVERYTHING uses Helius (one shared `Rpc` limiter, 0.11 s): live, discovery and history. Helius
  bills getTransaction/getSignaturesForAddress 1 credit (also for old data) and `getTransactionsForAddress` 10 credits
  per 100 full transactions (used for history, `ChainClient(bulk=True)`; free-plan availability unverified: if refused,
  one by one and bulk retried after 6 h). `FallbackRpc` caps the search at `helius_daily_credits` per UTC day, then
  the public RPC. Calls used: getSignaturesForAddress, getTransaction, getTransactionsForAddress, websocket
  logsSubscribe. `LogWatch` only WAKES the poller; the poller (safety poll `poll_leader_s` = 120 s) reads the swaps,
  so a dead websocket adds lag but never loses a trade.
- Search UX (owner request 2026-10-08): `/fomosearch` (or the first review when nobody is followed) ends with
  `hysteresis.rebalance`: top `max_leaders` (7) followed at once, followed ones outside it dropped, empty ranking drops
  nobody. `SolScorer.progress` feeds the search line in `/fomo` and `/fomoleaders` (also "best found, not followed");
  a `/fomosearch` during a running search only reports progress. `history_days` = 30 to keep a search affordable.
  Seeder and scorer can read one wallet's history together: `SolScorer.history` locks per wallet (Windows refused two
  writers of one cache file).
- Swap parsing (`chain.parse_tx`): the wallet's USDC/USDT moves one way and one token the other. A routed swap leaves a
  speck of the intermediate token (fixture `chain_tx_fomo_routed.json`): the token with the largest relative balance
  change wins if every other moved < 10%. SOL-priced swaps (~1 in 40) are skipped (no USD price). Leg id = signature.
- pump.fun was checked and has no trader leaderboard (Cloudflare, undocumented endpoints): not used.
- Discord is the owner's own implementation (`discord.py`, slash commands over the Gateway, `MultiUI` fan-out); the Solana
  book just talks to the same `ui`. Slash commands `/fomo`, `/fomotrades`, ... are in `tg.COMMANDS`.
- Ledger reuse: Solana positions are `Position(side=+1, leverage=1, coin=<mint>, sym=<symbol>)` in the same `State`; a
  `cursor` event stores the newest handled swap time per leader so a restart never re-copies old swaps.
- Entries need DexScreener liquidity >= `min_liquidity_usd`; bonding-curve pools often have none -> refused (fail closed).
- Reduces are target-based (`k x leader balance`), so a trim too small to trade is caught up by the next one.
- Bug found and fixed while building it: `_skip(..., kind=...)` collided with `notify(kind, ...)` (also in `positions.py`).
- Open risks: FOMO could change its fee payer (discovery would find 0 traders: log `sol_discovered traders=0`); followed
  wallets keep working. The first review on the public RPC takes hours. Copy lag = websocket alert + one read.

## Command families and resets (2026-10-08)
- Hyperliquid: `/hyper<x>`; FOMO book: `/fomo<x>` (`tg.ALIASES`/`canon` turn every name into one canonical name
  before `Bot.command`; `/fomo*` goes to `SolBot.command`). Discord lists only `/hyper*`, `/fomo*`, `/help`, `/restart`.
- `/hyperreset` (HL ledgers + side wallets) and `/fomoreset` (`data/sol/`) each archive the old ledger under their own
  `archive/reset-<stamp>/`, keep followed traders, pauses, cooldowns and swap cursors, are refused while a trade is open,
  and restart the process (`restart_at`). Neither touches the other book.
- HL eligibility now also needs: loss_7d <= 3%, loss_24h <= 1.5%, <= 4 losses in a row, last-15 win rate >= 45%, 7d
  drawdown <= 10% (`selection.max_loss_7d` ... `max_dd_7d`; in `ScoreParams.rules()` so cached scores are redone).
- `/hyperprogress` shows longs vs shorts and warns when every copy was a long. Code path checked: the detector and the
  open path treat shorts exactly like longs (real fills: shorts detected). An all-long run is a data effect (the followed
  traders were all long, or the market) until a ledger shows otherwise: send `data/ledger.jsonl` to investigate.

## Findings from the real API (2026-10-05)
- `tests/test_telegram.py::test_card_is_edited_in_place_rate_limited_and_skips_unchanged` is timing-based and can
  fail rarely under load; rerun before suspecting a bug.
- Hyperliquid closes the websocket about every 3 h with code 1000 "Expired"; the bot reconnects in 2–17 s and
  reconciles. Only an outage longer than `runtime.ws_alert_after_s` (60 s) alerts on Telegram.
- One websocket can track at most 15 users. Live `userFills` messages have no `isSnapshot`, and `hash` can be all zeros.
- Spot fills (`@107`) have `dir` = `Buy`/`Sell`. Builder perps (`xyz:TSLA`) appear in fills and are ignored. `#140` candles return HTTP 500.
- The owner's PC clock runs about 1.4–1.7 s ahead of the exchange (±0.15 s). It is corrected via the `Clock` offset.
- 47,266 leaderboard rows → about 2,168 pass the pre-screen. Most top candidates then fail the first-page screen:
  `too_fast`, `high_frequency`, or `round_trips<150` because they accumulate or hold a core position and almost never
  go flat (verified on real wallets; it is not a bug). Because of this, `max_candidates` was raised from 60 to 400, then to 2000 (owner request).

## Status and open questions
- **Live since 2026-10-05** on the owner's notebook (started from PowerShell with `python -m uv run copybot` in a
  restart loop; `uv` is not on PATH there). Telegram works against the real API. First review: 400 screened,
  13 fully scored; under the strict floors 3 leaders followed (0xc0b2…, 0x8a80…, 0xb556…). First copy 2026-10-06
  09:55 UTC: NEAR long from 0x8a80…, copied by all 5 wallets in ~0.5 s.
- Real-world issues already fixed: Telegram read timeouts killed the command thread; a second program polled the
  same bot token (getUpdates conflict; the owner changed tokens); websocket "Expired" closes every ~3 h alerted.
- Owner environment quirks: the Claude desktop app is a packaged app, so anything it installs under %APPDATA%
  is invisible to the owner's own terminal (install uv/Python from the owner's terminal). A VS Code-style terminal
  may auto-activate `.venv` (`(copybot)` prompt): run `deactivate` first, and load the secrets with
  `[Environment]::GetEnvironmentVariable(name, "User")` if the terminal predates `setx`.
- Next: after ~50 trades, analyse results per leader/coin/wallet risk level (owner wants this offline, not an AI
  in the trading path).
- **Scoring (owner decision 2026-10-05, differs from the spec on purpose):** only `selection.main_coins` (BTC, ETH,
  SOL, XRP, BNB, DOGE, ADA, AVAX, LINK, LTC) are screened, scored and copied. Hard rejects are only the copyability
  gates: too fast/HFT, core-perp share < 50% (spot), no main-coin trades, maker > 70%, history < 60 d, < 30 main-coin
  round trips, median hold < 15 min, too small to copy, pnl ≤ 0, PF ≤ 1, copy edge after costs ≤ 0. A wallet's
  off-list (alt) trading is ignored, not held against it: a first live run with a "main-coin share ≥ 50%" gate
  rejected 41 of the first 120 candidates (many top wallets trade HYPE/alts with some BTC/ETH on the side). Quality is points out of 100 (`scoring.WEIGHTS`):
  edge 25, profit factor 15, consistency 15, trade count 15, win rate 10, max DD 10, current DD 5, concentration 5.
  Eligibility floors (owner request 2026-10-05): win rate ≥ `min_win_rate` (60%), score ≥ `min_score` (70) and profit
  factor ≥ `min_profit_factor` (2.0), biggest drop ≤ `max_drawdown` (30%, owner request 2026-10-06). Each score stores its `rules`; changing a floor rescores at the next start.
  **Live account (owner request 2026-10-06, `scoring.VERSION` 5):** `Scorer.score` reads the wallet's
  `clearinghouseState` (`Info.account`, BULK). Each losing open position in the scored coins counts as a lost trade
  (win rate and profit factor); open losses over `max_open_loss` (15%) of max(perp value, leaderboard value), counting
  every coin, reject (`open_losses>15%`), and an empty perp account with no position rejects (`account_empty`).
  Followed leaders are rescored every cycle, so they leave after `confirm_cycles`. Found live: followed wallets with a
  90% win rate sitting on open losses of 60% of the account, and one that moved all its money out of perps.
  A failed account fetch skips these gates (logged `account_fetch_failed`).
  `max_candidates` = 2000 (≈ all of the ~2,200 pre-screened; the first full pass takes 5–6 h, about 7 wallets/min).
  `scoring.SCREEN_VERSION` and `scoring.VERSION` are separate: a score-only change rescores the screened wallets
  from cached fills at startup (`Scorer.rescore_missing`) instead of re-screening 400 wallets.
  A review screens until `pool_size` (100) wallets are fully scored; `join_rank` = 7 so the top 7 get followed
  (hysteresis unchanged). Check `review_done` / `event=scored` in the log.
- **Diversified wallets unlock alts/memecoins (owner request 2026-10-05):** a wallet that is profitable overall, net
  profitable in ≥ `alt_min_coins` (3) coins and has no coin above `alt_max_coin_share` (50%) of its profit is
  scored on ALL its core perps and copied in all of them (`Score.diversified`, `RiskGate.alts_ok`). Everyone else
  stays on the main coins. Spot (`@`), outcome (`#`) and builder (`xyz:`) markets stay excluded.
- Success metrics and the kill criterion are in the README and shown by `/progress`.
