"""Record REAL Hyperliquid responses into tests/fixtures (run once, before writing parsers).

Read-only public endpoints only. No keys, no signing.
Usage: uv run python tools/record_samples.py
"""
import asyncio
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

INFO = "https://api.hyperliquid.xyz/info"
LB = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def post(body):
    req = urllib.request.Request(INFO, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, indent=1), encoding="utf-8")
    print("saved", name)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(LB, timeout=120) as r:
        lb = json.loads(r.read())
    rows = lb["leaderboardRows"]
    print("leaderboard rows", len(rows))
    # keep a slice: first 150 rows + 150 rows of active mid-size wallets
    def wp(row, w):
        return dict(row["windowPerformances"]).get(w, {})
    active = [r for r in rows if 20_000 < float(r["accountValue"]) < 2_000_000
              and float(wp(r, "month").get("vlm", 0)) > 5 * float(r["accountValue"])
              and float(wp(r, "month").get("pnl", 0)) > 0]
    save("leaderboard_sample.json", {"leaderboardRows": rows[:150] + active[:150], "_total_rows": len(rows)})

    # pick a wallet with some but not crazy activity for fills sample
    picked = None
    for r in active[:60]:
        st, fills = post({"type": "userFillsByTime", "user": r["ethAddress"], "startTime": int(time.time() * 1000) - 90 * 86400_000})
        if st == 200 and isinstance(fills, list) and 50 < len(fills) < 2000:
            picked = r["ethAddress"]
            save("user_fills_by_time.json", {"user": picked, "fills": fills})
            break
    # a full page (2000) sample too, if found
    for r in active[60:150]:
        st, fills = post({"type": "userFillsByTime", "user": r["ethAddress"], "startTime": int(time.time() * 1000) - 30 * 86400_000})
        if st == 200 and isinstance(fills, list) and len(fills) == 2000:
            save("user_fills_full_page.json", {"user": r["ethAddress"], "fills": fills})
            break
    st, chs = post({"type": "clearinghouseState", "user": picked})
    save("clearinghouse_state.json", chs)
    st, book = post({"type": "l2Book", "coin": "BTC"})
    save("l2book_btc.json", book)
    now = int(time.time() * 1000)
    st, c = post({"type": "candleSnapshot", "req": {"coin": "ETH", "interval": "1h", "startTime": now - 3 * 86400_000, "endTime": now}})
    save("candle_snapshot_eth_1h.json", c)
    st, c = post({"type": "candleSnapshot", "req": {"coin": "#140", "interval": "1h", "startTime": now - 86400_000, "endTime": now}})
    save("candle_snapshot_hash_coin_error.json", {"status": st, "body": c})
    st, meta = post({"type": "metaAndAssetCtxs"})
    save("meta_and_asset_ctxs.json", meta)
    st, mids = post({"type": "allMids"})
    save("all_mids.json", {k: v for k, v in list(mids.items())})

    # websocket: userFills for the most active wallet we can find + trades fallback; record messages
    asyncio.run(record_ws([r["ethAddress"] for r in active[:40]]))


async def record_ws(users):
    msgs = []
    async with websockets.connect("wss://api.hyperliquid.xyz/ws", max_size=None) as ws:
        for u in users:
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": u}}))
        await ws.send(json.dumps({"method": "ping"}))
        deadline = time.time() + 240
        live = 0
        while time.time() < deadline and live < 3:
            try:
                raw = await asyncio.wait_for(ws.recv(), 30)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"method": "ping"}))
                continue
            m = json.loads(raw)
            if m.get("channel") == "userFills":
                d = m["data"]
                if d.get("isSnapshot"):
                    if len([x for x in msgs if x.get("channel") == "userFills" and x["data"].get("isSnapshot")]) < 1:
                        d = dict(d, fills=d["fills"][:20])
                        msgs.append(dict(m, data=d))
                    continue
                live += 1
            if len(msgs) < 60:
                msgs.append(m)
    save("ws_messages.json", msgs)


if __name__ == "__main__":
    main()
