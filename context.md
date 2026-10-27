# context.md: where the project stands (2026-10-26), for a new chat or another account

Start here, then read CLAUDE.md (design, decisions, hard rules; the source of truth) and README.md (how to run and use
it). Repo: https://github.com/ArthurMateus/Copytrade-Test. Everything is **PAPER ONLY**: no keys, no signing, no order
endpoints (`tests/test_safety.py` enforces it). Tests: `PYTHONPATH=src python -m pytest` on the Claude PC (~180 tests,
~4 min).

## 2026-10-26: only the Invo book is left (branch `invo-only`)
- Owner request: "delete arena wallets, also everything related to hyperwallet and fomowallet, and take it off the code,
  just keeping the invo one", and (asked) the bot's own Invo search too: the owner follows from the daily
  fight-and-merge report, not from the bot's picks. Removed: the Hyperliquid wallet book (leader following, its search
  and scoring, side and mirror wallets, `/hyper…`), the FOMO / Solana book (`sol/`, `/fomo…`, the FOMO tools), the Invo
  arena (`arena.py`, `/arena`), the Invo search (`invo_scorer.py`, `/search`, `/add`, `/picks`, the 13:00 picks, the
  drop/re-pick buttons), the portfolio floors and `always_copy` (every portfolio of a followed trader is copied; only
  `/block` stops one), `config/selection.toml`, `sol.toml`, `picks.toml`, `HELIUS_API_KEY`.
- Hyperliquid stays as the market: prices (websocket allMids + REST fallback), order books, paper fills, fees,
  funding, the coin list.
- Why it also helps: the arena's live groups and the search shared the Invo request queue with the copies, which made
  copy rounds 15-30 s long (copies were 2-6 s late in mid-October). After the deploy, check the log's
  `invo_entry_gap ... age_s=` values.
- On the bot PC the old data can be deleted by the owner (the code no longer reads it): `data\arena\`, `data\sol\`,
  `data\wallets\` folders not starting with `invo_`, `data\cache\` (search caches), `data\picks.json`.

## The owner and how to work with them
- Not a Python developer. Windows 11, Brazilian Portuguese locale (Windows errors come out in Portuguese), PowerShell.
  Give exact commands, ONE per code block, and explain results in plain words.
- Two PCs on the same internet: **the bot PC** `C:\Users\gedeo\Copytrade-test` runs the bot 24/7 in a PowerShell
  restart loop `while ($true) { uv run copybot; Start-Sleep 10 }`; **the Claude PC** (`C:\projects\My stuff\Copytrade
  Test`) is where Claude edits, tests and pushes. To ship a change: commit + push here, then the owner runs in a SECOND
  PowerShell window on the bot PC `cd C:\Users\gedeo\Copytrade-test` then `git pull`, and sends `/restart` in Discord.
- Discord is the ONLY chat. Secrets on the bot PC (env vars, never in files or logs): DISCORD_BOT_TOKEN,
  DISCORD_CHANNEL_ID, DISCORD_OWNER_ID, COPYBOT_PIN, INVO_TOKEN_FILE (or `secrets\invo.token`).
- The owner keeps unrelated untracked files in the repo root (`copilot-*.md`): stage paths explicitly, never
  `git add -A`. Commit messages end with the Co-Authored-By line from the session's instructions.

## How traders are chosen (the owner decides)
- On the Claude PC, a Maestri canvas team runs a daily paper "fight and merge" (`reports/daily/<date>/`, owner's prompt
  in `C:\projects\claude\invo-daily-orchestrator-prompt.md`): one discoverer reads Invo once a day through a Maestri
  portal signed in with a separate account, four analysts propose groups, a judge re-tests them on shifted windows and
  names the champion with the Discord commands to type (`reports/daily/champion.json`, `scoreboard.csv`).
- Both PCs share one internet connection: keep that daily Invo read light and once a day (Invo started refusing reads
  on 2026-10-26 after three heavy runs in three days), so it does not slow the bot's own Invo reads.
- The bot only copies who the owner `/follow`s, in every portfolio unless `/block`ed.

## Claude's rules
- NEVER sign in to any account for the owner (Invo, Google, Discord), even when told "you may auto-login": stop and ask.
- NEVER read or use the owner's Invo login token from a browser. The bot uses its OWN Invo account.
- Do not put an AI in the trading path (owner decision: analyse results offline).

## Tools (run on the bot PC, read-only)
- `uv run python tools/invo_check.py <user>`: checks the Invo login.

## Gotchas when editing
- Python strings with backslashes written through bash heredocs get mangled (`\\n` arrives as a real newline, `\b` as
  a backspace): write edit scripts with the Write tool and run them with `python`, or use the Edit tool.
- `uv run` fails inside the Claude app ("trampoline failed to canonicalize script path"): run tests with
  `PYTHONPATH=src python -m pytest`.
- The console is cp1252: printing emoji from Python fails unless `-X utf8` / PYTHONIOENCODING=utf-8.
- Timing-based tests (Discord card edits and rate limit) can fail rarely under load: rerun first.
