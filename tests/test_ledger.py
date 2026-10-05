import json
import random
import subprocess
import sys
import time
from dataclasses import asdict

from copybot.ledger import Ledger, Position, State


def _pos(coin="BTC", side=1, size=0.01, entry=100.0, stop=97.0, pid="p1"):
    return asdict(Position(pos_id=pid, coin=coin, side=side, size=size, entry_px=entry, stop_px=stop,
                           leverage=5, leader="0xabc", k=0.001, open_oid=1, opened_ms=1))


def _live(ledger: Ledger, st: State, ev: dict):
    st.apply(ledger.append(ev))


def test_replay_reproduces_live_state(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    st = lg.replay()
    _live(lg, st, {"ev": "genesis", "equity0": 300, "btc_px0": 50000})
    _live(lg, st, {"ev": "follow", "leader": "0xabc"})
    _live(lg, st, {"ev": "intent", "intent": "i1", "action": "open", "coin": "BTC"})
    _live(lg, st, {"ev": "open", "intent": "i1", "pos": _pos(size=1.0), "fee": 0.045, "lag_ms": 800})
    _live(lg, st, {"ev": "add", "coin": "BTC", "pos_id": "p1", "sz": 1.0, "px": 102.0, "fee": 0.05, "lag_ms": 900})
    _live(lg, st, {"ev": "funding", "coin": "BTC", "pos_id": "p1", "amount": -0.01})
    _live(lg, st, {"ev": "reduce", "coin": "BTC", "pos_id": "p1", "sz": 0.5, "px": 104.0, "fee": 0.02})
    _live(lg, st, {"ev": "close", "coin": "BTC", "pos_id": "p1", "px": 99.0, "fee": 0.03, "reason": "leader_close"})
    lg.close()
    st2 = Ledger(tmp_path / "l.jsonl").replay()
    assert st2.uncertain == []
    assert st2.positions == st.positions == {}
    assert abs(st2.realized - st.realized) < 1e-12
    # entry avg 101; reduce 0.5 @104 -> +1.5 ; close 1.5 @99 -> -3.0 ; fees .145 ; funding -.01
    assert abs(st2.realized - (1.5 - 3.0 - 0.145 - 0.01)) < 1e-9
    assert st2.closed[0]["pnl"] == st.closed[0]["pnl"]
    assert st2.lags_ms == [800, 900]
    assert st2.leader_stats["0xabc"].consec_losses == 1


def test_open_position_and_stop_survive_restart(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    st = lg.replay()
    _live(lg, st, {"ev": "open", "pos": _pos(coin="ETH", side=-1, entry=2000, stop=2060, pid="e1"), "fee": 0.1})
    lg.close()
    st2 = Ledger(tmp_path / "l.jsonl").replay()
    p = st2.positions["ETH"]
    assert (p.side, p.stop_px, p.entry_px) == (-1, 2060, 2000)
    assert st2.uncertain == []


def test_truncated_tail_is_flagged_and_cut(tmp_path):
    path = tmp_path / "l.jsonl"
    lg = Ledger(path)
    lg.append({"ev": "genesis", "equity0": 300})
    lg.close()
    with open(path, "ab") as f:
        f.write(b'{"seq":2,"ev":"open","pos":{"co')
    st = Ledger(path).replay()
    assert any("truncated" in u for u in st.uncertain)
    assert path.read_bytes().endswith(b"\n")  # torn tail removed; next append starts on a clean line
    lg = Ledger(path)
    lg.replay()
    lg.append({"ev": "boot"})
    lg.close()
    assert Ledger(path).replay().uncertain == []


def test_intent_without_result_is_uncertain(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    lg.append({"ev": "intent", "intent": "x", "action": "open", "coin": "SOL"})
    lg.close()
    st = Ledger(tmp_path / "l.jsonl").replay()
    assert any("intent x" in u for u in st.uncertain)


def test_position_without_valid_stop_is_uncertain(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    lg.append({"ev": "open", "pos": _pos(entry=100, stop=0), "fee": 0})
    lg.close()
    st = Ledger(tmp_path / "l.jsonl").replay()
    assert any("no valid stop" in u for u in st.uncertain)


def test_event_for_unknown_position_is_uncertain(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    lg.append({"ev": "close", "coin": "BTC", "pos_id": "nope", "px": 1, "fee": 0})
    lg.close()
    assert Ledger(tmp_path / "l.jsonl").replay().uncertain


def test_double_open_same_coin_is_refused(tmp_path):
    lg = Ledger(tmp_path / "l.jsonl")
    lg.append({"ev": "open", "pos": _pos(pid="a"), "fee": 0})
    lg.append({"ev": "open", "pos": _pos(pid="b"), "fee": 0})
    lg.close()
    st = Ledger(tmp_path / "l.jsonl").replay()
    assert st.positions["BTC"].pos_id == "a"
    assert any("already-open" in u for u in st.uncertain)


WRITER = r"""
import sys
from dataclasses import asdict
from copybot.ledger import Ledger, Position
lg = Ledger(sys.argv[1]); st = lg.replay()
i = st.seq
for iid in list(st.open_intents):
    st.apply(lg.append({"ev": "intent_abort", "intent": iid}))
for p in list(st.positions.values()):
    st.apply(lg.append({"ev": "close", "coin": p.coin, "pos_id": p.pos_id, "px": 101, "fee": 0.01}))
while True:
    i += 1
    pid = f"p{i}"
    pos = asdict(Position(pos_id=pid, coin="BTC", side=1, size=1, entry_px=100, stop_px=97, leverage=3,
                          leader="0xa", k=1, open_oid=i, opened_ms=i))
    st.apply(lg.append({"ev": "intent", "intent": pid, "action": "open", "coin": "BTC"}))
    st.apply(lg.append({"ev": "open", "intent": pid, "pos": pos, "fee": 0.01}))
    st.apply(lg.append({"ev": "close", "coin": "BTC", "pos_id": pid, "px": 101, "fee": 0.01}))
    print("ok", flush=True)
"""


def test_kill_minus_9_during_writes_never_corrupts(tmp_path):
    """Kill the writer at random points many times; every replay must be self-consistent."""
    path = tmp_path / "l.jsonl"
    rng = random.Random(7)
    for round_ in range(8):
        proc = subprocess.Popen([sys.executable, "-c", WRITER, str(path)], stdout=subprocess.PIPE)
        proc.stdout.readline()  # at least one full cycle written
        time.sleep(rng.uniform(0.0, 0.3))
        proc.kill()  # TerminateProcess on Windows / SIGKILL on POSIX
        proc.wait()
        st = Ledger(path).replay()
        # at most one open position (the one in flight), and it always has its stop
        assert len(st.positions) <= 1
        for p in st.positions.values():
            assert p.stop_px == 97
        # the only acceptable uncertainty is the in-flight write at the moment of the kill
        for u in st.uncertain:
            assert ("truncated" in u) or ("has no recorded result" in u), u
        closed = len(st.closed)
        assert abs(st.realized - closed * (1 - 0.02) + (0.01 if st.positions else 0)) < 1e-6
        # every line on disk is valid json after replay cut the torn tail
        for line in path.read_bytes().splitlines():
            json.loads(line)
