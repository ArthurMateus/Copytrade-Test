"""Why no (or few) Invo traders pass the strict rules (read-only: data/cache/invo/scores.json).

    uv run python tools/invo_why.py

Prints how many traders were checked, how often each rule failed, how many missed by a single rule (and which), and
the closest misses with their numbers (calls, win rate, profit factor, biggest drop, typical bet, points).
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path


def short(code: str) -> str:
    """'win_rate<45%' -> 'win_rate': the rule without its number, so the counts group."""
    return re.split(r"[<>=]", code, maxsplit=1)[0] or code


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/cache/invo/scores.json")
    if not path.exists():
        print(f"not found: {path} (let the bot run until an Invo search has finished)")
        return 1
    scores = json.loads(path.read_text(encoding="utf-8")).get("scores", {})
    el = [s for s in scores.values() if s.get("eligible")]
    bad = [s for s in scores.values() if not s.get("eligible")]
    print(f"Invo traders checked: {len(scores)}, pass every rule: {len(el)}\n")
    if bad:
        print("Rule failed:")
        for k, v in Counter(short(r) for s in bad for r in dict.fromkeys(s.get("reasons") or [])).most_common():
            print(f"  {k:<34}{v:>6}")
        one = Counter(s["reasons"][0] for s in bad if len(s.get("reasons") or []) == 1)
        if one:
            print("\nMissed by just ONE rule:")
            for k, v in one.most_common():
                print(f"  {k:<34}{v:>6}")
        close = sorted(bad, key=lambda s: (len(s.get("reasons") or []), -sum((s.get("points") or {}).values())))[:12]
        print("\nClosest misses:")
        for s in close:
            pts = sum((s.get("points") or {}).values())
            print(f"  @{s['address']:<18} points {pts:5.1f}  calls {s.get('trades', 0):>4}  "
                  f"win {s.get('win_rate', 0) * 100:3.0f}%  PF {min(s.get('profit_factor', 0), 99):5.2f}  "
                  f"drop {s.get('max_dd', 0) * 100:3.0f}%  bet {s.get('exposure', 0) * 100:4.0f}%  "
                  f"fails: {', '.join(s.get('reasons') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
