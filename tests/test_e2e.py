"""End to end: the real bot (all threads) against loopback fakes of Hyperliquid and Telegram.
Includes kill -9 of the real process with an open position and proof that position and stop survive."""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from copybot import config, log
from copybot.ledger import Ledger
from copybot.runner import Bot
from tests.fakes import FakeHL, FakeTelegram, make_fill

LEADER = "0x" + "ab" * 20
PIN = "2468"
ROOT = Path(__file__).resolve().parent.parent


def wait_for(cond, timeout=15.0, every=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(every)
    return False


def write_config(d: Path, hl: FakeHL, tg: FakeTelegram, data: Path) -> Path:
    cdir = d / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "config").glob("*.toml"):
        (cdir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (cdir / "runtime.toml").write_text(
        f'info_url = "{hl.info_url}"\nws_url = "{hl.ws_url}"\nleaderboard_url = "{hl.lb_url}"\n'
        f'data_dir = "{data.as_posix()}"\nlog_dir = "{(data / "logs").as_posix()}"\n'
        f'tick_s = 0.05\nreconcile_s = 2.0\nclock_refresh_s = 5.0\n', encoding="utf-8")
    (cdir / "telegram.toml").write_text(
        f'api_base = "{tg.api_base}"\nedit_min_interval_s = 0.3\nmin_send_interval_s = 0.02\npoll_timeout_s = 1\n',
        encoding="utf-8")
    # the Solana book needs no secret, so it would start and reach the REAL Solana network: off unless a test turns it on
    (cdir / "sol.toml").write_text("enabled = false\n", encoding="utf-8")
    return cdir


def env_for(tg: FakeTelegram) -> dict:
    return {**os.environ, "TELEGRAM_BOT_TOKEN": tg.token, "TELEGRAM_CHAT_ID": str(tg.chat_id), "COPYBOT_PIN": PIN,
            "PYTHONUNBUFFERED": "1"}


def seed_ledger(data: Path, leader=LEADER):
    lg = Ledger(data / "ledger.jsonl")
    lg.replay()
    lg.append({"ev": "genesis", "equity0": 300, "btc_px0": 100_000})
    lg.append({"ev": "follow", "leader": leader})
    lg.close()


def ledger_events(data: Path) -> list[dict]:
    p = data / "ledger.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_bytes().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


@pytest.fixture
def env(tmp_path):
    hl, tg = FakeHL(), FakeTelegram()
    data = tmp_path / "data"
    data.mkdir()
    cdir = write_config(tmp_path, hl, tg, data)
    yield hl, tg, data, cdir
    hl.close()
    tg.close()


def leader_fill(hl, coin, sz, side, oid=None):
    start = hl.positions.get(LEADER, {}).get(coin, 0.0)
    return make_fill(coin, hl.mids[coin], sz, side, start, oid=oid or random.randint(1, 10**9))


def test_full_bot_in_process(env):
    hl, tg, data, cdir = env
    seed_ledger(data)
    e = env_for(tg)
    cfg = config.load(cdir, env=e)
    log.setup(str(data / "logs"))
    for s in (cfg.tg_token, cfg.pin):
        log.add_secret(s)
    bot = Bot(cfg)
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    try:
        assert wait_for(lambda: LEADER in hl.subscribed_users())
        assert wait_for(lambda: bot.health().clock_ok and bot.health().mids_age_s < 1)
        # leader opens -> we open within the lag target, with a stop, and one live card appears
        t_fill = time.time()
        hl.push_fills(LEADER, [leader_fill(hl, "ETH", 50, "B")])
        assert wait_for(lambda: "ETH" in bot.st.positions, timeout=5)
        assert time.time() - t_fill < 5
        p = bot.st.positions["ETH"]
        assert p.stop_px == pytest.approx(p.entry_px * 0.97)
        assert bot.st.lags_ms[-1] < 5000
        assert wait_for(lambda: any("<b>ETH</b> ⬆️ LONG" in m["text"] for m in tg.sent))
        card = next(m for m in tg.sent if "<b>ETH</b> ⬆️ LONG" in m["text"])["message_id"]
        # price moves -> the SAME message is edited
        hl.mids["ETH"] = 3030.0
        assert wait_for(lambda: "3,030" in tg.messages[card] or "3030" in tg.messages[card])
        n_sent = len(tg.sent)
        # commands
        tg.say("/status")
        assert wait_for(lambda: any("📊 <b>Status</b>" in m["text"] for m in tg.sent))
        tg.say("/trades")
        assert wait_for(lambda: any("💼 <b>Trades</b> · 1 open" in m["text"] and "<b>ETH</b>" in m["text"]
                                    for m in tg.sent))
        tg.say("/traders")
        assert wait_for(lambda: any("👥 <b>Traders</b>" in m["text"] for m in tg.sent))
        # side wallets copied the same open at their own risk
        assert [w.risk_pct for w in bot.sides if not w.name.startswith("mirror")] == [2.0, 5.0, 10.0, 20.0]
        assert [w.name for w in bot.sides if w.name.startswith("mirror")] == ["mirror_x10"]
        assert all("ETH" in w.st.positions for w in bot.sides[:2])
        r5 = next(w for w in bot.sides if w.risk_pct == 5.0).st.positions["ETH"]
        assert r5.risk_usd() == pytest.approx(5 * bot.st.positions["ETH"].risk_usd(), rel=0.05)
        tg.say("/wallets")
        assert wait_for(lambda: any("💰 <b>Wallets</b>" in m["text"] and "20% risk" in m["text"]
                                    and "1% risk (main)" in m["text"] for m in tg.sent))
        tg.say("/flatten 0000")
        assert wait_for(lambda: any("Wrong or missing PIN" in m["text"] for m in tg.sent))
        assert "ETH" in bot.st.positions
        tg.say("/pause")
        assert wait_for(lambda: bot.st.entries_paused)
        hl.push_fills(LEADER, [leader_fill(hl, "SOL", 100, "B")])     # refused: paused
        time.sleep(0.5)
        assert "SOL" not in bot.st.positions
        tg.say("/resume")
        assert wait_for(lambda: not bot.st.entries_paused)
        # leader closes -> we close, and the trade card becomes a final summary in place
        hl.push_fills(LEADER, [leader_fill(hl, "ETH", 50, "A")])
        assert wait_for(lambda: "ETH" not in bot.st.positions, timeout=5)
        assert wait_for(lambda: "✅ WIN" in tg.messages[card] and "the trader closed" in tg.messages[card])
        assert not any("ETH" in m["text"] and "WIN" in m["text"] for m in tg.sent[n_sent:])   # no new message
        # flatten with PIN
        hl.push_fills(LEADER, [leader_fill(hl, "BTC", 5, "A")])
        assert wait_for(lambda: "BTC" in bot.st.positions, timeout=5)
        tg.say(f"/flatten {PIN}")
        assert wait_for(lambda: not bot.st.positions and bot.st.entries_paused)
        # secrets never in the logs
        for h in log.log.handlers:
            h.flush()
        text = (data / "logs" / "copybot.log").read_text(encoding="utf-8")
        assert PIN not in text and tg.token not in text
        assert "event=copy_open" in text and "lag_ms=" in text
    finally:
        bot.stop.set()
        th.join(5)
        bot.shutdown()


def test_ws_drop_missed_close_is_caught_by_reconcile(env):
    hl, tg, data, cdir = env
    seed_ledger(data)
    cfg = config.load(cdir, env=env_for(tg))
    log.setup(None)
    bot = Bot(cfg)
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    try:
        assert wait_for(lambda: LEADER in hl.subscribed_users() and bot.health().clock_ok)
        hl.push_fills(LEADER, [leader_fill(hl, "BTC", 2, "B")])
        assert wait_for(lambda: "BTC" in bot.st.positions)
        # websocket drops; the leader closes meanwhile (we never see the fill)
        hl.positions[LEADER] = {}
        hl.drop_ws()
        assert wait_for(lambda: "BTC" not in bot.st.positions, timeout=10)
        assert bot.st.closed[-1]["reason"] == "reconcile_leader_flat"
        # a quick reconnect (Hyperliquid's routine "Expired" close) is not worth an alert
        assert not any("ebsocket" in m["text"] for m in tg.sent)
    finally:
        bot.stop.set()
        th.join(5)
        bot.shutdown()


def start_proc(cdir, tg, data, n):
    out = open(data / f"stdout{n}.txt", "wb")
    return subprocess.Popen([sys.executable, "-m", "copybot.runner", "--config", str(cdir)], env=env_for(tg),
                            stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))


