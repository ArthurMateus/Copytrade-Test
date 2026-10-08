"""Record REAL Solana responses for the FOMO book into tests/fixtures (public endpoint, no key, read-only).

Usage: uv run python tools/record_chain.py

chain_tx_fomo_buy.json      the on-chain side of the first swap in fomo_swaps.json (ground truth, fixed signature)
chain_tx_fomo_sell.json     a FOMO user's sell (found by sampling FOMO's fee payer)
chain_tx_fomo_routed.json   a FOMO buy routed through an intermediate token (a speck of it is left over)
chain_sigs_feepayer.json    getSignaturesForAddress of FOMO's fee payer (20 newest)
chain_sigs_wallet.json      getSignaturesForAddress of the buy fixture's trader (30 newest)
"""
import json
import random
from pathlib import Path

from copybot.config import Sol
from copybot.sol.chain import USD, Rpc, deltas, parse_tx, trader_of

PUBLIC = "https://api.mainnet-beta.solana.com"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
BUY_SIG = "2yEEpEtDob2S6f5SnuvM8zhxhdmN8GJdpB3xQFqEPdB9nVTSdTihni4poYCACcLxcysv6J24qPyg1Ew7Rm5J4QWu"


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, indent=1), encoding="utf-8")
    print("saved", name)


def main():
    fee_payer = Sol().fomo_fee_payer
    rpc = Rpc(PUBLIC, min_interval_s=0.5, name="public")
    buy = rpc.transaction(BUY_SIG)
    save("chain_tx_fomo_buy.json", buy)
    wallet = trader_of(buy, fee_payer)
    save("chain_sigs_wallet.json", rpc.signatures(wallet, 30))
    page = rpc.signatures(fee_payer, 1000)
    save("chain_sigs_feepayer.json", page[:20])
    want = {"sell": None, "routed": None}
    for s in random.sample([s for s in page if s.get("err") is None], 200):
        tx = rpc.transaction(s["signature"])
        w = trader_of(tx, fee_payer) if tx else None
        if not w:
            continue
        legs = parse_tx(tx, w, s["signature"])
        moved = [m for m, v in deltas(tx, w).items() if m not in USD and abs(v) > 0]
        kind = ("routed" if len(moved) > 1 else legs[0].side) if legs else "none"
        if kind in want and want[kind] is None:
            want[kind] = tx
            save(f"chain_tx_fomo_{kind}.json", tx)
        if all(want.values()):
            break
    print("not found:", [k for k, v in want.items() if v is None] or "none")


if __name__ == "__main__":
    main()
