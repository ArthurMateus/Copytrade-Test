# CLAUDE.md: context for working on this repo

Copy-trading bot, **PAPER MODE ONLY**, with three books in one process: ⚡ Hyperliquid wallets, 🪙 FOMO (Solana
memecoin) traders followed on-chain, and 🧾 Invo traders' posted calls (traded on Hyperliquid prices). Each book has
simulated $300 wallets at several risk levels. It is controlled from Discord only (Telegram removed 2026-10-10) and
runs unattended on the owner's Windows PC. **New chat? Read `context.md` first** (current state, who does what, what
Claude must never do), then this file. The original brief is [docs/SPEC.md](docs/SPEC.md) (Hyperliquid only; the
FOMO and Invo books and everything marked "owner request" were added later at the owner's request). Do not add
features nobody asked for. The README covers run instructions and design choices.

## Owner
- Runs Windows 11 with a Brazilian Portuguese locale (Windows error messages come out in Portuguese) and uses
  PowerShell.
- Not a Python developer. Give exact copy-paste commands, one per block, and explain results in plain words.
- GitHub: https://github.com/ArthurMateus/Copytrade-Test (branch `main`).

## Hard rules (never break)
- Never import a signing library, read a wallet key, or call an exchange/order endpoint.
  `tests/test_safety.py` enforces this.
- Secrets come from environment variables only: `COPYBOT_PIN`, `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_OWNER_ID`, for the Solana book `HELIUS_API_KEY` (optional), and
  for the Invo calls wallet `INVO_TOKEN_FILE` (path of the file holding the bot's Invo refresh token; optional).
  Never log or commit them. `log.py` redacts them.
- Every order goes through `RiskGate.check` (`risk.py`). Exits (reduce/close/stop) are never refused; entries
  fail closed.
- Every state change goes through the ledger: `ledger.append(ev)` then `state.apply(ev)`. A restart replays the
  same `apply`, so never mutate `State` outside an event.
- Tests fake only the network (`tests/fakes.py`: loopback HL REST + WS + Discord REST/Gateway; `FakeDiscord.say("/cmd arg")` sends a slash command and `sent`/`edits` carry `text` = the card HTML rebuilt from the markdown, `md_to_card`). Never mock our own code.
  Parsers are tested against real recorded responses in `tests/fixtures` (re-record with
  `tools/record_samples.py`).
- Run `uv run pytest` after every change (about 5.5 minutes, 320+ tests at the time of writing, including
  real-process kill -9 restarts).

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
| `chat.py`, `cardfmt.py` | Platform-free chat: command names (`VERBS` x `BOOKS`)/aliases/help, `ChatUI` outbox (cards edited in place, rate-limited, 429-aware); renderers in a small HTML subset. Live cards: status, trades, traders, wallets of each book (`Bot.render_card`, `SolBot.render`). Card conventions: 🟢/🔴 = money only, ⬆️/⬇️ = side, one P&L after fees+funding, `label: value` lines (no `<pre>`), times in `discord.utc_offset_hours` |
| `discord.py` | The ONLY chat (Telegram removed 2026-10-10 at the owner's request): `DiscordUI(ChatUI)` over REST + Gateway (stdlib + `websockets.sync`), guild slash commands, owner-only, ephemeral replies (PIN never shown), card HTML → markdown embeds coloured by money. Card ids live in the ledger as `dc:<key>` (keys without the prefix are old Telegram messages, ignored) |
| `runner.py` | `Bot`: boot/repair (also boots the FOMO book), worker threads, trading loop, commands of the Hyperliquid and Invo books, selection application, daily picks, heartbeat |
| `invo.py`, `invo_scorer.py` | Invo: parsers, `InvoClient` (rotating refresh token), `Watcher` (polls followed traders, portfolio filter); the daily Invo search scoring each trader's calls with `scoring.full_score` |
| `picks.py` | The daily picks report (render, day-to-day comparison, `data/picks.json`) |
| `wallets.py` | Side wallets: same moves at other risk levels (`risk.side_wallets_risk_pct`), own ledger in `data/wallets/<name>/`, limits scaled by `config.scaled`; `boot_repair` shared with the main wallet |
| `sol/` | **Solana memecoin paper book** (own $300, own ledger `data/sol/`, own risk gate). `chain.py` Solana RPC client + swap parser + FOMO trader discovery + websocket alerts, `scoring.py` strict pure scoring, `scorer.py` thread + cache, `market.py` DexScreener + paper broker, `risk.py` gate, `trader.py` detector + positions, `runner.py` `SolBot` (threads inside the main process), `fmt.py` cards |

Tools (`tools/`): `hyper_why.py` / `fomo_why.py` / `invo_why.py` (why traders fail), `invo_check.py` (Invo login),
`record_samples.py` / `record_chain.py` (fixtures), and the FOMO daily-picks pipeline `fomo_collect.js` +
`fomo_receive.py` + `fomo_picks.py` + `fomo_picks.html` (run by a scheduled Claude task, see below).

Runtime state: `data/ledger.jsonl` (the source of truth), `data/wallets/<name>/` (side and Invo wallets),
`data/sol/` (FOMO book), `data/cache/`, `data/picks.json`, `data/bot.lock` (single instance) and
`logs/copybot.log`; `reports/` on the Claude PC (FOMO picks). All of these are git-ignored. Moving the bot to another PC means copying `data/`. Never run two
instances on the same wallet.

## Decisions made where the spec was open
(Hyperliquid commands below use their canonical short names: `/resume` = `/hyperresume`, `/search` = `/hypersearch`,
`/wallets` = `/hyperwallets`, ... The owner types the `/hyper…` names in Discord.)
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
  before the current start used to be ignored (Telegram redelivered them; Discord never does).
- Side wallets (owner request 2026-10-05): 2/5/10/20% risk, $300 each, compared by `/wallets`. EVERY risk limit
  scales with the level (per-symbol, total, consensus, daily/weekly loss stops capped at 100%); leverage, stop
  distance and liquidation buffer do not. They are allowed above the CEILINGS on purpose (paper comparison only).
  They follow the main wallet's followed/paused sets (`sync_leaders`), never pause leaders themselves, post no trade
  cards, and get the main wallet's moves after it (books reused for 1 s). A side-wallet error is logged/alerted and
  never reaches the main wallet. /hyperpause, /hyperresume and /hyperflatten act on every Hyperliquid wallet (not on
  the Invo wallets, which have their own /invo… controls since 2026-10-10).
- Lag = exchange-clock time of our paper fill minus the leader's fill time.
- Mirror side wallet (owner request 2026-10-09): `mirror_x10` in `data/wallets/`, sized by `Bot.mirror_size` =
  leader's new position notional / (its perp account value + spot stablecoins, since 2026-10-10) (leverage included) x `risk.mirror_mult` (10) x our
  equity, lifted to `min_notional_usd` (so small bets are not skipped). Limits = a `mirror_limits_pct` (5%) side
  wallet's (they clamp big bets). Account values of followed leaders: `sync_worker` every 5 min (BULK); unknown value
  -> the fixed 1% risk size (log `mirror_size_fallback`). Adds/reduces follow `k` like every wallet. Shown in
  /hyperwallets as "mirror x10 (their % of account)".
- HL floors changed (owner request 2026-10-09): `min_win_rate` 0.60 -> 0.45, plus a NEW hard gate
  `max_best_trade_share` 0.30 (best round trip / total pnl, "one_trade>30%_of_profit"). Both are in
  `ScoreParams.rules()`, so cached scores are redone at the next start. `tools/hyper_why.py` explains rejections.

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
  nobody. `SolScorer.progress` feeds the search line in `/fomostatus` and `/fomotraders`;
  a `/fomosearch` during a running search only reports progress. `history_days` = 30 to keep a search affordable.
  Seeder and scorer can read one wallet's history together: `SolScorer.history` locks per wallet (Windows refused two
  writers of one cache file).
- `/hyperadd 0x…` and `/fomoadd <wallet>` (owner request 2026-10-09): the owner supplies a wallet (Claude suggests
  some in a session or in the FOMO daily-picks task; only the owner sends the commands); the scorer thread checks it at once (`add_q`, also between wallets
  of a running review: HL re-screens even if screened this week; FOMO marks it `manual` in the pool, never pruned and
  re-checked at every search). Same strict rules, no bypass. Passes + free slot (< max_leaders) -> followed at once
  (`apply_plan`); else the reply says why (rules failed, already followed, paused, or full -> next search). Discord gets
  a required `wallet` option (`chat.WALLET_COMMANDS`).
- Owner picks (owner request 2026-10-09: "use Claude, not Helius, to find FOMO traders"): `/fomofollow <wallet>`
  follows at once WITHOUT the strict scoring (follow event `picked: true` -> `State.picked`, kept by `/fomoreset`);
  `select`/`rebalance` never drop a pick for rank (`keep=`), only when paused after a bad copy streak; the RiskGate and
  leader pause rules still apply. `/fomounfollow` drops it (open copies exit normally). Without `HELIUS_API_KEY` the
  daily search is off (`SolScorer.auto_search`, `sol.search_without_helius`); `/fomosearch` still works (slow). A new
  leader is seeded from its newest `seed_txs` (300) transactions unless a cached history exists (`SolScorer.recent`).
  How Claude picks (in a session, on request only, owner signed in to fomo.family in the browser pane): FOMO ranking
  + mofo.gg leaderboard (Wilson lower bound of 7-day win rate, win = coin +50% within 48 h) -> each handle's recent
  swaps read in the signed-in tab (`/v2/users/{id}/swaps`, id from the profile page's balances request) -> real
  wallet = owner whose balance of that mint changed by exactly that amount in a FOMO-co-signed tx near the time
  (FOMO's timestamp can trail the chain by minutes). fomowalletfinder.com gives the same for indexed handles (new
  lookups 2/day). The `address` in FOMO's own data is never the trading wallet.
- `tools/fomo_why.py` / `tools/hyper_why.py`: why scored wallets fail, from the saved caches.
- Swap parsing (`chain.parse_tx`): the wallet's USDC/USDT moves one way and one token the other. A routed swap leaves a
  speck of the intermediate token (fixture `chain_tx_fomo_routed.json`): the token with the largest relative balance
  change wins if every other moved < 10%. SOL-priced swaps (~1 in 40) are skipped (no USD price). Leg id = signature.
- pump.fun was checked and has no trader leaderboard (Cloudflare, undocumented endpoints): not used.
- Discord (`discord.py`, slash commands over the Gateway) is the only chat; the Solana book talks to the same `ui`.
  `/fomo…` commands go to `SolBot.command` (`chat.canon` first).
- Ledger reuse: Solana positions are `Position(side=+1, leverage=1, coin=<mint>, sym=<symbol>)` in the same `State`; a
  `cursor` event stores the newest handled swap time per leader so a restart never re-copies old swaps.
- Entries need DexScreener liquidity >= `min_liquidity_usd`; bonding-curve pools often have none -> refused (fail closed).
- Reduces are target-based (`k x leader balance`), so a trim too small to trade is caught up by the next one.
- Bug found and fixed while building it: `_skip(..., kind=...)` collided with `notify(kind, ...)` (also in `positions.py`).
- Open risks: FOMO could change its fee payer (discovery would find 0 traders: log `sol_discovered traders=0`); followed
  wallets keep working. The first review on the public RPC takes hours. Copy lag = websocket alert + one read.

## Invo calls (owner request 2026-10-09; not in the original spec)
- Invo (Involio, app.invoapp.com, a Flutter web app) traders post CALLS in PAPER portfolios (`"type": "paper"`; BTC
  entries with cents cannot be Hyperliquid fills). Invo never exposes other traders' Hyperliquid addresses
  (`master_address`/`wallet_address` in the app code belong to the logged-in user's own trading account), so the calls
  are the signal. `copybot/invo.py`: parsers (fixtures `tests/fixtures/invo_*.json`, recorded from the web app with an
  in-page XHR hook), `InvoClient` (POST JSON, `Authorization: Bearer <access>`; `/v1_0/auth/refresh_token` with
  `Bearer <refresh>` returns `accessToken`+`refreshToken`, the refresh token ROTATES and is written back to
  `INVO_TOKEN_FILE`; refresh tokens live ~1 year), `Watcher` (thread: one get_users_portfolios per followed trader per
  `invo.poll_s`, get_investments only for portfolios whose `openTrades` changed; first poll = baseline, never copied;
  new call <= `max_call_age_s` -> `("invo_open", user, Call)`, vanished call -> `("invo_close", ...)`).
- The "invo calls" wallet is a `SideWallet(own_leaders=True)` in `data/wallets/invo_calls/` (leaders `invo:<user>`):
  `on_sides` skips it for sync/move/reconcile; `position_leaders` excludes `invo:` (no HL address). Calls become
  synthetic `Move`s (start 0 -> +-1 open, +-1 -> 0 close) through the normal PositionManager + RiskGate (limits of an
  `invo.limits_pct` 5% wallet, our 3% stop, `max_entry_age_s` = max_call_age_s + poll_s); size via `Bot.invo_size` =
  their positionSize x leverage x `invo.size_mult` x our equity (>= 10$), capped by `Bot.invo_cap` =
  `invo.max_risk_pct` (2%) of equity at our stop, for opens AND adds (`PositionManager.cap`, skip `copy_cap`; owner
  request 2026-10-09 after a 25% x 5x STRK call became a 375$ copy). Tickers mapped to HL names case-insensitively;
  not on HL -> told, not copied. `/hyperreset` does not touch the Invo wallets (`/invoreset` does). Off without
  INVO_TOKEN_FILE (`/invostatus` says how).
- Adds and trims (owner request 2026-10-09): a call's COMMITTED size is `entrySize` (percent, fixed unless the trader
  adds or trims); `positionSize` drifts with the price and must not drive copies. Synthetic Moves use the committed
  exposure (entrySize/100 x leverage) as the leader "position", so `k = our size / exposure` and PositionManager
  mirrors an add (entry, RiskGate) or a trim (exit, never refused) in proportion. The watcher re-reads a portfolio's
  calls when its `updatedAt` changes and reports `("invo_resize", user, old, new)` for changes > `invo.RESIZE_MIN` (2%).
- Live UI (owner request 2026-10-09): `/invostatus`, `/invotrades`, `/invotraders`, `/invowallets` are live cards (keys `invo:*`,
  `Bot.render_card`, edited only when the body changes); each copy has its own live card `invo:pos:<pos_id>` with the
  trader's call (and resizes) that becomes the final summary on any close (the Invo wallet's PositionManager notifies
  `Bot.on_invo_notify`). `cardfmt.short("invo:x")` -> "@x".
- Risk-level wallets (owner request 2026-10-09: "1/2/5/10/20% of OUR wallet, same levels everywhere, set in config"):
  Invo: `invo.risk_wallets_pct` -> `Bot.invo_extra` = `SideWallet(own_leaders=True)` named `invo_risk_<r>pct`
  (fixed-risk sizing, no sizer), following the trader-size wallet's Invo traders (`sync_invo_extras`); `on_invo` runs
  every Invo wallet (`invo_one`, errors isolated per wallet; `invo_calls` keyed (wallet, call id)); only the trader-size
  wallet posts trade cards/notes. `/invowallets` compares them; `/hyperwallets` now lists only HL wallets
  (`not w.own_leaders`). FOMO: `sol.side_wallets_risk_pct` (2/5/10/20) -> `SolBot.sides` = `sol/wallets.SolSide`
  (own ledger `data/sol/wallets/<name>/`, own SolGate with `scaled()` limits, shared Prices/Detector, synced
  followed+pauses every tick, moves after the main wallet, `on_sides` isolates errors, prices fetched for every
  wallet's tokens, `wanted()` includes every wallet's open leaders, /fomopause/resume/flatten act on all,
  /fomoreset refused while any wallet holds a trade and archives `data/sol/wallets/`). `/fomowallets` compares them
  (it used to be an alias of `/fomowallet`).
- The bot must use its OWN Invo account (rotation: a browser and the bot on one login log each other out). The owner
  gets the refresh token with a DevTools console snippet (README) that decrypts `FlutterSecureStorage.REFRESH_TOKEN`
  (AES-GCM, key in `localStorage.FlutterSecureStorage`) and copies it; `tools/invo_check.py` verifies it.

## One start message (owner request 2026-10-10)
- `Bot.boot` boots the FOMO book itself (`SolBot.boot`, then `SolBot.run(booted=True)` on its thread) and sends ONE
  "Copybot started" message with a line per book (`SolBot.start_line`). The "nobody passed / following nobody new"
  alerts only fire when searches follow by themselves (`auto_follow`); in report-only mode the daily picks say it.

## Invo portfolio filter and blocks (owner request 2026-10-10)
- A trader's calls are copied only from its portfolios with win rate >= `invo.min_portfolio_win_rate` (0.80), return
  >= `min_portfolio_pnl_pct` (100%; Invo's `plSnapshot` is the portfolio's % return: $100 -> $130 shows ~28-30) and
  >= `min_portfolio_calls` (20) closed calls, and not blocked by the owner (`/invoblock <user> <portfolio>`, matched by
  the start of its title; `/invounblock`). Blocks are ledger events `invo_block`/`invo_unblock` in the trader-size Invo
  wallet -> `State.blocked` {leader: {portfolio id: title}}. `Bot.invo_skip(name, Portfolio)` gives the reason; the
  `Watcher(skip=)` drops new calls of skipped portfolios (log `invo_call_skipped`) but closes/resizes of existing
  copies still flow; `InvoScorer(skip=)` scores a trader only on the portfolios we would copy. /invotraders lists only
  the copied (✅) portfolios (none: one 🚫 line). `/invoblock` and `/invounblock` are the only book-specific commands
  (`chat.INVO_EXTRA`).

## One command set for every book (owner request 2026-10-10)
- `chat.VERBS` (status, trades, traders, wallets, progress, search, add, follow, unfollow, pause, resume, flatten,
  reset) x `chat.BOOKS` (hyper, fomo, invo) = the 39 slash commands (+ /help /restart /picks). Canonical names: HL the
  short ones (/status ...), FOMO /fomo + /fomo<verb>, Invo /invo + /invo<verb>; old names are ALIASES (hidden).
  Removed as commands: positions, leaders, /fomowallet (its money rows are in /fomostatus), bare /fomo and /invo.
- Each book's controls act on its own wallets only: `Bot.on_sides` skips own_leaders wallets for pause/resume/flatten
  too; `/hyperreset` archives only the main + HL side wallet dirs (Invo dirs stay); `/invopause` `/invoresume`
  `/invoflatten` `/invoreset` (`Bot.invo_reset`, archive `data/archive/invo-reset-<stamp>/`) act on `invo_wallets()`.
- New: `/hyperfollow` / `/hyperunfollow` (`Bot.hyper_follow`: follow event `picked: true`; `select`/`rebalance` take
  `keep=st.picked`, a pick is only dropped when paused), `/invoadd` (`InvoScorer.add_q` -> `("invo_added", name,
  score)` -> `Bot.on_invo_added`), `/invoprogress`.

## Invo copy speed (owner request 2026-10-10)
- `invo.poll_s` 30 -> 5 and `invo.request_gap_s` = 0.5 (the client's spacing, was a fixed 1 s): a new call is seen
  within ~3.5 s (7 traders) + 5 s instead of up to ~40 s (a STRK copy was 26 s late). Invo has no push channel we can
  use; if it starts refusing requests (watch `invo_poll_failed` in the log), raise them again. Copies are always filled
  on Hyperliquid's real book at Hyperliquid's fees; Invo's entry price is only shown ("their entry").
- 2026-10-11 (owner: "lower than 3 s?"): `poll_s` 0, `request_gap_s` 0.25 (~3.4 req/s; 7 traders = ~2 s per round,
  a call seen ~1 s after posting on average). Measured in the owner's Chrome: ~0.29 s per request, ~6 req/s clean,
  ~13 req/s began to fail; the app's only websocket is Hyperliquid's; `/posts/get_feed` (`filter: moves|recent|
  following`, `params.itemLimit`) is a small curated subset (0 of booobsas's 37 calls), so per-trader polling stays.
  `Watcher.round`: a failing trader no longer ends the round; all reads failing -> wait 2, 4 ... 60 s. The daily
  Invo search shares the client's spacing, so rounds are slower while it runs.

## Invo audit fixes (owner request 2026-10-11, after a review by four subagents)
- Watcher: a call missing from a read is closed only when it shows in its portfolio's closed calls (page 1; the
  close then carries Invo's closing price and reason) or after `invo.VANISH_S` (10 s) if it never does (log
  `invo_call_vanished`, reason "call deleted by the trader"). A portfolio answer without the `portfolios` list is a
  failed read. An open-call list shorter than the portfolio's count is read again (up to `REREAD_MAX` rounds), and a
  first read waits for it before the baseline.
- Each copy's Invo call id and card text are a ledger event `invo_link` (`State.invo_links`, by pos_id): a restart
  re-links copies by call id (coin+side only for older copies), so another portfolio's call on the same coin no longer
  moves our copy, and the card text survives. `on_invo_baseline` waits (`invo_baseline_wait`) until Hyperliquid's coin
  list is loaded (the `meta` event): without it every copy looked orphaned and was closed.
- Leader pause of the Invo wallet: copy drawdown > `invo.pause_dd_pct` (4%) of the wallet (was 10% of equity/7 =
  4.29$, one stop of a 140$ copy). Applied by setting the wallet's `leader_pause_dd_pct` = pause_dd_pct x max_leaders.
- `invo.aliases` (Invo ticker -> HL coin, e.g. MATIC = "POL"); a rotated refresh token that cannot be written stays in
  memory and is retried every round (log `invo_token_save_failed`); `invo_entry_gap` logs our entry vs the trader's
  (in their unit, kPEPE x1000) and the card shows it ("ours +0.12% worse").
- Search (`invo_scorer.VERSION` 2, cached scores redone): every portfolio of a trader is scored (active or not,
  whatever its own win rate: judging only the 80%-win ones selected on the outcome); a trader needs >= 1 portfolio
  passing the copy floors (`no_copyable_portfolio`); each call pays our `cost_bps` (13) as fees, so a +0.01% Invo "win"
  is a loss; PEPE/BONK/... are scored on kPEPE prices (`invo_trade_coin`). Fields `portfolios`,
  `copyable_portfolios`, `losing_portfolios` per score. Not done yet (owner to decide): a walk-forward test of the
  selection, ranking by a shrunk lower bound, skipping calls whose price already moved past the trader's entry.

## Invo per-portfolio scoring, real-money portfolios, Invo's score (owner request 2026-10-12)
- Study first (top 100 of Invo's new score leaderboard, 180 d of calls replayed on HL 1h candles, 0.11% cost): Invo's
  score had ~0 rank correlation with copy results (also on the next 30 days); a trader's own results persisted when
  following its exits (0.45) but much less with our 3% stop (0.15); 18 of 100 were "live" = real money on Hyperliquid
  through Invo, 9 of the 11 best. Bait seen: 3 calls = 76-87% of the profit, minute-long calls, many weak portfolios.
- Invo API (base WITHOUT /v1_0, `InvoClient.root`): POST /analytics/score/user/top {"listType":"all"},
  /analytics/score/portfolio {"portfolioId"}; live portfolios are EMPTY in /v1_0 and read from GET
  /dex/portfolio/live/<id>/overview and /dex/portfolio/position/<id>/list?isOpen=&limit=&cursor= (fixtures
  `invo_live_*.json`, `invo_score_*.json`). Other /dex/ endpoints place trades: `test_the_bot_never_calls_an_invo_
  trading_endpoint` allows only /dex/portfolio/.
- Live in the watcher: overview every `OVERVIEW_S` (300 s, real accuracy/return/closed as the Portfolio's numbers),
  open positions every round; `Call.units` (coins) detects adds/trims (size.pct drifts with the price); closes are
  confirmed in its closed positions. A live call's size is its % of the trader's equity (often < 1%: the trader-size
  wallet then copies the 10$ minimum; the fixed-risk wallets size normally).
- Search (`invo_scorer.VERSION` 3): candidates also from the score leaderboard; EACH portfolio scored on its own calls
  (`score_trader`) with Invo's score kept beside it (`by_portfolio[id]`: kind, eligible, score, invo_score, copy);
  `trader_result`: score = best copyable portfolio's, eligible only if one is copyable and all portfolios together
  did not lose (`loses_overall`). `Bot.invo_skip`: blocked; trader loses overall; scored portfolio failing our rules;
  a scored live one passes; else the owner's floors (`invo_floors`). The scorer's own filter is `invo_floor`
  (blocked + paper floors). `/invofollow` queues a quiet check (`add_q` item `(name, True)`) that says which
  portfolios are copied. Picks show the best portfolio, "real money" and Invo's score.

## Invo sizes x stops (owner request 2026-10-12: "3%, 8% and stop whenever the trader stops", for every size)
- `invo.stops_pct` [3, 8, 0] (must include risk.stop_pct): every Invo size (trader size = `invo_calls`, and each
  `risk_wallets_pct` level) runs once per stop: the 3% ones are the existing wallets (names/history unchanged), the
  others `<name>_stop8` / `<name>_trader` (18 wallets, all in `Bot.invo_extra` except the primary; same traders and
  calls). A size's versions copy the same notional (risk wallets: equity x r / 3%, i.e. sized at the usual stop;
  trader size: `invo_size` + `invo_cap`), so only the exit differs. Variants: `liq_buffer_mult` 1 (the stop only has
  to sit before liquidation) and limits of a wallet risking that notional at that stop (`risk x stop / 3`).
- 0 = the trader's exit: no stop of ours but a guard `invo_emergency_stop(mult)` = min(30%, 95% of (1/ceil(mult) -
  2.5%)): a copy that big (mult = notional / equity) is liquidated about there (20% risk: ~11.2%, 10%: ~21%).
  Positions always keep a valid stop (ledger/boot_repair).
- `/invowallets` = `cardfmt.invo_wallets_card`: 3 best + 3 worst combinations (size · stop) and the average result of
  each stop over all sizes. Copying one call into all 18 takes ~0.1 s (measured in the e2e test env).

## Invo follow-ups (owner request 2026-10-12)
- A refused real-money read (`/dex/` GETs: 401/403 are `InvoError`, not a lost login, `login_errors=False`) is logged
  `invo_live_read_failed`, reported once as `("invo_live", user, False, why)` -> Discord alert (and `True` when it works
  again), keeps the last known calls (never closes a copy) and blocks the trader's first baseline until a real answer.
- `invo.live_size_mult` (10): real-money calls (`Call.live`) are sized x10 in the trader-size wallet (they bet ~1%
  of the account), still capped by `max_risk_pct`.
- Search charges funding (`invo_scorer.VERSION` 4): `Scorer.funding` (fundingHistory, 20-day cached chunks,
  `hl.parse_funding`, fixture `funding_history.json`) -> `calls_to_fills(..., funding_of)` takes the funding paid while
  each call was held off its result (and so off the copy edge).

## Only Invo runs (owner request 2026-10-12)
- `selection.enabled` = false (config/selection.toml) turns the Hyperliquid book off: no HL side wallets/mirror, no
  HL search thread (`Scorer` object kept: it serves candles and funding to the Invo search), no followed wallets' fills
  (`wanted_users` = only leaders of still-open old copies, so they can exit), the main wallet gets a `pause` event
  ("Hyperliquid book off"), its daily progress message and picks are skipped, /hyper… commands answer "off" (except
  /picks /restart /flatten) and are not registered on Discord (but /hyperflatten). `sol.enabled` = false likewise
  (/fomo… hidden). Hyperliquid still gives every price, book and fill of the Invo copies. Tests turn HL back on in
  `test_e2e.write_config`; `test_with_the_hyperliquid_book_off_only_invo_runs` covers the off mode.
- `invo.max_traders` 7 -> 10 (owner request 2026-10-12).
- Short names (owner request 2026-10-12): with the HL book off (`DiscordUI.invo_only`), Discord lists Invo's commands
  WITHOUT the prefix (`chat.INVO_ONLY_COMMANDS`: /status /wallets /follow ... /block /unblock, + /hyperflatten);
  `chat.invo_name` maps a typed short name to the Invo command, /invo… names still work, and every outgoing message
  is rewritten by `chat.short_names` ("/invofollow x" -> "/follow x"). /help = `chat.INVO_HELP`. /block and /unblock
  are aliases of /invoblock and /invounblock in every mode.

## Invo 429s (live 2026-10-12: a copy 138 s late)
- The log showed Invo answering 429 to every read (and Discord 429 every ~6 s). `InvoClient.throttle`: a 429 doubles
  the shared request spacing (`slow`, up to `invo.SLOW_MAX_S` 3 s, Retry-After honoured up to 30 s), eased x0.75 per
  `CALM_S` (60 s) without one (logs `invo_throttled` / `invo_throttle_eased`); the search waits while
  `client.throttled()` (copies first). `Watcher.closed_call` returns `UNREAD` when Invo did not answer: a vanished call
  is then kept (before, copies were closed as "deleted by the trader" after VANISH_S during 429s).
- Discord: a card edit answered 429 no longer retries in place (`ChatUI._write(retry=False)`): the card waits 30 s and
  new messages go first (Discord limits edits of old messages hard; the live cards are old).

## Invo: ask before changing, picks, only hard failures skip a portfolio (owner request 2026-10-12)
- A 3 AM search dropped all 7 followed traders: VERSION bumps re-searched at every restart (each counted as a failed
  search), traders not read in that search were dropped too (empty reasons "()"), and /invofollow picks were not kept.
- Now: `/invofollow` = the owner's pick (`follow` with `picked: true`; an already-followed trader gets a `pick` event).
  The search never proposes dropping a pick. A followed trader READ in the search whose portfolios are ALL skipped
  (`copyable_portfolios` 0) for `drop_after_fails` searches (`InvoScorer.count_fail`: at most once per `FAIL_GAP_MS`
  20 h) is PROPOSED for a drop: `Bot.ask` posts a card with Discord buttons (🗑️ Drop / ✅ Keep = becomes a pick);
  `/invosearch` proposes its follows/drops (✅ Apply / ❌ Keep as is). Clicks: `DiscordUI._button` (INTERACTION_CREATE
  type 3, owner only, ack type 6) -> `Command("/button", custom_id)` -> `Bot.on_button`. Questions live in memory
  (`Bot.asks`): after a restart a click says it expired and the next search asks again. Loss pauses
  (`invo_drop`) stay immediate.
- Copy gate (`invo_scorer.VERSION` 5): a scored portfolio is skipped only for `invo_scorer.HARD` reasons (loses after
  our costs, losses > wins, one call = most of the profit, too fast); quality points (score < 70, PF < 2...) only rank
  the search (`eligible` = a copied portfolio passing EVERY rule). `loses_overall` no longer blocks copies.
- Agreement (two followed traders, same coin and side) already boosted Invo copies; in a capped wallet (trader
  size) the boost now stays within `invo_cap` (it was consensus_risk_pct x5 = ~250$ on a ~60$ copy).

## Invo fixes after the first 96 copies (owner request 2026-10-12)
- Analysis of the live ledgers (invo_calls: 96 closed copies, 60% won, -16.64$; avg win +0.95% vs avg loss -2.3% of the
  copy, so ~71% wins are needed): limpan96's "Scalping" (94% on Invo, PF 1.73 in our replay counting its losing OPEN
  calls) won 3 of 11 copies; 4,902 Invo 429s; the biggest copies lost most. A "skip if the price already moved" rule
  lost money at every threshold: not added.
- `invo_scorer.HARD` blocks `profit_factor<` again (below `selection.min_profit_factor`, 2); no VERSION bump (cached
  reasons already carry it). `invo.request_gap_s` 0.25 -> 0.5, `invo.max_risk_pct` 2 -> 1 (cap 100$).
- The 8% / trader-exit versions (`x.base_name`) copy only calls their usual-stop version copied (`invo_one`, log
  `invo_skip why="not copied by ..."`); their wallet-wide budgets (total risk, positions, daily/weekly loss) are loose
  so they hold the same calls; per-trade/per-coin limits and margin still apply (a 20% trader-exit copy cannot always
  be held: its guard stop allows ~3x leverage).
- `invo_link` now also carries `portfolio`, `title`, `live` and, when the trader's exit closed the copy, `closed_by`
  (`confirmed` = in its closed calls, `vanished` = `invo.DELETED`, `unseen` = closed while not watching); later
  `invo_link` events merge fields. The card says "real-money" or "paper" portfolio and its title.
- Main stop (owner request 2026-10-12, "keep the trades open until the traders close it; the 3% and 8% SL will still
  work"): `invo.main_stop_pct` (config/invo.toml 0; dataclass default 3 = the old layout, used by the tests). The
  wallets at that stop keep the plain names (`invo_calls` = cards, followed list, leader pause; `invo_risk_<r>pct`)
  and decide which calls are copied; the other stops are `<name>_stop3` / `_stop8` / `_trader`. Every risk wallet now
  has an explicit sizer (same notional at every stop). `/invoreset` also archives `data/wallets/invo_*` dirs of an
  earlier main stop. Not changed: the Invo search still replays copies with our 3% stop (`copy_edge`).

## Account value = perp + spot stablecoins (bug found live 2026-10-10)
- `/hyperadd` scored with the PERP account value only while the daily search used the leaderboard value, so the same
  wallet passed the search (3% drop) and failed /hyperadd (max_drawdown>30%): 0x6d73... had 1,481$ in perps and
  50,626$ USDC in spot (spot USDC backs perps; its `hold` is margin). Now `Scorer.handle_adds` uses
  `Info.account().value + Info.spot_usd()` (`hl.parse_spot_usd`, fixture `spot_clearinghouse_state.json`), never below
  the screened leaderboard value; the mirror wallet's leader values add spot stablecoins too.

## Trailing stop and Invo trader drops (owner request 2026-10-10)
- `risk.trail_after_pct` / `trail_pct` (3 / 3): `PositionManager.trail` (called from `check_stops` every tick) moves the
  stop, once the price is 3% in our favour, to 3% behind the price, never below break-even (entry + 2x(taker fee +
  extra slippage)); only in our favour, in steps >= 0.2% of the price, as `stop_set` ledger events (`why: trail`). A hit
  with the stop past the entry closes with reason `trail_stop` ("🔒 trailing stop"). Every wallet using PositionManager
  (main, HL side wallets, mirror, Invo) has it; the FOMO book does not (its own 30% stop).
- The Invo trader-size wallet now applies the leader pause rules (`SideWallet(pause_leaders=True)`: copy drawdown >
  `leader_pause_dd_pct` of equity/max_leaders, or `leader_pause_losses` in a row); a pause queues `("invo_drop", ...)`
  and `Bot.invo_drop` unfollows it in every Invo wallet at once (found live: lazylegendx lost 15.93$ in 10 trades and
  stayed followed because only the daily search could drop it).

## Invo fixes after the stop-loss study (owner request 2026-10-11)
- `invo.trail_after_pct` / `trail_pct` replace the risk.toml trail in every Invo wallet; 0 = off (default): on 180 days
  of real prices the 3/3 trail cut the big winners this book needs. The 3% hard stop stays.
- `Bot.invo_trade_coin`: a PEPE/BONK/SHIB/FLOKI call trades kPEPE/... (price x1000); `invo_coin` (search) is unchanged.
- Unfollowed traders with an open copy stay watched (`invo_refresh_watched`) so their exits/resizes are copied; new
  calls of theirs are not. The watcher's first read emits `("invo_baseline", user, calls)`: `on_invo_baseline`
  re-links our copies to the calls and closes copies whose call closed while we were not watching (`invo_closed_unseen`).
- `boot_repair` keeps a stop on the winning side of the entry (a trailed stop is no longer reset at restart).
- Each new call logs `invo_call` with the trader's own stop/target distance (to judge "use their stop" later).

## Daily picks, report-only searches (owner request 2026-10-09)
- `selection.auto_follow` and `sol.auto_follow` = false (default): `select()` (HL) never joins or swaps for rank, only
  drops paused / no-longer-eligible leaders; `sol/hysteresis.select` likewise (every bad one leaves; owner picks kept);
  `SolBot.on_review` re-picks only after `/fomosearch`. `/hypersearch` and `/fomosearch` still re-pick on request.
  Tests that cover the automatic flow set auto_follow = true (`tests/test_selection.CFG`, `test_sol_e2e.Env(auto=)`).
- No blacklist: the drop cooldown only limits automatic joins; `/hyperadd` / `/fomoadd` of a paused leader that passes
  writes a `follow` event (clears the pause; side wallets lift it in `sync_leaders`/`sync`).
- `copybot/picks.py`: `Book`/`Entry`, `render` (🆕/✅/❌ vs the previous DAILY report, follow command in `<code>`),
  `Store` = `data/picks.json` (UI memory, not ledger state). `Bot.tick` sends it once a day after `picks.hour` (13, local
  via `discord.utc_offset_hours`) and >= 3 min after a start (`picks.due`); `/picks` sends it without saving.
  Builders: `Bot.hyper_book`, `SolBot.picks_book(n, prev)`, `Bot.invo_book`.
- Invo search `copybot/invo_scorer.py` (`InvoScorer` thread, cache `data/cache/invo/scores.json`): candidates =
  followed + `usernames()` of `/trending/get_portfolios_pl` (filter trending/month/all_time, `{"filter", "params":
  {page,size}}`) and `/trending/get_users` (`{page,size}`) - request bodies recorded 2026-10-09, ANSWERS NOT RECORDED
  (the parser walks any JSON for `username`; log `invo_discovered traders=N`: 0 means the shape changed); closed calls
  via `get_investments isOpen:false` paged by 50. `calls_to_fills`: each call = its own pseudo-coin `"ETH|n"` (two
  synthetic fills, size = exposure x BASE 10,000 / entry), candles = the coin's 1h candles sliced to the call
  (`Scorer.candles`, now locked), open calls = `Account` positions at live mids -> `scoring.full_score` with the HL
  `ScoreParams` (coins=None) + Invo gates (`min_hl_share`, `min_hold_min`, `max_idle_days`). `("invo_review", n,
  scores, fails)`: a followed trader failing `invo.drop_after_fails` (2) searches in a row is unfollowed. The client is
  shared with the watcher (`_auth_lock`: one token refresh at a time, rotation-safe).
- `/invosearch` (owner request 2026-10-10): `InvoScorer.req` now; when that search ends `Bot.invo_repick` follows the
  best `invo.max_traders` eligible and drops followed ones outside them (empty result changes nothing). `/hypersearch`
  and `/fomosearch` are the same for their books.
- **FOMO daily picks by a scheduled Claude task (owner request 2026-10-10, owner dropped Helius).** Scheduled task
  `fomo-daily-picks` (13:00 local, on the owner's Claude PC, ~/.claude/scheduled-tasks) uses Claude in
  Chrome (owner's Chrome, signed in to fomo.family; the task must NEVER sign in itself: it stops and asks):
  `tools/fomo_collect.js` (in the tab: leaderboards 24h/7d/30d -> last 100 swaps of every board trader (up to 400) -> USDC/USDT legs ->
  loose pre-screen) -> `fomoSend()` navigates the tab to `tools/fomo_receive.py` on 127.0.0.1:8765 with the data in the
  #fragment (the extension truncates results ~1000 chars and blocks base64; fomo.family's CSP blocks fetch/iframe to
  localhost; popups are blocked) -> `tools/fomo_picks.py score` (copybot.sol.scoring.full_score, config/sol.toml
  unchanged; failing ONLY trades/active_days/history/positive_weeks = "promising", since FOMO shows 100 swaps) ->
  `wallets` (free public RPC: probe each token's busyness, search the quietest ones' signatures near the swap time,
  signer besides FOMO's fee payer whose balance moved by exactly the amount; getTransaction needs
  maxSupportedTransactionVersion 1 since Solana v1 transactions) -> `report` (reports/fomo/, git-ignored) -> published to
  https://claude.ai/artifact/9mqHKHdwAqN1J8y9rSaJdj . First run 2026-10-10: 353 on the boards, 150 read, 9 scored,
  0 pass, 1 promising (OinkersRUs, already followed).

## Resets (2026-10-08, Invo 2026-10-10)
- `/hyperreset` (main + HL side wallets), `/fomoreset` (`data/sol/`) and `/invoreset` (the Invo wallets) each archive
  their old ledgers under their own `archive/…reset-<stamp>/`, keep followed traders (and owner picks), pauses,
  cooldowns and swap cursors, are refused while a trade of that book is open, and restart the process (`restart_at`).
  None touches another book.
- HL eligibility now also needs: loss_7d <= 3%, loss_24h <= 1.5%, <= 4 losses in a row, last-15 win rate >= 45%, 7d
  drawdown <= 10% (`selection.max_loss_7d` ... `max_dd_7d`; in `ScoreParams.rules()` so cached scores are redone).
- `/hyperprogress` shows longs vs shorts and warns when every copy was a long. Code path checked: the detector and the
  open path treat shorts exactly like longs (real fills: shorts detected). An all-long run is a data effect (the followed
  traders were all long, or the market) until a ledger shows otherwise: send `data/ledger.jsonl` to investigate.

## Findings from the real API (2026-10-05)
- `tests/test_discord.py::test_rate_limit_is_honoured` is timing-based and can
  fail rarely under load; rerun before suspecting a bug.
- Hyperliquid closes the websocket about every 3 h with code 1000 "Expired"; the bot reconnects in 2–17 s and
  reconciles. Only an outage longer than `runtime.ws_alert_after_s` (60 s) alerts on Discord.
- One websocket can track at most 15 users. Live `userFills` messages have no `isSnapshot`, and `hash` can be all zeros.
- Spot fills (`@107`) have `dir` = `Buy`/`Sell`. Builder perps (`xyz:TSLA`) appear in fills and are ignored. `#140` candles return HTTP 500.
- The owner's PC clock runs about 1.4–1.7 s ahead of the exchange (±0.15 s). It is corrected via the `Clock` offset.
- 47,266 leaderboard rows → about 2,168 pass the pre-screen. Most top candidates then fail the first-page screen:
  `too_fast`, `high_frequency`, or `round_trips<150` because they accumulate or hold a core position and almost never
  go flat (verified on real wallets; it is not a bug). Because of this, `max_candidates` was raised from 60 to 400, then to 2000 (owner request).

## Status and open questions
- **Current state (2026-10-10): see `context.md`** (what runs where, the three books, Claude's scheduled FOMO task,
  what Claude must never do, gotchas). Owner's bot PC: `C:\Users\gedeo\Copytrade-test` (uv on PATH there; restart
  loop `while ($true) { uv run copybot; Start-Sleep 10 }`); updates = `git pull` there + `/restart` in Discord. Secrets
  set there: Discord, PIN, HELIUS_API_KEY (optional), INVO_TOKEN_FILE. Invo login verified 2026-10-09 (refresh = GET).
- Git hygiene: the owner keeps unrelated untracked files in the repo root (copilot-*.md, committed once by mistake and
  removed); always stage explicit paths, never `git add -A`.
- **Live since 2026-10-05** (first on the owner's notebook, now on the bot PC above; Telegram until 2026-10-10,
  Discord only since). First review: 400 screened,
  13 fully scored; under the strict floors 3 leaders followed (0xc0b2…, 0x8a80…, 0xb556…). First copy 2026-10-06
  09:55 UTC: NEAR long from 0x8a80…, copied by all 5 wallets in ~0.5 s.
- Real-world issues already fixed: chat read timeouts killed the command thread; a second program polled the same
  Telegram token; websocket "Expired" closes every ~3 h alerted; /hyperadd ignored spot USDC; a trailed stop past the
  entry was flagged at restart; an oversized Invo copy (cap added).
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
  Eligibility floors (owner request 2026-10-05): win rate ≥ `min_win_rate` (60%, 45% since 2026-10-09), score ≥ `min_score` (70) and profit
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
  A review screens until `pool_size` (100) wallets are fully scored. Since 2026-10-09 nobody is followed
  automatically (`auto_follow` false): the daily picks report the top 7. Check `review_done` / `event=scored` in the log.
- **Diversified wallets unlock alts/memecoins (owner request 2026-10-05):** a wallet that is profitable overall, net
  profitable in ≥ `alt_min_coins` (3) coins and has no coin above `alt_max_coin_share` (50%) of its profit is
  scored on ALL its core perps and copied in all of them (`Score.diversified`, `RiskGate.alts_ok`). Everyone else
  stays on the main coins. Spot (`@`), outcome (`#`) and builder (`xyz:`) markets stay excluded.
- Success metrics and the kill criterion are in the README and shown by `/hyperprogress` (and `/fomoprogress`,
  `/invoprogress`).
