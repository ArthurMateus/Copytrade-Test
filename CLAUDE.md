# CLAUDE.md: context for working on this repo

Hyperliquid copy-trading bot, **PAPER MODE ONLY**. It copies up to 7 automatically selected wallets on a
simulated $300 wallet. It is controlled from Discord (Telegram removed 2026-10-10) and runs unattended for 2–4 weeks on the owner's Windows PC.
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
| `chat.py`, `cardfmt.py` | Platform-free chat: command names/aliases/help, `ChatUI` outbox (cards edited in place, rate-limited, 429-aware); renderers in a small HTML subset. Live cards: /status, /leaders, /trades, /traders, /wallets (`Bot.render_card`). Card conventions: 🟢/🔴 = money only, ⬆️/⬇️ = side, one P&L after fees+funding, `label: value` lines (no `<pre>`), times in `discord.utc_offset_hours` |
| `discord.py` | The ONLY chat (Telegram removed 2026-10-10 at the owner's request): `DiscordUI(ChatUI)` over REST + Gateway (stdlib + `websockets.sync`), guild slash commands, owner-only, ephemeral replies (PIN never shown), card HTML → markdown embeds coloured by money. Card ids live in the ledger as `dc:<key>` (keys without the prefix are old Telegram messages, ignored) |
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
  before the current start used to be ignored (Telegram redelivered them; Discord never does).
- Side wallets (owner request 2026-10-05): 2/5/10/20% risk, $300 each, compared by `/wallets`. EVERY risk limit
  scales with the level (per-symbol, total, consensus, daily/weekly loss stops capped at 100%); leverage, stop
  distance and liquidation buffer do not. They are allowed above the CEILINGS on purpose (paper comparison only).
  They follow the main wallet's followed/paused sets (`sync_leaders`), never pause leaders themselves, post no trade
  cards, and get the main wallet's moves after it (books reused for 1 s). A side-wallet error is logged/alerted and
  never reaches the main wallet. /pause, /resume and /flatten act on every wallet.
- Lag = exchange-clock time of our paper fill minus the leader's fill time.
- Mirror side wallet (owner request 2026-10-09): `mirror_x10` in `data/wallets/`, sized by `Bot.mirror_size` =
  leader's new position notional / (its perp account value + spot stablecoins, since 2026-10-10) (leverage included) x `risk.mirror_mult` (10) x our
  equity, lifted to `min_notional_usd` (so small bets are not skipped). Limits = a `mirror_limits_pct` (5%) side
  wallet's (they clamp big bets). Account values of followed leaders: `sync_worker` every 5 min (BULK); unknown value
  -> the fixed 1% risk size (log `mirror_size_fallback`). Adds/reduces follow `k` like every wallet. Shown in
  /hyperwallet as "mirror x10 (their % of account)".
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
  nobody. `SolScorer.progress` feeds the search line in `/fomo` and `/fomoleaders` (also "best found, not followed");
  a `/fomosearch` during a running search only reports progress. `history_days` = 30 to keep a search affordable.
  Seeder and scorer can read one wallet's history together: `SolScorer.history` locks per wallet (Windows refused two
  writers of one cache file).
- `/hyperadd 0x…` and `/fomoadd <wallet>` (owner request 2026-10-09): the owner (or Claude, only when asked in a
  session; never on a schedule) supplies a wallet; the scorer thread checks it at once (`add_q`, also between wallets
  of a running review: HL re-screens even if screened this week; FOMO marks it `manual` in the pool, never pruned and
  re-checked at every search). Same strict rules, no bypass. Passes + free slot (< max_leaders) -> followed at once
  (`apply_plan`); else the reply says why (rules failed, already followed, paused, or full -> next search). Discord gets
  a required `wallet` option (`tg.WALLET_COMMANDS`).
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
- Discord is the owner's own implementation (`discord.py`, slash commands over the Gateway, `MultiUI` fan-out); the Solana
  book just talks to the same `ui`. Slash commands `/fomo`, `/fomotrades`, ... are in `tg.COMMANDS`.
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
  not on HL -> told, not copied. `/hyperreset` keeps the Invo follows. Off without INVO_TOKEN_FILE (`/invo` says how).
