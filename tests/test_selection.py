import queue
import time

import pytest

from copybot import config, hl
from copybot.selection import HOUR, Scorer, select
from tests.fakes import FakeHL
from tests.gen import trader

CFG = config.load("config", env={})
NOW = 1_800_000_000_000


def run(cycles, followed=None, paused=(), dropped=None, sel=None, start=NOW, step=HOUR):
    """Apply a sequence of rankings; returns the final followed set and the plans."""
    followed = dict(followed or {})
    dropped = dict(dropped or {})
    sel = sel or {}
    plans = []
    now = start
    for ranking in cycles:
        p = select(sel, ranking, followed, set(paused), dropped, now, CFG)
        for a, _ in p.drops:
            followed.pop(a)
            dropped[a] = now
        for a in p.joins:
            followed[a] = now
        sel = p.state
        plans.append(p)
        now += step
    return followed, plans, sel


R = [f"0x{i:02d}" for i in range(1, 30)]


def test_join_needs_two_consecutive_cycles_at_rank_8_or_better():
    f, plans, _ = run([R[:10]])
    assert f == {}
    f, plans, _ = run([R[:10], R[:10]])
    assert set(f) == set(R[:7])           # max 7 leaders, free slots fill in one cycle
    f, _, _ = run([R[:10], list(reversed(R[:10]))])
    assert set(f) == set(R[2:8])         # only wallets at rank <= 8 in BOTH cycles


def test_zero_eligible_follows_nobody():
    f, plans, _ = run([[], [], []])
    assert f == {} and all(not p.joins for p in plans)


def test_drop_after_two_cycles_beyond_15_and_min_24h_then_one_swap():
    start_follow = {a: NOW - 48 * HOUR for a in R[:7]}
    bad = R[7:22] + R[:7]               # our 7 are now ranks 16-22
    f, plans, _ = run([bad], followed=start_follow)
    assert set(f) == set(R[:7])         # one cycle is not enough
    f, plans, _ = run([bad, bad], followed=start_follow)
    assert len(plans[1].drops) == 1 and len(plans[1].joins) == 1     # exactly one swap
    assert len(f) == 7


def test_min_follow_time_protects_new_leaders():
    recent = {a: NOW - 2 * HOUR for a in R[:7]}
    bad = R[7:22] + R[:7]
    f, plans, _ = run([bad, bad, bad], followed=recent)
    assert set(f) == set(R[:7]) and not any(p.drops for p in plans)


def test_no_flip_flop_between_rank_8_and_15():
    followed = {a: NOW - 48 * HOUR for a in R[:7]}
    # leaders wander anywhere between ranks 1 and 15: never dropped
    import random
    rng = random.Random(1)
    rankings = []
    for _ in range(12):
        top = R[:15]
        rng.shuffle(top)
        rankings.append(top + R[15:])
    f, plans, _ = run(rankings, followed=followed)
    assert set(f) == set(R[:7])


def test_paused_leader_is_dropped_first_even_if_new():
    followed = {a: NOW - HOUR for a in R[:7]}
    f, plans, _ = run([R[:10], R[:10]], followed=followed, paused={R[3]})
    assert plans[0].drops == [(R[3], "paused after a bad streak")]
    assert R[3] not in f


def test_dropped_leader_cooldown():
    followed = {a: NOW - 48 * HOUR for a in R[1:7]}
    f, plans, _ = run([R[:10], R[:10]], followed=followed, dropped={R[0]: NOW - HOUR})
    assert R[0] not in f


def make_rows(addrs):
    rows = []
    for a in addrs:
        rows.append({"ethAddress": a, "accountValue": "200000", "windowPerformances": [
            ["day", {"pnl": "1000", "roi": "0", "vlm": "500000"}],
            ["week", {"pnl": "3000", "roi": "0", "vlm": "2000000"}],
            ["month", {"pnl": "20000", "roi": "0", "vlm": "8000000"}],
            ["allTime", {"pnl": "150000", "roi": "0", "vlm": "60000000"}]], "prize": 0, "displayName": None})
    return {"leaderboardRows": rows}


@pytest.fixture
def scorer_env(tmp_path):
    fake = FakeHL()
    now = (int(time.time() * 1000) // HOUR) * HOUR
    good = [f"0x{i:040x}" for i in range(1, 14)]
    bad = [f"0x{i:040x}" for i in range(100, 103)]
    for i, a in enumerate(good):
        raw, cs = trader(now, seed=i + 1, trips=450, win=0.78)
        fake.fills[a] = raw
        fake.candles.update(cs)
    for i, a in enumerate(bad):
        raw, _ = trader(now, seed=50 + i, trips=450, win=0.3)
        fake.fills[a] = raw
    fake.leaderboard = make_rows(good + bad)
    cfg = config.load("config", env={})
    cfg.runtime.leaderboard_url = fake.lb_url
    info = hl.Info(fake.info_url, hl.RateBudget(1_000_000, 300))
    yield fake, cfg, info, tmp_path, good, bad
    fake.close()


def test_scorer_scores_publishes_ranking_and_resumes_from_cache(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    out = queue.Queue()
    sc = Scorer(cfg, info, out, tmp / "cache")
    sc.review(weekly=True)
    msgs = []
    while not out.empty():
        msgs.append(out.get())
    rankings = [m for m in msgs if m[0] == "ranking"]
    assert rankings, msgs
    _, ranking, n_scored, scores = rankings[-1]
    assert n_scored >= 12
    assert set(ranking) <= set(good) and len(ranking) >= 10
    assert not (set(ranking) & set(bad))
    assert ("review", True) == msgs[-1][:2]
    # candles for a coin are downloaded once and shared by every wallet (cache)
    candle_reqs = [r for r in fake.requests if r["type"] == "candleSnapshot"]
    assert len(candle_reqs) <= 3 * 8
    # restart: a new scorer resumes from disk without re-screening anyone
    n_before = len(fake.requests)
    out2 = queue.Queue()
    sc2 = Scorer(cfg, info, out2, tmp / "cache")
    assert len(sc2.scores) == len(sc.scores)
    sc2.review(weekly=True)   # even a weekly review does not re-screen what was screened this week
    assert sum(1 for r in fake.requests[n_before:] if r["type"] == "userFillsByTime") <= len(good) + 2
    assert all(r["type"] != "candleSnapshot" or r["req"]["endTime"] > sc.now() - 31 * 86_400_000
               for r in fake.requests[n_before:])   # closed candle chunks come from the disk cache


def test_scorer_restart_mid_review_resumes(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    sc = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    calls = {"n": 0}
    real = sc.screen_and_score

    def stop_after_5(a, av):
        calls["n"] += 1
        real(a, av)
        if calls["n"] == 5:
            sc.stop.set()   # the process dies in the middle of the review
    sc.screen_and_score = stop_after_5
    sc.review(weekly=True)
    first = {a for a in sc.screened}
    assert len(first) == 5
    n_before = len(fake.requests)
    sc2 = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    sc2.review(weekly=True)
    pages = [r["user"] for r in fake.requests[n_before:] if r["type"] == "userFillsByTime"
             and r["startTime"] < sc2.now() - 170 * 86_400_000]
    assert not (set(pages) & {a for a in first if not sc.screened[a]["ok"]})   # rejected ones not fetched again
    assert len(sc2.screened) == len(good) + len(bad)


def test_scorer_rejects_hash_coin_candles_gracefully(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    sc = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    assert sc.candles("#140", NOW - 40 * 86_400_000, NOW) == []
