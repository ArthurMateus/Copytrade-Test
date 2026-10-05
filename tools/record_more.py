"""Second pass: re-record the samples that hit HTTP 429 in the first pass (slowly)."""
import json, time
from record_samples import post, save

lb = json.load(open("../tests/fixtures/leaderboard_sample.json"))["leaderboardRows"]
st, book = post({"type": "l2Book", "coin": "BTC"}); print(st); save("l2book_btc.json", book)
st, meta = post({"type": "metaAndAssetCtxs"}); print(st); save("meta_and_asset_ctxs.json", meta)
now = int(time.time() * 1000)
st, c = post({"type": "candleSnapshot", "req": {"coin": "#140", "interval": "1h", "startTime": now - 86400_000, "endTime": now}})
save("candle_snapshot_hash_coin_error.json", {"status": st, "body": c}); print(st)
for r in lb[150:]:
    time.sleep(2)
    st, fills = post({"type": "userFillsByTime", "user": r["ethAddress"], "startTime": now - 120 * 86400_000})
    print(st, len(fills) if isinstance(fills, list) else fills)
    if st == 200 and isinstance(fills, list) and 100 < len(fills) < 2000:
        save("user_fills_by_time.json", {"user": r["ethAddress"], "fills": fills})
        st, chs = post({"type": "clearinghouseState", "user": r["ethAddress"]}); save("clearinghouse_state.json", chs)
        break
st, f = post({"type": "userFunding", "user": r["ethAddress"], "startTime": now - 3 * 86400_000}); print(st)
save("user_funding.json", f if isinstance(f, list) else {"status": st, "body": f})
