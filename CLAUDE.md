# CLAUDE.md: context for working on this repo

Copy-trading bot, **PAPER MODE ONLY**, with one book since 2026-10-26: 🧾 Invo traders' posted calls, copied on
simulated $300 wallets at Hyperliquid's real prices, books and fees. It is controlled from Discord only and runs
unattended on the owner's Windows PC. **New chat? Read `context.md` first** (current state, who does what, what Claude
must never do), then this file. The original brief is [docs/SPEC.md](docs/SPEC.md) (a Hyperliquid wallet copier; that
book, the FOMO / Solana book, the Invo arena and the bot's own Invo search were built later and REMOVED on 2026-10-26 at
the owner's request; they are in the git history before that date). Do not add features nobody asked for. The README
covers run instructions and design choices.

## Owner
- Runs Windows 11 with a Brazilian Portuguese locale (Windows error messages come out in Portuguese) and uses
  PowerShell.
- Not a Python developer. Give exact copy-paste commands, one per block, and explain results in plain words.
- GitHub: https://github.com/ArthurMateus/Copytrade-Test.

## Hard rules (never break)
- Never import a signing library, read a wallet key, or call an exchange/order endpoint (Hyperliquid's or Invo's
  `/dex/` trading endpoints). `tests/test_safety.py` and `test_the_bot_never_calls_an_invo_trading_endpoint` enforce it.
- Secrets come from environment variables only: `COPYBOT_PIN`, `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`,
  `DISCORD_OWNER_ID`, and `INVO_TOKEN_FILE` (path of the file holding the bot's Invo refresh token; without it Invo is
  off). Never log or commit them. `log.py` redacts them.
- Every order goes through `RiskGate.check` (`risk.py`). Exits (reduce/close/stop) are never refused; entries
  fail closed.
- Every state change goes through the ledger: `ledger.append(ev)` then `state.apply(ev)`. A restart replays the
  same `apply`, so never mutate `State` outside an event.
- Tests fake only the network (`tests/fakes.py`: loopback HL REST + WS + Discord REST/Gateway; `tests/fakes_invo.py`:
  Invo; `FakeDiscord.say("/cmd arg")` sends a slash command and `sent`/`edits` carry `text` = the card HTML rebuilt
  from the markdown, `md_to_card`). Never mock our own code. Parsers are tested against real recorded responses in
  `tests/fixtures` (Hyperliquid: `tools/record_samples.py`; Invo: recorded from the web app with an in-page XHR hook).
- Run the tests after every change: `PYTHONPATH=src python -m pytest` on the Claude PC (`uv run` fails inside the
  Claude app), about 4 minutes, ~180 tests including real-process kill -9 restarts.

## Layout (`src/copybot/`)
| Module | Role |
|---|---|
| `config.py` | Loads TOML from `config/` (`risk`, `broker`, `discord`, `runtime`, `invo`) and validates the `CEILINGS`; unknown keys are an error |
| `log.py` | key=value logs, rotating file, secret redaction |
| `ledger.py` | `Ledger` (append-only JSONL, fsync), `State`, `Position` and the event fold. Unresolved intents, torn tail lines and stop-less positions go into `state.uncertain`. Old ledgers' events of removed features (`sel`, `cursor` ...) still replay |
| `hl.py` | Hyperliquid market data: parsers (meta, books, fills, websocket), `RateBudget` (CRITICAL vs BULK classes), `Info` client with a hard timeout |
| `risk.py` | The gate: sizing, leverage (highest <= 10x keeping liquidation >= 3x the stop away; the trader's leverage in "trader's exit" wallets), limits, health checks, the `margin_full` reason |
| `broker.py` | Paper fills that walk the real L2 book plus slippage; taker fee 0.045%; funding helper |
| `positions.py` | The copy engine: `Move` (leader position before/after), `PositionManager` mirrors moves with `k = our size / leader position`; stop at entry; trailing stop; leader pause rules; agreement (backers, boost). Its reconcile, conflict and hand-over parts came from the Hyperliquid book and are not used by Invo (`pm.handover = False`; opposite-side calls are skipped before the engine) |
| `wallets.py` | `SideWallet`: one paper wallet (own ledger in `data/wallets/<name>/`, own `RiskGate` and `PositionManager`, limits scaled by `config.scaled`); `boot_repair`; `max_drop_pct` |
| `feed.py` | Websocket allMids prices and the exchange `Clock` offset estimate |
| `chat.py`, `cardfmt.py` | Platform-free chat: command names and aliases, help, `ChatUI` outbox (cards edited in place, rate-limited, 429-aware); renderers in a small HTML subset. Card conventions: 🟢/🔴 = money only, ⬆️/⬇️ = side, one P&L after fees+funding, `label: value` lines, times in `discord.utc_offset_hours` |
| `discord.py` | The ONLY chat: `DiscordUI(ChatUI)` over REST + Gateway (stdlib + `websockets.sync`), guild slash commands (short names), owner-only, ephemeral replies (PIN never shown), card HTML → markdown embeds coloured by money; outgoing text shows the short names (`chat.short_names`). Card ids live in the bot's ledger as `dc:<key>` |
| `invo.py` | Invo: parsers, `InvoClient` (rotating refresh token, shared spacing, 429 throttle), `Watcher` (polls the watched traders) |
| `runner.py` | `Bot`: boot/repair, worker threads, trading loop, the 18 Invo wallets (`invo_set`), Invo call routing (`on_invo`, `invo_one`), commands, cards, heartbeat |

`data/ledger.jsonl` is the bot's own ledger (genesis, boots, Discord card ids); each Invo wallet has its own in
`data/wallets/<name>/`. Also `data/bot.lock` (single instance) and `logs/copybot.log`. All git-ignored. Moving the bot to
another PC means copying `data/`. Never run two instances on the same wallets. `tests/detector.py` is a test helper
(Hyperliquid fills → moves) that drives the engine in `tests/test_positions.py`.

## Commands (`chat.py`)
- `chat.VERBS` = status, trades, traders, wallets, progress, follow, unfollow, block, unblock, pause, resume, flatten,
  reset. Discord lists them without a prefix (`SLASH_COMMANDS`, + /help /restart); `chat.canon` maps every accepted
  name (`/follow`, `/invofollow`, older aliases `/invo`, `/positions`, `/leaders`) to the canonical `/invo<verb>` that
  `Bot.invo_command` handles. `PIN_COMMANDS` = flatten, reset; `WALLET_COMMANDS` (a required `wallet` option) =
  follow, unfollow, block, unblock.
- `/status`, `/trades`, `/traders`, `/wallets` are live cards (keys `invo:*`, `Bot.render_card`, edited only when the
  body changes); each copy has its own live card `invo:pos:<pos_id>` with the trader's call (and resizes) that becomes
  the final summary on any close. `/reset <PIN>` (no open trade) archives the Invo wallets to
  `data/archive/invo-reset-<stamp>/`, keeps the followed traders and restarts; `/restart` stops the loop after 3 s and
  the owner's PowerShell `while` loop starts it again. `/resume` writes `ack` events that clear restart uncertainties.

## Invo calls (owner request 2026-10-09)
- Invo (Involio, app.invoapp.com, a Flutter web app) traders post CALLS in PAPER portfolios (`"type": "paper"`) or trade
  real money in "live" portfolios. Invo never exposes other traders' Hyperliquid addresses, so the calls are the signal.
  `InvoClient`: POST JSON, `Authorization: Bearer <access>`; `/v1_0/auth/refresh_token` with `Bearer <refresh>` returns
  `accessToken`+`refreshToken`; the refresh token ROTATES and is written back to `INVO_TOKEN_FILE` (a token that cannot
  be written stays in memory and is retried every round, log `invo_token_save_failed`); refresh tokens live ~1 year.
- The bot must use its OWN Invo account (rotation: a browser and the bot on one login log each other out). The owner
  gets the refresh token with a DevTools console snippet (README) that decrypts `FlutterSecureStorage.REFRESH_TOKEN`
  (AES-GCM, key in `localStorage.FlutterSecureStorage`); `tools/invo_check.py` verifies it.
- `Watcher` (thread): one `get_users_portfolios` per watched trader per round (every page while a page is full, up to
  `invo.PAGES_MAX`), `get_investments` only for portfolios whose `openTrades` or `updatedAt` changed (and every
  `invo.RECHECK_S` 60 s for portfolios holding calls); first read = baseline (`("invo_baseline", user, calls)`), never
  copied; new call <= `max_call_age_s` -> `("invo_open", user, Call)`; closes, resizes and re-ids as below. A failing
  trader no longer ends the round; all reads failing -> wait 2, 4 ... 60 s. Watched traders (`Bot.invo_refresh_watched`)
  = followed + unfollowed ones we still hold a copy of (their exits and trims are copied, their new calls are not).
- Speed: `invo.poll_s` 0 (no pause between rounds) and `invo.request_gap_s` 0.5 (0.25 brought 429 storms). A new call
  is seen when the round reaches its trader, so rounds must stay short: measured live, copies were 2-6 s late with 7
  traders and 15-30 s late while the arena's groups and the search shared the queue (removed 2026-10-26). Invo has no
  push channel we can use (`/posts/get_feed` is a small curated subset), so per-trader polling stays.
- 429s (live 2026-10-12): `InvoClient.throttle` doubles the shared request spacing (`slow`, up to `invo.SLOW_MAX_S`
  3 s, Retry-After honoured up to 30 s), eased x0.75 per `CALM_S` (60 s) without one (logs `invo_throttled` /
  `invo_throttle_eased`). `Watcher.closed_call` returns `UNREAD` when Invo did not answer: a vanished call is then
  kept (before, copies were closed as "deleted by the trader" during 429s). Discord: a card edit answered 429 waits 30 s
  and new messages go first (`ChatUI._write(retry=False)`).
- Closes: a call missing from a read is closed only when it shows in its portfolio's closed calls (page 1, newest
  first; the close then carries Invo's closing price and reason) or after `invo.VANISH_S` (10 s) if it never does (log
  `invo_call_vanished`, reason "call deleted by the trader"); a call whose portfolio was not fully read waits
  `VANISH_UNREAD_S` (1 h); log `invo_call_missing` once per call says why it went missing. Open: henderson_14's 6 of 6
  closes on 2026-10-25 came as "deleted" although his calls normally close in the list (maybe the closed list lags
  10 s); waiting for the owner's log lines before changing `VANISH_S`.
- Re-ids (live 2026-10-13): Invo lists an EDITED call under a NEW id with the same createdAt; the old id never shows in
  the closed list. A vanished call with a new call of the same portfolio, ticker and side created within
  `invo.REPLACED_S` (120 s) of it is the same call -> `("invo_replace", user, old, new)` (log `invo_call_replaced`);
  `Bot.invo_one` re-keys, writes `invo_link` with the new id (log `invo_relinked`) and mirrors a size change.
- Real-money portfolios (2026-10-12): empty in /v1_0, read from GET `/dex/portfolio/live/<id>/overview` (every
  `OVERVIEW_S` 300 s) and `/dex/portfolio/position/<id>/list?isOpen=` (every round), base without /v1_0
  (`InvoClient.root`); other `/dex/` endpoints place trades and are never called. `Call.units` (coins) detects adds and
  trims. A refused read (`invo_live_read_failed`) is reported once as `("invo_live", user, False, why)` -> Discord alert
  (and `True` when it works again), keeps the last known calls (never closes a copy) and blocks the trader's first
  baseline until a real answer. A live call's size is its % of the trader's equity (often < 1%): x
  `invo.live_size_mult` (10) in the trader-size wallet.
- Committed size (2026-10-09): a call's size is `entrySize` (percent, fixed unless the trader adds or trims);
  `positionSize` drifts with the price and must not drive copies. Synthetic `Move`s use the committed exposure
  (entrySize/100 x leverage) as the leader "position" (start 0 -> +-exposure open, back to 0 close), so `k = our size /
  exposure` and adds/trims scale in proportion. Changes below `RESIZE_MIN` (2%) add up until they count. Resizes are
  always mirrored (owner 2026-10-13: "if the trader resizes it, resize it, up or down, always"), an add through the
  RiskGate and the cap; an unfollowed trader's adds are not copied. `Bot.invo_catch_up` brings a copy found at a
  restart to the call's current size (paper calls).
- Tickers map to Hyperliquid names case-insensitively (`invo.aliases`, e.g. MATIC = "POL"); PEPE/BONK/SHIB/FLOKI trade
  kPEPE/... at the trader's price x1000 (`Bot.invo_trade_coin`); not on Hyperliquid -> told, not copied.
- Links: each copy's Invo call id, card text, portfolio, title, live flag, coin, entry gap and (when the trader's exit
  closed it) `closed_by` (`confirmed`, `vanished`, `unseen`) are `invo_link` ledger events (`State.invo_links`, by
  pos_id; later events merge). A restart re-links copies by call id (coin+side only for older copies, or an open call
  of that coin/side created near the copy's `opened_ms` for a re-id). `on_invo_baseline` waits (`invo_baseline_wait`)
  until Hyperliquid's coin list is loaded (without it every copy looked orphaned), and closes copies whose call closed
  while we were not watching (`invo_closed_unseen`).
- Followed traders: `/follow` = the owner's pick (`follow` with `picked: true`); EVERY portfolio is copied
  (`Bot.invo_skip` only returns "blocked by you"); `/block <user> <portfolio>` (matched by the start of its title)
  writes `invo_block` / `invo_unblock` events in the trader-size wallet -> `State.blocked` {leader: {portfolio id:
  title}}; the `Watcher(skip=)` drops new calls of blocked portfolios (log `invo_call_skipped`) but closes/resizes of
  existing copies still flow. `invo.max_traders` (10) caps the followed traders.
- Leader pause: the trader-size wallet applies the pause rules (`SideWallet(pause_leaders=True)`: copy drawdown >
  `invo.pause_dd_pct` (4%) of the wallet, set as `leader_pause_dd_pct` = pause_dd_pct x max_leaders, or
  `leader_pause_losses` in a row); a pause queues `("invo_drop", ...)` and `Bot.invo_drop` unfollows it in every Invo
  wallet at once (`/follow` brings it back).
- Agreement (owner choice "B", 2026-10-13): another followed trader calling the coin AND side a wallet holds becomes a
  backer (`Bot.invo_agree` -> boost = `risk.consensus_risk_pct` scaled like the wallet's limits, half a normal copy,
  within `invo_cap` in the trader-size wallet, per-coin limit on the total). Its close takes that size off (`unback`);
  its resizes do not change it. `pm.handover = False`: the first trader's exit closes the whole copy. Opposite side,
  the same trader again (another portfolio) or an existing backer: skipped ("coin already held"). The card gets
  "@x also called it" (`Bot.invo_note`). At a restart `on_invo_baseline` re-maps a backer's call by coin+side or takes
  the extra size off if it closed meanwhile (log `invo_backer_closed_unseen`).
- Each new call logs `invo_call` with the trader's own stop/target distance; each copy logs `invo_entry_gap` (our
  entry vs theirs, in their unit, and `age_s` = how long after their call) and its card shows "⏱️ In 3.3s after them ·
  0.12% worse than their price (-0.12$)".

