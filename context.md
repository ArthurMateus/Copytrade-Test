# context.md: state of the work (2026-10-09), for the next session

Repo: https://github.com/ArthurMateus/Copytrade-Test (branch main). Read CLAUDE.md (the source of truth for design and
decisions) and README.md first. ~298 tests pass (`uv run pytest`, ~3 min). Everything is PAPER ONLY.

## What runs on the owner's bot PC (C:\Users\gedeo\Copytrade-test, PowerShell restart loop)
- **Hyperliquid book** (main $300 wallet + side wallets 2/5/10/20% + **mirror x10** + **invo calls**). Leaders from the
  HL leaderboard, strict scoring (win rate floor 45% since 2026-10-09, plus "no single trade > 30% of profit").
  `/hyperadd 0x…` checks one wallet now. `tools/hyper_why.py` explains rejections.
- **FOMO / Solana book** (`src/copybot/sol/`): FOMO traders followed ON-CHAIN (FOMO's fee payer co-signs every FOMO
  swap; the address FOMO shows is not the trading wallet). Optional HELIUS_API_KEY (search + live). `/fomosearch`,
  `/fomoadd <wallet>` (strict scoring), `/fomofollow <wallet>` (owner pick, no scoring), `/fomounfollow`.
  `tools/fomo_why.py`. Owner's current pick: OinkersRUs `DPAN2Vig8BokpQCCu4mtV1mTWYLS7ktKe1hHKVBcsayL`.
- **Invo calls wallet** (`src/copybot/invo.py`, side wallet `invo_calls`): copies Invo traders' posted PAPER calls at
  live HL prices. Needs INVO_TOKEN_FILE (the bot's OWN Invo account's refresh token; README "Invo calls wallet").
  Working on the owner's PC since 2026-10-09 (`tools/invo_check.py` OK) after one fix: the token refresh is
  GET /v1_0/auth/refresh_token (POST answers 405). `/invofollow <user>`, `/invounfollow <user>`, `/invo`.

## Daily picks (2026-10-10)
- The bot no longer follows by itself (`auto_follow = false`): it sends the best 7 per book at 13:00 local
  (`/picks` any time) with the follow commands; the owner follows. Invo has its own daily search with the HL scoring
  (`copybot/invo_scorer.py`). Bad traders still leave automatically; re-adding works (no blacklist). See CLAUDE.md.
- The Invo ranking ANSWER shape was never recorded (only the request bodies): if `invo_discovered traders=0` shows in
  the log, record one answer (DevTools on app.invoapp.com/discover) and fix `invo.usernames` / add a fixture.

## FOMO daily picks (scheduled Claude task, since 2026-10-10)
- Task `fomo-daily-picks` at 13:00 on the owner's Claude PC, via Claude in Chrome (signed in to fomo.family). Never sign
  in for the owner, even if asked to "auto-login": stop and ask. Details in CLAUDE.md; page
  https://claude.ai/artifact/9mqHKHdwAqN1J8y9rSaJdj . The owner then sends /fomoadd <wallet> to the bot.

## How Claude helps pick traders (only when the owner asks in a session; FOMO also by the daily task above)
- FOMO: owner signs in to fomo.family in the browser pane; read the 7d/30d ranking and each candidate's last 100
  swaps in the signed-in tab (FOMO caps at 100), vet (win rate, PF, best-trade share, pace, USDC share), then find
  the real wallet = the SIGNER (besides FOMO's fee payer) of a FOMO-co-signed tx where its balance of that mint
  changed by exactly the swap amount (never just any owner: pools also change by that amount). Of 223 top FOMO
  traders on 2026-10-09 only 1 passed; most top profits are one lucky trade.
- mofo.gg leaderboard ranks by "coin +50% within 48 h of the buy", not by the trader's profit: most of its top names
  lost money on their own last 100 trades.
- Invo: portfolios are paper and Invo hides HL addresses, so the calls themselves are copied. Pick from the Discover
  rankings (Trending / This Month / All Time) and let the invo calls wallet's results decide (/invo, /hyperwallet).
  Do not bulk-harvest Invo data through the owner's session; the bot reads only the followed traders.

## Gotchas
- Owner: Windows 11, pt-BR, PowerShell, not a Python developer: exact commands, one per block, plain words.
- Never commit the owner's untracked files in the repo root (copilot-*.md): stage paths explicitly, never `git add -A`.
- Writing Python strings with backslash escapes through bash heredocs gets mangled: use the Write/Edit tools or a
  script file.
- Some timing-based tests (discord card edits and rate limit) can fail rarely under load: rerun first.
