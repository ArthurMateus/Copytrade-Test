# context.md: state of the work (2026-10-08), for the next session

Repo: https://github.com/ArthurMateus/Copytrade-Test (branch main). Read CLAUDE.md and README.md first. 272 tests pass (`uv run pytest`, ~3 min).

## What exists
- Hyperliquid paper bot (original) + stricter "losing right now" gates (selection.max_loss_7d/24h, streak, recent win rate, 7d drawdown).
- Solana/FOMO paper book in `src/copybot/sol/` (own $300, own ledger `data/sol/`), runs in the same process. Since 2026-10-08 it
  follows FOMO traders ON-CHAIN (no FOMO login): see CLAUDE.md "On-chain since 2026-10-08". Optional `HELIUS_API_KEY` for live data.
- Telegram + Discord (owner's own `discord.py`, slash commands). Commands: `/hyper<x>` (Hyperliquid) and `/fomo<x>` (FOMO): status, trades, traders, wallet, positions, leaders, progress, search, pause, resume, flatten <PIN>, reset <PIN>. Resets archive and restart the process; refused while a trade is open.
- Tools: `tools/record_chain.py` (re-records the Solana fixtures from the public RPC).

## RESOLVED: the bot could not log in to FOMO
- FOMO's API answers 403 `{"authorization":false}` to ANY script, even for robots.txt with no cookie: Cloudflare bot
  blocking, not the login. Not bypassed on purpose. The cookie code and `tools/fomo_cookie_trim.py` were removed.
- Replaced by on-chain discovery through FOMO's fee payer and Helius for live data. Owner set `HELIUS_API_KEY` on the
  other PC (not on this one). Next: owner `git pull` + restart; first review takes hours; check `/fomoleaders`.

## OPEN PROBLEM 2: owner's Hyperliquid run opened only longs and loses
- Code checked: detector and open path treat shorts like longs (real recorded fills give short opens). Not reproduced; likely the followed traders were all long. Need the owner's `data/ledger.jsonl` (other PC, C:\Users\gedeo\Copytrade-Test) to investigate. `/hyperprogress` now shows longs vs shorts and warns when all are long.
- To reset that run on the other PC: `git pull`, restart the bot, `/hyperflatten <PIN>`, `/hyperreset <PIN>`.

## Gotchas
- Owner: Windows 11, pt-BR, PowerShell, not a Python developer. Other PC path: C:\Users\gedeo\Copytrade-Test.
- GitHub push needs the ArthurMateus login (gh auth is set up on this PC as ArthurMateus).
- Writing Python strings with backslash escapes through bash heredocs gets mangled: use the Edit/Write tools, or chr(92).
- Some upstream tests (test_discord rate limit, test_search_repicks) are timing flaky under load; they pass alone.
