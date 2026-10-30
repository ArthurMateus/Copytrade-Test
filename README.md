# copybot: copying Invo traders' calls in paper mode

This bot copies the trade calls Invo traders post onto **simulated** $300 wallets. The copies use Hyperliquid's real
prices, order books and fees. Since 2026-10-26 this is the only book: the Hyperliquid wallet book, the FOMO / Solana
book, the Invo arena and the bot's own Invo search were removed at the owner's request. You pick the traders; the daily
fight-and-merge report on the Claude PC suggests them.

**It runs in PAPER MODE ONLY.** It has no signing library and no key handling, and it never calls an order
endpoint. A test enforces this (`tests/test_safety.py`). It is controlled from Discord. New to this project (or a new
Claude chat)? Read `context.md` for the current state, then `CLAUDE.md` for the design.

## Run (Windows, Python 3.11, uv)

```powershell
setx DISCORD_BOT_TOKEN  "<the bot's token>"
setx DISCORD_CHANNEL_ID "<the channel it posts in>"
setx DISCORD_OWNER_ID   "<your Discord user id>"
setx COPYBOT_PIN        "<a PIN for /flatten and /reset>"
setx INVO_TOKEN_FILE    "C:\Users\gedeo\Copytrade-test\secrets\invo.token"
# open a NEW terminal so the variables are visible, then:
uv run copybot
```

For unattended runs, restart the bot automatically if it crashes. It is designed to be killed at any moment:

```powershell
while ($true) { uv run copybot; Start-Sleep 10 }
```

Discord is the bot's only chat: create a bot at https://discord.com/developers/applications (scopes `bot` +
`applications.commands`; permissions View Channels, Send Messages, Embed Links, Read Message History). The commands
appear as slash commands; only the owner can use them, and replies to commands are private. Card times follow
`utc_offset_hours` in `config/discord.toml`.

**Updating** (the bot keeps running the old code until it restarts): in a second PowerShell window run
`cd C:\Users\gedeo\Copytrade-test` then `git pull`, then send `/restart` in Discord; the loop starts the new code.

Keep the PC from sleeping. Each wallet's state lives in `data/wallets/<name>/ledger.jsonl` (append-only, fsync on every
write); `data/ledger.jsonl` keeps the bot's own start events and the Discord card ids. Logs go to `logs/copybot.log`
(rotating, key=value, no secrets). Settings are in `config/*.toml` (`risk`, `broker`, `discord`, `runtime`, `invo`).
Every limit has a documented range, and the bot refuses to start when a value is outside it or a key is unknown.

Run the tests: `uv run pytest` (about 4 minutes; this includes real-process kill -9 restarts).

## Commands

| Command | What it does |
|---|---|
| `/status` | the book at a glance: wallet, P&L, Invo login, health (live card) |
| `/trades` | open copies at live prices, P&L against the $300 start (live) |
| `/traders` | followed traders, their copied portfolios and what copying them made (live) |
| `/wallets` | every size × stop combination: the 3 best, the 3 worst, the average per stop (live) |
| `/progress` | success metrics (longs and shorts separately) |
| `/follow <user>` | follow an Invo trader: every portfolio of it is copied (at most `invo.max_traders`) |
| `/unfollow <user>` | stop following it (its open copies still follow its exits) |
| `/block <user> <portfolio>` | stop copying one portfolio (matched by the start of its name); `/unblock` undoes it |
| `/pause`, `/resume` | stop / allow new copies (exits and stops always run) |
| `/flatten <PIN>` | close every copy and pause |
| `/reset <PIN>` | every wallet back to $300, traders kept, old history archived (refused while a copy is open) |
| `/help`, `/restart` | the list, a restart (the loop above starts it again) |

The older `/invo…` names (`/invofollow`, `/invowallets` ...) still work but are not listed. Each copy gets one
message, which is edited until it becomes the final ✅/❌ summary.

## How a call is copied

Invo (Involio, app.invoapp.com) traders post calls in their portfolios: coin, long/short, leverage, entry, take-profit,
stop-loss and size as a share of the portfolio (paper portfolios), or real trades in "live" (real-money) portfolios.
Invo never shows a trader's Hyperliquid address, so the calls themselves are the signal.
- The watcher reads each followed trader in turn, one request every `invo.request_gap_s` (0.5 s), and sees a new call
  on its next read. A call at most `invo.max_call_age_s` old when first seen is copied; calls already open when we
  start watching a trader are never copied.