## The 18 Invo wallets (owner requests 2026-10-09 .. 10-24)
**Updated owner decision, 2026-10-30:** shipped `invo.margin_allocations = true` uses margin allocation rather
than stop-loss risk. Main wallet = `invo.limits_pct` (5%) margin; comparison wallets = their named 1/2/5/10/20%
margin. Each agreeing trader adds half the level. `MarginPositionManager` in `allocations.py` tracks independent
`Position.shares`; each trader's resizes and exit affect only their contribution. The first trader's exit leaves
other funded shares open. Shared stops still close all shares. Every fill carries its new shares in the same ledger
event; individual fees/funding/P&L feed `State.allocation_closed` and trader cards. Minimum-size partial exits stay
pending without selling others' shares. Old positions are adopted at their actual sizes, without topping them up;
`margin_policy` ledger events timestamp the sizing change. The descriptions of trader-size/fixed-risk sizing and
first-owner closes above/below apply only when `margin_allocations = false`. Do not interpret old backtests as
tests of the new allocation policy.

- Sizes: trader size (`invo_calls`: `Bot.invo_size` = their committed exposure x `invo.size_mult` (x `live_size_mult`
  for real money) x our equity, >= 1.05 x min_notional, capped by `Bot.invo_cap` = `invo.max_risk_pct` (1%) of equity
  at our 3% stop = 100$, for opens AND adds; owner request 2026-10-09 after a 25% x 5x STRK call became a 375$ copy)
  and each `invo.risk_wallets_pct` level (1/2/5/10/20): an r% wallet loses r% of itself at ITS stop (owner choice "A",
  2026-10-13): copy = equity x r / stop, lifted to 1.05 x min_notional, limits of level max(r, room for a minimum copy).
