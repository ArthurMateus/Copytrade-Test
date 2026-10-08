"""Solana and DexScreener parsers against REAL recorded responses, plus the RPC client against the loopback fake."""
import copy
from datetime import datetime

import pytest

from copybot.sol import chain
from copybot.sol.market import parse_pairs
from tests.fakes import fixture
from tests.fakes_sol import FEE_PAYER, KEY, USDC, FakeSolana

DAY = 86_400_000


def test_fomo_buy_on_chain_matches_fomos_own_record_of_it():
    """Ground truth: the recorded transaction is the on-chain side of the first swap in fomo_swaps.json."""
    tx = fixture("chain_tx_fomo_buy.json")
    fomo = fixture("fomo_swaps.json")["responseObject"]["swaps"][0]
    w = chain.trader_of(tx, FEE_PAYER)
    assert w == "AGdtsMmphhymH5wYW3dWKSkAxpLYRrNDCTFAfuq8ii9v"
    assert w != fomo["address"]                         # the address FOMO shows is not the trading wallet
    [g] = chain.parse_tx(tx, w)
    assert g.side == "buy" and g.token == fomo["outTokenAddress"]
    assert g.amount == pytest.approx(fomo["outHumanAmount"], abs=0.01)
    assert g.usd == pytest.approx(89.0483) and g.usd - fomo["inHumanAmount"] == pytest.approx(0.95)   # FOMO's fee
    fomo_ms = datetime.fromisoformat(fomo["createdAt"].replace("Z", "+00:00")).timestamp() * 1000
    assert abs(g.ts - fomo_ms) <= 2000
    assert g.id == tx["transaction"]["signatures"][0]


def test_fomo_sell_parses():
    tx = fixture("chain_tx_fomo_sell.json")
    w = chain.trader_of(tx, FEE_PAYER)
    [g] = chain.parse_tx(tx, w)
    assert g.side == "sell" and g.token == "BZFYNPeQAEW3HWQ4DNsTVahC1n4ZjTgn6jB2nnBbB96W"
    assert g.amount == pytest.approx(152564.440373) and g.usd == pytest.approx(301.295184)


def test_a_routed_buy_ignores_the_speck_of_intermediate_token_it_leaves():
    tx = fixture("chain_tx_fomo_routed.json")
    w = chain.trader_of(tx, FEE_PAYER)
    d = chain.deltas(tx, w)
    assert len([m for m, v in d.items() if m not in chain.USD and v > 0]) == 2      # two tokens came in
    [g] = chain.parse_tx(tx, w)
    assert g.side == "buy" and g.token == "3xrw3JKyaSYjzksYc8nrZE1kReQAxoHT3epi3P1mpZVf"
    assert g.amount == pytest.approx(22750.256053) and g.usd == pytest.approx(10.2)


def test_two_tokens_both_moving_a_lot_is_ambiguous_and_skipped():
    tx = copy.deepcopy(fixture("chain_tx_fomo_routed.json"))
    w = chain.trader_of(tx, FEE_PAYER)
    for b in tx["meta"]["preTokenBalances"]:            # the intermediate token: from 0.0165 to nothing before
        if b.get("owner") == w and b["mint"].startswith("SPCX"):
            b["uiTokenAmount"].update(amount="0", uiAmountString="0")
    assert chain.parse_tx(tx, w) == []


def test_failed_transfers_and_unrelated_transactions_give_no_swap():
    tx = copy.deepcopy(fixture("chain_tx_fomo_buy.json"))
    w = chain.trader_of(tx, FEE_PAYER)
    failed = copy.deepcopy(tx)
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert chain.parse_tx(failed, w) == []
    deposit = copy.deepcopy(tx)                          # only USDC moves: a deposit or withdrawal, not a swap
    for k in ("preTokenBalances", "postTokenBalances"):
        deposit["meta"][k] = [b for b in deposit["meta"][k] if b["mint"] == USDC]
    assert chain.parse_tx(deposit, w) == []
    assert chain.parse_tx(tx, "SomeoneElse111111111111111111111111111111111") == []
    assert chain.parse_tx(None, w) == []
    assert chain.trader_of(tx, "NotFomo11111111111111111111111111111111111") is None


