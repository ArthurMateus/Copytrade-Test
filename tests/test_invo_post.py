"""Our trades as Invo share predictions (owner request 2026-11-10; copybot/invo_post.py): one wallet's trades become
paper calls in a paper portfolio of the bot's own Invo account, resized and closed with the trade. Invo's endpoints
are faked from the web app's code (tests/fakes_invo.py); no money is ever on Invo."""
import json
import threading

import pytest

from copybot import invo
from copybot.invo_post import Poster
from tests.fakes_invo import FakeInvo
from tests.test_e2e import env, stop_bot, wait_for  # noqa: F401 (env is a fixture)
from tests.test_invo import invo_env, start_with_invo  # noqa: F401 (invo_env is a fixture)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def fake(tmp_path):
    f = FakeInvo()
    yield f
    f.close()


def poster(fake, tmp_path, clock=None, title="Lockstep signals"):
    tok = tmp_path / "invo.token"
    if not tok.exists():
        tok.write_text("REFRESH0", encoding="utf-8")
    alerts = []
    p = Poster(invo.InvoClient(fake.url, str(tok), min_interval_s=0), "@lockstep", title,
               tmp_path / "invo_posts.json", threading.Event(), alert=alerts.append, clock=clock or Clock())
    return p, alerts


def our_portfolio(fake, title="Lockstep signals"):
    pid = fake.add_user("lockstep")[0]
    fake.port_meta[pid] = {"title": title}
    return pid


def drain(p):
    for _ in range(50):
        if p.jobs.empty():
            return
        p.step()


def created(fake):
    return [b for path, b in fake.requests if path == "/investments/ticker/create"]


def test_a_trade_becomes_a_prediction_that_is_resized_and_closed_with_it(fake, tmp_path):
    pid = our_portfolio(fake)
    p, alerts = poster(fake, tmp_path)
    p.open("pos-1", "ETH", True, 0.05, 2760.0, 5)          # 5% of the wallet as margin, 8% stop at 5x
    drain(p)
    (body,) = created(fake)
    assert body == {"ticker": "ETH", "portfolioId": pid, "directionLong": True, "entrySim": 500.0,
                    "priceTarget": None, "stopLoss": 2760.0, "leverage": 5, "liquidationPrice": None}
    assert fake.sims[pid] == 9500.0 and len(fake.calls[pid]) == 1
    saved = json.loads((tmp_path / "invo_posts.json").read_text(encoding="utf-8"))
    assert saved["pos-1"]["base"].startswith("base-")       # kept for a restart

    p.resize("pos-1", 0.051, 2760.0)                        # < 5% change: not posted
    drain(p)
    assert not any(path == "/investments/ticker/modify" for path, _ in fake.requests)
    p.resize("pos-1", 0.10, 2800.0)                         # doubled: +5% of the portfolio's 10,000
    drain(p)
    mod = next(b for path, b in fake.requests if path == "/investments/ticker/modify")
    assert mod["simIncrease"] is True and mod["simDifference"] == 500.0 and mod["stopLoss"] == 2800.0
    assert fake.sims[pid] == 9000.0

    p.close("pos-1")
    drain(p)
    assert any(path == "/investments/sell" for path, _ in fake.requests)
    assert not fake.calls[pid] and len(fake.closed[pid]) == 1 and fake.sims[pid] == 10_000.0
    assert p.calls == {} and json.loads((tmp_path / "invo_posts.json").read_text(encoding="utf-8")) == {}
    assert not alerts


def test_after_a_restart_calls_whose_trade_closed_meanwhile_are_closed(fake, tmp_path):
    pid = our_portfolio(fake)
    p, _ = poster(fake, tmp_path)
    p.open("pos-1", "ETH", True, 0.05, 2760.0, 5)
    p.open("pos-2", "SOL", False, 0.05, 160.0, 5)
    drain(p)
    assert len(fake.calls[pid]) == 2
    p2, _ = poster(fake, tmp_path)                          # the bot restarted: SOL closed while it was off
    assert set(p2.calls) == {"pos-1", "pos-2"}
    p2.sync({"pos-1"})
    drain(p2)
    assert [c["ticker"] for c in fake.calls[pid]] == ["ETH"] and set(p2.calls) == {"pos-1"}