- Stops: `invo.stops_pct` [3, 8, 0] (must include risk.stop_pct). 0 = the trader's exit: no stop of ours but a guard
  `GUARD_OF_LIQ` (95%) of the distance to liquidation at the TRADER's leverage (at most `risk.max_leverage` 10x:
  `RiskGate.follow_leverage` / `call_leverage`), capped at 30%; risk wallets at the trader's exit are sized at that real
  guard (`RiskGate.stop_for(..., leverage_for(asset), asset)`, audit 2026-10-24; it was the 30% cap). Variants:
  `liq_buffer_mult` 1 (the stop only has to sit before liquidation). Positions always keep a valid stop (`boot_repair`).
- Names: `invo.main_stop_pct` (config 0 = the trader's exit; dataclass default 3, used by the tests) picks the wallets
  that keep the plain names (`invo_calls` = cards, followed list, leader pause; `invo_risk_<r>pct`) and DECIDE which
  calls are copied; the others are `<name>_stop3` / `_stop8` / `_trader` (`x.base_name` = its deciding version) and copy
  only calls their deciding version copied (`invo_one`, log `invo_skip why="not copied by ..."`), with loose
  wallet-wide budgets (total risk, positions, daily/weekly loss) so they hold the same calls; per-trade/per-coin limits
  and margin still apply. A deciding wallet refused ONLY for margin (`RiskGate` reason `margin_full`, read from
  `PositionManager.last_refusal`) still counts the call as decided (`Bot.invo_decided`, memory only; log
  `invo_decided_no_margin`), so its 3%/8% versions copy it with their own margin (audit 2026-10-24).
- Minimum copies: when lot rounding leaves a request of >= min_notional under it (10.5$ of SOL -> 9.66$), the gate
  takes one lot more if the limits hold it and it stays <= 1.5x the request (`risk._entry`).
- Trailing: `invo.trail_after_pct` / `trail_pct` replace the risk.toml trail in every Invo wallet; 0 = off (default):
  on 180 days of real prices the 3/3 trail cut the big winners this book needs.
- `/wallets` = `cardfmt.invo_wallets_card`: 3 best + 3 worst combinations (size · stop) and the average of each stop.
  Copying one call into all 18 takes ~0.1 s. `boot_repair` keeps a stop on the winning side of the entry (a trailed
  stop is not reset at restart).

## Findings from the real API
- `tests/test_discord.py::test_rate_limit_is_honoured` is timing-based and can fail rarely under load; rerun before
  suspecting a bug.
- Hyperliquid closes the websocket about every 3 h with code 1000 "Expired"; the bot reconnects in 2–17 s. Only an
  outage longer than `runtime.ws_alert_after_s` (60 s) alerts on Discord (new copies are refused while it is down,
  exits and stops keep working). Live 2026-10-25: 5 outages in 8 h (one of 13.5 min) with Invo network errors at the
  same times: the bot PC's internet connection, not the code.
- The owner's PC clock runs about 1.4–1.7 s ahead of the exchange (±0.15 s). It is corrected via the `Clock` offset.
- Invo, measured in the owner's Chrome 2026-10-11: ~0.29 s per request, ~6 req/s clean, ~13 req/s began to fail. Both
  of the owner's PCs share one internet connection: the Claude PC's daily Invo read (Maestri portal, separate account)
  must stay light so Invo does not slow the bot's reads.

## Status and open questions
- **Current state: see `context.md`.** Owner's bot PC: `C:\Users\gedeo\Copytrade-test` (uv on PATH there; restart loop
  `while ($true) { uv run copybot; Start-Sleep 10 }`); updates = `git pull` there + `/restart` in Discord.
- Git hygiene: the owner keeps unrelated untracked files in the repo root (copilot-*.md): always stage explicit paths,
  never `git add -A`.
- Owner environment quirks: the Claude desktop app is a packaged app, so anything it installs under %APPDATA% is
  invisible to the owner's own terminal. A VS Code-style terminal may auto-activate `.venv` (`(copybot)` prompt): run
  `deactivate` first, and load the secrets with `[Environment]::GetEnvironmentVariable(name, "User")` if the terminal
  predates `setx`. Files written by the Claude PC may use CRLF line endings: scripted multi-line replaces need the Edit
  tool.
- Open (owner to decide, from the wallet audit `reports/wallet_audit/AUDIT.md`): the 4.75% guard on 10x-max alts (F5),
  the 100% daily loss stop of the biggest deciding wallets (F7).
- Next: after ~50 trades, analyse results per trader/coin/wallet (offline, never an AI in the trading path).
- Success metrics and the kill criterion are in the README and shown by `/progress`.
