# Production plan: paper bot → live trading → paid product

Written 2026-11-03 at the owner's request, for the **`invo-only` branch** (the one the bot runs: only the Invo
book, no Hyperliquid wallet book, no FOMO book, no bot-run Invo search). Until Phase 1 ships, the bot stays **paper only** and the hard rules in
CLAUDE.md still apply. This plan is what changes them, step by step.

Starting point (paper, Invo book, ~3 weeks): the 5% margin · 8% stop wallet was the best of 18 combinations,
+53.02$ (+17.67%) on 105 trades, 75% won, biggest drop −18.9%, 7 trades still open. Small sample, partly unrealized,
and the best of 18 is flattered by being picked: live results are expected to be worse (delay, slippage, failed orders).

There are five phases, and each one has to work before the next starts. Phases 1–3 are for the owner's own use.
Phases 4–5 turn it into a product.

## Owner decisions (2026-11-03)
| Question | Answer |
|---|---|
| Exchange | Hyperliquid (check that Brazil is allowed; no VPN) |
| Rollout | Small live wallet next to the paper wallets |
| Live setup | 5% margin · 8% stop; later 2% margin with a bigger balance |
| Where it runs | The home PC for now; a small server later, when there is money for it |
| Live balance | 300$ |
| Daily stop | No new entries once the **day's net result** is −15% of the balance at the start of the day. Measured from the day's start, **not** from the day's best: a day that goes +30% then falls to +15% does not stop |
| Total stop | No new entries once the live wallet is at or below **150$** (−50% of 300$), until the owner resumes it |
| Hyperliquid account | Not yet: the owner creates it (Phase 0) |
| Traders | The ones the owner follows, picked from the daily Claude report and changed by hand at any time |
| Discord | Owner control stays private; a public read-only server later; then a paid product |
| Web dashboard | Reachable from anywhere |

## Why Hyperliquid (checked 2026-11-03)
Exchanges for copying perp trades from Brazil. Sources are news sites and guides, not official pages: re-check fees on
the exchanges' own docs and the legal points with the CVM / a lawyer before relying on them.

| Exchange | Taker fee | Futures open to Brazilians? | Fit for this bot |
|---|---|---|---|
| **Hyperliquid** | 0.045% | Yes (not on the restricted lists found; no KYC) | ✅ Paper results already use its prices, books and fees; 180+ coins; agent keys that cannot withdraw |
| Lighter | 0% (standard account) | Yes (decentralized) | ⚠️ Cheapest, but ~168 coins (some Invo calls missed), 200–300 ms order delay, new code and new paper testing |
| Aster | ~0.04% | Yes (decentralized) | Small saving, one source, smaller market |
| Binance | 0.05% | ❌ CVM ban on futures for Brazilians since 2020 (2024 Termo de Compromisso) | No |
| Bybit | 0.055% | ❌ Brazilians' derivative positions liquidated Sept 2026, users moved to a local entity | No |
| OKX / Bitget | 0.05–0.06% | Unclear: no confirmed position in Brazil | Risky: access could be cut with trades open |

- Using a VPN to reach a blocked exchange breaks its terms and can freeze the account.
- Fee cost at this size: ~100$ per copy → ~0.09$ per round trip on Hyperliquid, ~9–10$ over the paper period's 105
  trades (vs +53$ made). Lighter's 0% would save that but costs missed coins and restarting the paper test.
- Small Hyperliquid savings: a referral code (~4%) and a small HYPE stake (~5%). Limit (maker) orders would risk missed
  copies: not used.
- Later option (not planned yet): track Lighter on paper next to Hyperliquid to measure fee saving vs missed coins.
- Taxes (ask an accountant): gains on a foreign / DeFi platform likely fall under the 15% annual rate (Lei 14.754/2023,
  no R$35k monthly exemption), and monthly DeCripto reports may be the owner's own job since July 2026
  (IN RFB 2.291/2025). The bot's ledger (every fill with fees and funding) is the record for this.

## Phase 0: Before any code
| Item | Who | Why |
|---|---|---|
| Read Hyperliquid's current Terms of Use (no VPN) | Owner | Confirm Brazil is allowed |
| Create a Hyperliquid account, deposit 300$ USDC | Owner | The live wallet |
| Create an **API / agent key** in Hyperliquid's settings | Owner | It can trade but **can't withdraw**, so a leaked key cannot take the money out |
| Store the key as an environment variable on the bot PC | Owner | Claude never sees, types or handles the key |
| Brazilian lawyer (crypto / CVM) | Owner | Required before Phase 5 charges anyone; can run in parallel with Phases 1–4 |

The testnet faucet may require an existing mainnet account, so the account is needed even for the test step.

## Phase 1: Live trading engine (personal use)
Every trade already goes through one function, `broker.market(...)` (`src/copybot/broker.py`), returning a fill.
A **LiveBroker** with the same shape is added, so the RiskGate, ledger, PositionManager and Discord cards keep working.

1. **Testnet first.** The live wallet runs on Hyperliquid's test network (fake money) for 3+ days before mainnet.
2. **LiveBroker.** Real orders that fill at once or are cancelled (IOC), with a price limit so a thin book cannot give
   a terrible fill. The ledger records the intent **before** sending, so a crash mid-order is caught at restart (the
   existing `intent` / `state.uncertain` mechanism). Partial fills are recorded as what actually filled.
