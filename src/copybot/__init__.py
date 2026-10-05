"""Minimal Hyperliquid copy-trading bot. PAPER MODE ONLY: no keys, no signing, no real orders."""

__version__ = "0.1.0"


def main() -> None:
    from copybot.runner import main as _main

    _main()