def test_signature_lists_have_what_the_client_reads():
    for name in ("chain_sigs_wallet.json", "chain_sigs_feepayer.json"):
        rows = fixture(name)
        assert rows and all({"signature", "blockTime", "err"} <= set(r) for r in rows)
        assert [r["blockTime"] for r in rows] == sorted((r["blockTime"] for r in rows), reverse=True)  # newest first
    assert fixture("chain_sigs_wallet.json")[-1]["signature"] in {
        r["signature"] for r in fixture("chain_sigs_wallet.json")}


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


# ---- client against the loopback node ------------------------------------------------------------------
@pytest.fixture
def fake():
    f = FakeSolana()
    yield f
    f.close()


def client(fake, key=""):
    return chain.ChainClient(chain.Rpc(f"{fake.url}/?api-key={key}" if key else fake.url, 0))


def test_swaps_reads_each_transaction_once(fake):
    t = 1_800_000_000_000
    fake.swap("W1", "buy", "MintA", 100, 50.0, t)
    fake.swap("W1", "sell", "MintA", 100, 70.0, t + 60_000)
    fake.swap("W1", "buy", "MintB", 5, 20.0, t + 120_000, failed=True)
    c = client(fake)
    legs, full = c.swaps("W1", 25)
    assert [(g.side, g.token, g.usd) for g in legs] == [("buy", "MintA", 50.0), ("sell", "MintA", 70.0)]
    assert not full
    n = sum(1 for m, _ in fake.calls if m == "getTransaction")
    assert n == 2                                        # the failed one is never opened (err in the list)
    c.swaps("W1", 25)
    assert sum(1 for m, _ in fake.calls if m == "getTransaction") == n


def test_fetch_history_pages_back_stops_at_since_and_updates_incrementally(fake):
    t0 = 1_800_000_000_000
    for i in range(30):
        fake.swap("W2", "buy" if i % 2 == 0 else "sell", f"M{i // 2}", 10, 10.0 + i, t0 + i * 3_600_000)
    c = client(fake)
    rpc = c.rpc
    sigs, done = chain.signatures_until(rpc, "W2", 0, max_sigs=12)
    assert len(sigs) == 12 and not done
    h = chain.fetch_history(c, "W2", since_ms=t0 + 10 * 3_600_000)
    assert h.complete and len(h.legs) == 20 and h.legs[0].ts == t0 + 10 * 3_600_000
    newest = h.newest_sig
    fake.swap("W2", "buy", "Late", 1, 5.0, t0 + 40 * 3_600_000)
    h2 = chain.fetch_history(c, "W2", since_ms=0, stop_sig=newest)
    assert h2.complete and [g.token for g in h2.legs] == ["Late"]


def test_a_wallet_too_busy_for_its_history_is_not_downloaded(fake):
    t0 = 1_800_000_000_000
    for i in range(50):
        fake.swap("Busy", "buy", f"T{i}", 1, 1.0, t0 + i * 1000)
    c = client(fake)
    h = chain.fetch_history(c, "Busy", since_ms=0, max_sigs=20, min_span_ms=21 * DAY)
    assert not h.complete and h.legs == [] and h.n_sigs == 20
    assert not any(m == "getTransaction" for m, _ in fake.calls)


def test_sample_fomo_finds_traders_only_through_fomos_fee_payer(fake):
    t = 1_800_000_000_000
    fake.swap("Alice", "buy", "X", 10, 100.0, t)
    fake.swap("Bob", "sell", "Y", 10, 40.0, t + 1000)
    fake.swap("Carol", "buy", "Z", 10, 30.0, t + 2000, fomo=False)        # not a FOMO trade
    seen = chain.sample_fomo(client(fake), FEE_PAYER, pages=1, per_page=50)
    assert sorted((w, [g.usd for g in legs]) for w, legs in seen) == [("Alice", [100.0]), ("Bob", [40.0])]


