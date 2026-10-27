"""End to end: the real bot (all threads) against loopback fakes of Hyperliquid (prices, books, fills), Invo and
Discord. Includes kill -9 of the real process with open copies and proof that every copy and its stop survive.
Only the Invo book exists since 2026-10-26 (owner request); the Invo copy flows themselves are in test_invo.py."""
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
from tests.fakes import FakeDiscord, FakeHL
from tests.fakes_invo import FakeInvo

LEADER = "invo:nicush"
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


def write_config(d: Path, hl: FakeHL, dc: FakeDiscord, data: Path) -> Path:
    cdir = d / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "config").glob("*.toml"):
        (cdir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (cdir / "runtime.toml").write_text(
        f'info_url = "{hl.info_url}"\nws_url = "{hl.ws_url}"\n'
        f'data_dir = "{data.as_posix()}"\nlog_dir = "{(data / "logs").as_posix()}"\n'
        f'tick_s = 0.05\nreconcile_s = 2.0\nclock_refresh_s = 5.0\n', encoding="utf-8")
    (cdir / "discord.toml").write_text(
        f'api_base = "{dc.api_base}"\ngateway_url = "{dc.gateway_url}"\nedit_min_interval_s = 0.3\n'
        'min_send_interval_s = 0.02\n', encoding="utf-8")
    return cdir


def env_for(dc: FakeDiscord) -> dict:
    return {**os.environ, "DISCORD_BOT_TOKEN": dc.token, "DISCORD_CHANNEL_ID": dc.channel,
            "DISCORD_OWNER_ID": dc.owner, "COPYBOT_PIN": PIN, "PYTHONUNBUFFERED": "1"}


def seed_ledger(data: Path):
    lg = Ledger(data / "ledger.jsonl")
    lg.replay()
    lg.append({"ev": "genesis", "equity0": 300, "btc_px0": 100_000})
    lg.close()


def ledger_events(path: Path) -> list[dict]:
    p = path if path.suffix == ".jsonl" else path / "ledger.jsonl"
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
    hl, dc = FakeHL(), FakeDiscord()
    data = tmp_path / "data"
    data.mkdir()
    cdir = write_config(tmp_path, hl, dc, data)
    yield hl, dc, data, cdir
    hl.close()
    dc.close()


def start_bot(env):
    hl, dc, data, cdir = env
    seed_ledger(data)
    cfg = config.load(cdir, env=env_for(dc))
    log.setup(str(data / "logs"))
    for s in (cfg.dc_token, cfg.pin):
        log.add_secret(s)
    bot = Bot(cfg)
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    assert wait_for(lambda: bot.health().clock_ok and bot.health().mids_age_s < 1)
    return bot, th


def stop_bot(bot, th):
    bot.stop.set()
    th.join(5)
    bot.shutdown()


def test_only_the_invo_commands_exist_and_prices_still_flow(env):
    """Owner request 2026-10-26: the Hyperliquid and FOMO books, the arena and the bot's own Invo search are gone.
    Hyperliquid still gives the prices (and books and fills) of the Invo copies; Invo itself is off without a token."""
    hl, dc, data, cdir = env
    bot, th = start_bot(env)
    try:
        assert wait_for(lambda: any("Copybot started" in m["text"] and "Invo is off" in m["text"] for m in dc.sent))
        assert not bot.sides and not hl.subscribed_users()                  # no Hyperliquid wallet is followed
        assert wait_for(lambda: dc.commands is not None)
        names = {c["name"] for c in dc.commands}
        assert names == {"help", "restart", "status", "trades", "traders", "wallets", "progress", "follow",
                         "unfollow", "block", "unblock", "pause", "resume", "flatten", "reset"}
        flat = next(c for c in dc.commands if c["name"] == "flatten")
        assert flat["options"][0]["name"] == "pin"
        follow = next(c for c in dc.commands if c["name"] == "follow")
        assert follow["options"][0]["name"] == "wallet" and "Invo" in follow["description"]
        dc.say("/wallets")
        assert wait_for(lambda: any("Invo is off" in m["text"] for m in dc.sent))
        dc.say("/help")
        assert wait_for(lambda: any("/follow nicush" in m["text"] for m in dc.sent))
        assert not any("/hyper" in m["text"] or "/fomo" in m["text"] or "/invo" in m["text"] for m in dc.sent)
        hl.mids["ETH"] = 3123.0
        assert wait_for(lambda: bot.mids.get("ETH") == 3123.0)
        for h in log.log.handlers:
            h.flush()
        text = (data / "logs" / "copybot.log").read_text(encoding="utf-8")
        assert PIN not in text and dc.token not in text                   # secrets never in the logs
    finally:
        stop_bot(bot, th)


def test_ws_alert_only_when_it_stays_down(env):
    hl, dc, data, cdir = env
    (cdir / "runtime.toml").write_text((cdir / "runtime.toml").read_text(encoding="utf-8")
                                       + "ws_alert_after_s = 2.0\n", encoding="utf-8")
    bot, th = start_bot(env)
    try:
        assert wait_for(lambda: bot.feed.connected)
        hl.drop_ws()                                    # a quick reconnect (Hyperliquid's "Expired"): no alert
        time.sleep(1.0)
        assert wait_for(lambda: bot.feed.connected)
        assert not any("ebsocket" in m["text"] for m in dc.sent)
        hl.ws_refuse = True
        hl.drop_ws()
        assert wait_for(lambda: any("websocket down for" in m["text"] for m in dc.sent), timeout=15)
        assert sum("websocket down" in m["text"] for m in dc.sent) == 1        # once, not on every retry
        hl.ws_refuse = False
        assert wait_for(lambda: any("Websocket back after" in m["text"] for m in dc.sent), timeout=30)
    finally:
        stop_bot(bot, th)


def test_restart_command_stops_the_loop_for_the_restart_loop(env):
    hl, dc, data, cdir = env
    bot, th = start_bot(env)
    try:
        dc.say("/restart")
        assert wait_for(lambda: any("Restarting" in m["text"] for m in dc.sent))
        assert wait_for(lambda: bot.stop.is_set(), timeout=8)
    finally:
        stop_bot(bot, th)


# ---- kill -9 of the real process -------------------------------------------------------------------------------
@pytest.fixture
def proc_env(env, tmp_path):
    """The real process with Invo on (the fake's login) and @nicush already followed."""
    hl, dc, data, cdir = env
    f = FakeInvo()
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    kept = [x for x in (ROOT / "config" / "invo.toml").read_text(encoding="utf-8").splitlines()
            if not x.startswith(("api_base", "poll_s"))]               # the owner's invo.toml, on the fake
    (cdir / "invo.toml").write_text("\n".join(kept) + f'\napi_base = "{f.url}"\npoll_s = 0.5\n', encoding="utf-8")
    pid = f.add_user("nicush")[0]
    seed_ledger(data)
    lg = Ledger(data / "wallets" / "invo_calls" / "ledger.jsonl")
    lg.replay()
    lg.append({"ev": "follow", "leader": LEADER, "picked": True})
    lg.close()
    yield hl, dc, data, cdir, f, tok, pid
    f.close()


def start_proc(cdir, dc, data, tok, n):
    out = open(data / f"stdout{n}.txt", "wb")
    return subprocess.Popen([sys.executable, "-m", "copybot.runner", "--config", str(cdir)],
                            env={**env_for(dc), "INVO_TOKEN_FILE": str(tok)},
                            stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))


