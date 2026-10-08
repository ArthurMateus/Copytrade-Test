"""FOMO and DexScreener parsers against REAL recorded responses, plus the client against the loopback fake."""
import pytest

from copybot.sol import fomo
from copybot.sol.market import parse_pairs
from tests.fakes import fixture
from tests.fakes_sol import COOKIE, FakeFomo, USDC, make_row, make_swap

NOW = 1_800_000_000_000


def test_leaderboard_fixture_parses():
    rows = fomo.parse_leaderboard("30d", fixture("fomo_leaderboard_30d.json"))
    assert len(rows) == 8
    r = rows[0]
    assert r.handle == "ExactTallTakin" and r.address.startswith("DPHECQ")
    assert r.uid == "d6a85eb5-d3fb-5b8a-8445-019af88ba512"
    assert r.pnl == pytest.approx(803082.5322347687) and r.window == "30d"
    assert r.swap_count == 446 and r.volume > 1e6 and not r.private and not r.restricted
    assert r.holdings_usd == pytest.approx(296.79, abs=0.01) and r.total_holdings == 2


def test_leaderboard_pnl_field_fallback_is_not_silently_zero():
    body = fixture("fomo_leaderboard_30d.json")
    rows = fomo.parse_leaderboard("7d", body)        # asked for 7d but the row only has pnl30d
    assert rows[0].pnl == pytest.approx(803082.5322347687)


def test_swaps_fixture_parses_into_buy_and_sell_legs():
    legs, more = fomo.parse_swaps(fixture("fomo_swaps.json"))
    assert more is True and len(legs) == 25
    assert legs == sorted(legs, key=lambda g: (g.ts, g.id))                  # oldest first
    assert {g.side for g in legs} == {"buy", "sell"}
    newest = legs[-1]
    assert (newest.side, newest.token) == ("buy", "DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP")
    assert newest.amount == pytest.approx(3565.63) and newest.usd == pytest.approx(88.0983)
    assert newest.px == pytest.approx(88.0983 / 3565.63)
    assert all(g.token not in fomo.QUOTES for g in legs)


def test_token_to_token_swap_becomes_a_sell_and_a_buy():
    raw = make_swap("buy", "TOKB", 100, 50, NOW, sid="x1")
    raw.update(inTokenAddress="TOKA", inHumanAmount=10.0, humanUsdAmountIn=50.0, humanUsdAmountOut=50.0)
    legs, _ = fomo.parse_swaps({"responseObject": {"swaps": [raw], "hasNextPage": False}})
    assert [(g.side, g.token) for g in legs] == [("sell", "TOKA"), ("buy", "TOKB")] or \
           sorted((g.side, g.token) for g in legs) == [("buy", "TOKB"), ("sell", "TOKA")]
    assert {g.id for g in legs} == {"x1:s", "x1:b"}


def test_unpriceable_swaps_are_ignored():
    raw = make_swap("buy", "T", 100, 0.0, NOW)
    raw["humanUsdAmountOut"] = 0.0
    assert fomo.parse_swaps({"responseObject": {"swaps": [raw]}})[0] == []
    assert fomo.parse_swaps(None) == ([], False)


def test_dexscreener_fixture_liquidity_missing_means_unknown():
    q = parse_pairs(fixture("dexscreener_tokens.json"))
    bordr = q["6Mix12LiHrQFojaQEnfPUC65Qkwd6X4Y5Qg93oFbordr"]
    assert bordr.symbol == "BORDR" and bordr.px == pytest.approx(0.00006974) and bordr.liq_usd == 0.0
    si = q["DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP"]
    assert si.liq_usd == pytest.approx(692664.25)


def test_dexscreener_picks_the_deepest_pool():
    a, b = dict(fixture("dexscreener_tokens.json")[1]), dict(fixture("dexscreener_tokens.json")[1])
    a["liquidity"], b["liquidity"] = {"usd": 10.0}, {"usd": 99.0}
    a["priceUsd"], b["priceUsd"] = "1.0", "2.0"
    assert parse_pairs([a, b])[a["baseToken"]["address"]].px == 2.0


@pytest.fixture
def fake():
    f = FakeFomo()
    yield f
    f.close()


