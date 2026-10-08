"""Leader selection hysteresis for the Solana book (pure). Independent of the Hyperliquid selection rules, which
evolve on their own.

  join  when rank <= join_rank for `confirm_cycles` consecutive cycles (free slots: any number per cycle)
  drop  when rank >  drop_rank (or not eligible) for `confirm_cycles` cycles AND followed >= min_follow_hours,
        or when the leader is paused (bad streak)
  at most ONE drop (with its replacement) per cycle; a dropped leader cannot rejoin during the cooldown.
"""
from __future__ import annotations

from dataclasses import dataclass

from copybot.config import Sol

HOUR = 3_600_000
DAY = 86_400_000


@dataclass
class Plan:
    joins: list
    drops: list          # [(address, reason)]
    state: dict


def select(sel: dict, ranking: list[str], followed: dict[str, int], paused: set[str], dropped: dict[str, int],
           now_ms: int, c: Sol) -> Plan:
    streaks = dict(sel.get("streaks", {}))
    rank = {a: i + 1 for i, a in enumerate(ranking)}
    new: dict[str, dict] = {}
    for a in set(streaks) | set(followed) | set(ranking[: c.drop_rank + 5]):
        r = rank.get(a)
        old = streaks.get(a, {"join": 0, "drop": 0})
        j = old["join"] + 1 if r is not None and r <= c.join_rank else 0
        d = old["drop"] + 1 if r is None or r > c.drop_rank else 0
        if j or d or a in followed:
            new[a] = {"join": j, "drop": d}
    cands = []
    for a, since in followed.items():
        if a in paused:
            cands.append((0, -rank.get(a, 10**6), a, "paused after a bad streak"))
        elif new.get(a, {}).get("drop", 0) >= c.confirm_cycles and now_ms - since >= c.min_follow_hours * HOUR:
            cands.append((1, -rank.get(a, 10**6), a, f"rank > {c.drop_rank} for {c.confirm_cycles} cycles"))
    cands.sort()
    cooldown = c.dropped_cooldown_days * DAY
    joinable = [a for a in ranking if a not in followed and new.get(a, {}).get("join", 0) >= c.confirm_cycles
                and now_ms - dropped.get(a, -10**15) >= cooldown]
    joins = joinable[:max(0, c.max_leaders - len(followed))]
    rest = joinable[len(joins):]
    drops: list[tuple[str, str]] = []
    if cands:
        _, _, a, why = cands[0]
        drops.append((a, why))
        if rest:
            joins.append(rest[0])
    return Plan(joins, drops, {"streaks": new, "cycles": int(sel.get("cycles", 0)) + 1, "at": now_ms})


def rebalance(sel: dict, ranking: list[str], followed: dict[str, int], paused: set[str], dropped: dict[str, int],
              now_ms: int, c: Sol) -> Plan:
    """/fomosearch (and the very first pick): follow the best `max_leaders` of the ranking AT ONCE, without the
    confirmation cycles, like /hypersearch. Followed wallets outside that top are dropped (their open copies are still
    managed until they exit). Paused wallets and those in their drop cooldown are skipped. An empty ranking changes
    nothing (a failed search never drops anyone)."""
    if not ranking:
        return Plan([], [], {**sel, "at": now_ms})
    cooldown = c.dropped_cooldown_days * DAY
    top = [a for a in ranking if a not in paused and (a in followed or now_ms - dropped.get(a, -10**15) >= cooldown)]
    top = top[: c.max_leaders]
    drops = [(a, f"not in the best {c.max_leaders} of the new search") for a in followed if a not in top]
    joins = [a for a in top if a not in followed]
    streaks = {a: {"join": c.confirm_cycles, "drop": 0} for a in top}
    return Plan(joins, drops, {"streaks": streaks, "cycles": int(sel.get("cycles", 0)) + 1, "at": now_ms})