def wallet_dirs(data: Path) -> list[Path]:
    return sorted(p for p in (data / "wallets").iterdir() if (p / "ledger.jsonl").exists())


def started(dc, n):
    return sum("Copybot started" in m["text"] for m in dc.sent) >= n


def test_kill_minus_9_with_an_open_copy_its_stop_survives(proc_env):
    hl, dc, data, cdir, f, tok, pid = proc_env
    proc = start_proc(cdir, dc, data, tok, 1)
    stop3 = data / "wallets" / "invo_calls_stop3"
    try:
        assert wait_for(lambda: started(dc, 1), timeout=30)
        time.sleep(2.0)                                  # clock estimate and the watcher's first read (baseline)
        f.open_call(pid, "ETH", long=True, leverage=5, size=0.05, entry=hl.mids["ETH"])
        assert wait_for(lambda: any(e["ev"] == "open" for e in ledger_events(stop3)), timeout=15)
    finally:
        proc.kill()   # kill -9
        proc.wait()
    opened = [e for e in ledger_events(stop3) if e["ev"] == "open"]
    assert len(opened) == 1
    stop = opened[0]["pos"]["stop_px"]
    assert stop == pytest.approx(opened[0]["pos"]["entry_px"] * 0.97)
    hl.mids["ETH"] = stop * 0.995                        # while we are dead the price falls through the 3% stop
    proc = start_proc(cdir, dc, data, tok, 2)
    try:
        assert wait_for(lambda: any(e["ev"] == "close" and e.get("reason") == "stop" for e in ledger_events(stop3)),
                        timeout=30)
        assert len([e for e in ledger_events(stop3) if e["ev"] == "open"]) == 1   # never double-opened
        st = Ledger(stop3 / "ledger.jsonl").replay()
        assert not st.positions and st.closed[-1]["reason"] == "stop"
        main = Ledger(data / "wallets" / "invo_calls" / "ledger.jsonl").replay()
        assert "ETH" in main.positions                   # the trader's-exit wallet still holds it (its guard is far)
    finally:
        proc.kill()
        proc.wait()


