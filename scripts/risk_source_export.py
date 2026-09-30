"""READ-ONLY export of the inputs for a point-in-time risk reconstruction, issuer-partitioned JSON lines (first line =
metadata, then one line per issuer), streamed in batches so memory holds one batch.

    python scripts/risk_source_export.py --ciks 0001213037,... --start 2021-03-01 --end 2021-06-30 [--efts] > src.jsonl

The last line is a completion trailer ({"_trailer": {...}}: issuer count + digest, batches, per-phrase finished
batches), written only after every issuer: the reconstruction publishes coverage only when it verifies.

Database: one READ ONLY transaction per batch. Network (optional --efts): EDGAR full-text search with a DATABASE-FREE
HTTP limiter - the shared limiter (which records every request in the production rate_limits table) is switched off
before any app module is imported, and the command refuses to run if it is still on."""
from __future__ import annotations

import os

os.environ["PE_SHARED_RATELIMIT"] = "0"  # before app.http is imported anywhere: the limiter must not touch a database

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from datetime import date, timedelta  # noqa: E402

from sqlalchemy import text  # noqa: E402

EFTS_MAX_HITS = 500
BATCH = 25


def ensure_db_free_http() -> None:
    from app import http
    if http._SHARED:
        raise SystemExit("refusing to run: the shared (database-writing) HTTP rate limiter is enabled")


def _batch_rows(c, ciks: list[str]) -> dict[str, dict]:
    recs = {cik: {"cik": cik, "tenk": [], "shares": [], "efts_hits": [], "actions": {}} for cik in ciks}
    for r in c.execute(text("""SELECT accession, cik, form, accepted_at, filing_date, source_url, profile FROM tenk_filings
                               WHERE cik = ANY(:c) AND status = 'ok' ORDER BY cik, filing_date"""), {"c": ciks}).mappings():
        recs[r["cik"]]["tenk"].append(dict(r))
    for cik, acc, e, f, form, val in c.execute(text("""SELECT cik, accession, end_date, filed, form, val
                                                      FROM shares_outstanding_facts WHERE cik = ANY(:c)
                                                      ORDER BY cik, filed, end_date"""), {"c": ciks}):
        recs[cik]["shares"].append([acc, e, f, form, val])
    for cik, earliest, fetched in c.execute(text("""SELECT sm.cik, (SELECT min(action_date) FROM corporate_actions ca
                                                          WHERE ca.cik = sm.cik AND ca.source = 'sec_submissions'),
                                                        sm.submissions_fetched_at::date
                                                   FROM security_master sm WHERE sm.cik = ANY(:c)"""), {"c": ciks}):
        recs[cik]["actions"] = {"earliest": earliest, "fetched": fetched}
    return recs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ciks", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--efts", action="store_true", help="also scan the production TEXT_RISKS phrases (network)")
    a = ap.parse_args()
    ensure_db_free_http()
    from app.db.session import get_engine
    ciks = sorted({c.strip().zfill(10) for c in a.ciks.split(",") if c.strip()})
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    eng = get_engine()
    meta: dict = {"ciks": ciks, "start": a.start, "end": a.end}
    with eng.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        meta["tenk_by_year"] = {str(int(y)): n for y, n in c.execute(text(
            "SELECT extract(year FROM filing_date), count(*) FROM tenk_filings GROUP BY 1 ORDER BY 1")).all()}
        meta["thesis_changes_from"] = c.execute(text("SELECT min(created_at)::date FROM thesis_changes")).scalar()
        c.rollback()
    lo = start - timedelta(days=365)
    if a.efts:
        from app.discovery.risks import TEXT_RISKS
        meta["efts_scan"] = {"done": True, "from": lo.isoformat(), "to": end.isoformat(), "truncated": False,
                             "phrases": sorted(TEXT_RISKS), "batch": BATCH}
    # the reader needs the metadata first; a truncated full-text query is recorded on each issuer of its batch
    # (efts_truncated) and folded into the scan status by the reconstruction
    print(json.dumps(meta, default=str))
    batches, efts_batches = 0, {}
    for i in range(0, len(ciks), BATCH):
        part = ciks[i:i + BATCH]
        with eng.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            recs = _batch_rows(c, part)
            c.rollback()
        if a.efts:
            from app.discovery.risks import TEXT_RISKS
            from app.discovery.sec_data import efts_search
            for key, (phrase, forms, _sev) in TEXT_RISKS.items():
                got = efts_search(phrase, forms, lo, end, max_hits=EFTS_MAX_HITS, ciks=part)
                truncated = len(got) >= EFTS_MAX_HITS
                for h in got:
                    if h["cik"] in recs:
                        recs[h["cik"]]["efts_hits"].append({**h, "risk_key": key})
                if truncated:
                    for r in recs.values():
                        r["efts_truncated"] = True
                efts_batches[key] = efts_batches.get(key, 0) + 1
        for cik in part:
            print(json.dumps(recs[cik], default=str))
        batches += 1
        del recs
    from app.replay.risk_history import TRAILER, scope_key
    print(json.dumps({TRAILER: {"issuers": len(ciks), "ciks_digest": scope_key(ciks), "batches": batches,
                                "efts_batches": efts_batches if a.efts else {}}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