- Every wallet gets the call through its own risk gate, filled on Hyperliquid's real order book with its 0.045% fee
  (Invo's own price is only shown as "their entry", and the card says how much later and worse we got in).
- With `margin_allocations = true`, traders on the same coin and side have separate contributions in one position.
  When a trader closes, only their contribution is sold; the remaining traders keep their contributions open.
  The last trader's exit closes the position. The shared stop still closes all remaining contributions.
  When a trader adds or trims, only their contribution grows or shrinks, subject to risk limits on adds. If an add is refused, later trims
  scale the position actually held, not the larger position we never bought. Changes below the minimum order size
  accumulate until tradable. Resize cards show what our copy actually added, trimmed or skipped. A second followed
  trader calling the same coin and side adds half the wallet's initial margin allocation. Each trader's fees,
  funding and result are tracked separately. A partial exit smaller than the minimum order stays pending until
  enough exiting size accumulates or the final contribution exits; it never sells another trader's contribution.
- A trader whose copies lose too much (`invo.pause_dd_pct` of the wallet, or 5 losses in a row) is dropped at once;
  `/follow` brings it back.

**The wallets (18).** Every size × every stop, all on the same calls:
- sizes with `margin_allocations = true` (enabled in the shipped config): 5% margin in the main wallet
  (`invo.limits_pct`), and 1/2/5/10/20% margin in the named comparison wallets (`risk_wallets_pct`, a legacy setting
  name). At $300, 5% is $15 margin; at 10x it controls $150. Each agreeing trader receives another half allocation
  (2.5% / $7.50 margin in that example), within available margin, health and wallet risk limits. These percentages
  are allocations, not the amount lost at the stop. Cards are labelled “margin”. Set `margin_allocations = false`
  only to retain the older trader-size / fixed-risk comparison mode;
- stops: 3%, 8% and the trader's exit (`stops_pct`; the trader's exit has only a guard just before liquidation at the
  trader's leverage, at most 10x). `main_stop_pct` picks the wallets that keep the plain names and decide which calls
  are copied; the others are `<name>_stop3`, `_stop8` or `_trader`.

Existing positions are adopted at their actual sizes; enabling margin allocations does not top them up or reset
their balances. Their existing funded agreement shares are preserved. A `margin_policy` event records when the
new sizing began in each wallet, so historical results before that event retain their old meaning.

**Use a SEPARATE Invo account for the bot.** The bot logs in with a refresh token; every renewal replaces it, so the
bot and a browser sharing one login keep logging each other out, and if Invo objects to automated reading only that
account is affected.

### Design choices
- **Stops are set at entry.** Exits are never refused: stale prices, a paused bot, uncertain state, or the 30
  orders/min limit all block entries only. If the book cannot be fetched, an exit fills at mid ± 20 bps.
- **Leverage** is the highest value (≤10x and ≤ the coin's maximum) that still keeps liquidation at least 3× the stop
  distance away; the trader's-exit wallets take the trader's leverage (at most 10x).
- **Restart with doubt:** if a wallet's ledger has a torn line, an order intent with no result, or a position without a
  valid stop, that wallet's entries pause, Discord gets an alert and stops and exits keep running. `/resume`
  acknowledges the problem.
- **Copies closed while the bot was off** are found at the restart: a copy whose call is gone is closed then.

### Setup of the Invo login (once)
1. In Chrome, open an **Incognito window** (Ctrl+Shift+N), go to https://app.invoapp.com and sign in with the bot's
   Invo account.
2. Press F12, open **Console**, type `allow pasting` and Enter if Chrome asks, then paste this and press Enter:
   ```
   (async()=>{const b=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));const k=await crypto.subtle.importKey('raw',b(localStorage.FlutterSecureStorage),{name:'AES-GCM'},false,['decrypt']);const [iv,ct]=localStorage['FlutterSecureStorage.REFRESH_TOKEN'].split('.').map(b);const t=new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv},k,ct));copy(t);console.log('Invo token copied ('+t.length+' characters)')})()
   ```
   It says "Invo token copied (about 750 characters)" and the token is on the clipboard.
3. Open Notepad, paste, save as `secrets\invo.token` inside the bot folder ("Save as type: All files").
4. **Close the Incognito window without logging out** (logging out would cancel the token).
5. In PowerShell: `setx INVO_TOKEN_FILE "C:\Users\gedeo\Copytrade-test\secrets\invo.token"`, open a NEW window,
   check it with `uv run python tools/invo_check.py nicush` (prints "login OK" and nicush's portfolios), then start
   the bot as usual. The token lasts about a year and is renewed by the bot itself.
If Invo refuses the login later, the bot alerts on Discord; repeat steps 1-4.

## Findings from the real API (recorded in `tests/fixtures`)
- Hyperliquid closes the websocket about every 3 h ("Expired"); the bot reconnects in seconds. Only an outage longer
  than `runtime.ws_alert_after_s` (60 s) alerts on Discord. New copies wait while it is down; exits keep working.
- This PC's clock was about **1.4 s ahead** of the exchange (±0.15 s). The offset is measured and corrected.
- Invo answers "too many requests" (429) when read too fast: the bot then doubles its spacing (up to 3 s) and eases back
  after a calm minute (log `invo_throttled`).

## Success metrics (fixed before running)
- 50–100 copied trades in 2–4 weeks.
- 0 missed exits and 0 stop-less positions.
- Median copy lag ≤ 5 s from the trader's call to our paper fill. Every copy is logged (`invo_entry_gap`).
- Paper P&L after fees, funding and slippage, compared with holding BTC from the first start.

**Kill criterion:** P&L clearly negative after costs over ≥ 50 trades, or any unexplained missed exit or
stop-less position. `/progress` shows all of these.

A few dozen paper trades show that the bot works and that a profit is plausible. They do not prove an edge exists.
