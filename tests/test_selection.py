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


def test_join_needs_two_consecutive_cycles_in_the_top_7():
    f, plans, _ = run([R[:10]])
    assert f == {}
    f, plans, _ = run([R[:10], R[:10]])
    assert set(f) == set(R[:7])           # max 7 leaders, free slots fill in one cycle
    f, _, _ = run([R[:10], list(reversed(R[:10]))])
    assert set(f) == set(R[3:7])         # only wallets at rank <= 7 in BOTH cycles


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


def test_paused_leader_is_dropped_at_once_even_if_new():
    followed = {a: NOW - HOUR for a in R[:7]}
    f, plans, _ = run([R[:10], R[:10]], followed=followed, paused={R[3], R[4]})
    assert plans[0].drops == [(R[3], "paused after a bad streak"), (R[4], "paused after a bad streak")]
    assert R[3] not in f and R[4] not in f
    assert not any(p.joins for p in plans)      # replacements wait for the daily change window


def test_a_leader_that_no_longer_passes_the_rules_leaves_after_two_cycles():
    followed = {a: NOW - HOUR for a in R[:7]}   # followed only an hour ago: not protected
    ranking = R[:4]                             # R[4..6] are no longer eligible
    f, plans, _ = run([ranking], followed=followed)
    assert not plans[0].drops                   # one cycle is not enough
    f, plans, _ = run([ranking, ranking], followed=followed)
    assert {a for a, _ in plans[1].drops} == set(R[4:7]) and set(f) == set(R[:4])


def test_new_leaders_join_at_most_once_a_day():
    followed = {a: NOW - 2 * HOUR for a in R[:5]}   # two free slots, last join 2 hours ago
    f, plans, _ = run([R[:10]] * 21, followed=followed)
    assert set(f) == set(R[:5])                     # 20 hours later: still the same five
    f, plans, _ = run([R[:10]] * 24, followed=followed)
    assert set(f) == set(R[:7])                     # once 24 h passed since the last join, both slots fill
    joins_at = [i for i, p in enumerate(plans) if p.joins]
    assert len(joins_at) == 1


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


