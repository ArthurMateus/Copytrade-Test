"""Record a clearinghouseState that actually has open positions."""
import json, time
from record_samples import post, save
lb = json.load(open("../tests/fixtures/leaderboard_sample.json"))["leaderboardRows"]
for r in lb[150:200]:
    st, c = post({"type": "clearinghouseState", "user": r["ethAddress"]})
    if st == 200 and c.get("assetPositions"):
        save("clearinghouse_state.json", c); print(json.dumps(c["assetPositions"][0])); break
    time.sleep(0.5)
