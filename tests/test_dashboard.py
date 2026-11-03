"""The read-only web dashboard (copybot/dashboard.py): real ledger events written to tmp files, the real server on a
free port, prices stubbed (no network). Numbers, ranking, the live wallet vs its twin, torn lines, 405s, escaping."""
import http.client
import json
import shutil
import threading
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

import pytest

from copybot import config, dashboard
from copybot.ledger import Position

REPO = Path(__file__).resolve().parent.parent
DAY = 86_400_000
T0 = 1_760_000_000_000                 # the live wallet's start
EVIL = "invo:<script>alert(1)</script>"


def pos(coin, side, size, entry, stop, leader="invo:nicush", pid=None, opened=T0):
    return asdict(Position(pos_id=pid or coin + "-1", coin=coin, side=side, size=size, entry_px=entry, stop_px=stop,
                           leverage=5, leader=leader, k=1.0, open_oid=1, opened_ms=opened))


def write(root: Path, name: str, events: list[dict]) -> Path:
    path = root / "data" / "wallets" / name / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for i, ev in enumerate(events, 1):
            f.write(json.dumps({"seq": i, "ts": ev.pop("ts", T0), **ev}) + "\n")
    return path


def genesis(eq, ts):
    return {"ev": "genesis", "equity0": eq, "btc_px0": 100_000, "ts": ts}


@pytest.fixture
def root(tmp_path):
    return build(tmp_path)


