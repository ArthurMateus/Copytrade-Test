"""Daily picks (owner request 2026-10-09): every day at `picks.hour` (local time) one message per book with the best
`picks.top_n` traders that pass every rule, compared with the previous day's report (new / still there / gone and
why), each with the command that follows it. Searches no longer follow anyone by themselves (`auto_follow` = false):
the owner decides. /picks sends the same report at any time without replacing the day's baseline.

The report remembers only what it showed (data/picks.json): it is UI memory, not trading state (no ledger event).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copybot.cardfmt import esc


@dataclass
class Entry:
    id: str                 # address / username: what the follow command takes
    name: str               # how it is shown
    score: float            # 0-100
    stats: str              # one line, already plain text
    cmd: str                # e.g. "/hyperadd 0x..."
    followed: bool = False


@dataclass
class Book:
    key: str                # "hyper" | "fomo" | "invo"
    title: str              # "🔷 Hyperliquid"
    entries: list           # best first, already cut to top_n
    status: str = ""        # how the search is doing (checked / running)
    gone_why: dict = field(default_factory=dict)   # id -> why it is no longer in the list (for yesterday's ids)


def local_day(now_s: float, utc_offset_h: float) -> str:
    return (datetime.fromtimestamp(now_s, timezone.utc) + timedelta(hours=utc_offset_h)).strftime("%Y-%m-%d")


def local_hour(now_s: float, utc_offset_h: float) -> float:
    t = datetime.fromtimestamp(now_s, timezone.utc) + timedelta(hours=utc_offset_h)
    return t.hour + t.minute / 60


def due(store: "Store", now_s: float, utc_offset_h: float, hour: float) -> bool:
    return local_hour(now_s, utc_offset_h) >= hour and store.data.get("sent_day") != local_day(now_s, utc_offset_h)


class Store:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def prev(self, book: str) -> dict | None:
        return (self.data.get("books") or {}).get(book)

    def save_day(self, day: str, books: list[Book]) -> None:
        self.data = {"sent_day": day, "books": {b.key: {"day": day, "ids": [e.id for e in b.entries],
                                                        "names": {e.id: e.name for e in b.entries}}
                                                for b in books}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)


def render(b: Book, prev: dict | None, day: str, top_n: int) -> str:
    before = list((prev or {}).get("ids") or [])
    since = (prev or {}).get("day")
    head = f"📋 <b>Daily picks · {b.title}</b> · {day}"
    if b.entries:
        sub = f"The best {len(b.entries)} that pass every rule" + (f" (fewer than {top_n} pass)"
                                                                     if len(b.entries) < top_n else "")
    else:
        sub = "Nobody passes every rule right now"
    lines = [head, sub + (f" · compared with {since}" if since and since != day else "")]
    if b.status:
        lines.append(f"<i>{esc(b.status)}</i>")
    for i, e in enumerate(b.entries, 1):
        tag = "🆕" if prev is not None and e.id not in before else ("✅" if prev is not None else "")
        lines.append("")
        lines.append(f"{i}. {tag + ' ' if tag else ''}<b>{esc(e.name)}</b> · {e.score:.0f}/100"
                     + (" · ⭐ followed" if e.followed else ""))
        lines.append(f"   {esc(e.stats)}")
        if not e.followed:
            lines.append(f"   <code>{esc(e.cmd)}</code>")
    gone = [x for x in before if x not in {e.id for e in b.entries}]
    if gone:
        names = (prev or {}).get("names") or {}
        lines.append("")
        lines.append("Left the list since the last report:")
        for x in gone:
            lines.append(f"❌ {esc(names.get(x, x))} · {esc(b.gone_why.get(x, 'not checked again yet'))}")
    if prev is not None and b.entries:
        n_new = sum(1 for e in b.entries if e.id not in before)
        lines.append("")
        lines.append(f"🆕 {n_new} new · ✅ {len(b.entries) - n_new} still in · ❌ {len(gone)} left")
    return "\n".join(lines)
