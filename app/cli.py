"""Operator CLI.

  python -m app.cli watchlist TICKER CATEGORY "reason" [--release]
  python -m app.cli security-master                     # full security-master refresh (SEC sources)
  python -m app.cli cleanup [--apply] [--steps 13d,multi,owners,clusters]   # dry run unless --apply
  python -m app.cli reattribute                         # re-derive joint-filing owners from stored XML
  python -m app.cli revalidate-returns                  # verify price identity of computed returns
  python -m app.cli walkforward                         # walk-forward threshold study (test windows only)
  python -m app.cli quality                             # data-quality overview (JSON)
  python -m app.cli validation-sample SAMPLE_ID
  python -m app.cli validation-label SAMPLE_ID TICKER LABEL REASON[,REASON] "notes" LABELER
  python -m app.cli validation-precision SAMPLE_ID
"""
from __future__ import annotations

import json
import sys

from app.db.session import get_engine
from app.entities.registry import resolver


def _watchlist(argv: list[str]) -> int:
    from app.discovery.watchlist import CATEGORIES, set_category
    if len(argv) < 4:
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


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, eng = argv[0], None
    if cmd == "watchlist":
        return _watchlist(argv)
    eng = get_engine()
    if cmd == "security-master":
        from app.config import settings
        from app.securitymaster.build import run_security_master
        _print(run_security_master(eng, settings.backfill_years))
    elif cmd == "cleanup":
        from app.integrity.cleanup import run_cleanup
        steps = tuple(argv[argv.index("--steps") + 1].split(",")) if "--steps" in argv else ("13d", "multi", "owners", "clusters")
        _print(run_cleanup(eng, dry_run="--apply" not in argv, steps=steps))
    elif cmd == "reattribute":
        from app.integrity.reattribute import reattribute_xml_rows
        _print(reattribute_xml_rows(eng, batch=100000))
    elif cmd == "revalidate-returns":
        from app.backtest.jobs import revalidate_price_identity
        _print(revalidate_price_identity(eng, batch=200000))
    elif cmd == "walkforward":
        from app.backtest.walkforward import run_all
        _print(run_all(eng))
    elif cmd == "quality":
        from app.integrity.quality_report import overview
        with eng.connect() as conn:
            _print(overview(conn))
    elif cmd == "validation-sample" and len(argv) >= 2:
        from app.discovery.validation import create_sample
        _print(create_sample(eng, argv[1]))
    elif cmd == "validation-label" and len(argv) >= 7:
        from app.discovery.validation import label
        with eng.begin() as conn:
            ok = label(conn, argv[1], argv[2], argv[3], [r for r in argv[4].split(",") if r], argv[5], argv[6])
        print("ok" if ok else "not found")
        return 0 if ok else 1
    elif cmd == "validation-precision" and len(argv) >= 2:
        from app.discovery.validation import precision
        with eng.connect() as conn:
            _print(precision(conn, argv[1]))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
