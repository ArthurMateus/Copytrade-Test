"""Command names (only the Invo book since 2026-10-26: short names on Discord, the /invo… names still accepted) and the
long/short breakdown of /progress."""
from dataclasses import asdict

from copybot import cardfmt
from copybot.chat import (ALIASES, COMMANDS, INVO_COMMANDS, PIN_COMMANDS, SLASH_COMMANDS, VERBS, WALLET_COMMANDS,
                          canon, short_names)
from copybot.ledger import Position, State, now_ms

# the canonical names the bot handles (Bot.command)
HANDLED = set(INVO_COMMANDS) | {"/restart", "/help"}


# ---- names ------------------------------------------------------------------------------------------------------
def test_every_command_name_resolves_to_something_the_bot_handles():
    for name in COMMANDS:
        assert canon(name) in HANDLED, name
    for a, target in ALIASES.items():
        assert a in COMMANDS and target in COMMANDS and a != target
    assert canon("/status") == canon("/invostatus") == canon("/invo") == "/invostatus"     # older names still work
    assert canon("/positions") == "/invotrades" and canon("/block") == "/invoblock"


def test_discord_lists_the_short_names_only():
    """Owner request 2026-10-26: only Invo, so no book prefix (and no Hyperliquid, FOMO, search or arena commands)."""
    assert SLASH_COMMANDS == ("/help", "/restart") + tuple(f"/{v}" for v in VERBS)
    assert not any(c.startswith(("/hyper", "/fomo", "/invo")) for c in SLASH_COMMANDS)
    assert not {"/search", "/add", "/picks", "/arena"} & set(COMMANDS)
    assert all(len(c) - 1 <= 32 and c[1:].islower() for c in SLASH_COMMANDS)          # Discord's name rules
    assert set(PIN_COMMANDS) == {canon("/flatten"), canon("/reset")}
    assert set(WALLET_COMMANDS) == {canon(f"/{v}") for v in ("follow", "unfollow", "block", "unblock")}


def test_messages_show_the_short_names():
    assert short_names("/invofollow x · /invowallets · /invoblock a b") == "/follow x · /wallets · /block a b"


# ---- long / short ----------------------------------------------------------------------------------------------------
def closed_state(sides: list[int], open_sides: list[int] = ()) -> State:
    st = State()
    st.apply({"ev": "genesis", "equity0": 300.0, "btc_px0": 100_000.0, "ts": 1})
    for i, side in enumerate(sides):
        p = Position(pos_id=f"p{i}", coin=f"C{i}", side=side, size=1.0, entry_px=100.0,
                     stop_px=97.0 if side > 0 else 103.0, leverage=5, leader="L", k=1.0, open_oid=1, opened_ms=1)
        st.apply({"ev": "open", "pos": asdict(p), "fee": 0.0, "ts": 1})
        st.apply({"ev": "close", "coin": p.coin, "pos_id": p.pos_id, "px": 101.0 if side > 0 else 99.0, "fee": 0.0,
                  "reason": "leader_close", "ts": 2})
    for j, side in enumerate(open_sides):
        p = Position(pos_id=f"o{j}", coin=f"O{j}", side=side, size=1.0, entry_px=100.0,
                     stop_px=97.0 if side > 0 else 103.0, leverage=5, leader="L", k=1.0, open_oid=1, opened_ms=1)
        st.apply({"ev": "open", "pos": asdict(p), "fee": 0.0, "ts": 1})
    return st


def test_progress_shows_longs_and_shorts_separately():
    text = cardfmt.progress_text(closed_state([1, 1, -1, 1, -1, 1, 1, -1], [-1]), {}, None, now_ms())
    assert "Longs: <b>5 closed" in text and "Shorts: <b>3 closed" in text and "1 open" in text
    assert "Every copy so far was a long" not in text


def test_progress_warns_when_every_copy_so_far_was_a_long():
    text = cardfmt.progress_text(closed_state([1] * 9), {}, None, now_ms())
    assert "Shorts: <b>0 closed" in text and "Every copy so far was a long" in text
    assert "Every copy so far was a long" not in cardfmt.progress_text(closed_state([1] * 3), {}, None, now_ms())  # too few
    assert "Every copy so far was a long" not in cardfmt.progress_text(closed_state([1] * 9, [-1]), {}, None, now_ms())
