"""Paper mode only: no signing library, no key handling, no exchange order endpoint anywhere."""
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from copybot import config, log
from copybot.ledger import Ledger, Position
from copybot.runner import Bot
from tests.fakes import FakeHL, FakeTelegram
from tests.test_e2e import LEADER, env_for, wait_for, write_config

SRC = Path(__file__).resolve().parent.parent / "src" / "copybot"
FORBIDDEN = re.compile(r"eth_account|eth_keys|eth_keyfile|coincurve|ecdsa|nacl|web3|hyperliquid\.exchange|"
                       r"private_key|privkey|secret_key|mnemonic|/exchange\b|sign_l1|sign_typed", re.I)


def test_source_has_no_signing_or_keys():
    for f in SRC.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            assert not FORBIDDEN.search(line), f"{f.name}:{i}: {line.strip()}"


def test_solana_side_has_no_wallet_keys_or_transaction_signing():
    """The Solana book is paper only: it never imports a Solana/crypto library or touches a key."""
    bad = re.compile(r"solders|solana.*import|import solana|nacl|base58|keypair|secretkey|sendTransaction|"
                     r"signTransaction|simulateTransaction|private_key|mnemonic|seed_phrase", re.I)
    for f in (SRC / "sol").glob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            assert not bad.search(line), f"{f.name}:{i}: {line.strip()}"


def test_importing_the_bot_loads_no_signing_library():
    code = ("import sys, copybot.runner; bad=[m for m in sys.modules if m.split('.')[0] in "
            "('eth_account','eth_keys','coincurve','ecdsa','nacl','web3','hyperliquid')]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_uncertain_restart_pauses_entries_alerts_and_still_runs_stops(tmp_path):
    hl, tg = FakeHL(), FakeTelegram()
    data = tmp_path / "data"
    data.mkdir()
    cdir = write_config(tmp_path, hl, tg, data)
    lg = Ledger(data / "ledger.jsonl")
    lg.replay()
    lg.append({"ev": "genesis", "equity0": 300, "btc_px0": 100_000})
    lg.append({"ev": "follow", "leader": LEADER})
    pos = Position(pos_id="ETH-x", coin="ETH", side=1, size=0.03, entry_px=3000, stop_px=2910, leverage=9,
                   leader=LEADER, k=0.001, open_oid=1, opened_ms=1)
    lg.append({"ev": "open", "pos": asdict(pos), "fee": 0.04})
    lg.append({"ev": "intent", "intent": "dead", "action": "open", "coin": "SOL"})   # crashed mid-order
    lg.close()
    hl.positions[LEADER] = {"ETH": 30.0}
    log.setup(None)
    bot = Bot(config.load(cdir, env=env_for(tg)))
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    try:
        assert wait_for(lambda: any("Restart with uncertainty" in m["text"] for m in tg.sent))
        assert bot.st.entries_paused
        msg = next(m["text"] for m in tg.sent if "uncertainty" in m["text"])
        assert "intent dead" in msg
        hl.mids["ETH"] = 2900.0                       # exits keep working while paused
        assert wait_for(lambda: "ETH" not in bot.st.positions)
        assert bot.st.closed[-1]["reason"] == "stop"
        tg.say("/resume")                             # owner acknowledges
        assert wait_for(lambda: not bot.st.entries_paused and not bot.st.uncertain)
    finally:
        bot.stop.set()
        th.join(5)
        bot.shutdown()
        hl.close()
        tg.close()
    # next restart is clean: the acknowledged issue does not pause again
    st = Ledger(data / "ledger.jsonl").replay()
    assert not st.uncertain and not st.entries_paused