def test_kill_minus_9_with_open_position_stop_survives(env):
    hl, tg, data, cdir = env
    seed_ledger(data)
    proc = start_proc(cdir, tg, data, 1)
    try:
        assert wait_for(lambda: LEADER in hl.subscribed_users(), timeout=30)
        assert wait_for(lambda: any("Copybot started" in m["text"] for m in tg.sent), timeout=10)
        time.sleep(1.0)   # clock estimate
        hl.push_fills(LEADER, [leader_fill(hl, "ETH", 40, "B")])
        assert wait_for(lambda: any(e["ev"] == "open" for e in ledger_events(data)), timeout=10)
    finally:
        proc.kill()   # kill -9
        proc.wait()
    opened = [e for e in ledger_events(data) if e["ev"] == "open"]
    assert len(opened) == 1
    stop = opened[0]["pos"]["stop_px"]
    # while we are dead the price falls through the stop
    hl.mids["ETH"] = stop * 0.995
    proc = start_proc(cdir, tg, data, 2)
    try:
        assert wait_for(lambda: any(e["ev"] == "close" and e.get("reason") == "stop" for e in ledger_events(data)),
                        timeout=30)
        evs = ledger_events(data)
        assert len([e for e in evs if e["ev"] == "open"]) == 1   # never double-opened
        st = Ledger(data / "ledger.jsonl").replay()
        assert not st.positions and st.closed[-1]["reason"] == "stop"
    finally:
        proc.kill()
        proc.wait()


