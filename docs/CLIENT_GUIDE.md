# Member guide: copying the signals

Welcome. This server posts the trades of one copy-trading wallet. **Members** see every trade the moment it is made;
the **free** channel sees the same posts 30 minutes later. This guide shows how to copy a signal by hand on
Hyperliquid.

> **Risk.** Leveraged trading can lose more than you expect, quickly. Past results do not promise future ones. Only use
> money you can afford to lose, and never share your wallet's private key or recovery phrase with anyone, including
> us. We never ask for it.

## What a signal looks like

```
🆕 BTC ⬆️ LONG · 5x
Entry: 78,989.9 · Stop: 72,670.7 (−8.0%)
Size: 5.0% of your balance as margin (25% exposure)
```

- **BTC ⬆️ LONG**: buy (a ⬇️ SHORT means sell).
- **5x**: the leverage.
- **Entry**: the price the wallet got. Yours will be a little different.
- **Stop**: where the trade is closed if it goes wrong. Set the same stop.
- **Size**: how much of YOUR balance to put in as margin. With 200$ on the exchange, 5% = 10$ of margin, which at
  5x is a 50$ position.

Later posts about the same coin:

- `↕️ BTC ⬆️: size raised to 7.5% ...` (or `trimmed`): add to, or reduce, your position to that size.
- `✅ WIN · BTC ...` or `❌ LOSS · BTC ...`: the trade is closed. Close yours.
- `📊` each evening: the day's and the overall result. `🗓️` on Sundays (members): the week's report.

## Copying a trade on Hyperliquid

1. Open https://app.hyperliquid.xyz and connect your wallet. Deposit USDC (on the Arbitrum network) if you have not.
2. Pick the coin (e.g. BTC) at the top left.
3. Set the leverage to the signal's (the "Cross / 20x" button above the order form): choose **Isolated** and the
   signal's leverage, e.g. 5x.
4. Choose **Long** or **Short**, then **Market**.
5. Size: margin × leverage. 5% of a 200$ balance at 5x = 10$ × 5 = **50$**. Type 50 (USD) in the size field.
6. Tick **TP/SL**, and in **Stop Loss** type the signal's stop price.
7. Place the order. Check the position and its stop under "Positions" at the bottom.
8. When a ✅/❌ close comes for that coin, click **Close** (Market) on your position.

Hyperliquid's smallest order is 10$. A size under that cannot be copied; skip that signal.

## Questions
Ask in the help channel. We can explain the signals, but we cannot tell you what to do with your own money.
