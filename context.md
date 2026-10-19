# context.md: where the project stands (2026-10-10), for a new chat or another account

Start here, then read CLAUDE.md (design, decisions, hard rules; the source of truth) and README.md (how to run and use
it). Repo: https://github.com/ArthurMateus/Copytrade-Test (branch `main`). Everything is **PAPER ONLY**: no keys, no
signing, no order endpoints (`tests/test_safety.py` enforces it). Tests: `uv run pytest` (~320 tests, ~5.5 min).

## 2026-10-19: the Invo arena (branch `invo-arena`)
- Built at the owner's request: the bot tests which set of <= 10 Invo traders makes 300$ the most in 2 weeks
  (backtest on past calls, then live 2-week seasons), `/arena` in Discord, report `data/arena/index.html`. Design in
  CLAUDE.md "Invo arena". Tested only on synthetic data so far: the Claude PC has no Invo login (and must not use the
  bot's). First real results come after the owner pulls and restarts (one Invo search runs at start, then the arena).
- Check after deploy: the log has `arena_done` with traders > 0, Discord got "New arena season", /arena answers.

## Where we left off (2026-10-12 evening): read this first
- Only the Invo book runs (`selection.enabled` / `sol.enabled` false). Commands on Discord have no prefix: /status,
  /wallets, /traders, /follow, /unfollow, /flatten, /reset <PIN>, /picks, /block ...
- Today: the first 96 Invo copies were analysed (CLAUDE.md "Invo fixes after the first 96 copies"; data in
  `reports/trades-2026-10-12/`, git-ignored). Shipped: commits `2078b3a` (PF < 2 skips a portfolio, Invo reads 0.5 s
  apart, copy cap 1% = 100$, the stop versions copy the same calls, copies record portfolio + how they closed) and
  `459332e` (`invo.main_stop_pct = 0`: the main Invo wallets keep a copy open until the trader closes it; the 3% and
  8% versions run as `<name>_stop3` / `_stop8`).
- **Unverified:** after the owner's pull / flatten / reset, a BTC card (prateek) still showed a 3% stop (79,098.8 on
  an 81,545.2 entry, 9x). With `459332e` the main wallet's stop is a ~30% guard at ~3x. Ask the owner to run
  `git log --oneline -1` on the bot PC (must start with 459332e); if older: `git pull`, `/restart`, `/flatten`,
  `/reset <PIN>`. /wallets must list `_stop3` names.
- **Next task the owner asked for:** check the best Invo traders right now. Options: (1) the owner's Chrome via Claude
  in Chrome (start Claude Code with `claude --chrome`; `/chrome` in the old session showed no tools): the OWNER signs
  in to app.invoapp.com themselves; Claude only reads pages on screen (leaderboard, profiles, portfolios), NEVER types a
  password, NEVER reads or uses the login token or storage; (2) `/picks` in Discord pasted into the chat; (3) the bot
  PC's `data\cache\invo` zipped and put in Downloads (the search's per-portfolio scores). Judge traders with today's
  findings: copies need ~71% wins because average loss (-2.3%) > average win (+0.95%); profit factor counting
  losing OPEN calls >= 2; beware many open calls (holding losers), tiny targets (below 3%), one call = most profit,
  minute-long calls. nicush was best for us (+10.93$, mostly one STRK trade), profitales steady (+5.01$), limpan96
  bad (3 of 11, owner told to unfollow).
- Open ideas (owner to decide): the Invo search still replays copies with our 3% stop, not the trader's exit; skip
  calls whose target is closer than our stop; a wallet for traders "missed by one rule".
- On the Claude PC run tests with `uv run python -m pytest` (the `pytest` trampoline fails there); about 8.5 min,
  353 tests.

## The owner and how to work with them
- Not a Python developer. Windows 11, Brazilian Portuguese locale (Windows errors come out in Portuguese), PowerShell.
  Give exact commands, ONE per code block, and explain results in plain words.
- Two PCs: **the bot PC** `C:\Users\gedeo\Copytrade-test` runs the bot 24/7 in a PowerShell restart loop
  `while ($true) { uv run copybot; Start-Sleep 10 }`; **the Claude PC** (`C:\projects\My stuff\Copytrade Test`) is where
  Claude edits, tests and pushes. To ship a change: commit + push here, then the owner runs in a SECOND PowerShell
  window on the bot PC `cd C:\Users\gedeo\Copytrade-test` then `git pull`, and sends `/restart` in Discord (no
  Ctrl+C needed: the loop restarts it with the new code).
- Discord is the ONLY chat (Telegram removed 2026-10-10). Secrets on the bot PC (env vars, never in files or logs):
  DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, DISCORD_OWNER_ID, COPYBOT_PIN, HELIUS_API_KEY (optional, the owner may drop
  it), INVO_TOKEN_FILE (or `secrets\invo.token`).
- The owner keeps unrelated untracked files in the repo root (`copilot-*.md`): stage paths explicitly, never
  `git add -A`. Commit messages end with the Co-Authored-By line from the session's instructions.