def test_a_refused_key_raises_auth_error_and_the_key_is_sent_only_to_the_live_client(fake):
    fake.swap("W3", "buy", "M", 1, 2.0, 1_800_000_000_000)
    assert client(fake, KEY).swaps("W3")[0]
    assert all(k for _, k in fake.calls)
    fake.refuse = True
    with pytest.raises(chain.AuthError):
        client(fake, KEY).swaps("W3")
    assert client(fake).swaps("W3")[0]                  # the public endpoint (no key) still answers


def history_of(fake, wallet, n, t0=1_800_000_000_000):
    for i in range(n):
        fake.swap(wallet, "buy" if i % 2 == 0 else "sell", f"M{i // 2}", 10, 10.0 + i, t0 + i * 3_600_000)


def test_with_a_helius_key_history_comes_in_bulk_pages_of_100(fake):
    history_of(fake, "W4", 250)
    c = chain.ChainClient(chain.Rpc(f"{fake.url}/?api-key={KEY}", 0), parallel=4, bulk=True)
    h = chain.fetch_history(c, "W4", since_ms=0, max_sigs=2000)
    assert h.complete and len(h.legs) == 250
    assert [g.ts for g in h.legs] == sorted(g.ts for g in h.legs)
    bulk = [m for m, _ in fake.calls if m == "getTransactionsForAddress"]
    assert len(bulk) == 3 and not any(m == "getTransaction" for m, _ in fake.calls)
    one_by_one = chain.ChainClient(chain.Rpc(fake.url, 0), parallel=4)
    assert chain.fetch_history(one_by_one, "W4", since_ms=0).legs == h.legs       # same swaps either way


def test_when_bulk_is_refused_history_is_read_one_by_one_and_bulk_rests(fake):
    history_of(fake, "W5", 30)
    fake.bulk_refuse = True
    c = chain.ChainClient(chain.Rpc(f"{fake.url}/?api-key={KEY}", 0), parallel=4, bulk=True)
    h = chain.fetch_history(c, "W5", since_ms=0)
    assert len(h.legs) == 30 and not c.bulk_ready()
    assert sum(1 for m, _ in fake.calls if m == "getTransaction") == 30
    n = len(fake.calls)
    chain.fetch_history(chain.ChainClient(c.rpc, bulk=True), "W5", since_ms=0)   # a fresh client tries bulk again
    assert any(m == "getTransactionsForAddress" for m, _ in fake.calls[n:])


def test_the_daily_credit_cap_moves_the_search_to_the_free_endpoint(fake):
    history_of(fake, "W6", 8)
    helius, public = chain.Rpc(f"{fake.url}/?api-key={KEY}", 0, name="helius"), chain.Rpc(fake.url, 0, name="public")
    rpc = chain.FallbackRpc(helius, public, daily_credits=5)
    c = chain.ChainClient(rpc)
    h = chain.fetch_history(c, "W6", since_ms=0)
    assert len(h.legs) == 8
    keyed = [k for m, k in fake.calls]
    assert keyed[:5] == [True] * 5 and not any(keyed[5:]) and len(keyed) == 9      # 1 list + 8 reads
    assert rpc.used == 5


def test_a_missing_old_transaction_is_skipped_in_history_but_retried_live(fake):
    history_of(fake, "W7", 4)
    sig = fake.sigs["W7"][1]["signature"]
    del fake.tx[sig]
    c = client(fake)
    assert len(chain.fetch_history(c, "W7", since_ms=0).legs) == 3
    with pytest.raises(chain.ChainError):
        c.swaps("W7")
