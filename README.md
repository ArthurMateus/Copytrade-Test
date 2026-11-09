# copybot: copying Invo traders' calls (paper, plus one optional live wallet)

This bot copies the trade calls Invo traders post onto **simulated** $300 wallets. The copies use Hyperliquid's real
prices, order books and fees. Since 2026-10-26 this is the only book: the Hyperliquid wallet book, the FOMO / Solana
book, the Invo arena and the bot's own Invo search were removed at the owner's request. You pick the traders; the daily
fight-and-merge report on the Claude PC suggests them.

**Paper by default.** The 18 Invo wallets are simulated. One optional **live wallet** (off until you turn it
on, see *Live trading* below) copies one of them with real orders on Hyperliquid. Signing and the key exist only in
`src/copybot/live.py`, which can place and cancel orders and set leverage, and nothing that moves money out of the
account (a test enforces this: `tests/test_safety.py`). It is controlled from Discord. New to this project (or a new
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
(rotating, key=value, no secrets). Settings are in `config/*.toml` (`risk`, `broker`, `discord`, `runtime`, `invo`, `live`).
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
| `/pause`, `/resume` | stop / allow new paper copies (exits and stops always run) |
| `/close <coin>` | close that trade now in every paper wallet (e.g. `/close BTC`): a held copy blocks opposite calls on its coin, so this lets them be copied again |
| `/flatten <PIN>` | close every paper copy and pause |
| `/reset <PIN>` | every wallet back to $300, traders kept, old history archived (refused while a copy is open) |
| `/help`, `/restart` | the list, a restart (the loop above starts it again) |
| `/livewallet` | the real account as Hyperliquid itself reports it: value, margin, free balance, spot USDC, positions with their liquidation price, resting stop orders |
| `/livestatus` | the live wallet: balance, the real account, today vs the daily stop, open trades and their stops on the exchange, live vs paper |
| `/livepause`, `/liveresume` | stop / allow new live copies (live exits and stops always run) |
| `/liveclose <coin> <PIN>` | close that one live trade now |
| `/livekill <PIN>` | close every live position (also anything else on the account), cancel its orders, pause |

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

## Running unattended (health alerts, auto-start, backups)

### Health alerts
The bot posts `⚠️ Health: ...` in Discord once when something goes wrong, and `✅ ... OK again` when it is over:
prices older than 60 s, Invo not answering for 5 min (while traders are followed), the live account not checked
for 2 min. (`config/runtime.toml`: `stale_prices_alert_s`, `invo_silent_alert_s`.)

A bot cannot tell you that its own PC is off. For that, an outside service waits for the bot's check-in every
minute and warns you when it stops:

1. Make a free account on https://healthchecks.io (you sign up yourself), "Add Check", period **5 minutes**, grace
   **5 minutes**. Under "Integrations" add your email, or Discord via a webhook of a channel.
2. Copy the check's ping address (`https://hc-ping.com/...`) and in PowerShell on the bot PC:

```powershell
setx HEALTHCHECK_URL "<the ping address>"
```

3. Restart the bot (a NEW window or the task below). While something is wrong, the check-in reports a failure, so
   healthchecks.io warns you about that too.

### Start by itself after a reboot or a Windows update
Close the old `while ($true) ...` window first (only one bot may run), then in PowerShell in the bot folder:

```powershell
cd C:\Users\gedeo\Copytrade-test
```

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
```

```powershell
Start-ScheduledTask -TaskName copybot
```

It starts at every Windows sign-in and is restarted if it stops; `/restart` in Discord works as before. To start
even before anyone signs in, run the install again in a PowerShell opened with "Run as administrator", adding
`-AtBoot`. `-Remove` takes the task away. The bot's output is in `logs\copybot.log` and `logs\console.log`.

Keep the PC awake (you change this setting yourself):

```powershell
powercfg /change standby-timeout-ac 0
```

and in Windows Update set "Active hours" to the whole day, so it does not reboot by surprise.

### Daily backups
Once a day the bot zips `data\` (every wallet's ledger, including the live one) into `backups\`, keeping the newest
14. For a copy that survives the PC, set `backup_dir` in `config/runtime.toml` to a OneDrive or Google Drive folder,
e.g. `backup_dir = "C:/Users/gedeo/OneDrive/copybot-backups"`. To restore: stop the bot, unzip one into `data\`.

### Moving to a server (later)
`deploy/` has what a Linux server needs: `setup_server.sh` (installs everything as a service that restarts by
itself), `copybot.service` and `copybot.env.example` (the secrets, the same names as above). Never run the bot on two
machines at once: remove the Windows task first, then copy `data\` over.

## Public results channel (read-only for others)

The bot can post one wallet's trades to a second Discord channel that other people can read but not write in. It
shows new trades (coin, side, leverage, entry, stop), closed trades (result in % of the trade and of the wallet,
how long, why) and a summary every day at 21:00 (your time). Never dollar amounts, and no trader names unless you set
`public_show_traders = true`. Commands keep working only in your own channel, only for you.

1. In your Discord server create a channel, e.g. `#copy-results`. In its settings → Permissions: `@everyone` may
   **View Channel** and **Read Message History** but NOT **Send Messages**; the bot's role may **Send Messages** and
   **Embed Links**.
2. Discord → User Settings → Advanced → turn on Developer Mode. Right-click the channel → Copy Channel ID.
3. In PowerShell on the bot PC, then restart the bot:

```powershell
setx DISCORD_PUBLIC_CHANNEL_ID "<the channel id>"
```

`config/discord.toml` picks the wallet (`public_wallet`: the 5% · 8% paper wallet by default, `"live"` for the real
one once it runs), the hour and whether traders are named. Before inviting paying users, read the legal note in
`docs/PRODUCTION_PLAN.md` (Phase 5): sharing results is fine, selling copy signals may need a CVM registration.

## Live trading (real money, Hyperliquid)

One wallet, `live`, copies the paper wallet `copy_wallet` in `config/live.toml` (5% margin · 8% stop) with real orders:
the same calls and stop, sized the same way on its own 100$ ledger (`data/wallets/live/`; the paper wallets keep
300$, so compare percentages). On top of the paper limits:

- **Daily stop:** no new entries once the day's NET result is −15% of the day's start (a day at +30% that falls back
  to +15% does not stop). It holds until your next day (local midnight, `discord.utc_offset_hours`).
- **Weekly stop:** no new entries while the week's NET result is −25% of the week's start (`live.weekly_loss_pct`).
- **Floor:** no new entries once the wallet is at or below 75$ (25% under the 100$ start), until `/liveresume <PIN>`.
- **Position cap:** one coin never above 70$ notional (a bigger order is trimmed to it). Entries fill at most 0.5%
  from the mid, else they are skipped. All open stops together risk at most 30% of the wallet, and liquidation sits
  at least 1.25x the stop distance away (so the leverage can be a step lower than the paper twin's).
- **Stops on the exchange:** every live position's stop also rests on Hyperliquid as a stop-market order, so it fires
  while the bot or the PC is down. Exits are reduce-only and can never open a position.
- **Reconcile:** every 15 s the ledger is compared with the real account. A stop the exchange fired is booked at its
  real price; anything the bot did not open pauses live entries. **Use the Hyperliquid account only for the bot.**
- `/flatten`, `/pause`, `/resume` and `/reset` act on the paper wallets only; the live wallet has its own
  `/livekill`, `/livepause`, `/liveresume`.

Fees are booked at 0.045% (`/livestatus` shows the real account value next to the ledger's; an account holding less
than the ledger alerts).

### Turning it on (testnet first)

1. On https://app.hyperliquid.xyz create your account. Then **More → API**, create an API wallet named `copybot` and
   authorize it. It can trade but cannot take money out. Copy its **private key** straight into a password manager
   (it is shown once). Never use your main wallet's key: the bot refuses it.
2. For the test network, do the same on https://app.hyperliquid-testnet.xyz (it has its own API wallets; its faucet
   gives test USDC to an address that has used mainnet).
3. In PowerShell on the bot PC (paste the key yourself; it never goes into a file, a chat or a log):

```powershell
setx HL_ACCOUNT_ADDRESS "<your account address, 0x...>"
```

```powershell
setx HL_AGENT_KEY "<the API wallet's private key>"
```

4. In `config/live.toml` set `enabled = true` (keep `network = "testnet"`), open a NEW PowerShell window for the
   restart loop, and send `/restart`. The start message says `paper + LIVE testnet`; `/livestatus` shows the wallet.
5. After a few days on testnet with no mismatch alert, set `network = "mainnet"` and the API wallet key of the mainnet
   account, deposit the 100$ USDC, `/restart`.

If something is wrong (keys missing, API wallet not approved, coin list unreadable), the start message says
`LIVE is off: ...` or the live wallet starts paused, and the paper wallets run as usual.

## Web dashboard

A read-only web page with the live wallet (balance vs its start, today vs the day's start, floor, paused or not, open
and closed trades, a balance chart), the live wallet vs its paper twin in % since the live start, the 18 paper wallets
ranked by return (the live wallet's twin highlighted), what each trader made, and the open and last closed trades of
every wallet. It refreshes itself every 15 s. Times follow `utc_offset_hours` in `config/discord.toml`. The same data
is at `/api/summary` (JSON).

It is a **separate program** from the bot: it only reads `data/wallets/*/ledger.jsonl`, never writes anything, and
cannot change a trade (any request other than reading gets "405"). If it crashes, the bot does not notice. Prices of
open trades come from Hyperliquid's public prices; when they cannot be read, the page says so and counts open trades at
their entry price.

### Starting it

In a NEW PowerShell window on the bot PC (the bot keeps running in its own window):

```powershell
cd C:\Users\gedeo\Copytrade-test
```

```powershell
uv run copybot-dashboard
```

Then open http://127.0.0.1:8787 in a browser on that PC. It listens only on this PC (127.0.0.1): nobody else on the
network can open it. Another port, if 8787 is taken:

```powershell
uv run copybot-dashboard --port 8790
```

### Starting it automatically

This creates a Windows task that starts the dashboard (and restarts it if it stops) every time you sign in to
Windows:

```powershell
schtasks /Create /TN copybot-dashboard /SC ONLOGON /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File C:\Users\gedeo\Copytrade-test\tools\dashboard_loop.ps1"
```

Start it now without signing out:

```powershell
schtasks /Run /TN copybot-dashboard
```

To remove it later:

```powershell
schtasks /Delete /TN copybot-dashboard /F
```

After a `git pull` the dashboard keeps the old code until it restarts: sign out and in again, or close its minimized
PowerShell window (the task starts again at the next sign-in; `schtasks /Run /TN copybot-dashboard` starts it now).

### Opening it from anywhere (Cloudflare Tunnel + Cloudflare Access)

The page has no password of its own. Remote access goes through Cloudflare: a **tunnel** carries the page from the
PC to Cloudflare (no port is opened on your router), and **Access** asks for a one-time code sent to your email before
it shows anything. You do the sign-up and every sign-in yourself. Cloudflare's menu names change from time to time, so
they may differ slightly from the ones below. The free plan is enough.

1. **A domain on Cloudflare.** Sign up at https://dash.cloudflare.com. The tunnel needs a domain whose DNS is on
   Cloudflare: add one you own (**Add a domain**, then change its nameservers where you bought it, as Cloudflare
   shows), or buy one in **Domain Registration**. Below it is called `yourdomain.com`.
2. **Zero Trust.** In the dashboard open **Zero Trust** and pick a team name and the Free plan (it may ask for a card
   even for the free plan).
3. **Who may open it, FIRST** (so the page is never public, not even for a minute): **Zero Trust → Access →
   Applications → Add an application → Self-hosted**. Name `copybot`, domain `bot.yourdomain.com`. Add a policy:
   name `owner`, action **Allow**, include **Emails** = your email address. Under login methods keep
   **One-time PIN**. Save.
4. **Install cloudflared** on the bot PC:

```powershell
winget install --id Cloudflare.cloudflared
```

5. **Create the tunnel:** **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**, name `copybot`, choose
   Windows. Cloudflare shows a command like `cloudflared.exe service install eyJ...`. Open PowerShell **as
   administrator** (Start menu, right-click PowerShell, "Run as administrator") and paste that command there yourself.
   The long `eyJ...` part is the tunnel's secret: never put it in a chat, a file in the repo or a screenshot. This
   installs cloudflared as a Windows service, so it starts with the PC.
6. **Point the tunnel at the dashboard:** in the tunnel's **Public Hostname** (or "Routes") tab add subdomain `bot`,
   domain `yourdomain.com`, service type **HTTP**, URL `127.0.0.1:8787`. Save.
7. **Test** on your phone with Wi-Fi off: open https://bot.yourdomain.com, type your email, type the code it sends.
   You should see the dashboard. Any other email gets no code and no page.

To cut remote access at any time: delete the tunnel (or its public hostname) in Zero Trust; the dashboard keeps
working on the PC itself.

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