def build(tmp_path: Path) -> Path:
    """The repo's config and four wallets' ledgers (also used to look at the page by hand)."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    cfg = config.load(tmp_path / "config", env={})
    assert cfg.live.copy_wallet == "invo_risk_5pct_stop8" and cfg.live.network == "testnet"
    assert cfg.discord.utc_offset_hours == -3
    # the twin: +30 before the live start (330$ at T0), +16.5 after it -> +5% since the live start
    write(tmp_path, "invo_risk_5pct_stop8", [
        genesis(300, T0 - 10 * DAY),
        {"ev": "open", "pos": pos("SOL", 1, 1.0, 100, 92, opened=T0 - 5 * DAY), "fee": 0.0, "ts": T0 - 5 * DAY},
        {"ev": "close", "coin": "SOL", "pos_id": "SOL-1", "px": 130, "fee": 0.0, "reason": "leader_close",
         "ts": T0 - 4 * DAY},
        {"ev": "open", "pos": pos("SOL", 1, 1.0, 100, 92, pid="SOL-2"), "fee": 0.0, "ts": T0 + 1000},
        {"ev": "close", "coin": "SOL", "pos_id": "SOL-2", "px": 116.5, "fee": 0.0, "reason": "leader_close",
         "ts": T0 + DAY},
    ])
    # the main wallet: nicush +3 closed, an ETH copy open (100$ at 3000), a trader with a hostile name followed
    write(tmp_path, "invo_calls", [
        genesis(300, T0 - 10 * DAY),
        {"ev": "follow", "leader": "invo:nicush", "picked": True, "ts": T0 - 10 * DAY},
        {"ev": "follow", "leader": EVIL, "ts": T0 - 2 * DAY},
        {"ev": "open", "pos": pos("BTC", -1, 0.001, 50_000, 54_000), "fee": 0.0},
        {"ev": "close", "coin": "BTC", "pos_id": "BTC-1", "px": 47_000, "fee": 0.0, "reason": "leader_close",
         "ts": T0 + 2 * DAY},
        {"ev": "open", "pos": pos("ETH", 1, 0.01, 3000, 2760), "fee": 0.0},
    ])
    write(tmp_path, "invo_risk_1pct", [            # -6$ = -2%
        genesis(300, T0 - 10 * DAY),
        {"ev": "open", "pos": pos("SOL", 1, 1.0, 100, 92), "fee": 0.0},
        {"ev": "close", "coin": "SOL", "pos_id": "SOL-1", "px": 94, "fee": 0.0, "reason": "stop", "ts": T0 + DAY},
    ])
    # the live wallet (100$): BTC +5 minus fees closed, ETH open, day mark 105$, paused by the daily stop
    write(tmp_path, "live", [
        genesis(100, T0),
        {"ev": "mark", "kind": "day", "key": "2025-10-10", "equity": 105.0},
        {"ev": "open", "pos": pos("BTC", 1, 0.001, 50_000, 46_000), "fee": 0.0225},
        {"ev": "close", "coin": "BTC", "pos_id": "BTC-1", "px": 55_000, "fee": 0.02475, "reason": "leader_close",
         "ts": T0 + DAY},
        {"ev": "open", "pos": pos("ETH", 1, 0.01, 3000, 2760, pid="ETH-L"), "fee": 0.0135},
        {"ev": "pause", "reason": "live_daily_stop:2025-10-10"},
    ])
    return tmp_path


class StubPrices:
    """allMids stand-in: mainnet and testnet prices differ; `fail` makes every fetch raise."""

    def __init__(self):
        self.calls, self.fail = [], False

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("no network in tests")
        return {"ETH": 3100.0, "BTC": 50_000.0} if "testnet" not in url else {"ETH": 3200.0, "BTC": 51_000.0}


@pytest.fixture
def stub():
    return StubPrices()


@pytest.fixture
def server(root, stub):
    dash = dashboard.Dashboard(root, prices=dashboard.Prices(stub, ttl=0), clock=lambda: T0 + 3 * DAY)
    srv = dashboard.make_server(dash, port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers, r.read().decode()


def summary(base):
    return json.loads(get(base + "/api/summary")[2])


def snapshot(root):
    return {p: p.read_bytes() for p in sorted((root / "data").rglob("*")) if p.is_file()}


def test_the_live_wallet_balance_today_pause_and_open_pnl_at_testnet_prices(server, stub):
    s = summary(server)
    lv = s["live"]
    # 100 + (5 - 0.0225 - 0.02475) closed - 0.0135 ETH entry fee + 0.01 x (3200 - 3000) at the TESTNET price
    assert lv["equity"] == pytest.approx(100 + 4.95275 - 0.0135 + 2.0)
    assert lv["pnl"] == pytest.approx(6.93925) and lv["return_pct"] == pytest.approx(6.93925)
    assert lv["today"] == pytest.approx(lv["equity"] - 105.0)
    assert lv["paused"] and lv["pause_reason"] == "live_daily_stop:2025-10-10"
    assert lv["floor"] == 50.0 and not lv["below_floor"] and lv["network"] == "testnet"
    [p] = lv["positions"]
    assert (p["coin"], p["side"], p["entry"], p["mark"], p["stop"]) == ("ETH", "long", 3000, 3200.0, 2760)
    assert p["pnl"] == pytest.approx(2.0 - 0.0135)
    [c] = lv["closed"]
    assert c["coin"] == "BTC" and c["pnl"] == pytest.approx(4.95275)
    assert [v for _, v in lv["curve"]] == pytest.approx([100.0, 104.95275, lv["equity"]])
    assert dashboard.INFO_URLS["testnet"] in stub.calls and dashboard.INFO_URLS["mainnet"] in stub.calls


def test_paper_wallets_are_ranked_by_return_with_the_live_twin_marked(server):
    s = summary(server)
    names = [r["name"] for r in s["paper"]]
    assert names == ["invo_risk_5pct_stop8", "invo_calls", "invo_risk_1pct"]
    twin, main, one = s["paper"]
    assert twin["twin"] and not main["twin"] and twin["rank"] == 1
    assert twin["return_pct"] == pytest.approx(46.5 / 300 * 100) and twin["trades"] == 2 and twin["win_rate"] == 100
    assert main["pnl"] == pytest.approx(3.0 + 0.01 * 100) and main["open"] == 1        # ETH at the mainnet 3100
    assert one["return_pct"] == pytest.approx(-2.0) and one["win_rate"] == 0
    assert one["max_drop_pct"] == pytest.approx(2.0)
    assert (twin["size"], twin["stop"]) == ("5% margin", "8% stop")
    assert (main["size"], main["stop"]) == ("5% margin (main)", "trader's exit")
    page = get(server + "/")[2]
    assert 'class="twin"' in page and "live twin" in page


def test_live_vs_its_twin_in_percent_since_the_live_start(server):
    tw = summary(server)["twin"]
    assert tw["paper_start"] == pytest.approx(330.0) and tw["live_start"] == 100
    assert tw["paper_pct"] == pytest.approx(5.0)                                    # 16.5 / 330
    assert tw["live_pct"] == pytest.approx(6.93925)
    assert tw["gap_pct"] == pytest.approx(1.93925)
    assert tw["paper_curve"][0][1] == 0 and tw["paper_curve"][-1][1] == pytest.approx(5.0)


def test_traders_open_and_closed_trades_across_wallets(server):
    s = summary(server)
    t = {r["trader"]: r for r in s["traders"]}
    assert t["@nicush"]["made"] == pytest.approx(3.0 + 1.0) and t["@nicush"]["trades"] == 1
    assert t["@nicush"]["open"] == 1 and t["@nicush"]["win_rate"] == 100
    assert EVIL[5:] in [r["trader"][1:] for r in s["traders"]] and t["@" + EVIL[5:]]["trades"] == 0
    assert sorted((r["wallet"], r["coin"]) for r in s["open_trades"]) == [("invo_calls", "ETH"), ("live", "ETH")]
    closed = s["closed_trades"]
    assert [r["closed_ms"] for r in closed] == sorted((r["closed_ms"] for r in closed), reverse=True)
    assert len(closed) == 5 and closed[0]["wallet"] == "invo_calls"           # the BTC short closed at T0 + 2 days


def test_every_text_is_escaped(server):
    page = get(server + "/")[2]
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    frag = get(server + "/?fragment=1")[2]
    assert "<script>alert" not in frag and "<html" not in frag and "Paper wallets" in frag


def test_a_torn_last_line_is_ignored_and_no_file_is_ever_written(root, server):
    before = summary(server)
    path = root / "data" / "wallets" / "live" / "ledger.jsonl"
    with open(path, "ab") as f:
        f.write(b'{"seq":99,"ev":"close","coin":"ETH","pos_id":"ETH-L","px":40')      # the bot mid-write
    snap = snapshot(root)
    after = summary(server)
    assert after["torn"] == ["live"]
    assert after["live"]["equity"] == pytest.approx(before["live"]["equity"])
    assert after["live"]["positions"] and after["live"]["uncertain"] == 0
    for _ in range(2):
        get(server + "/")
    assert snapshot(root) == snap                                                    # read-only: nothing changed
    assert not (root / "data" / "bot.lock").exists() and not (root / "data" / "ledger.jsonl").exists()


def test_a_ledger_is_folded_again_only_when_it_changes(root, server):
    cache = dashboard.LedgerCache()
    path = root / "data" / "wallets" / "live" / "ledger.jsonl"
    a = cache.get(path)
    assert cache.get(path) is a
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"seq": 50, "ts": T0 + 2 * DAY, "ev": "resume"}) + "\n")
    b = cache.get(path)
    assert b is not a and a.st.entries_paused and not b.st.entries_paused
    assert cache.get(root / "missing.jsonl") is None


def test_no_prices_means_entry_values_and_a_stale_banner(server, stub):
    stub.fail = True
    s = summary(server)
    assert not s["prices"]["ok"] and not s["prices"]["live_ok"]
    assert s["live"]["equity"] == pytest.approx(100 + 4.95275 - 0.0135)             # ETH counted at its entry
    assert s["live"]["positions"][0]["mark"] is None
    assert "Prices are stale" in get(server + "/")[2]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "FOO"])
def test_nothing_but_get_is_allowed(server, root, method):
    snap = snapshot(root)
    host = server.split("//")[1]
    c = http.client.HTTPConnection(host, timeout=5)
    c.request(method, "/api/summary", body=b'{"ev":"pause"}', headers={"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 405 and r.getheader("Allow") == "GET, HEAD"
    c.close()
    assert snapshot(root) == snap


def test_headers_404_and_no_secret_from_the_environment(server, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-SECRET-123")
    monkeypatch.setenv("COPYBOT_PIN", "pin-SECRET-456")
    status, headers, page = get(server + "/")
    assert status == 200 and headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    body = page + get(server + "/api/summary")[2]
    assert "SECRET" not in body
    with pytest.raises(urllib.error.HTTPError) as ei:
        get(server + "/data/wallets/live/ledger.jsonl")
    assert ei.value.code == 404


def test_no_live_wallet_and_no_data_still_render(tmp_path, stub):
    shutil.copytree(REPO / "config", tmp_path / "config")
    dash = dashboard.Dashboard(tmp_path, prices=dashboard.Prices(stub, ttl=0))
    s = dash.summary()
    assert s["live"] is None and s["twin"] is None and s["paper"] == []
    assert "No live wallet yet" in dashboard.render_page(s)
    assert not (tmp_path / "data").exists()


def test_the_18_paper_wallets_are_named_as_the_bot_names_them():
    cfg = config.load(REPO / "config", env={})
    names = [n for n, _, _ in dashboard.paper_wallets(cfg)]
    assert len(names) == len(set(names)) == 18
    assert {"invo_calls", "invo_risk_20pct", "invo_calls_stop3", "invo_risk_5pct_stop8", cfg.live.copy_wallet} \
        <= set(names)
