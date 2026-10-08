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
- Secrets come from environment variables only: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `COPYBOT_PIN`, and for the
  Solana book `FOMO_COOKIE` or `FOMO_COOKIE_FILE`, `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_OWNER_ID`.
  Never log or commit them. `log.py` redacts them.
- Every order goes through `RiskGate.check` (`risk.py`). Exits (reduce/close/stop) are never refused; entries
  fail closed.
- Every state change goes through the ledger: `ledger.append(ev)` then `state.apply(ev)`. A restart replays the
  same `apply`, so never mutate `State` outside an event.
- Tests fake only the network (`tests/fakes.py`: loopback HL REST + WS + Telegram). Never mock our own code.
  Parsers are tested against real recorded responses in `tests/fixtures` (re-record with
  `tools/record_samples.py`).
- Run `uv run pytest` after every change (about 2 minutes, 196 tests at the time of writing, including real-process
  kill -9 restarts).

## Layout (`src/copybot/`)
| Module | Role |
|---|---|
| `config.py` | Loads TOML from `config/` (one file per section) and validates the `CEILINGS`; unknown keys are an error |
| `log.py` | key=value logs, rotating file, secret redaction |
| `ledger.py` | `Ledger` (append-only JSONL, fsync), `State`, `Position` and the event fold. Unresolved intents, torn tail lines and stop-less positions go into `state.uncertain` |
| `hl.py` | Data contracts/parsers, `RateBudget` (CRITICAL vs BULK classes, BULK can't touch the reserve), `Info` client with a hard timeout, fill paging |
| `risk.py` | The gate. Sizing: 1% risk at a 3% stop, about $100 notional. Leverage = highest ≤10x that keeps liquidation ≥ 3× the stop distance away |
| `broker.py` | Paper fills that walk the real L2 book plus slippage; taker fee 0.045%; funding helper |
| `detector.py` | Websocket fills → leader `Move`s (open/add/reduce/close/flip) from `startPosition`; dedupe by tid; snapshots never trade |
| `positions.py` | Mirrors moves using `k = our size / leader position`; stop at entry; reconcile against `clearinghouseState`; leader pause rules |
| `scoring.py` | Pure, deterministic: `prescreen` → `fill_screen` (first 2,000 fills) → `full_score` (180 d fills + 1h candles) → `ranking` |
| `selection.py` | Pure `select()` hysteresis; `Scorer` thread with a disk cache in `data/cache` (resumes after a restart) |
| `feed.py` | Websocket (allMids + userFills, max 15 users) and the exchange `Clock` offset estimate |
| `tg.py`, `tgfmt.py` | Telegram: owner-only commands, outbox; cards edited in place, rate-limited; renderers |
| `runner.py` | `Bot`: boot/repair, worker threads, trading loop, commands, selection application, heartbeat |
| `sol/` | **Solana memecoin paper book** (own $300, own ledger `data/sol/`, own risk gate). `fomo.py` client + parsers, `scoring.py` strict pure scoring, `scorer.py` thread + cache, `market.py` DexScreener + paper broker, `risk.py` gate, `trader.py` detector + positions, `runner.py` `SolBot` (threads inside the main process), `fmt.py` cards |
| `discord_ui.py` | Discord REST UI with the same interface as `TelegramUI`; `UIGroup` fans out to both. Cards keep ids under `dc:<key>` |

Runtime state: `data/ledger.jsonl` (the source of truth), `data/cache/`, `data/bot.lock` (single instance) and
`logs/copybot.log`. All of these are git-ignored. Moving the bot to another PC means copying `data/`. Never run two
instances on the same wallet.

## Decisions made where the spec was open
- One net position per coin (as on the exchange). If a second leader trades a coin we already hold, that trade is skipped.
- Fills of the order that opened a copy only re-anchor `k`; they never add. An add with no copy of ours is skipped.
- A partial mirror under $10 is skipped. A reduce that would leave under $10 becomes a full close.
- Missed websocket fills are caught by reconcile every 60 s and right after any reconnect.
- Leader pause: copy drawdown > 10% of (equity / 7) or 5 consecutive losses. A paused leader is dropped at the next cycle, then a 7-day cooldown.
- A screen result is cached for 7 days per wallet (rejected wallets get retried after that).
- `/resume` writes an `ack` event that clears acknowledged restart uncertainties.
- Lag = exchange-clock time of our paper fill minus the leader's fill time.

## Solana / FOMO / Discord (added 2026-10-07 at the owner's request; not in the original spec)
- Wallets come from the FOMO leaderboard (`prod-api.fomo.family/v2/leaderboard/{24h,7d,30d}`, 150 rows each) and are
  re-verified from `/v2/users/{id}/swaps?limit=N` (newest first; `limit` is the ONLY working paging parameter). Both need
  the owner's logged-in session cookie. The API is private and can change; fixtures in `tests/fixtures/fomo_*.json`
  are real recordings. pump.fun was checked and has no trader leaderboard (Cloudflare, undocumented endpoints): not used.
- Ledger reuse: Solana positions are `Position(side=+1, leverage=1, coin=<mint>, sym=<symbol>)` in the same `State`; a
  `cursor` event stores the newest handled swap time per leader so a restart never re-copies old swaps.
- Entries need DexScreener liquidity >= `min_liquidity_usd`; bonding-curve pools often have none -> refused (fail closed).
- Reduces are target-based (`k x leader balance`), so a trim too small to trade is caught up by the next one.
- Bug found and fixed while building it: `_skip(..., kind=...)` collided with `notify(kind, ...)` (also in `positions.py`).
- Open risks: the FOMO cookie may expire (hours or days unknown); while it is dead the bot cannot see leader sells, only
  stops protect open copies. Copy lag is poll-based (seconds).

## Findings from the real API (2026-10-05)
- One websocket can track at most 15 users. Live `userFills` messages have no `isSnapshot`, and `hash` can be all zeros.
- Spot fills (`@107`) have `dir` = `Buy`/`Sell`. Builder perps (`xyz:TSLA`) appear in fills and are ignored. `#140` candles return HTTP 500.
- The owner's PC clock runs about 1.4–1.7 s ahead of the exchange (±0.15 s). It is corrected via the `Clock` offset.
- 47,266 leaderboard rows → about 2,168 pass the pre-screen. Most top candidates then fail the first-page screen:
  `too_fast`, `high_frequency`, or `round_trips<150` because they accumulate or hold a core position and almost never
  go flat (verified on real wallets; it is not a bug). Because of this, `max_candidates` was raised from 60 to 400.

## Status and open questions
- Built and tested; the live smoke runs against real Hyperliquid were fine. Telegram has not been tested against
  the real API yet (only the fake). No leader had been followed yet when this was written: the first review was
  still screening.
- **Open proposal, waiting for the owner's decision:** if too few wallets pass, keep the copyability gates hard
  (too fast/HFT, never closes, maker/spot share, copy edge after costs ≤ 0). Turn the quality cutoffs into score
  penalties: 150 round trips (keep a floor of about 30), 4/6 positive blocks, drawdown limits, 25% concentration.
  Then follow the top 7 of whatever remains. This differs from the spec, so only change it if the owner agrees.
  Check `review_done` / `event=scored` in the log first.
- Success metrics and the kill criterion are in the README and shown by `/progress`.
