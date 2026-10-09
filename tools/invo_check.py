"""Check the bot's Invo login before starting the bot.

    uv run python tools/invo_check.py [username]

Reads the refresh token from INVO_TOKEN_FILE (or secrets/invo.token), logs in (which saves the NEW token back into
the same file), looks up a trader (default: nicush) and prints their portfolios. Never prints a token.
"""
import os
import sys

from copybot import config
from copybot.invo import InvoAuthError, InvoClient, InvoError


def main() -> int:
    path = os.environ.get("INVO_TOKEN_FILE") or "secrets/invo.token"
    if not os.path.exists(path):
        print(f"token file not found: {path}  (save the token there, or set INVO_TOKEN_FILE)")
        return 2
    who = (sys.argv[1] if len(sys.argv) > 1 else "nicush").lstrip("@")
    c = InvoClient(config.load("config", env={}).invo.api_base, path)
    try:
        found = c.user(who)
    except InvoAuthError as e:
        print(f"Invo refused the login: {e}. Copy a fresh token (the console step) and try again.")
        return 1
    except InvoError as e:
        print(f"Invo did not answer properly: {e}")
        return 1
    print("login OK (the token file now holds a new token, as expected)")
    if not found:
        print(f"no Invo user called @{who}")
        return 0
    for p in c.portfolios(found[0]):
        print(f"  {p.title:<30} {p.kind:<6} win {p.win_rate:5.1f}%  closed {p.closed:>5}  open {p.open_count}"
              + (f" ({', '.join(p.open_assets)})" if p.open_assets else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