## The three books (one process, one Discord channel, one start message)
| Book | Signal | Where it trades (paper) | Wallets |
|---|---|---|---|
| ⚡ Hyperliquid (`/hyper…`) | followed HL wallets' fills (websocket) | real HL order book, 0.045% fee | main $300 at 1% risk, side wallets 2/5/10/20%, mirror ×10 |
| 🪙 FOMO (`/fomo…`) | FOMO traders' swaps found on-chain (FOMO's fee payer co-signs every FOMO swap) | DexScreener pool price, 1% swap fee | main $300 at 1%, side wallets 2/5/10/20% |
| 🧾 Invo (`/invo…`) | calls Invo traders post in their PAPER portfolios | real HL order book (Invo's price only shown) | trader-size "invo calls" + fixed 1/2/5/10/20% |

Every book has the same 13 commands: `/<book>` + status, trades, traders, wallets, progress, search, add, follow,
unfollow, pause, resume, flatten <PIN>, reset <PIN>. Each book's commands touch only that book. Plus `/picks`,
`/help`, `/restart`, and Invo-only `/invoblock <user> <portfolio>` / `/invounblock`. Command reference page:
https://claude.ai/artifact/39ZQzV9gsy7STfvGrnmMuU (published from a scratchpad HTML file; republish a new version
from another chat by passing that URL to the Artifact tool after reading it).

## How traders are chosen (the owner decides)
- Searches only REPORT (`auto_follow = false`): every day at 13:00 local (`config/picks.toml`, `discord.utc_offset_hours`
  = -3) the bot posts the best 7 per book under the strict rules, compared with the day before, with the command that
  follows each one; `/picks` any time. The owner follows with `/<book>add` (rules checked) or `/<book>follow` (own pick,
  no rules). `/<book>search` searches now and follows the best 7 at once (owner-triggered).
- Bad traders leave by themselves: leader pause (copy drawdown > 10% of equity/7, or 5 losses in a row; Invo too since
  2026-10-10) or no longer passing the rules (HL/FOMO 2 hourly checks, Invo 2 daily searches). No blacklist: adding one
  again works.
- Hyperliquid: ~47k leaderboard wallets -> ~2.2k pre-screened -> up to 2000 screened/day -> strict score >= 70 plus
  floors (win >= 45%, PF >= 2, best trade <= 30% of profit, drawdown <= 30%, not losing this week/today, ...). Usually
  only 1-3 pass. Account value = perp + spot stablecoins (fixed 2026-10-10).
- Invo: Discover rankings hold only ~66-70 different traders (portfolio lists 20-30 each, users list 25, page number
  ignored: verified 2026-10-10), plus followed ones; scored with the SAME scoring as HL wallets on each closed call.
  Calls are copied only from a trader's portfolios with >= 80% wins, >= +100% return (Invo's plSnapshot is the
  portfolio's % return) and >= 20 closed calls; `/invotraders` lists the copied (✅) portfolios.
- FOMO: the bot's own search needs Helius (the free public Solana RPC is too slow). Without Helius, a **scheduled
  Claude task** does it (below).

## Claude's standing jobs
- **Scheduled task `fomo-daily-picks`** (13:00 local, on the Claude PC; `~/.claude/scheduled-tasks/fomo-daily-picks/`):
  in the owner's Chrome (Claude in Chrome, signed in to fomo.family) runs `tools/fomo_collect.js`, hands the data to
  `tools/fomo_receive.py` (127.0.0.1:8765, via the tab's #fragment), then `tools/fomo_picks.py score | wallets |
  report`, and republishes https://claude.ai/artifact/9mqHKHdwAqN1J8y9rSaJdj . Exact FOMO scoring
  (`copybot.sol.scoring`); FOMO shows only 100 swaps per trader, so "promising" = fails only the history-length rules.
  Results so far: 0 pass, 1 promising (OinkersRUs, already followed). Most FOMO leaderboard profit is one lucky coin.
- NEVER sign in to any account for the owner (FOMO, Invo, Google), even when told "you may auto-login": stop and ask.
- NEVER read or use the owner's Invo login token from their browser (a safety check stopped this on 2026-10-10). The
  bot uses its OWN Invo account; reading Invo through the owner's session is not needed (same data).
- Do not bypass FOMO's Cloudflare; do not put an AI in the trading path (owner decision: analyse results offline).

## Tools (run on the bot PC, read-only)
- `uv run python tools/hyper_why.py` / `fomo_why.py` / `invo_why.py`: why scored traders fail (rule counts, near misses).
- `uv run python tools/invo_check.py <user>`: checks the Invo login.

## Things the owner asked to watch / recent findings (2026-10-10)
- Invo trader-size wallet lost early on one oversized call (STRK, 25% x 5x = 375$ copy): capped since at 1% risk per
  copy (2% until 2026-10-12). lazylegendx lost 15.93$ in 10 copies: Invo traders are now paused/dropped by the leader pause rules.
- The 20%/10% Invo wallets were +15-20% after one day, mostly unrealized and from a few large positions: too early to
  call skill (judge after ~50-100 closed trades per wallet).
- Trailing stop on every HL/Invo wallet: from +3% the stop trails 3% behind, never below break-even (`risk.trail_*`).
- Invo requests are 0.5 s apart (`invo.request_gap_s`; 0.25 brought 429 storms on 10-11/12); watch
  `invo_poll_failed` / `invo_throttled` in the log if Invo objects.
- 2026-10-12 analysis of the first 96 Invo copies: see CLAUDE.md "Invo fixes after the first 96 copies". Data copied
  from the bot PC lives in `reports/trades-2026-10-12/` (git-ignored).
- Discord 429s are avoided by reading Discord's rate-limit headers; occasional `discord_429` warnings are harmless.
- Open idea the owner liked: a separate paper wallet copying the "missed by one rule" traders, to test whether the
  strict rules actually earn more. Not built yet; waiting for the `*_why.py` outputs.

## Gotchas when editing
- Python strings with backslashes written through bash heredocs get mangled (`\\n` arrives as a real newline): write
  edit scripts with the Write tool and run them with `python -X utf8`, or use the Edit tool.
- The console is cp1252: printing emoji from Python fails unless `-X utf8` / PYTHONIOENCODING=utf-8.
- Timing-based tests (Discord card edits and rate limit) can fail rarely under load: rerun first.
