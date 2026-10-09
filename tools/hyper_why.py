"""Why no (or few) Hyperliquid wallets are eligible (read-only: data/cache/screened.json and scores.json).

    uv run python tools/hyper_why.py

Prints the first-check (screen) results, then for the fully scored wallets how often each strict rule failed, how
many missed by a single rule (and which), and the closest misses with their numbers.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path


def short(code: str) -> str:
    """'win_rate<60%' -> 'win_rate': the rule, without the number (so the counts group)."""
    return re.split(r"[<>=]", code, maxsplit=1)[0] or code


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/cache")
    screened = json.loads((root / "screened.json").read_text(encoding="utf-8")) if (root / "screened.json").exists() else {}
    scores = json.loads((root / "scores.json").read_text(encoding="utf-8")) if (root / "scores.json").exists() else {}
    if not screened and not scores:
        print(f"nothing in {root} yet (let the bot run until its search has checked some wallets)")
        return 1
    ok = sum(1 for d in screened.values() if d.get("ok"))
    print(f"First check: {len(screened)} wallets, {ok} passed to full scoring\n")
    print("Most common first-check rejections:")
    for k, v in Counter(d.get("reason", "?") for d in screened.values() if not d.get("ok")).most_common(8):
        print(f"  {k:<34}{v:>6}")
    el = [s for s in scores.values() if s.get("eligible")]
    bad = [s for s in scores.values() if not s.get("eligible")]
    print(f"\nFully scored: {len(scores)}, eligible: {len(el)}")
    if bad:
        print("\nStrict rule failed (fully scored wallets):")
        for k, v in Counter(short(r) for s in bad for r in dict.fromkeys(s.get("reasons") or [])).most_common():
            print(f"  {k:<34}{v:>6}")
        one = Counter(s["reasons"][0] for s in bad if len(s.get("reasons") or []) == 1)
        if one:
            print("\nMissed by just ONE rule:")
            for k, v in one.most_common():
                print(f"  {k:<34}{v:>6}")
        close = sorted(bad, key=lambda s: (len(s.get("reasons") or []), -s.get("score", 0)))[:10]
        print("\nClosest misses:")
        for s in close:
            a = s["address"]
            print(f"  {a[:6]}...{a[-4:]}  score {s.get('score', 0):5.1f}  trades {s.get('trades', 0):>4}  "
                  f"win {s.get('win_rate', 0) * 100:3.0f}%  PF {s.get('profit_factor', 0):5.2f}  "
                  f"drop {s.get('max_dd', 0) * 100:3.0f}%  fails: {', '.join(s.get('reasons') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
