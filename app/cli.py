"""Operator CLI.  python -m app.cli watchlist TICKER CATEGORY "reason" [--release]"""
from __future__ import annotations

import sys

from app.db.session import get_engine
from app.discovery.watchlist import CATEGORIES, set_category
from app.entities.registry import resolver


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[0] != "watchlist":
        print(__doc__)
        return 2
    ticker, cat, reason = argv[1].upper(), argv[2].upper(), argv[3]
    if cat not in CATEGORIES:
        print(f"category must be one of {CATEGORIES}")
        return 2
    eng = get_engine()
    with eng.begin() as conn:
        resolver.load(conn, force=True)
        cik = resolver.ticker_to_cik.get(ticker)
        if not cik:
            print(f"unknown ticker {ticker}")
            return 1
        # manual override pins the category; --release hands the company back to automation
        set_category(conn, cik, ticker, cat, reason, actor="manual", manual_override="--release" not in argv)
    print(f"{ticker} -> {cat} ({'released to auto' if '--release' in argv else 'manual override'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
