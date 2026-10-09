"""Command families (/hyper* for Hyperliquid, /fomo* for the Solana book), the long/short breakdown, the FOMO cards
and the two resets. The real SolBot and real ledgers; only the chat is a recorder."""
import time
from dataclasses import asdict

import pytest

from copybot import config, tgfmt
from copybot.ledger import Ledger, Position, State, now_ms
from copybot.sol import fmt
from copybot.sol.runner import SolBot
from copybot.tg import ALIASES, COMMANDS, FOMO_COMMANDS, HYPER_COMMANDS, ONCE, PIN_COMMANDS, SLASH_COMMANDS, Command, canon

PIN = "2468"
A, B = "LeaderAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1", "LeaderBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"
HL_HANDLED = {"/status", "/trades", "/traders", "/wallets", "/positions", "/leaders", "/progress", "/search", "/pause",
              "/resume", "/flatten", "/reset", "/restart", "/help", "/add", "/invo", "/invotrades", "/invotraders", "/invofollow", "/invounfollow"}
FOMO_HANDLED = set(FOMO_COMMANDS)


# ---- names ------------------------------------------------------------------------------------------------------
def test_every_command_name_resolves_to_something_the_bot_handles():
    for name in COMMANDS:
        assert canon(name) in HL_HANDLED | FOMO_HANDLED, name
    for a, target in ALIASES.items():
        assert a in COMMANDS and target in COMMANDS and a != target
    assert canon("/hyperwallet") == canon("/hyperwallets") == "/wallets"
    assert canon("/hyperreset") == "/reset" and canon("/fomostatus") == "/fomo" and canon("/fomo") == "/fomo"


def test_the_two_families_are_complete_and_symmetrical():
    wanted = {"status", "trades", "traders", "wallet", "positions", "leaders", "progress", "search", "pause", "resume",
              "flatten", "reset", "add"}
    assert {c[len("/hyper"):] for c in HYPER_COMMANDS} == wanted
    assert {"/fomotrades", "/fomotraders", "/fomowallet", "/fomosearch", "/fomoreset", "/fomoflatten",
            "/fomopause", "/fomoresume", "/fomoprogress", "/fomopositions", "/fomoleaders", "/fomo",
            "/fomoadd", "/fomofollow", "/fomounfollow"} == set(FOMO_COMMANDS)
    assert len(SLASH_COMMANDS) < 100 and len(set(SLASH_COMMANDS)) == len(SLASH_COMMANDS)
    assert all(len(c) - 1 <= 32 and c[1:].islower() for c in SLASH_COMMANDS)          # Discord's name rules


def test_dangerous_commands_need_the_pin_and_are_never_replayed_after_a_restart():
    for c in ("/hyperflatten", "/hyperreset", "/fomoflatten", "/fomoreset"):
        assert canon(c) in PIN_COMMANDS and canon(c) in ONCE
    assert "/restart" in ONCE


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
    text = tgfmt.progress_text(closed_state([1, 1, -1, 1, -1, 1, 1, -1], [-1]), {}, None, now_ms())
    assert "Longs: <b>5 closed" in text and "Shorts: <b>3 closed" in text and "1 open" in text
    assert "Every copy so far was a long" not in text


def test_progress_warns_when_every_copy_so_far_was_a_long():
    text = tgfmt.progress_text(closed_state([1] * 9), {}, None, now_ms())
    assert "Shorts: <b>0 closed" in text and "Every copy so far was a long" in text
    assert "Every copy so far was a long" not in tgfmt.progress_text(closed_state([1] * 3), {}, None, now_ms())  # too few
    assert "Every copy so far was a long" not in tgfmt.progress_text(closed_state([1] * 9, [-1]), {}, None, now_ms())


# ---- the FOMO book: cards and resets ----------------------------------------------------------------------------------
class Chat:
    """Records what the bot says (the real Telegram/Discord UIs are covered elsewhere)."""

    def __init__(self):
        self.sent, self.cards = [], {}

    def send(self, t):
        self.sent.append(t)

    def set_card(self, key, text, new=False):
        self.cards[key] = text

    def final_card(self, key, text):
        self.cards[key] = text

    def check_pin(self, given):
        return given == PIN