- Adds and trims (owner request 2026-10-09): a call's COMMITTED size is `entrySize` (percent, fixed unless the trader
  adds or trims); `positionSize` drifts with the price and must not drive copies. Synthetic Moves use the committed
  exposure (entrySize/100 x leverage) as the leader "position", so `k = our size / exposure` and PositionManager
  mirrors an add (entry, RiskGate) or a trim (exit, never refused) in proportion. The watcher re-reads a portfolio's
  calls when its `updatedAt` changes and reports `("invo_resize", user, old, new)` for changes > `invo.RESIZE_MIN` (2%).
- Live UI (owner request 2026-10-09): `/invo`, `/invotrades`, `/invotraders` are live cards (keys `invo:*`,
  `Bot.render_card`, edited only when the body changes); each copy has its own live card `invo:pos:<pos_id>` with the
  trader's call (and resizes) that becomes the final summary on any close (the Invo wallet's PositionManager notifies
  `Bot.on_invo_notify`). `cardfmt.short("invo:x")` -> "@x".
- Risk-level wallets (owner request 2026-10-09: "1/2/5/10/20% of OUR wallet, same levels everywhere, set in config"):
  Invo: `invo.risk_wallets_pct` -> `Bot.invo_extra` = `SideWallet(own_leaders=True)` named `invo_risk_<r>pct`
  (fixed-risk sizing, no sizer), following the trader-size wallet's Invo traders (`sync_invo_extras`); `on_invo` runs
  every Invo wallet (`invo_one`, errors isolated per wallet; `invo_calls` keyed (wallet, call id)); only the trader-size
  wallet posts trade cards/notes. `/invowallets` compares them; `/hyperwallet` now lists only HL wallets
  (`not w.own_leaders`). FOMO: `sol.side_wallets_risk_pct` (2/5/10/20) -> `SolBot.sides` = `sol/wallets.SolSide`
  (own ledger `data/sol/wallets/<name>/`, own SolGate with `scaled()` limits, shared Prices/Detector, synced
  followed+pauses every tick, moves after the main wallet, `on_sides` isolates errors, prices fetched for every
  wallet's tokens, `wanted()` includes every wallet's open leaders, /fomopause/resume/flatten act on all,
  /fomoreset refused while any wallet holds a trade and archives `data/sol/wallets/`). `/fomowallets` compares them
  (it used to be an alias of `/fomowallet`).
- The bot must use its OWN Invo account (rotation: a browser and the bot on one login log each other out). The owner
  gets the refresh token with a DevTools console snippet (README) that decrypts `FlutterSecureStorage.REFRESH_TOKEN`
  (AES-GCM, key in `localStorage.FlutterSecureStorage`) and copies it; `tools/invo_check.py` verifies it.

## Invo portfolio filter and blocks (owner request 2026-10-10)
- A trader's calls are copied only from its portfolios with win rate >= `invo.min_portfolio_win_rate` (0.80), return
  >= `min_portfolio_pnl_pct` (10%; Invo's `plSnapshot` is the portfolio's % return: $100 -> $130 shows ~28-30) and
  >= `min_portfolio_calls` (20) closed calls, and not blocked by the owner (`/invoblock <user> <portfolio>`, matched by
  the start of its title; `/invounblock`). Blocks are ledger events `invo_block`/`invo_unblock` in the trader-size Invo
  wallet -> `State.blocked` {leader: {portfolio id: title}}. `Bot.invo_skip(name, Portfolio)` gives the reason; the
  `Watcher(skip=)` drops new calls of skipped portfolios (log `invo_call_skipped`) but closes/resizes of existing
  copies still flow; `InvoScorer(skip=)` scores a trader only on the portfolios we would copy. /invotraders shows each
  portfolio as ✅ copied or 🚫 skipped (why). `/invoblock` and `/invounblock` are the only book-specific commands
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
- **Current state (2026-10-09): see `context.md`** (what runs where, how Claude helps pick FOMO/Invo traders, gotchas).
  Owner's bot PC: `C:\Users\gedeo\Copytrade-test` (uv on PATH there; restart loop
  `while ($true) { uv run copybot; Start-Sleep 10 }`). Secrets set there: Discord, PIN, HELIUS_API_KEY,
  INVO_TOKEN_FILE. Invo login verified working 2026-10-09 (refresh = GET; POST answers 405).
- Git hygiene: the owner keeps unrelated untracked files in the repo root (copilot-*.md, committed once by mistake and
  removed); always stage explicit paths, never `git add -A`.
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