def test_scorer_stops_at_the_pool_size(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    cfg.selection.pool_size = 7
    sc = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    sc.review(weekly=True)
    assert len(sc.scores) == 7 and set(sc.scores) <= set(good)
    assert all(1 <= d["score"] <= 100 for d in sc.scores.values() if d["eligible"])


def test_scorer_redoes_results_of_an_older_version(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    sc = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    sc.screened = {good[0]: {"ok": False, "reason": "round_trips<150", "ts": sc.now(), "v": 1}}
    sc.scores = {good[1]: {"address": good[1], "eligible": True, "score": 0.5, "v": 1}}
    sc._save()
    sc2 = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    assert sc2.screened == {} and sc2.scores == {}


def test_scorer_scores_main_coins_only_unless_diversified(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    cfg.selection.main_coins = ["BTC", "ETH"]
    cfg.selection.alt_min_coins = 1000           # no wallet can unlock the alts
    sc = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    sc.screen_and_score(good[0], 200_000)
    d = sc.scores[good[0]]
    assert d["eligible"] and not d["diversified"] and set(d["coin_pnl"]) == {"BTC", "ETH", "SOL"}
    cfg.selection.alt_min_coins = 3              # profitable in BTC, ETH and SOL: unlocked, scored on all three
    sc2 = Scorer(cfg, info, queue.Queue(), tmp / "cache2")
    sc2.screen_and_score(good[0], 200_000)
    assert sc2.scores[good[0]]["diversified"] and sc2.scores[good[0]]["trades"] > d["trades"]


def test_a_finished_review_publishes_a_ranking_even_with_few_scored(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    cfg.selection.min_scored_to_start = 50          # more than this review can ever score
    out = queue.Queue()
    sc = Scorer(cfg, info, out, tmp / "cache")
    calls = {"n": 0}
    real = sc.screen_and_score

    def stop_after_3(a, av):
        calls["n"] += 1
        real(a, av)
        if calls["n"] == 3:
            sc.stop.set()
    sc.screen_and_score = stop_after_3
    sc.review(weekly=True)                           # interrupted: not finished, not ready
    sc.maybe_cycle()
    assert not sc.ready() and all(m[0] != "ranking" for m in list(out.queue))
    sc2 = Scorer(cfg, info, out, tmp / "cache")
    sc2.review(weekly=True)                          # finished with only ~13 scored
    sc2.maybe_cycle()
    rankings = [m for m in list(out.queue) if m[0] == "ranking"]
    assert sc2.ready() and rankings and rankings[-1][2] < 50 and rankings[-1][1]


def test_a_restart_does_not_push_the_hourly_cycle_back(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    out = queue.Queue()
    sc = Scorer(cfg, info, out, tmp / "cache")
    sc.review(weekly=True)
    last = sc.now() - 20 * 60_000                  # the last real cycle was 20 minutes ago
    sc.meta["last_cycle"] = last
    sc._save()
    sc2 = Scorer(cfg, info, out, tmp / "cache")    # restart: republishes the ranking at once ...
    sc2.maybe_cycle(force=True)
    assert sc2.meta["last_cycle"] == last          # ... but the next cycle stays 40 minutes away, not 60
    sc2.meta["last_cycle"] = sc2.now() - 2 * HOUR
    sc2.maybe_cycle(force=True)                    # long overdue: the forced cycle is the real one
    assert sc2.meta["last_cycle"] >= sc2.now() - 60_000


def test_changing_the_floors_rescores_saved_wallets(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    sc = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    for a in good[:3]:
        sc.screen_and_score(a, 200_000)
    assert len(Scorer(cfg, info, queue.Queue(), tmp / "cache").scores) == 3   # same rules: kept
    cfg.selection.min_profit_factor = 50.0
    sc2 = Scorer(cfg, info, queue.Queue(), tmp / "cache")
    assert sc2.scores == {}                                   # other rules: redone ...
    n = len(fake.requests)
    sc2.rescore_missing()                                     # ... from the screen results kept on disk
    assert set(sc2.scores) == set(good[:3]) and not sc2.ranking()
    assert all(d["reasons"] == ["profit_factor<50"] for d in sc2.scores.values())
    assert not any(r["type"] == "userFillsByTime" and r["startTime"] < sc2.now() - 170 * 86_400_000
                   for r in fake.requests[n:])                # no first-page screen again


# ---- /search: re-pick the best now ----------------------------------------------------------------------
def test_rebalance_follows_the_best_seven_now():
    from copybot.selection import rebalance
    followed = {R[0]: NOW - HOUR, R[8]: NOW - HOUR, R[20]: NOW - 48 * HOUR}   # R[20] is no longer eligible
    p = rebalance(R[:10], followed, set(), {}, NOW, CFG)
    assert p.joins == R[1:7] and dict(p.drops) == {R[8]: "replaced by a better trader (/search)",
                                                   R[20]: "no longer passes the rules"}
    # fewer than 7 eligible: nobody eligible is dropped, free slots fill
    p = rebalance(R[:3], {R[2]: NOW}, set(), {}, NOW, CFG)
    assert p.joins == R[:2] and not p.drops


def test_rebalance_skips_paused_and_cooling_down_leaders():
    from copybot.selection import rebalance
    p = rebalance(R[:10], {R[0]: NOW}, {R[0]}, {R[1]: NOW - HOUR}, NOW, CFG)
    assert p.drops == [(R[0], "paused after a bad streak")]
    assert R[1] not in p.joins and p.joins == R[2:9]


def test_hyperadd_checks_one_wallet_now_and_reports_the_result(scorer_env):
    fake, cfg, info, tmp, good, bad = scorer_env
    out = queue.Queue()
    sc = Scorer(cfg, info, out, tmp / "cache")
    sc.screened[good[0]] = {"ok": False, "reason": "old", "ts": sc.now(), "v": 99}   # a stale result is redone
    for a in (good[0], bad[0], "0x" + "77" * 20):
        sc.add_q.put(a)
    sc.handle_adds()
    added = {}
    while not out.empty():
        m = out.get()
        if m[0] == "added":
            added[m[1]] = m
    assert set(added) == {good[0], bad[0], "0x" + "77" * 20}
    _, _, screened, score, ranking, scores = added[good[0]]
    assert screened["ok"] and score["eligible"] and good[0] in ranking
    _, _, screened, score, _, _ = added[bad[0]]
    assert not (score or {}).get("eligible")
    _, _, screened, score, _, _ = added["0x" + "77" * 20]                # no trades at all
    assert screened is not None and not screened["ok"] and score is None