@pytest.fixture
def sol(tmp_path):
    cfg = config.load("config", env={})
    cfg.runtime.data_dir = str(tmp_path / "data")
    chat, restarts = Chat(), []
    bot = SolBot(cfg, chat, lambda *a, **k: None, restart=lambda: restarts.append(1))
    bot.rec({"ev": "genesis", "equity0": 300.0, "btc_px0": 0.0})
    bot.rec({"ev": "mark", "kind": "day", "key": "d", "equity": 300.0})
    bot.rec({"ev": "mark", "kind": "week", "key": "w", "equity": 300.0})
    bot.rec({"ev": "follow", "leader": A, "ts": 111})
    bot.rec({"ev": "follow", "leader": B, "ts": 222})
    bot.rec({"ev": "unfollow", "leader": "Old" * 10, "ts": 5})
    bot.rec({"ev": "cursor", "leader": A, "t": 987_654})
    bot.ranks = {A: 1, B: 2}
    bot.scores = {A: {"score": 0.44, "trades": 90, "win_rate": 0.6, "profit_factor": 3.1, "median_hold_s": 1200,
                      "open_buy_share": 0.1, "pnl_7d": 500.0, "pnl_24h": 40.0}}
    yield bot, chat, restarts
    bot.ledger.close()


def open_pos(bot, token="Tok1", sym="MEME", leader=A, px=0.01, size=1000.0):
    p = Position(pos_id=f"{sym}-1", coin=token, side=1, size=size, entry_px=px, stop_px=px * 0.7, leverage=1.0,
                 leader=leader, k=0.001, open_oid=0, opened_ms=now_ms() - 600_000, sym=sym, open_lag_ms=2500)
    bot.rec({"ev": "open", "pos": asdict(p), "fee": 0.1, "lag_ms": 2500})
    return p


def test_fomo_cards_show_the_book_the_trades_the_traders_and_the_wallet(sol):
    bot, chat, _ = sol
    open_pos(bot)
    bot.prices.q["Tok1"] = type("Q", (), {"px": 0.012, "liq_usd": 5e5, "symbol": "MEME", "ts": time.time()})()
    for name, key, needles in [
            ("/fomo", "sol:status", ["FOMO (paper)", "copying", "Wallet", "Solana data"]),
            ("/fomotrades", "sol:trades", ["FOMO trades", "1 open", "MEME", "⬆️ BUY", "Entry → now", "0.01 → 0.012",
                                           "Sells when", tgfmt.short(A), "if hit", "If every stop hits"]),
            ("/fomotraders", "sol:traders", ["FOMO traders", tgfmt.short(A), "Made for you", "Their record", "90 trades",
                                             "Typical hold", "This week / today", "Following for"]),
            ("/fomowallet", "sol:wallet", ["FOMO wallet", "Cash", "In open trades", "Fees paid", "Today", "This week",
                                           "stops new copies at −5%", "max 8"]),
            ("/fomoleaders", "sol:leaders", ["FOMO leaders", tgfmt.short(A), "#1"])]:
        bot.command(Command(name))
        assert key in chat.cards and key in bot.live_cards, name
        for n in needles:
            assert n in chat.cards[key], (name, n, chat.cards[key])
    bot.command(Command("/fomopositions"))
    assert "MEME" in chat.sent[-1] and "stop" in chat.sent[-1]
    bot.command(Command("/fomoprogress"))
    assert "FOMO progress" in chat.sent[-1] and "every trade is a long" in chat.sent[-1]


def test_fomo_wallet_cash_plus_invested_is_the_wallet(sol):
    bot, chat, _ = sol
    open_pos(bot, size=1000.0, px=0.01)
    bot.command(Command("/fomowallet"))
    text = chat.cards["sol:wallet"]
    eq = bot.st.equity({"Tok1": 0.01})
    assert f"Wallet now: <b>{eq:,.2f}$" in text and "In open trades: <b>10.00$" in text
    assert f"Cash: <b>{eq - 10.0:,.2f}$" in text


def test_fomo_pause_resume_flatten_and_the_pin(sol):
    bot, chat, _ = sol
    open_pos(bot)
    bot.command(Command("/fomopause"))
    assert bot.st.entries_paused
    bot.command(Command("/fomoresume"))
    assert not bot.st.entries_paused
    bot.command(Command("/fomoflatten", "wrong"))
    assert "Wrong or missing PIN" in chat.sent[-1] and bot.st.positions
    bot.command(Command("/fomoflatten", PIN))
    assert not bot.st.positions and bot.st.entries_paused and bot.st.closed[-1]["reason"] == "flatten"


