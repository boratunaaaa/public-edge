"""READ-ONLY export of the inputs for a point-in-time risk reconstruction (one READ ONLY transaction; optional EDGAR
full-text scan over the network - no database write, run with PE_SHARED_RATELIMIT=0 so not even the shared limiter row
is touched).

    python scripts/risk_source_export.py --ciks 0001213037,... --start 2021-03-01 --end 2021-06-30 [--efts] > risk_src.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

from sqlalchemy import text

from app.db.session import get_engine

EFTS_MAX_HITS = 500


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ciks", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--efts", action="store_true", help="also scan the production TEXT_RISKS phrases (network)")
    a = ap.parse_args()
    ciks = [c.strip().zfill(10) for c in a.ciks.split(",") if c.strip()]
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    out: dict = {"ciks": ciks, "start": a.start, "end": a.end}
    with get_engine().connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        out["tenk"] = {}
        for r in c.execute(text("""SELECT accession, cik, form, accepted_at, filing_date, source_url, profile FROM tenk_filings
                                   WHERE cik = ANY(:c) AND status = 'ok' ORDER BY cik, filing_date"""), {"c": ciks}).mappings():
            out["tenk"].setdefault(r["cik"], []).append(dict(r))
        out["tenk_by_year"] = {str(int(y)): n for y, n in c.execute(text(
            "SELECT extract(year FROM filing_date), count(*) FROM tenk_filings GROUP BY 1 ORDER BY 1")).all()}
        out["tenk_profile_versions"] = {str(v): n for v, n in c.execute(text(
            "SELECT COALESCE((profile->'_meta'->>'v')::int, 1), count(*) FROM tenk_filings GROUP BY 1")).all()}
        out["actions_from"] = c.execute(text("""SELECT max(m) FROM (SELECT min(action_date) m FROM corporate_actions
                                                WHERE action IN ('SHELF_REGISTRATION', 'PROSPECTUS_424B', 'UNREGISTERED_SALE',
                                                                 'LISTING_DEFICIENCY') GROUP BY action) x""")).scalar()
        out["thesis_changes_from"] = c.execute(text("SELECT min(created_at)::date FROM thesis_changes")).scalar()
        out["shares"] = {}
        for cik, acc, e, f, form, val in c.execute(text("""SELECT cik, accession, end_date, filed, form, val
                                                          FROM shares_outstanding_facts WHERE cik = ANY(:c)
                                                          ORDER BY cik, filed, end_date"""), {"c": ciks}):
            out["shares"].setdefault(cik, []).append([acc, e, f, form, val])
        c.rollback()
    if a.efts:
        from app.discovery.risks import TEXT_RISKS
        from app.discovery.sec_data import efts_search
        lo = start - timedelta(days=365)
        hits, truncated, queries = [], False, 0
        for key, (phrase, forms, _sev) in TEXT_RISKS.items():
            got = efts_search(phrase, forms, lo, end, max_hits=EFTS_MAX_HITS, ciks=ciks)
            queries += 1
            truncated |= len(got) >= EFTS_MAX_HITS
            hits += [{**h, "risk_key": key} for h in got if h["cik"] in ciks]
        out["efts_hits"] = hits
        out["efts_scan"] = {"done": True, "from": lo.isoformat(), "to": end.isoformat(), "truncated": truncated,
                            "queries": queries, "phrases": sorted(TEXT_RISKS)}
    json.dump(out, sys.stdout, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