def test_client_reads_leaderboard_and_swaps(fake):
    fake.rows["30d"] = [make_row("u1", "AddrOne", "one", 5000.0)]
    fake.swaps["u1"] = [make_swap("buy", "TOK", 10, 5, NOW + i * 1000) for i in range(30)]
    c = fomo.FomoClient(fake.url, COOKIE, min_interval_s=0)
    rows = c.leaderboard("30d")
    assert rows[0].address == "AddrOne" and rows[0].pnl == 5000.0
    legs, more = c.swaps("u1", 25)
    assert len(legs) == 25 and more is True
    legs, more = c.swaps("u1", 100)
    assert len(legs) == 30 and more is False


def test_client_without_a_valid_session_raises_auth_error(fake):
    with pytest.raises(fomo.AuthError):
        fomo.FomoClient(fake.url, "", min_interval_s=0).leaderboard("30d")
    with pytest.raises(fomo.AuthError):
        fomo.FomoClient(fake.url, "wrong=cookie", min_interval_s=0).leaderboard("30d")
    fake.refuse = True
    with pytest.raises(fomo.AuthError):
        fomo.FomoClient(fake.url, COOKIE, min_interval_s=0).swaps("u1")


def test_fetch_history_raises_limit_until_it_reaches_the_start(fake):
    fake.swaps["u1"] = [make_swap("buy", "TOK", 10, 5, NOW + i * 60_000) for i in range(500)]
    c = fomo.FomoClient(fake.url, COOKIE, min_interval_s=0)
    h = fomo.fetch_history(c, "u1", since_ms=NOW, max_limit=3000)
    assert h.complete and len(h.legs) == 500
    h = fomo.fetch_history(c, "u1", since_ms=NOW + 400 * 60_000)           # only the recent part wanted
    assert h.complete and len(h.legs) == 100 and h.legs[0].ts >= NOW + 400 * 60_000


def test_fetch_history_keeps_what_it_has_when_the_server_refuses_a_big_limit(fake):
    fake.swaps["u1"] = [make_swap("buy", "TOK", 10, 5, NOW + i * 60_000) for i in range(500)]
    fake.max_limit = 250
    c = fomo.FomoClient(fake.url, COOKIE, min_interval_s=0)
    h = fomo.fetch_history(c, "u1", since_ms=NOW)
    assert not h.complete and len(h.legs) == 200


def test_cookie_file_is_used_and_re_read_when_it_changes(fake, tmp_path):
    f = tmp_path / "fomo.cookie"
    f.write_text("wrong=cookie\n", encoding="utf-8")
    c = fomo.FomoClient(fake.url, "", min_interval_s=0, cookie_file=str(f))
    with pytest.raises(fomo.AuthError):
        c.leaderboard("30d")                         # expired session
    fake.rows["30d"] = [make_row("u1", "AddrOne", "one", 5000.0)]
    f.write_text("session=abc123;\n  other=xyz\n", encoding="utf-8")   # the owner pastes a fresh one
    import os
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    assert c.leaderboard("30d")[0].address == "AddrOne"                  # no restart needed
    f.unlink()
    assert c.leaderboard("30d")[0].address == "AddrOne"                  # a vanished file keeps the last cookie
    assert c.usable


def test_an_oversized_cookie_gets_a_clear_message_and_the_trim_tool_keeps_only_what_is_needed(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("trim", "tools/fomo_cookie_trim.py")
    trim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trim)
    pasted = "cookie: privy-token=AAA; _ga=GA1; ph_x=PH; privy-session=BBB; _dd_s=DD; privy-token=AAA2\n"
    c = trim.parse(pasted)
    assert list(c) == ["privy-token", "privy-session", "_ga", "ph_x", "_dd_s"] or set(c) == {
        "privy-token", "privy-session", "_ga", "ph_x", "_dd_s"}
    assert c["privy-token"] == "AAA2"                                    # a duplicate: the later one wins
    assert not any(trim.TRACKING.match(k) for k in ("privy-token", "privy-session"))
    assert all(trim.TRACKING.match(k) for k in ("_ga", "ph_x", "_dd_s"))
    assert trim.parse("not a cookie") == {} and trim.header({"a": "1", "b": "2"}) == "a=1; b=2"
    assert trim.status("x" * 8000) == 431                                # never even sent
