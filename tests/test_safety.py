"""Signing, keys and the exchange's order endpoint exist in ONE module, copybot/live.py (the live wallet, owner request
2026-11-03), and it can only place and cancel orders and set leverage: no withdrawal or transfer code anywhere. The
paper bot never loads a signing library."""
import re
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from copybot import config, log
from copybot.ledger import Ledger, Position
from copybot.runner import Bot
from tests.fakes import FakeHL, FakeDiscord
from tests.fakes_invo import FakeInvo
from tests.test_e2e import LEADER, env_for, wait_for, write_config

SRC = Path(__file__).resolve().parent.parent / "src" / "copybot"
FORBIDDEN = re.compile(r"eth_account|eth_keys|eth_keyfile|coincurve|ecdsa|nacl|web3|hyperliquid\.exchange|"
                       r"private_key|privkey|secret_key|mnemonic|/exchange\b|sign_l1|sign_typed", re.I)


LIVE = SRC / "live.py"
# what could move money out of the account or hand control of it to someone else
MONEY_OUT = re.compile(r"withdraw|transfer|usdSend|spotSend|sendAsset|approveAgent|approveBuilder|builder|vault"
                       r"Transfer|subAccount|tokenDelegate|cDeposit|cWithdraw|MultiSig|sign_user_signed|sign_agent|"
                       r"sign_usd|sign_spot|sign_send|sign_withdraw|sign_approve|sign_token|setReferrer|"
                       r"mnemonic|seed_phrase", re.I)
ACTIONS = {"order", "cancel", "updateLeverage"}           # the only signed actions
READS = {"userRole", "metaAndAssetCtxs", "clearinghouseState", "frontendOpenOrders", "userFillsByTime", "allMids",
         "orderStatus", "spotClearinghouseState", "userAbstraction"}      # (the last one: /livewallet shows the spot USDC, read-only)


def test_source_has_no_signing_or_keys_outside_the_live_module():
    for f in SRC.rglob("*.py"):
        if f == LIVE:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            assert not FORBIDDEN.search(line), f"{f.name}:{i}: {line.strip()}"
            assert not re.search(r"(environ|getenv|env\.get)\W.*HL_AGENT_KEY", line), f"{f.name}:{i}"   # key: live.py


def test_the_live_module_can_only_trade_never_move_money():
    text = LIVE.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        assert not MONEY_OUT.search(line), f"live.py:{i}: {line.strip()}"
    types = set(re.findall(r'"type":\s*"(\w+)"', text))
    assert types <= ACTIONS | READS, types - ACTIONS - READS
    assert "order_wires_to_order_action" in text and text.count("self.act(") == 3   # order, cancel, leverage
    assert "os.environ" in text and "HL_AGENT_KEY" in text                           # the key: environment only


def test_importing_the_bot_loads_no_signing_library():
    """(copybot/live.py imports the SDK only when the live wallet starts)"""
    code = ("import sys, copybot.runner; bad=[m for m in sys.modules if m.split('.')[0] in "
            "('eth_account','eth_keys','coincurve','ecdsa','nacl','web3','hyperliquid')]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_uncertain_restart_pauses_entries_alerts_and_still_runs_stops(tmp_path):
    hl, dc, f = FakeHL(), FakeDiscord(), FakeInvo()
    data = tmp_path / "data"
    data.mkdir()
    cdir = write_config(tmp_path, hl, dc, data)
    (cdir / "invo.toml").write_text(f'api_base = "{f.url}"\npoll_s = 1.0\n', encoding="utf-8")
    tok = tmp_path / "invo.token"
    tok.write_text("REFRESH0", encoding="utf-8")
    pid = f.add_user("nicush")[0]
    f.open_call(pid, "ETH", long=True, leverage=5, size=0.05, entry=3000.0)      # the call our copy follows
    w = data / "wallets" / "invo_calls" / "ledger.jsonl"
    lg = Ledger(w)
    lg.replay()
    lg.append({"ev": "genesis", "equity0": 300, "btc_px0": 100_000})
    lg.append({"ev": "follow", "leader": LEADER, "picked": True})
    pos = Position(pos_id="ETH-x", coin="ETH", side=1, size=0.03, entry_px=3000, stop_px=2910, leverage=9,
                   leader=LEADER, k=0.6, open_oid=1, opened_ms=1)
    lg.append({"ev": "open", "pos": asdict(pos), "fee": 0.04})
    lg.append({"ev": "intent", "intent": "dead", "action": "open", "coin": "SOL"})   # crashed mid-order
    lg.close()
    log.setup(None)
    bot = Bot(config.load(cdir, env={**env_for(dc), "INVO_TOKEN_FILE": str(tok)}))
    th = threading.Thread(target=bot.run, daemon=True)
    th.start()
    try:
        assert wait_for(lambda: any("restarted with uncertainty" in m["text"] for m in dc.sent))
        assert bot.invo.st.entries_paused
        msg = next(m["text"] for m in dc.sent if "uncertainty" in m["text"])
        assert "intent dead" in msg
        hl.mids["ETH"] = 2900.0                       # exits keep working while paused
        assert wait_for(lambda: "ETH" not in bot.invo.st.positions)
        assert bot.invo.st.closed[-1]["reason"] == "stop"
        dc.say("/resume")                             # owner acknowledges
        assert wait_for(lambda: not bot.invo.st.entries_paused and not bot.invo.st.uncertain)
    finally:
        bot.stop.set()
        th.join(5)
        bot.shutdown()
        hl.close()
        dc.close()
        f.close()
    # next restart is clean: the acknowledged issue does not pause again
    st = Ledger(w).replay()
    assert not st.uncertain and not st.entries_paused