def test_fomosearch_asks_the_scorer_even_while_live_data_is_refused(sol):
    bot, chat, _ = sol
    bot.auth_ok = False                   # discovery and history use the public endpoint, not the refused key
    bot.command(Command("/fomosearch"))
    assert bot.scorer.search_req.is_set() and bot.search_pending and "on-chain" in chat.sent[-1]


def test_a_search_follows_the_best_at_once_and_reports(sol):
    bot, chat, _ = sol
    bot.rec({"ev": "sel", "state": {"at": now_ms(), "streaks": {}}})      # a cycle ran a moment ago: normally skipped
    ranking = ["Wallet9", A]
    scores = {"Wallet9": {"eligible": True, "score": 0.5}, A: {"eligible": True, "score": 0.4}}
    bot.c.min_scored_to_start = 1
    bot.on_ranking(ranking, 2, scores)
    assert "Wallet9" not in bot.st.followed                               # hysteresis: not yet (and too soon)
    bot.search_pending = True
    bot.on_review(2, 2, 2, ranking, scores)                               # the search ends
    assert not bot.search_pending
    assert "Wallet9" in bot.st.followed and A in bot.st.followed          # the best, at once, no confirmation cycles
    assert B not in bot.st.followed                                       # followed but not found again: dropped
    text = " ".join(chat.sent)
    assert "Following" in text and "Dropped" in text
    assert "FOMO search done" in chat.sent[-1] and "+1 new" in chat.sent[-1] and "50 pts" in chat.sent[-1]


def test_an_empty_search_drops_nobody(sol):
    bot, chat, _ = sol
    bot.search_pending = True
    bot.on_review(5, 5, 0, [], {})
    assert set(bot.st.followed) == {A, B} and "Nobody passed" in chat.sent[-1]


def test_fomosearch_while_a_search_runs_reports_progress_instead_of_starting_another(sol):
    bot, chat, _ = sol
    bot.scorer.progress = {**bot.scorer.progress, "phase": "scoring", "done": 12, "todo": 200, "eligible": 1,
                           "started": now_ms() - 3_600_000}
    bot.command(Command("/fomosearch"))
    assert not bot.scorer.search_req.is_set() and bot.search_pending
    assert "already running" in chat.sent[-1] and "12/200" in chat.sent[-1]
    bot.command(Command("/fomoleaders"))
    assert "12/200 FOMO traders" in chat.cards["sol:leaders"]


def test_fomoreset_refuses_without_pin_or_with_open_trades(sol):
    bot, chat, restarts = sol
    bot.command(Command("/fomoreset", "wrong"))
    assert "Wrong or missing PIN" in chat.sent[-1]
    open_pos(bot)
    bot.command(Command("/fomoreset", PIN))
    assert "reset refused" in chat.sent[-1] and "1 open trade" in chat.sent[-1] and not restarts
    assert bot.st.positions and (bot.data / "ledger.jsonl").exists() and not (bot.data / "archive").exists()


def test_fomoreset_archives_the_old_history_and_starts_clean_but_keeps_the_traders(sol):
    bot, chat, restarts = sol
    open_pos(bot)
    bot.command(Command("/fomoflatten", PIN))
    bot.command(Command("/fomoresume"))
    assert bot.st.closed and bot.st.realized != 0
    old_lines = (bot.data / "ledger.jsonl").read_bytes()
    bot.command(Command("/fomoreset", PIN))
    assert restarts == [1] and "FOMO reset done" in chat.sent[-1] and "Restarting" in chat.sent[-1]
    arch = next((bot.data / "archive").iterdir())
    assert (arch / "ledger.jsonl").read_bytes() == old_lines                   # nothing deleted
    assert bot.ledger.frozen
    bot.rec({"ev": "note", "text": "a late write between the reset and the restart"})   # must not reach any file
    assert "late write" not in (bot.data / "ledger.jsonl").read_text(encoding="utf-8")
    assert "late write" not in (arch / "ledger.jsonl").read_text(encoding="utf-8")
    fresh = Ledger(bot.data / "ledger.jsonl").replay()                         # what the restarted bot will load
    assert fresh.equity0 == 300.0 and fresh.realized == 0 and not fresh.positions and not fresh.closed
    assert fresh.followed == {A: 111, B: 222} and "Old" * 10 in fresh.dropped
    assert fresh.cursors == {A: 987_654}                                       # old swaps are never copied again
    assert not fresh.uncertain and not fresh.entries_paused


