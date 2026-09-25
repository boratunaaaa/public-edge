"""Operator CLI.

  python -m app.cli watchlist TICKER CATEGORY "reason" [--release]
  python -m app.cli security-master                     # full security-master refresh (SEC sources)
  python -m app.cli cleanup [--apply] [--steps 13d,multi,owners,clusters]   # dry run unless --apply
  python -m app.cli reattribute                         # re-derive joint-filing owners from stored XML
  python -m app.cli revalidate-returns                  # verify price identity of computed returns
  python -m app.cli walkforward                         # walk-forward threshold study (test windows only)
  python -m app.cli quality                             # data-quality overview (JSON)
  python -m app.cli news-score                          # timestamp confidence / story fields for stored news
  python -m app.cli validation-sample SAMPLE_ID
  python -m app.cli validation-label SAMPLE_ID TICKER LABEL REASON[,REASON] "notes" LABELER
  python -m app.cli validation-precision SAMPLE_ID
  python -m app.cli alpha                               # ALPHA VALIDATION verdict per signal family
  python -m app.cli tenk-live | tenk-backfill [MAX_DOCS]  # 10-K text-change engine
  python -m app.cli tenk-reclassify [--apply]           # supersede (never delete) filings of an older classifier
  python -m app.cli insider-profiles                    # refresh insider person track records
  python -m app.cli paper-init | paper-run | paper-status
  python -m app.cli paper-propose EVENT_ID [--research] # gate + PENDING proposal (never an order)
  python -m app.cli paper-approve CODE | paper-reject CODE
  python -m app.cli paper-notify                        # send pending proposals to Telegram
  python -m app.cli tiingo-backfill | provider-coverage
  python -m app.cli tiingo-test                         # ONE request to verify the key (status only)
  python -m app.cli tiingo-batch [N]                    # identity-gated Tiingo free-tier batch
  python -m app.cli finsaber-load | av-refresh          # FINSABER open dataset / Alpha Vantage delisting list
  python -m app.cli recovery-report [--hard-set] [--probe]   # FREE DATA RECOVERY coverage
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
    elif cmd == "news-score":
        from app.integrity.news_backfill import score_existing
        _print(score_existing(eng))
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
    elif cmd == "alpha":
        from app.paper.alpha import all_families
        with eng.begin() as conn:
            _print(all_families(conn, use_cache="--fresh" not in argv))
    elif cmd == "tenk-live":
        from app.tenk.engine import run_live
        _print(run_live(eng))
    elif cmd == "tenk-backfill":
        from app.tenk.engine import run_backfill
        _print(run_backfill(eng, max_docs=int(argv[1]) if len(argv) > 1 else 120))
    elif cmd == "tenk-reclassify":
        from app.tenk.engine import supersede_old_classifier
        _print(supersede_old_classifier(eng, dry_run="--apply" not in argv))
    elif cmd == "insider-profiles":
        from app.signals.insider_profile import refresh_all
        _print(refresh_all(eng))
    elif cmd == "paper-init":
        from app.paper.ledger import ensure_accounts
        with eng.begin() as conn:
            _print(ensure_accounts(conn))
    elif cmd == "paper-run":
        from app.paper.ledger import run
        _print(run(eng))
    elif cmd == "paper-status":
        from app.paper.ledger import account_metrics
        from app.paper.proposals import pending_summary
        with eng.connect() as conn:
            ids = [r[0] for r in conn.exec_driver_sql("SELECT id FROM paper_accounts ORDER BY id").all()]
            _print({"accounts": [account_metrics(conn, i) for i in ids], "proposals": pending_summary(conn)})
    elif cmd == "paper-propose" and len(argv) >= 2:
        from app.paper.proposals import create_proposal
        _print(create_proposal(eng, int(argv[1]), research_only="--research" in argv))
    elif cmd in ("paper-approve", "paper-reject") and len(argv) >= 2:
        from app.paper.proposals import decide
        r = decide(eng, argv[1], cmd == "paper-approve", by="cli")
        _print(r)
        return 0 if r.get("ok") else 1
    elif cmd == "paper-notify":
        from app.paper.proposals import notify_pending
        _print({"sent": notify_pending(eng)})
    elif cmd == "tiingo-test":
        from app.pricesources.tiingo import connection_test
        _print(connection_test(eng))
    elif cmd == "tiingo-batch":
        from app.pricesources.tiingo import run as tiingo_batch
        _print(tiingo_batch(eng, max_symbols=int(argv[1]) if len(argv) > 1 else 22))
    elif cmd == "finsaber-load":
        from app.pricesources.finsaber import run as finsaber_run
        _print(finsaber_run(eng))
    elif cmd == "av-refresh":
        from app.pricesources.alphavantage import refresh
        _print(refresh(eng))
    elif cmd == "recovery-report":
        from app.pricesources import quota, report
        base = report.baseline(eng)
        with eng.connect() as conn:
            out = {"baseline": base, "now": report.unresolved_by_cause(conn), "providers": report.provider_results(conn),
                   "tiingo_quota": quota.summary(conn, "tiingo")}
            if "--hard-set" in argv:
                hs = report.hard_set(conn)
                out["hard_set"] = hs
        if "--probe" in argv and "hard_set" in out:
            from datetime import date as _d
            syms = [(h["symbol"], h["events"]["first_unresolved"] or _d(2023, 1, 3)) for h in out["hard_set"] if h.get("symbol")]
            out["probe_yahoo"] = report.probe_yahoo(syms)
            out["probe_wayback"] = report.probe_wayback([s for s, _ in syms[:30]])
        _print(out)
    elif cmd == "tiingo-backfill":
        from app.securitymaster.tiingo_backfill import run as tiingo_run
        _print(tiingo_run(eng))
    elif cmd == "provider-coverage":
        from app.securitymaster.tiingo_backfill import coverage_report
        _print(coverage_report(eng))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