def test_random_kills_never_lose_a_position_or_its_stop(env):
    hl, tg, data, cdir = env
    seed_ledger(data)
    rng = random.Random(11)
    n = 0
    survived = 0
    coins = ["ETH", "SOL", "BTC"]
    for rnd in range(4):
        n += 1
        proc = start_proc(cdir, tg, data, n)
        try:
            assert wait_for(lambda: LEADER in hl.subscribed_users(), timeout=30)
            time.sleep(1.2)
            coin = coins[rnd % 3]
            hl.push_fills(LEADER, [leader_fill(hl, coin, {"ETH": 40, "SOL": 500, "BTC": 2}[coin], "B")])
            time.sleep(rng.uniform(0.0, 1.5))   # kill at a random point: before, during or after the open
        finally:
            proc.kill()
            proc.wait()
            hl.drop_ws()
        for path in [data / "ledger.jsonl", *sorted((data / "wallets").glob("*/ledger.jsonl"))]:
            st = Ledger(path).replay()                          # the main wallet and every side wallet
            survived += len(st.positions) if path.parent == data else 0
            for p in st.positions.values():
                assert p.stop_px > 0 and p.stop_px < p.entry_px     # every surviving long has its stop
            for u in st.uncertain:
                assert "truncated" in u or "no recorded result" in u, u
    assert survived >= 1    # the kills really happened with open positions
    # final run: the leader closes everything; every copy we hold must be closed, none opened twice
    n += 1
    proc = start_proc(cdir, tg, data, n)
    try:
        assert wait_for(lambda: LEADER in hl.subscribed_users(), timeout=30)
        time.sleep(1.0)
        for coin, szi in list(hl.positions.get(LEADER, {}).items()):
            hl.push_fills(LEADER, [make_fill(coin, hl.mids[coin], abs(szi), "A", szi)])
        ledgers = [data, *sorted(p for p in (data / "wallets").iterdir())]
        assert len(ledgers) == 6            # main + 4 risk levels + the mirror wallet
        assert wait_for(lambda: not any(Ledger(d / "ledger.jsonl").replay().positions for d in ledgers), timeout=20)
        for d in ledgers:                                  # in EVERY wallet: nothing opened twice, nothing left
            evs = ledger_events(d)
            ids = [e["pos"]["pos_id"] for e in evs if e["ev"] == "open"]
            assert len(ids) == len(set(ids))
            coins_open = [e["pos"]["coin"] for e in evs if e["ev"] == "open"]
            assert len(coins_open) == len(set(coins_open)), d   # each coin was opened at most once
    finally:
        proc.kill()
        proc.wait()


