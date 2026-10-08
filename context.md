# context.md: state of the work (2026-10-08), for the next session

Repo: https://github.com/ArthurMateus/Copytrade-Test (branch main). Read CLAUDE.md and README.md first. 277 tests pass (`uv run pytest`, ~3 min).

## What exists
- Hyperliquid paper bot (original) + stricter "losing right now" gates (selection.max_loss_7d/24h, streak, recent win rate, 7d drawdown).
- Solana/FOMO paper book in `src/copybot/sol/` (own $300, own ledger `data/sol/`), runs in the same process. Needs a FOMO login cookie, else it stays off.
- Telegram + Discord (owner's own `discord.py`, slash commands). Commands: `/hyper<x>` (Hyperliquid) and `/fomo<x>` (FOMO): status, trades, traders, wallet, positions, leaders, progress, search, pause, resume, flatten <PIN>, reset <PIN>. Resets archive and restart the process; refused while a trade is open.
- Tools: `tools/fomo_cookie_trim.py` (diagnoses the cookie and trims it).

## OPEN PROBLEM 1: the bot cannot log in to FOMO (HTTP 403 {"authorization":false})
- Auth is the HttpOnly cookie `privy-token` (a Privy JWT, lives exactly 1 HOUR) + `privy-session`, `__cf_bm`. A fresh cookie from the owner's browser (taken from a prod-api.fomo.family request) is still refused when sent from Python (tried cookie only, and cookie + Authorization: Bearer).
- The website's own requests to prod-api.fomo.family send NO authorization header, only these custom headers: `app-language`, `content-type`, `x-supported-chains`. I just added both to `browser_headers()` in `sol/fomo.py` with guessed values (`en`, `1399811149`; env overrides FOMO_APP_LANGUAGE / FOMO_SUPPORTED_CHAINS). UNTESTED against the real API. Next: owner reads the real values (console snippet printing only those two header values) and reruns `uv run python tools/fomo_cookie_trim.py secrets\fomo.cookie`.
- If still refused: suspect Cloudflare (`__cf_bm` is bound to the browser) or IP binding; the answer body was `{"authorization":false}` (app-level, not a Cloudflare page). Other ideas: the page may use the Privy identity token (`privy-id-token`) or a cookie the owner did not copy; compare a working in-page `fetch` (it worked from the owner's Chrome with `credentials:'include'`, no headers).
- Hourly expiry: need an automatic refresh (Privy refresh token via privy.fomo.family /api/v1/sessions, unverified). Owner has not yet sent the request details (URL, header NAMES, cookie NAMES, response field names, no values).
- Owner pasted a live cookie in chat once: never ask for values, only names.

## OPEN PROBLEM 2: owner's Hyperliquid run opened only longs and loses
- Code checked: detector and open path treat shorts like longs (real recorded fills give short opens). Not reproduced; likely the followed traders were all long. Need the owner's `data/ledger.jsonl` (other PC, C:\Users\gedeo\Copytrade-Test) to investigate. `/hyperprogress` now shows longs vs shorts and warns when all are long.
- To reset that run on the other PC: `git pull`, restart the bot, `/hyperflatten <PIN>`, `/hyperreset <PIN>`.

## Gotchas
- Owner: Windows 11, pt-BR, PowerShell, not a Python developer. Other PC path: C:\Users\gedeo\Copytrade-Test.
- GitHub push needs the ArthurMateus login (gh auth is set up on this PC as ArthurMateus).
- Writing Python strings with backslash escapes through bash heredocs gets mangled: use the Edit/Write tools, or chr(92).
- Some upstream tests (test_discord rate limit, test_search_repicks) are timing flaky under load; they pass alone.