def test_random_kills_never_lose_a_copy_or_its_stop(proc_env):
    hl, dc, data, cdir, f, tok, pid = proc_env
    rng = random.Random(11)
    coins = ["ETH", "SOL", "BTC"]
    n = 0
    for rnd in range(3):
        n += 1
        proc = start_proc(cdir, dc, data, tok, n)
        try:
            assert wait_for(lambda: started(dc, n), timeout=30)
            time.sleep(2.0)
            coin = coins[rnd]
            f.open_call(pid, coin, long=True, leverage=5, size=0.05, entry=hl.mids[coin])
            time.sleep(rng.uniform(0.5, 3.0))            # kill at a random point: before, during or after the copies
        finally:
            proc.kill()
            proc.wait()
            hl.drop_ws()
        for d in wallet_dirs(data):
            st = Ledger(d / "ledger.jsonl").replay()
            for p in st.positions.values():
                assert 0 < p.stop_px < p.entry_px, d.name        # every surviving long has its stop
            for u in st.uncertain:
                assert "truncated" in u or "no recorded result" in u, u
    assert any(Ledger(d / "ledger.jsonl").replay().positions for d in wallet_dirs(data))   # kills hit open copies
    # final run: the trader closes every call; every copy must close, none opened twice
    n += 1
    proc = start_proc(cdir, dc, data, tok, n)
    try:
        assert wait_for(lambda: started(dc, n), timeout=30)
        time.sleep(2.0)
        for c in list(f.calls[pid]):
            f.close_call(pid, c["id"])
        dirs = wallet_dirs(data)
        assert len(dirs) == 18                                    # every size x every stop
        assert wait_for(lambda: not any(Ledger(d / "ledger.jsonl").replay().positions for d in dirs), timeout=30)
        for d in dirs:
            evs = ledger_events(d)
            ids = [e["pos"]["pos_id"] for e in evs if e["ev"] == "open"]
            assert len(ids) == len(set(ids)), d.name
            coins_open = [e["pos"]["coin"] for e in evs if e["ev"] == "open"]
            assert len(coins_open) == len(set(coins_open)), d.name   # each coin was opened at most once
    finally:
        proc.kill()
        proc.wait()
