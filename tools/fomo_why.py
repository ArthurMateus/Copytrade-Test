"""Why the scored FOMO traders failed the strict rules (read-only: data/sol/cache/scores.json).

    uv run python tools/fomo_why.py

Prints how many wallets were scored, how many pass, how often each rule failed, how many missed by a single rule
(and which rule), and the closest misses with their numbers.
"""
import json
import sys
from collections import Counter
from pathlib import Path

from copybot.sol.fmt import reject_text


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/sol/cache/scores.json")
    if not path.exists():
        print(f"not found: {path} (run the bot until a FOMO search has scored some wallets)")
        return 1
    scores = json.loads(path.read_text(encoding="utf-8"))
    bad = [s for s in scores.values() if not s.get("eligible")]
    print(f"{len(scores)} FOMO traders scored, {len(scores) - len(bad)} pass the strict rules\n")
    print("Rule failed                         wallets")
    for k, v in Counter(reject_text(r) for s in bad for r in dict.fromkeys(s.get("reasons") or [])).most_common():
        print(f"  {k:<34}{v:>6}")
    one = Counter(reject_text(s["reasons"][0]) for s in bad if len(s.get("reasons") or []) == 1)
    if one:
        print("\nMissed by just ONE rule:")
        for k, v in one.most_common():
            print(f"  {k:<34}{v:>6}")
    close = sorted((s for s in bad if s.get("trades")), key=lambda s: (len(s["reasons"]), -s.get("pnl", 0)))[:10]
    if close:
        print("\nClosest misses:")
        for s in close:
            print(f"  {s['address'][:6]}...{s['address'][-4:]}  trades {s['trades']:>4}  win {s['win_rate'] * 100:3.0f}%  "
                  f"PF {s['profit_factor']:5.2f}  pnl {s['pnl']:>9,.0f}$  fails: "
                  + ", ".join(reject_text(r) for r in s["reasons"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
