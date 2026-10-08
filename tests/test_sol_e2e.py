"""End to end: the real bot with the Solana book enabled (all threads) against loopback fakes of Hyperliquid,
Telegram, FOMO, DexScreener and Discord. A wallet is discovered from the leaderboard, scored from its swaps,
followed, copied on a new buy, closed on its sell, and the position survives a restart."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from copybot import config, log
from copybot.ledger import Ledger
from copybot.runner import Bot
from tests.fakes import FakeHL, FakeTelegram
from tests.fakes_sol import COOKIE, FakeDex, FakeDiscord, FakeFomo, make_row, make_swap
from tests.test_e2e import PIN, env_for, wait_for, write_config

DAY = 86_400_000
MINT = "MemeMint1111111111111111111111111111111111"
GOOD, UID = "GoodWallet11111111111111111111111111111111", "uid-good"


def history_swaps(now_ms: int, n=90, days=60) -> list[dict]:
    """A wallet that passes every strict rule: 90 closed trades, ~60% wins, 20 min holds, many tokens."""
    pattern = [0.40, 0.40, -0.15, 0.40, -0.15]
    out = []
    for i in range(n):
        t = now_ms - days * DAY + int(i * (days * DAY - 3 * 3600_000) / n)
        tok = f"Hist{i % 37:02d}"
        out.append(make_swap("buy", tok, 1000, 200.0, t, wallet=GOOD))
        out.append(make_swap("sell", tok, 1000, 200.0 * (1 + pattern[i % 5]), t + 1_200_000, wallet=GOOD))
    return out


class Env:
    def __init__(self, tmp_path: Path, cookie=COOKIE, discord=True):
        self.hl, self.tg, self.fomo, self.dex = FakeHL(), FakeTelegram(), FakeFomo(), FakeDex()
        self.dc = FakeDiscord() if discord else None
        self.data = tmp_path / "data"
        self.data.mkdir(exist_ok=True)
        cdir = write_config(tmp_path, self.hl, self.tg, self.data)
        (cdir / "sol.toml").write_text(
            f'api_base = "{self.fomo.url}"\ndex_url = "{self.dex.url}"\npoll_leader_s = 1.0\nprice_poll_s = 0.2\n'
            'min_scored_to_start = 1\nrescore_minutes = 0.03\nmax_candidates = 10\n', encoding="utf-8")
        if self.dc:
            (cdir / "discord.toml").write_text(
                f'api_base = "{self.dc.url}"\nedit_min_interval_s = 0.3\nmin_send_interval_s = 0.02\n'
                'poll_interval_s = 0.1\n', encoding="utf-8")
        self.cdir = cdir
        env = {**env_for(self.tg), "FOMO_COOKIE": cookie}
        if self.dc:
            env.update(DISCORD_BOT_TOKEN="dc-token-xyz", DISCORD_CHANNEL_ID=self.dc.channel, DISCORD_OWNER_ID="42")
        self.env = env
        self.bot = None
        self.th = None
        now = int(time.time() * 1000)
        self.fomo.rows["30d"] = [make_row(UID, GOOD, "GoodTrader", 9000.0)]
        self.fomo.rows["7d"] = [make_row(UID, GOOD, "GoodTrader", 2500.0, "7d")]
        self.fomo.rows["24h"] = [make_row(UID, GOOD, "GoodTrader", 80.0, "24h")]
        self.fomo.swaps[UID] = history_swaps(now)
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
        for f in (self.hl, self.tg, self.fomo, self.dex, self.dc):
            if f:
                f.close()

    def tg_text(self):
        return " ".join(m["text"] for m in self.tg.sent) + " " + " ".join(m["text"] for m in self.tg.edits)

    def dc_text(self):
        return " ".join(m["content"] for m in (self.dc.sent + self.dc.edits)) if self.dc else ""


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_discover_follow_copy_close_and_survive_a_restart(env):
    bot = env.start()
    assert wait_for(lambda: GOOD in bot.sol.st.followed, timeout=40), env.tg_text()
    assert "GoodTrader" in env.tg_text() and "Following" in env.tg_text()
    assert wait_for(lambda: GOOD in bot.sol.ready, timeout=20)
    assert env.fomo.requests and not any("order" in r or "exchange" in r for r in env.fomo.requests)

    # the leader buys: we copy (a fresh swap, priced by DexScreener)
    env.fomo.swaps[UID].append(make_swap("buy", MINT, 5_000_000, 50_000.0, int(time.time() * 1000), wallet=GOOD))
    assert wait_for(lambda: MINT in bot.sol.st.positions, timeout=20), env.tg_text()
    p = bot.sol.st.positions[MINT]
    assert p.sym == "MEME" and p.stop_px < p.entry_px and p.leader == GOOD
    assert wait_for(lambda: "MEME" in env.tg_text() and "MEME" in env.dc_text())     # card on both Telegram + Discord
    assert env.dc.sent and all(m["author"]["bot"] for m in env.dc.sent)

    # restart with the position open: same position, same stop, no double open
    before = (p.size, p.entry_px, p.stop_px)
    env.stop()
    env.fomo.swaps[UID].append(make_swap("sell", "Other", 1, 1.0, int(time.time() * 1000), wallet=GOOD))   # noise
    bot = env.start()
    assert wait_for(lambda: MINT in bot.sol.st.positions and GOOD in bot.sol.ready, timeout=30)
    q = bot.sol.st.positions[MINT]
    assert (q.size, q.entry_px, q.stop_px) == before
    time.sleep(2.5)
    assert len(bot.sol.st.positions) == 1 and bot.sol.st.counters.get("orders") == 1

    # the leader sells everything: we close and the same card becomes the final summary
    env.dex.set(MINT, 0.012, 500_000.0, "MEME")
    env.fomo.swaps[UID].append(make_swap("sell", MINT, 5_000_000, 60_000.0, int(time.time() * 1000), wallet=GOOD))
    assert wait_for(lambda: MINT not in bot.sol.st.positions, timeout=20)
    t = bot.sol.st.closed[-1]
    assert t["reason"] == "leader_close" and t["pnl"] > 0
    assert wait_for(lambda: "WIN" in env.tg_text() and "WIN" in env.dc_text(), timeout=10)


def test_commands_from_telegram_and_discord(env):
    bot = env.start()
    assert wait_for(lambda: GOOD in bot.sol.st.followed, timeout=40)
    env.tg.say("/sol")
    env.dc.say("42", "!solleaders")
    env.dc.say("999", "!solpause")                       # a stranger: ignored
    assert wait_for(lambda: "Solana (paper)" in env.tg_text() and "Solana (paper)" in env.dc_text() or
                    "Solana leaders" in env.dc_text(), timeout=10)
    assert wait_for(lambda: "Solana leaders" in env.dc_text(), timeout=10)
    assert not bot.sol.st.entries_paused
    env.dc.say("42", "!solpause")
    assert wait_for(lambda: bot.sol.st.entries_paused, timeout=10)
    env.tg.say("/solresume")
    assert wait_for(lambda: not bot.sol.st.entries_paused, timeout=10)
    env.tg.say("/solflatten wrong")
    assert wait_for(lambda: "Wrong or missing PIN" in env.tg_text(), timeout=10)
    env.dc.say("42", f"!solflatten {PIN}")
    assert wait_for(lambda: bot.sol.st.entries_paused, timeout=10)
    assert wait_for(lambda: len(env.dc.deleted) == 1, timeout=10)        # the message carrying the PIN is removed
    env.dc.say("42", "!help")
    assert wait_for(lambda: "/solflatten" in env.dc_text(), timeout=10)


def test_expired_fomo_session_alerts_stops_new_wallets_and_keeps_the_stop(env):
    bot = env.start()
    assert wait_for(lambda: GOOD in bot.sol.st.followed and GOOD in bot.sol.ready, timeout=40)
    env.fomo.swaps[UID].append(make_swap("buy", MINT, 5_000_000, 50_000.0, int(time.time() * 1000), wallet=GOOD))
    assert wait_for(lambda: MINT in bot.sol.st.positions, timeout=20)
    env.fomo.refuse = True                               # the session expires
    assert wait_for(lambda: "FOMO session rejected" in env.tg_text() and "FOMO session rejected" in env.dc_text(),
                    timeout=20)
    assert not bot.sol.auth_ok
    env.dex.set(MINT, 0.001, 500_000.0, "MEME")          # the price collapses: the stop still works
    assert wait_for(lambda: MINT not in bot.sol.st.positions, timeout=20)
    assert bot.sol.st.closed[-1]["reason"] == "stop"


def test_without_a_fomo_cookie_the_solana_book_stays_off_and_says_so(tmp_path):
    e = Env(tmp_path, cookie="", discord=False)
    try:
        bot = e.start()
        assert bot.sol is None
        e.tg.say("/sol")
        assert wait_for(lambda: "Solana is off" in e.tg_text(), timeout=10)
        assert not (e.data / "sol" / "ledger.jsonl").exists()
    finally:
        e.close()


def test_a_wallet_that_fails_the_strict_rules_is_never_followed(tmp_path):
    e = Env(tmp_path, discord=False)
    try:
        now = int(time.time() * 1000)
        swaps = history_swaps(now)
        swaps.append(make_swap("buy", "BigBag", 1e6, 9_000.0, now - DAY, wallet=GOOD))   # a large open bag
        e.fomo.swaps[UID] = swaps
        bot = e.start()
        assert wait_for(lambda: bot.sol.scorer.scores.get(GOOD) is not None, timeout=30)
        s = bot.sol.scorer.scores[GOOD]
        assert not s["eligible"] and ("holding_a_lot" in s["reasons"] or "open_bag_vs_pnl" in s["reasons"])
        time.sleep(4)
        assert not bot.sol.st.followed
    finally:
        e.close()