def test_ws_alert_only_when_it_stays_down(env):
    hl, tg, data, cdir = env
    seed_ledger(data)
    (cdir / "runtime.toml").write_text((cdir / "runtime.toml").read_text(encoding="utf-8")
                                       + "ws_alert_after_s = 2.0\n", encoding="utf-8")
    cfg = config.load(cdir, env=env_for(tg))
    log.setup(None)
    bot = Bot(cfg)
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    try:
        assert wait_for(lambda: LEADER in hl.subscribed_users() and bot.feed.connected)
        hl.ws_refuse = True
        hl.drop_ws()
        assert wait_for(lambda: any("websocket down for" in m["text"] for m in tg.sent), timeout=15)
        assert sum("websocket down" in m["text"] for m in tg.sent) == 1        # once, not on every retry
        hl.ws_refuse = False
        assert wait_for(lambda: any("Websocket back after" in m["text"] for m in tg.sent), timeout=30)
    finally:
        bot.stop.set()
        th.join(5)
        bot.shutdown()


# ---- /search, /reset, /restart -----------------------------------------------------------------------------
def start_bot(env, scorer=False):
    hl, tg, data, cdir = env
    seed_ledger(data)
    cfg = config.load(cdir, env=env_for(tg))
    log.setup(str(data / "logs"))
    bot = Bot(cfg)
    if not scorer:
        bot.scorer.stop.set()          # the test feeds rankings itself
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    assert wait_for(lambda: LEADER in hl.subscribed_users() and bot.health().clock_ok)
    return bot, th


def stop_bot(bot, th):
    bot.stop.set()
    th.join(5)
    bot.shutdown()


TOP = [f"0x{i:040x}" for i in range(1, 10)]


def test_search_repicks_the_best_seven_now(env):
    hl, tg, data, cdir = env
    bot, th = start_bot(env)
    try:
        scores = {a: {"score": 90 - i, "trades": 100, "win_rate": 0.7, "profit_factor": 3} for i, a in enumerate(TOP)}
        bot.q.put(("ranking", TOP, len(TOP), scores))
        assert wait_for(lambda: bot.ranking == TOP)
        tg.say("/search")
        assert wait_for(lambda: set(bot.st.followed) == set(TOP[:7]))     # at once: no window, no 2 checks
        assert LEADER in bot.st.dropped                                     # it is not in the ranking any more
        assert wait_for(lambda: all(set(w.st.followed) == set(TOP[:7]) for w in bot.sides))   # synced just after
        assert wait_for(lambda: any("following 7 new, dropping 1" in m["text"] for m in tg.sent))
        assert bot.search_pending
        better = [f"0x{i:040x}" for i in range(50, 52)] + TOP          # the background search found 2 better ones
        scores.update({a: {"score": 99, "trades": 100, "win_rate": 0.8, "profit_factor": 4} for a in better[:2]})
        bot.q.put(("searched", better, len(better), scores))
        assert wait_for(lambda: set(bot.st.followed) == set(better[:7]))
        assert TOP[5] in bot.st.dropped and TOP[6] in bot.st.dropped
        assert wait_for(lambda: any("Search finished" in m["text"] for m in tg.sent)) and not bot.search_pending
    finally:
        stop_bot(bot, th)


def test_reset_needs_no_open_trades_and_the_pin_then_starts_fresh(env):
    hl, tg, data, cdir = env
    bot, th = start_bot(env)
    try:
        hl.push_fills(LEADER, [leader_fill(hl, "ETH", 50, "B")])
        assert wait_for(lambda: "ETH" in bot.st.positions and "ETH" in bot.sides[0].st.positions)
        tg.say(f"/reset {PIN}")
        assert wait_for(lambda: any("Reset refused" in m["text"] for m in tg.sent))
        hl.mids["ETH"] = 3010.0
        hl.push_fills(LEADER, [leader_fill(hl, "ETH", 50, "A")])
        assert wait_for(lambda: not bot.st.positions and not any(w.st.positions for w in bot.sides))
        assert bot.st.closed and bot.st.realized != 0
        since = bot.st.followed[LEADER]
        tg.say("/reset 0000")
        assert wait_for(lambda: any("Wrong or missing PIN. Usage: /reset" in m["text"] for m in tg.sent))
        tg.say(f"/reset {PIN}")
        assert wait_for(lambda: any("Reset done" in m["text"] for m in tg.sent))
        assert wait_for(lambda: bot.stop.is_set(), timeout=8)              # restarts itself
        th.join(5)
        arch = next((data / "archive").glob("reset-*"))
        old = Ledger(arch / "ledger.jsonl").replay()
        assert old.closed and old.realized != 0                             # nothing deleted
        assert (arch / "wallets" / "risk_20pct" / "ledger.jsonl").exists() and not (data / "wallets").exists()
        new = Ledger(data / "ledger.jsonl").replay()
        assert new.equity() == 300 and not new.closed and not new.positions and not new.uncertain
        assert new.followed == {LEADER: since}                             # traders kept, with their 'since'
    finally:
        stop_bot(bot, th)