def test_a_refused_close_is_retried_and_the_owner_told_once(fake, tmp_path):
    pid = our_portfolio(fake)
    clock = Clock()
    p, alerts = poster(fake, tmp_path, clock)
    p.open("pos-1", "ETH", True, 0.05, 2760.0, 5)
    drain(p)
    fake.refuse_posts = True
    p.close("pos-1")
    for _ in range(4):                                      # refused 4 times, retries after 5, 30, 120 s ...
        drain(p)
        clock.t += 700
        p.step()
    assert len(alerts) == 1 and "ETH" in alerts[0]
    assert "pos-1" in p.calls and fake.calls[pid]           # still open: never forgotten
    fake.refuse_posts = False
    clock.t += 700
    p.step()
    drain(p)
    assert not fake.calls[pid] and "pos-1" not in p.calls


def test_an_entry_that_cannot_be_posted_in_time_is_dropped(fake, tmp_path):
    clock = Clock()
    p, alerts = poster(fake, tmp_path, clock)               # the portfolio does not exist yet
    p.open("pos-1", "ETH", True, 0.05, 2760.0, 5)
    drain(p)
    assert not created(fake) and len(alerts) == 1 and "lockstep" in alerts[0]
    our_portfolio(fake)
    clock.t += 200                                          # found later, but the entry is 200 s old: not posted
    for _ in range(5):
        p.step()
    clock.t += 61
    p.open("pos-2", "SOL", True, 0.05, 160.0, 5)
    drain(p)
    assert [b["ticker"] for b in created(fake)] == ["SOL"] and len(alerts) == 1


def test_a_real_money_portfolio_is_never_used(fake, tmp_path):
    fake.add_user("lockstep", 0)
    fake.add_live("lockstep")
    p, alerts = poster(fake, tmp_path, title="Live Portfolio")
    p.open("pos-1", "ETH", True, 0.05, 2760.0, 5)
    drain(p)
    assert not created(fake) and alerts and "not a paper portfolio" in alerts[0]


def test_the_bot_posts_the_signal_wallets_trades_on_invo(invo_env, monkeypatch):
    hl, dc, data, cdir, f, tok = invo_env
    with open(cdir / "invo.toml", "a", encoding="utf-8") as fh:
        fh.write('post_username = "lockstep"\npost_portfolio = "Lockstep"\n')
    ours = our_portfolio(f)
    pid = f.add_user("nicush")[0]
    bot, th = start_with_invo((hl, dc, data, cdir), tok, monkeypatch)
    try:
        assert bot.invo_poster is not None
        dc.say("/follow nicush")
        assert wait_for(lambda: bot.invo_watch.known.get("nicush") is not None, timeout=10)
        cid = f.open_call(pid, "ETH", long=True, leverage=10, size=0.05, entry=2990.0)
        w = bot.invo_post_wallet()
        assert wait_for(lambda: any(c["ticker"] == "ETH" for c in f.calls[ours]), timeout=15)
        (body,) = created(f)
        (p,) = w.st.positions.values()
        assert body["directionLong"] is True and body["leverage"] == p.leverage and body["stopLoss"] == p.stop_px
        assert body["entrySim"] == pytest.approx(10_000 * p.size * p.entry_px / p.leverage / w.st.equity0, rel=0.02)
        f.close_call(pid, cid)                               # the trader closes: our trade and our prediction close
        assert wait_for(lambda: not f.calls[ours] and len(f.closed[ours]) == 1, timeout=15)
    finally:
        stop_bot(bot, th)


def test_kpepe_is_posted_as_pepe():
    from copybot.runner import Bot
    assert Bot.invo_ticker("kPEPE") == ("PEPE", 1000.0)
    assert Bot.invo_ticker("ETH") == ("ETH", 1.0) and Bot.invo_ticker("kNEIRO")[0] == "NEIRO"
