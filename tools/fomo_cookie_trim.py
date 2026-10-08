"""Trim a too-big FOMO cookie (HTTP 431) down to the cookies the login really needs.

    uv run python tools/fomo_cookie_trim.py [path]        (default: secrets/fomo.cookie, or $FOMO_COOKIE_FILE)

It reads the pasted cookie, drops duplicates and obvious tracking cookies, then asks FOMO's leaderboard which
cookies are needed (smallest set that still gets HTTP 200). The old file is kept as <file>.full and the trimmed
cookie replaces it. Only cookie NAMES and SIZES are printed, never values.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from copybot.sol.fomo import read_cookie_file

URL = "https://prod-api.fomo.family/v2/leaderboard/24h"
MAX_HEADER = 7000          # stay far below the server's limit (431)
TRACKING = re.compile(r"^(_ga|_gid|_gat|_gcl|_fbp|_fbc|_dd|_hj|ph_|amplitude|mp_|intercom|__stripe|_clck|_clsk|"
                      r"datadog|_scid|_tt|_uet|_pin|ajs_|__hs|hubspot|_rdt)", re.I)


def parse(text: str) -> dict[str, str]:
    """Accepts 'a=b; c=d', a 'cookie: ...' line, or several lines; later duplicates win."""
    text = text.strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = [re.sub(r"^cookie:\s*", "", l, flags=re.I) for l in lines]
    out: dict[str, str] = {}
    for part in "; ".join(lines).split(";"):
        name, eq, val = part.strip().partition("=")
        if eq and name and re.fullmatch(r"[A-Za-z0-9_.\-%:]+", name):
            out[name] = val.strip()
    return out


def header(c: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in c.items())


def status(cookie: str) -> int:
    if len(cookie) > MAX_HEADER:
        return 431
    req = urllib.request.Request(URL, headers={"Cookie": cookie, "Accept": "application/json",
                                               "Origin": "https://fomo.family", "Referer": "https://fomo.family/",
                                               "User-Agent": "Mozilla/5.0 (copybot paper)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:      # network problem: say so, do not guess
        print(f"network error: {type(e).__name__}")
        return 0


def minimal(cookies: dict[str, str]) -> dict[str, str] | None:
    # Cloudflare cookies (__cf_bm, cf_clearance) are kept unless proven unnecessary below: a login can need them.
    if len(header(cookies)) <= MAX_HEADER:
        code = status(header(cookies))
        print(f"the cookie as pasted ({len(header(cookies))} bytes) answers HTTP {code}")
        if code != 200:
            print("it is probably EXPIRED (the login token lives only 1 hour): sign in again, copy the cookie "
                  "again right away, and rerun this")
            return None
        keep = dict(cookies)
        for k in sorted(keep, key=lambda k: -len(keep[k])):        # drop whatever is not needed, biggest first
            trial = {n: v for n, v in keep.items() if n != k}
            if trial and status(header(trial)) == 200:
                keep = trial
        return keep
    keep = {k: v for k, v in cookies.items() if not TRACKING.match(k)}
    print(f"{len(cookies)} cookies, {len(keep)} after dropping tracking ones, {len(header(keep))} bytes")
    if len(header(keep)) > MAX_HEADER:
        # too big: start from the likely login cookies, then add the others smallest first
        core = {k: v for k, v in keep.items() if re.search(r"privy|token|session|auth|jwt|sid|refresh", k, re.I)}
        rest = sorted((k for k in keep if k not in core), key=lambda k: len(keep[k]))
        cur = dict(core)
        for k in [None, *rest]:
            if k:
                if len(header({**cur, k: keep[k]})) > MAX_HEADER:
                    continue
                cur[k] = keep[k]
            if status(header(cur)) == 200:
                keep = cur
                break
        else:
            return None
    code = status(header(keep))
    if code != 200:
        print(f"even the filtered cookie answers HTTP {code}: it is probably expired or incomplete")
        return None
    for k in sorted(keep, key=lambda k: -len(keep[k])):       # drop whatever is not needed, biggest first
        trial = {n: v for n, v in keep.items() if n != k}
        if trial and status(header(trial)) == 200:
            keep = trial
    return keep


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FOMO_COOKIE_FILE") or "secrets/fomo.cookie")
    if not path.exists():
        print(f"file not found: {path}")
        return 2
    cookies = parse(read_cookie_file(str(path)))
    if not cookies:
        print("no cookies found in the file: paste the value of the 'cookie:' request header")
        return 2
    print("cookies in the file (name: size):")
    for k, v in sorted(cookies.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k}: {len(v)}")
    best = minimal(cookies)
    if not best:
        print("could not find a working cookie. Sign in again, copy the cookie again, and rerun this.")
        return 1
    backup = path.with_name(path.name + ".full")
    backup.write_text(read_cookie_file(str(path)) + chr(10), encoding="utf-8")
    path.write_text(header(best) + "\n", encoding="utf-8")
    print(f"OK: kept {len(best)} cookie(s): {', '.join(best)} ({len(header(best))} bytes). Old file: {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
