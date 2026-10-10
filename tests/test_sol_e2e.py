"""End to end: the real bot with the Solana book enabled (all threads) against loopback fakes of Hyperliquid,
Telegram, a Solana node (JSON-RPC + websocket), DexScreener and Discord. A FOMO trader is discovered through FOMO's
fee payer, scored from its on-chain swaps, followed, copied on a new buy (woken by the websocket), closed on its
sell, and the position survives a restart."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from copybot import config, log, tgfmt
from copybot.runner import Bot
from tests.fakes import FakeDiscord, FakeHL, FakeTelegram
from tests.fakes_sol import KEY, FakeDex, FakeSolana
from tests.test_e2e import PIN, env_for, wait_for, write_config

DAY = 86_400_000
MINT = "MemeMint1111111111111111111111111111111111"
GOOD = "GoodWallet11111111111111111111111111111111"


def history_swaps(sol: FakeSolana, now_ms: int, wallet=GOOD, n=90, days=60, fomo=True) -> None:
    """A FOMO trader that passes every strict rule: 90 closed trades, ~60% wins, 20 min holds, many tokens."""
    pattern = [0.40, 0.40, -0.15, 0.40, -0.15]
    for i in range(n):
        t = now_ms - days * DAY + int(i * (days * DAY - 3 * 3600_000) / n)
        tok = f"Hist{i % 37:02d}"
        sol.swap(wallet, "buy", tok, 1000, 200.0, t, fomo=fomo)
        sol.swap(wallet, "sell", tok, 1000, 200.0 * (1 + pattern[i % 5]), t + 1_200_000, fomo=fomo)


class Env:
    def __init__(self, tmp_path: Path, key=KEY, discord=True, poll_s=60.0, history=True, extra="", auto=True):
        self.hl, self.tg, self.sol, self.dex = FakeHL(), FakeTelegram(), FakeSolana(), FakeDex()
        self.dc = FakeDiscord() if discord else None
        self.data = tmp_path / "data"
        self.data.mkdir(exist_ok=True)
        cdir = write_config(tmp_path, self.hl, self.tg, self.data)
        (cdir / "sol.toml").write_text(
            f'rpc_url = "{self.sol.url}"\nlive_rpc_url = "{self.sol.url}"\nlive_ws_url = "{self.sol.ws_url}"\n'
            f'public_ws_url = "{self.sol.ws_url}"\ndex_url = "{self.dex.url}"\nrpc_interval_s = 0.0\n'
            f'live_rpc_interval_s = 0.0\npoll_leader_s = {poll_s}\nprice_poll_s = 0.2\nmin_scored_to_start = 1\n'
            'max_candidates = 10\ndiscover_pages = 1\ndiscover_per_page = 200\n'
            + f'auto_follow = {str(auto).lower()}\n'      # most tests cover the copy flow after an automatic pick
            + (extra or 'rescore_minutes = 0.03\n'), encoding="utf-8")
        if self.dc:
            (cdir / "discord.toml").write_text(
                f'api_base = "{self.dc.api_base}"\ngateway_url = "{self.dc.gateway_url}"\n'
                'edit_min_interval_s = 0.3\nmin_send_interval_s = 0.02\n', encoding="utf-8")
        self.cdir = cdir
        env = {**env_for(self.tg), "HELIUS_API_KEY": key}
        if self.dc:
            env.update(DISCORD_BOT_TOKEN=self.dc.token, DISCORD_CHANNEL_ID=self.dc.channel,
                       DISCORD_OWNER_ID=self.dc.owner)
        self.env = env
        self.bot = None
        self.th = None
        if history:
            history_swaps(self.sol, int(time.time() * 1000))
        self.dex.set(MINT, 0.01, 500_000.0, "MEME")

    def start(self):
        log.setup(None)
        self.bot = Bot(config.load(self.cdir, env=self.env))
        self.th = threading.Thread(target=self.bot.run, daemon=True)
        self.th.start()
        return self.bot

    def stop(self):
        if self.bot:
            self.bot.stop.set()
            self.th.join(8)
            self.bot.shutdown()
            self.bot.lock.f.close()                     # release the single-instance lock (in-process restart)
            self.bot = None

    def close(self):
        self.stop()
        for f in (self.hl, self.tg, self.sol, self.dex, self.dc):
            if f:
                f.close()

    def following(self, bot) -> bool:
        """GOOD followed, its history seeded, and the websocket subscribed to it (so a trade wakes the poller)."""
        return GOOD in bot.sol.st.followed and GOOD in bot.sol.ready and self.sol.subscribed(GOOD)

    def tg_text(self):
        return " ".join(m["text"] for m in self.tg.sent) + " " + " ".join(m["text"] for m in self.tg.edits)

    def dc_text(self):
        return " ".join(m["description"] for m in (self.dc.sent + self.dc.edits)) if self.dc else ""


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_discover_follow_copy_close_and_survive_a_restart(env):
    bot = env.start()
    assert wait_for(lambda: env.following(bot), timeout=40), env.tg_text()
    assert "Following" in env.tg_text() and tgfmt.short(GOOD) in env.tg_text()
    assert "Live trades from: Helius" in env.tg_text()
    # with a key everything goes to Helius (the search within its daily credit cap), history in bulk pages
    assert all(k for _, k in env.sol.calls)
    assert any(m == "getTransactionsForAddress" for m, _ in env.sol.calls)
    assert all(m in ("getTransaction", "getSignaturesForAddress", "getTransactionsForAddress")
               for m, _ in env.sol.calls)                                                   # read-only calls

    # the leader buys: the websocket wakes the poller at once (the safety poll is 60 s away) and we copy it
    t0 = time.time()
    env.sol.swap(GOOD, "buy", MINT, 5_000_000, 50_000.0)
    assert wait_for(lambda: MINT in bot.sol.st.positions, timeout=20), env.tg_text()
    assert time.time() - t0 < 15
    p = bot.sol.st.positions[MINT]
    assert p.sym == "MEME" and p.stop_px < p.entry_px and p.leader == GOOD
    # the side wallets copy the same buy at 2/5/10/20% risk (the main one is 1%): r x the main size
    assert [w.risk_pct for w in bot.sol.sides] == [2.0, 5.0, 10.0, 20.0]
    assert wait_for(lambda: all(MINT in w.st.positions for w in bot.sol.sides), timeout=10)
    for w in bot.sol.sides:
        assert w.st.positions[MINT].size == pytest.approx(p.size * w.risk_pct, rel=0.05), w.name
    env.tg.say("/fomowallets")
    assert wait_for(lambda: "FOMO wallets" in env.tg_text() and "20% risk" in env.tg_text())
    assert wait_for(lambda: "MEME" in env.tg_text() and "MEME" in env.dc_text())     # card on both Telegram + Discord

    # restart with the position open: same position, same stop, no double open
    before = (p.size, p.entry_px, p.stop_px)
    env.stop()
    env.sol.swap(GOOD, "sell", "Other", 1, 1.0)                                      # noise while we were down
    bot = env.start()
    assert wait_for(lambda: MINT in bot.sol.st.positions and env.following(bot), timeout=30)
    q = bot.sol.st.positions[MINT]
    assert (q.size, q.entry_px, q.stop_px) == before
    time.sleep(2.5)
    assert len(bot.sol.st.positions) == 1 and bot.sol.st.counters.get("orders") == 1

    # the leader sells everything: we close and the same card becomes the final summary
    env.dex.set(MINT, 0.012, 500_000.0, "MEME")
    assert wait_for(lambda: bot.sol.prices.marks().get(MINT) == pytest.approx(0.012), timeout=10)
    env.sol.swap(GOOD, "sell", MINT, 5_000_000, 60_000.0)
    assert wait_for(lambda: MINT not in bot.sol.st.positions, timeout=20)
    t = bot.sol.st.closed[-1]
    assert t["reason"] == "leader_close" and t["pnl"] > 0
    assert wait_for(lambda: all(MINT not in w.st.positions for w in bot.sol.sides), timeout=10)
    assert all(w.st.closed and w.st.closed[-1]["pnl"] > 0 for w in bot.sol.sides)
    assert wait_for(lambda: "WIN" in env.tg_text() and "WIN" in env.dc_text(), timeout=10)


def test_commands_from_telegram_and_discord(env):
    bot = env.start()
    assert wait_for(lambda: GOOD in bot.sol.st.followed, timeout=40)
    assert wait_for(lambda: env.dc.connected() and env.dc.commands is not None, timeout=10)
    names = {c["name"] for c in env.dc.commands}
    assert {"fomo", "fomoleaders", "fomoflatten", "fomoreset", "hyperwallet", "hyperreset"} <= names                      # registered as slash commands
    assert any(c["name"] == "fomoflatten" and c.get("options") for c in env.dc.commands)    # which needs the PIN
    env.tg.say("/fomo")
    env.dc.interact("fomoleaders")
    env.dc.interact("fomopause", user="999")                                  # a stranger: refused
    assert wait_for(lambda: "FOMO (paper)" in env.tg_text(), timeout=10)
    assert wait_for(lambda: "FOMO leaders" in env.dc_text(), timeout=10)
    assert not bot.sol.st.entries_paused
    env.dc.interact("fomopause")
    assert wait_for(lambda: bot.sol.st.entries_paused, timeout=10)
    env.tg.say("/fomoresume")
    assert wait_for(lambda: not bot.sol.st.entries_paused, timeout=10)
    env.tg.say("/fomoflatten wrong")
    assert wait_for(lambda: "Wrong or missing PIN" in env.tg_text(), timeout=10)
    env.dc.interact("fomoflatten", {"pin": PIN})
    assert wait_for(lambda: bot.sol.st.entries_paused, timeout=10)
    assert PIN not in env.dc_text() and PIN not in env.tg_text()


def test_a_refused_helius_key_alerts_stops_new_copies_and_keeps_the_stop(tmp_path):
    e = Env(tmp_path, poll_s=1.0)
    try:
        bot = e.start()
        assert wait_for(lambda: e.following(bot), timeout=40)
        e.sol.swap(GOOD, "buy", MINT, 5_000_000, 50_000.0)
        assert wait_for(lambda: MINT in bot.sol.st.positions, timeout=20)
        e.sol.refuse = True                                  # the key is revoked
        assert wait_for(lambda: "Solana data refused" in e.tg_text() and "Solana data refused" in e.dc_text(),
                        timeout=20)
        assert not bot.sol.auth_ok
        assert KEY not in e.tg_text() and KEY not in e.dc_text()
        e.dex.set(MINT, 0.001, 500_000.0, "MEME")            # the price collapses: the stop still works
        assert wait_for(lambda: MINT not in bot.sol.st.positions, timeout=20)
        assert bot.sol.st.closed[-1]["reason"] == "stop"
    finally:
        e.close()


def test_without_helius_the_owner_picks_with_fomofollow_and_the_bot_copies_on_the_public_endpoint(tmp_path):
    """The no-Helius setup: no automatic search, the owner (or Claude, asked in a session) picks wallets with
    /fomofollow, the bot copies them on the free public endpoint, and the pick survives re-ranking and a restart."""
    picked = "PickedWa" + "5" * 36
    e = Env(tmp_path, key="", discord=False, poll_s=1.0, history=False)
    try:
        bot = e.start()
        assert wait_for(lambda: "public Solana endpoint" in e.tg_text(), timeout=20)
        time.sleep(3)
        assert not bot.sol.scorer.busy and not bot.sol.scorer.meta["last_review"]       # no automatic search
        e.sol.swap(picked, "buy", "Old", 10, 10.0, int(time.time() * 1000) - DAY, fomo=False)   # an older trade
        e.tg.say("/fomofollow 0x" + "ab" * 20)
        assert wait_for(lambda: "Usage: /fomofollow" in e.tg_text(), timeout=10)
        e.tg.say(f"/fomofollow {picked}")
        assert wait_for(lambda: picked in bot.sol.st.followed and picked in bot.sol.st.picked, timeout=10)
        assert wait_for(lambda: picked in bot.sol.ready and e.sol.subscribed(picked), timeout=20)
        e.sol.swap(picked, "buy", MINT, 5_000_000, 50_000.0, fomo=False)
        assert wait_for(lambda: MINT in bot.sol.st.positions, timeout=20)
        assert not any(k for _, k in e.sol.calls)                                     # never a key: public only
        assert not any(p.leader == picked and p.coin == "Old" for p in bot.sol.st.positions.values())
        bot.sol.on_ranking([], 10, {})                                                # re-ranking keeps the pick
        assert picked in bot.sol.st.followed
        e.stop()
        bot = e.start()                                                               # and so does a restart
        assert wait_for(lambda: picked in bot.sol.st.picked and MINT in bot.sol.st.positions, timeout=20)
        e.tg.say(f"/fomounfollow {picked}")
        assert wait_for(lambda: picked not in bot.sol.st.followed, timeout=10)
        assert wait_for(lambda: "Unfollowed" in e.tg_text(), timeout=10)
        assert MINT in bot.sol.st.positions                                           # its copy still exits normally
    finally:
        e.close()


def test_a_wallet_that_fails_the_strict_rules_is_never_followed(tmp_path):
    e = Env(tmp_path, discord=False)
    try:
        e.sol.swap(GOOD, "buy", "BigBag", 1e6, 9_000.0, int(time.time() * 1000) - DAY)   # a large open bag
        bot = e.start()
        assert wait_for(lambda: bot.sol.scorer.scores.get(GOOD) is not None, timeout=30)
        s = bot.sol.scorer.scores[GOOD]
        assert not s["eligible"] and ("holding_a_lot" in s["reasons"] or "open_bag_vs_pnl" in s["reasons"])
        time.sleep(4)
        assert not bot.sol.st.followed
    finally:
        e.close()


def test_fomo_commands_end_to_end_including_the_reset_and_a_restart_after_it(env):
    bot = env.start()
    assert wait_for(lambda: env.following(bot), timeout=40)
    env.sol.swap(GOOD, "buy", MINT, 5_000_000, 50_000.0)
    assert wait_for(lambda: MINT in bot.sol.st.positions, timeout=20)
    env.tg.say("/fomotrades")
    env.tg.say("/fomowallet")
    env.tg.say("/fomotraders")
    env.dc.interact("fomotrades")
    assert wait_for(lambda: "FOMO trades" in env.tg_text() and "FOMO wallet" in env.tg_text()
                    and "FOMO traders" in env.tg_text(), timeout=15)
    assert wait_for(lambda: "FOMO trades" in env.dc_text() and "MEME" in env.dc_text(), timeout=15)
    # a reset is refused while a trade is open, and without the PIN
    env.tg.say("/fomoreset wrong")
    assert wait_for(lambda: "Wrong or missing PIN" in env.tg_text(), timeout=10)
    env.tg.say(f"/fomoreset {PIN}")
    assert wait_for(lambda: "FOMO reset refused" in env.tg_text(), timeout=10)
    assert MINT in bot.sol.st.positions and bot.restart_at == 0
    # flatten, then the reset goes through, archives the history and restarts the process
    env.tg.say(f"/fomoflatten {PIN}")
    assert wait_for(lambda: MINT not in bot.sol.st.positions, timeout=10)
    env.tg.say(f"/fomoreset {PIN}")
    assert wait_for(lambda: "FOMO reset done" in env.tg_text() and bot.restart_at > 0, timeout=10)
    assert wait_for(lambda: not env.th.is_alive(), timeout=15)                      # the loop ended: the wrapper restarts it
    arch = list((env.data / "sol" / "archive").iterdir())
    assert len(arch) == 1 and (arch[0] / "ledger.jsonl").exists()
    assert (env.data / "ledger.jsonl").exists() and not (env.data / "archive").exists()    # Hyperliquid untouched
    env.stop()
    bot = env.start()                                                                # what the wrapper does next
    assert wait_for(lambda: GOOD in bot.sol.st.followed and GOOD in bot.sol.ready, timeout=30)
    assert bot.sol.st.equity0 == 300.0 and not bot.sol.st.closed and not bot.sol.st.positions
    assert bot.sol.st.realized == 0


def test_hyper_commands_work_and_reset_only_hyperliquid(env):
    bot = env.start()
    assert wait_for(lambda: GOOD in bot.sol.st.followed, timeout=40)
    sol_before = (env.data / "sol" / "ledger.jsonl").read_bytes()[:200]
    for cmd, needle in [("/hyperstatus", "Status"), ("/hypertrades", "Trades"), ("/hypertraders", "Traders"),
                        ("/hyperwallet", "Wallets"), ("/hyperprogress", "Longs"), ("/hyperpositions", "No open trades")]:
        env.tg.say(cmd)
        assert wait_for(lambda: needle in env.tg_text(), timeout=15), cmd
    env.dc.interact("hyperwallet")
    assert wait_for(lambda: "Wallets" in env.dc_text(), timeout=15)
    env.tg.say("/hyperpause")
    assert wait_for(lambda: bot.st.entries_paused, timeout=10)
    env.tg.say("/hyperresume")
    assert wait_for(lambda: not bot.st.entries_paused, timeout=10)
    env.tg.say("/hyperreset wrong")
    assert wait_for(lambda: "Wrong or missing PIN" in env.tg_text(), timeout=10)
    env.tg.say(f"/hyperreset {PIN}")
    assert wait_for(lambda: "Reset done" in env.tg_text() and bot.restart_at > 0, timeout=10)
    assert (env.data / "archive").exists()                                           # the Hyperliquid history is archived
    assert not (env.data / "sol" / "archive").exists()                               # the FOMO book is not
    assert (env.data / "sol" / "ledger.jsonl").read_bytes()[:200] == sol_before


def test_fomosearch_from_telegram_finds_scores_and_follows_the_best_at_once(tmp_path):
    """The owner's flow: nothing to follow at first; GOOD starts trading on FOMO; /fomosearch finds it in FOMO's flow,
    reads its history in bulk from Helius, scores it and follows it at once (the hourly cycle is 10 h away)."""
    e = Env(tmp_path, discord=False, history=False, extra="rescore_minutes = 600\n")
    try:
        bot = e.start()
        assert wait_for(lambda: "Nobody passed" in e.tg_text(), timeout=30), e.tg_text()     # the first, empty search
        history_swaps(e.sol, int(time.time() * 1000))
        e.tg.say("/fomosearch")
        assert wait_for(lambda: "Looking for active FOMO traders" in e.tg_text(), timeout=10)
        assert wait_for(lambda: GOOD in bot.sol.st.followed, timeout=40), e.tg_text()
        assert wait_for(lambda: e.tg_text().count("FOMO search done") == 2, timeout=10)
        text = e.tg_text()
        assert "1 FOMO traders seen" in text and "+1 new" in text and "Following" in text
        assert any(m == "getTransactionsForAddress" and k for m, k in e.sol.calls)       # history in bulk (Helius)
        e.tg.say("/fomoleaders")
        assert wait_for(lambda: "last search done" in e.tg_text() and tgfmt.short(GOOD) in e.tg_text(), timeout=10)
    finally:
        e.close()


def test_fomoadd_checks_any_solana_wallet_and_follows_it_when_it_passes(tmp_path):
    """/fomoadd works for wallets the search can never see (this one never trades through FOMO)."""
    added, loser = "GoodTrader" + "2" * 34, "LoserTrader" + "3" * 33
    e = Env(tmp_path, discord=False, history=False, extra="rescore_minutes = 600\n")
    try:
        bot = e.start()
        assert wait_for(lambda: "Nobody passed" in e.tg_text(), timeout=30), e.tg_text()
        now = int(time.time() * 1000)
        history_swaps(e.sol, now, wallet=added, fomo=False)
        e.sol.swap(loser, "buy", "Bag", 1000, 500.0, now - 5 * DAY, fomo=False)
        e.sol.swap(loser, "sell", "Bag", 1000, 100.0, now - 4 * DAY, fomo=False)
        e.tg.say("/fomoadd 0x" + "ab" * 20)
        assert wait_for(lambda: "Usage: /fomoadd" in e.tg_text(), timeout=10)
        e.tg.say(f"/fomoadd {added}")
        assert wait_for(lambda: added in bot.sol.st.followed, timeout=30), e.tg_text()
        assert "Following" in e.tg_text() and bot.sol.scorer.pool[added]["manual"]
        assert wait_for(lambda: added in bot.sol.ready and e.sol.subscribed(added), timeout=20)
        e.sol.swap(added, "buy", MINT, 5_000_000, 50_000.0, fomo=False)        # and it is copied like any leader
        assert wait_for(lambda: MINT in bot.sol.st.positions, timeout=20)
        e.tg.say(f"/fomoadd {loser}")
        assert wait_for(lambda: "fails the strict rules" in e.tg_text(), timeout=30), e.tg_text()
        assert loser not in bot.sol.st.followed and "lost money (30 d)" in e.tg_text()
    finally:
        e.close()