3. **Stops held by the exchange.** The 8% stop is also placed **on Hyperliquid** as a trigger order, so it fires when
   the bot, the PC or the internet is down. This matters most while the bot runs on a home PC.
4. **Checking against the real account.** Every minute the ledger is compared with the real Hyperliquid positions and
   open orders. A mismatch stops new entries and alerts on Discord.
5. **Live wallet limits** (its own, separate from the paper ceilings in `risk.toml`):
   - balance used: 300$
   - daily stop: −15% net for the day (see the owner decisions above), resets at local midnight
   - total stop: wallet at or below 150$ (balance + open P&L) → no new entries, Discord alert, until `/liveresume`;
     open copies keep their stops and the traders' exits
   - a maximum size per trade
6. **Kill switch.** `/livekill <PIN>` closes everything and stops entries. `/livepause` and `/liveresume`.
7. **One wallet live.** Only 5% margin · 8% stop goes live; every paper wallet keeps running. A new card shows
   **live vs paper** side by side (slippage, delay, fees, missed calls).
8. **Traders.** The live wallet copies the same followed Invo traders as the paper wallets. The owner picks them from
   the daily Claude report and changes them with `/invofollow` / `/invounfollow`; the live wallet follows that.
9. **New safety rules, enforced by `tests/test_safety.py`:**
   - signing is allowed in **one file only**
   - the key comes only from an environment variable and is never logged (`log.py` redacts it)
   - no withdraw or transfer function exists anywhere in the code
   - live mode needs an explicit `live = true` setting **and** the key; otherwise it stays paper
   - CLAUDE.md and context.md are updated to the new rules in the same change

**Done when:** 3+ days on testnet with no mismatch, then mainnet with the 300$.

**Status (2026-11-03):** the code is built on branch `live/phase1` (`src/copybot/live.py`, `config/live.toml`, README
"Live trading") and tested against a fake exchange that checks every signature. Not yet run on the real testnet:
waiting for the owner's API wallet.

### Home PC risks (until Phase 2)
- Power cut, Windows update or internet drop: open trades stay protected only by the exchange-side stops (step 3).
- Windows sleep must be off, and the restart loop must start after a reboot.
- Two instances on one wallet must never run (the existing `data/bot.lock`).

## Phase 2: Production hosting (when the owner is ready to pay for it)
- A small Linux server (about US$5–10/month), 24/7, with automatic restart on crash or reboot.
- Daily backups of `data/` (the ledger is the source of truth).
- Health alerts on Discord: bot down, stale feeds, Invo login failing, order rejected, mismatch with the real account.
- Deploys: push to GitHub, then one command on the server.
- Moving from the home PC: stop the bot, copy `data/`, start it on the server (never both at once).

## Phase 3: Web dashboard (reachable from anywhere)
- Pages: live balance and chart, open trades, closed trades, traders, paper vs live, the 18-combination table.
- **Read-only at first.** Commands stay on Discord until the login is proven.
- Access through **Cloudflare Tunnel + Cloudflare Access**: no open ports, login by email one-time code. Works from
  the home PC too, so it does not wait for Phase 2.
- It reads the same ledger the bot writes and cannot change trades.

## Phase 4: Public Discord server (read-only for others)
- Public channels where the bot posts trade cards and daily results; the owner chooses what is shown.
- The owner's control channel stays private and owner-only, as today.
- Roles: `public` (read), later `subscriber`; Discord's anti-spam settings on.
- Low legal risk; builds an audience before charging.

## Phase 5: Subscription product (only after the lawyer says how)
A **rebuild of the core**: today the bot is single-user (one set of files and environment variables).

Architecture
- A database (Postgres) instead of files: each user has their own ledger, settings and limits.
- **Users keep their own money.** Each subscriber connects their own Hyperliquid **trade-only agent key**; the
  business never holds or moves anyone's funds.
- Users' keys stored encrypted with a key-management service, decrypted only inside the trading process, never shown
  again. Users can revoke them on Hyperliquid at any time.
- One worker per user, all fed by one shared signal stream (Invo is read once, not once per user).
- Per-user risk settings, inside hard maximums set by the owner.

Account and payments
- Sign-up and login by email with 2FA, plus Discord login to sync roles.
- Payments: Mercado Pago (Pix + card) or Stripe. A webhook gives or removes access; a cancelled subscription stops new
  entries but never removes the stops of open trades.
- Terms of service, risk disclosure, and LGPD (Brazil's data protection law).

Security before launch
- An outside security review, rate limits, audit logs, and a global kill switch for all users.

Two ways to sell it (the lawyer decides which is allowed)
- **A, signals only:** alerts and the dashboard; subscribers trade by hand. Less risk.
- **B, automatic copying:** the full product. May need CVM analyst registration (Ofício-Circular 3/2025/CVM/SIN on
  copy trading) and/or a Central Bank license (BCB Resolutions 519–521 on virtual asset service providers).

## Go / no-go checks
- **Keep growing the live balance** only after 50+ live closed trades, with the live result within about 2 percentage
  points of the paper 5% · 8% wallet.
- **Phase 3 → 5:** 2–3 months of live results the owner would show to paying users, plus the lawyer's answer.
- **Any phase:** a loss limit hit means stop and find out why before resuming.