def test_hyperliquid_reset_and_fomo_reset_are_independent(sol, tmp_path):
    """The Hyperliquid /reset archives data/ledger.jsonl and data/wallets; the FOMO one only data/sol."""
    bot, chat, restarts = sol
    hl_ledger = bot.data.parent / "ledger.jsonl"
    hl_ledger.write_text('{"seq":1,"ev":"genesis","equity0":300,"ts":1}\n', encoding="utf-8")
    bot.command(Command("/fomoreset", PIN))
    assert hl_ledger.exists() and hl_ledger.read_text(encoding="utf-8").startswith('{"seq":1')
    assert (bot.data / "archive").exists() and not (bot.data.parent / "archive").exists()


def test_fomo_card_helpers_are_safe_with_empty_data():
    st = State()
    st.apply({"ev": "genesis", "equity0": 300.0, "btc_px0": 0.0, "ts": 1})
    cfg = config.Sol()
    assert "No open trades" in fmt.trades_card(st, {}, now_ms())
    assert "None followed yet" in fmt.traders_card(st, {}, {}, {}, {}, now_ms())
    assert "None followed yet" in fmt.leaders_card(st, {}, {}, now_ms())
    assert "FOMO wallet" in fmt.wallet_card(st, {}, cfg, now_ms())
    assert "No open FOMO trades" in fmt.positions_text(st, {})


def test_a_partial_ranking_during_a_search_does_not_say_nobody_passed(sol):
    bot, chat, _ = sol
    bot.c.min_scored_to_start = 1
    bot.scorer.progress = {**bot.scorer.progress, "phase": "scoring", "done": 10, "todo": 200}
    bot.on_ranking([], 10, {"W": {"eligible": False}})
    assert not any("passed the strict scoring" in m for m in chat.sent)
    bot.scorer.progress = {**bot.scorer.progress, "phase": "idle"}
    bot.rec({"ev": "sel", "state": {"at": 0, "streaks": {}}})
    bot.on_ranking([], 10, {"W": {"eligible": False}})                    # an hourly cycle after the search: says it
    assert any("passed the strict scoring" in m for m in chat.sent)


def test_search_done_says_why_the_wallets_failed(sol):
    bot, chat, _ = sol
    scores = {"W1": {"eligible": False, "reasons": ["pnl<=0", "win_rate<40%"]},
              "W2": {"eligible": False, "reasons": ["pnl<=0"]},
              "W3": {"eligible": False, "reasons": ["too_busy>2000tx"]}}
    bot.on_review(3, 3, 0, [], scores)
    msg = chat.sent[-1]
    assert "Nobody passed" in msg and "Why they failed (out of 3)" in msg
    assert "lost money (30 d) 2" in msg and "win rate too low 1" in msg and "trades too often to read 1" in msg
    assert "2 missed by just one rule" in msg


def test_fomofollow_is_capped_at_the_maximum_and_picks_survive_a_reset(sol):
    bot, chat, _ = sol
    w = "PickWa" + "7" * 38
    bot.command(Command("/fomofollow", w))
    assert w in bot.st.followed and w in bot.st.picked and bot.seed_q.get_nowait() == w
    for i in range(bot.c.max_leaders - len(bot.st.followed)):
        bot.rec({"ev": "follow", "leader": f"Fill{i}" + "8" * 30})
    bot.command(Command("/fomofollow", "Xther" + "9" * 39))
    assert "Already following" in chat.sent[-1] and ("Xther" + "9" * 39) not in bot.st.followed
    bot.reset_book()
    st = Ledger(bot.data / "ledger.jsonl").replay()
    assert w in st.followed and w in st.picked


def test_picks_are_never_ranked_away_but_a_paused_pick_is_dropped():
    from copybot.config import Sol
    from copybot.sol.hysteresis import rebalance, select
    c = Sol()
    followed = {"P": 0, "X": 0}
    p = select({"streaks": {"P": {"join": 0, "drop": 5}, "X": {"join": 0, "drop": 5}}}, ["A"], followed, set(), {},
               10**13, c, keep={"P"})
    assert [a for a, _ in p.drops] == ["X"]
    p = rebalance({}, ["A", "B"], followed, set(), {}, 10**13, c, keep={"P"})
    assert "P" not in [a for a, _ in p.drops] and set(p.joins) == {"A", "B"}
    p = select({}, ["A"], followed, {"P"}, {}, 10**13, c, keep={"P"})
    assert p.drops and p.drops[0][0] == "P"