def test_restart_command_stops_the_loop_for_the_restart_loop(env):
    hl, tg, data, cdir = env
    bot, th = start_bot(env)
    try:
        tg.say("/restart")
        assert wait_for(lambda: any("Restarting" in m["text"] for m in tg.sent))
        assert wait_for(lambda: bot.stop.is_set(), timeout=8)
    finally:
        stop_bot(bot, th)


def test_hyperadd_queues_the_wallet_and_follows_it_when_it_passes(env):
    hl, tg, data, cdir = env
    bot, th = start_bot(env)
    try:
        tg.say("/hyperadd nonsense")
        assert wait_for(lambda: any("Usage: /hyperadd" in m["text"] for m in tg.sent))
        new = "0x" + "C1" * 20
        tg.say(f"/hyperadd {new}")
        assert wait_for(lambda: bot.scorer.add_q.qsize() == 1)
        assert bot.scorer.add_q.get() == new.lower()
        good = {"eligible": True, "score": 88, "trades": 120, "win_rate": 0.7, "profit_factor": 3}
        bot.q.put(("added", new.lower(), {"ok": True}, good, [new.lower()], {new.lower(): good}))
        assert wait_for(lambda: new.lower() in bot.st.followed)
        assert wait_for(lambda: all(new.lower() in w.st.followed for w in bot.sides))
        bad = "0x" + "d2" * 20
        bot.q.put(("added", bad, {"ok": True}, {"eligible": False, "reasons": ["win_rate<60%"]}, [], {}))
        assert wait_for(lambda: any("fails the strict rules: win_rate&lt;60%" in m["text"] for m in tg.sent))
        assert bad not in bot.st.followed
    finally:
        stop_bot(bot, th)


def test_mirror_wallet_sizes_copies_by_the_leaders_share_of_its_account(env):
    """The leader's account is worth ~541,765$ (recorded clearinghouseState). A 1 ETH buy (~3,000$) is 0.55% of it:
    the mirror wallet buys 10 x 0.55% = 5.5% of its 300$ (~16.6$). A tiny buy is lifted to the 10$ minimum, and
    the main wallet keeps its fixed-risk size."""
    hl, tg, data, cdir = env
    bot, th = start_bot(env)
    try:
        mirror = next(w for w in bot.sides if w.name.startswith("mirror"))
        assert wait_for(lambda: bot.leader_value.get(LEADER, 0) > 500_000, timeout=10)
        hl.push_fills(LEADER, [leader_fill(hl, "ETH", 1, "B")])
        assert wait_for(lambda: "ETH" in mirror.st.positions and "ETH" in bot.st.positions, timeout=10)
        p = mirror.st.positions["ETH"]
        share = 1 * 3000.0 / bot.leader_value[LEADER]
        assert p.size * p.entry_px == pytest.approx(share * 10 * 300, rel=0.05)
        main = bot.st.positions["ETH"]
        assert main.size * main.entry_px > 60                               # fixed 1% risk at a 3% stop: ~100$
        hl.push_fills(LEADER, [leader_fill(hl, "SOL", 0.5, "B")])          # 75$ of 541k: 0.014% -> lifted to 10$
        assert wait_for(lambda: "SOL" in mirror.st.positions, timeout=10)
        q = mirror.st.positions["SOL"]
        assert 10 <= q.size * q.entry_px < 12
        tg.say("/hyperwallet")
        assert wait_for(lambda: any("mirror x10 (their % of account)" in m["text"] for m in tg.sent + tg.edits))
    finally:
        stop_bot(bot, th)
