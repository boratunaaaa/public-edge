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
from pathlib import Path  # noqa: E402
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


def _batch_rows(c, ciks: list[str], pit: bool = False) -> dict[str, dict]:
    recs = {cik: {"cik": cik, "tenk": [], "tenk_other": [], "shares": [], "efts_hits": [], "actions": {},
                  "delisting_date": None} for cik in ciks}
    if pit:
        _pit_rows(c, ciks, recs)
    for r in c.execute(text("""SELECT accession, cik, form, accepted_at, filing_date, source_url, profile FROM tenk_filings
                               WHERE cik = ANY(:c) AND status = 'ok' ORDER BY cik, filing_date"""), {"c": ciks}).mappings():
        recs[r["cik"]]["tenk"].append(dict(r))
    # every other 10-K the ingestion saw (no sections, not found, fetch/extract failure, superseded): the coverage
    # must know about reports whose going-concern state was NOT profiled (a superseded row keeps its original
    # accession in the first 20 characters; one with an ok re-creation is matched there)
    for r in c.execute(text("""SELECT left(accession, 20) accession, cik, form, filing_date, status FROM tenk_filings
                               WHERE cik = ANY(:c) AND status <> 'ok' ORDER BY cik, filing_date"""), {"c": ciks}).mappings():
        recs[r["cik"]]["tenk_other"].append(dict(r))
    for cik, dd in c.execute(text("SELECT cik, delisting_date FROM security_master WHERE cik = ANY(:c)"), {"c": ciks}):
        recs[cik]["delisting_date"] = dd
    for cik, acc, e, f, form, val in ([] if pit else c.execute(text("""SELECT cik, accession, end_date, filed, form, val
                                                      FROM shares_outstanding_facts WHERE cik = ANY(:c)
                                                      ORDER BY cik, filed, end_date"""), {"c": ciks})):
        recs[cik]["shares"].append([acc, e, f, form, val])
    for cik, earliest, fetched in c.execute(text("""SELECT sm.cik, (SELECT min(action_date) FROM corporate_actions ca
                                                          WHERE ca.cik = sm.cik AND ca.source = 'sec_submissions'),
                                                        sm.submissions_fetched_at::date
                                                   FROM security_master sm WHERE sm.cik = ANY(:c)"""), {"c": ciks}):
        recs[cik]["actions"] = {"earliest": earliest, "fetched": fetched}
    return recs


def _pit_rows(c, ciks: list[str], recs: dict[str, dict]) -> None:
    """The point-in-time tables (app.pit.readers: no insider filing and no fact citing one is ever read): every issuer's
    dei cover-page share facts
    (the DILUTION input, same concept production stores in shares_outstanding_facts - but complete), the production
    runway concepts (cash, annual OCF), the 8-Ks of the THESIS proxy, the acceptance time of every filing behind them and
    the backfill state that proves completeness."""
    from app.pit import readers
    from app.pit.sources import RUNWAY_CASH, RUNWAY_OCF
    from app.replay.risk_history import THESIS_PROXY_ITEMS
    shares = readers.facts(c, ciks, ["EntityCommonStockSharesOutstanding"])
    runway = readers.facts(c, ciks, [RUNWAY_CASH, RUNWAY_OCF])
    acc = readers.acceptance(c, ciks)
    k8 = readers.filings(c, ciks, ["8-K", "8-K/A"])
    from app.replay.risk_history import FOREIGN_ANNUAL_FORMS, TENK_ANNUAL_FORMS
    annual = readers.filings(c, ciks, list(TENK_ANNUAL_FORMS + FOREIGN_ANNUAL_FORMS))
    prog = readers.progress(c, ciks)
    for cik in ciks:
        r = recs[cik]
        r["shares"] = [[f.accession, f.end, f.filed, f.form, int(f.val)] for f in shares.get(cik, [])]
        r["runway"] = [[f.family, f.concept, f.start, f.end, f.val, f.filed, f.accession, f.form] for f in runway.get(cik, [])]
        used = {f.accession for f in shares.get(cik, []) + runway.get(cik, [])}
        r["proxy_8k"] = [f for f in k8.get(cik, []) if any(i in (f.get("items") or "") for i in THESIS_PROXY_ITEMS)]
        used |= {f["accession"] for f in r["proxy_8k"]}
        r["acceptance"] = {a: t for a, t in acc.get(cik, {}).items() if a in used}
        r["pit"] = prog.get(cik, {})
        # every annual report the issuer filed (sec_filings: the ground truth for the going-concern coverage)
        r["annual"] = [{k: f[k] for k in ("accession", "form", "filing_date", "acceptance_at")} for f in annual.get(cik, [])]
        r["acceptance"].update({f["accession"]: f["acceptance_at"] for f in annual.get(cik, []) if f["acceptance_at"]})


def attach_store_hits(c, recs: dict, part: list[str], store_hits: dict, lo: date, pit: bool) -> None:
    """The store's documents of this batch: production phrases from `lo` (the scan window) as efts_hits, going-concern
    wording on any annual report as gc_hits, and - with the point-in-time tables - their exact acceptance times."""
    from app.discovery.risks import TEXT_RISKS
    from app.replay.risk_history import GC_FOREIGN_KEYS, GC_TEXT_KEYS
    from app.replay.risk_history import LATE_FILING_FORMS, LATE_FILING_KEY, late_filing_hits
    acc, nt = {}, {}
    if pit:
        from app.pit import readers
        acc = readers.acceptance(c, part)
        nt = readers.filings(c, part, list(LATE_FILING_FORMS))
    for cik in part:
        hs = [h for h in store_hits.get(cik, []) if not (pit and h["risk_key"] == LATE_FILING_KEY)]
        if pit:
            hs += late_filing_hits(cik, nt.get(cik, []))
        recs[cik]["efts_hits"] = [h for h in hs if h["risk_key"] in TEXT_RISKS and lo.isoformat() <= (h["file_date"] or "")]
        recs[cik]["gc_hits"] = [h for h in hs if h["risk_key"] in GC_TEXT_KEYS + GC_FOREIGN_KEYS]
        if pit:
            used = {h["adsh"] for h in recs[cik]["efts_hits"] + recs[cik]["gc_hits"]}
            recs[cik].setdefault("acceptance", {}).update({k: v for k, v in acc.get(cik, {}).items() if k in used})


ANNUAL_LOOKBACK_DAYS = 500  # the annual report public at the window start (one fiscal year + the filing deadline)


def _store_meta(meta: dict, store, ciks: list[str], start: date, end: date, lo: date) -> dict[str, list[dict]]:
    """Scan status from the full-text store (app.pit.fulltext.scan_status) and the hits of these issuers."""
    from app.discovery.risks import TEXT_RISKS
    from app.pit.fulltext import load_hits, scan_status
    from app.replay.risk_history import GC_FOREIGN_KEYS, GC_TEXT_KEYS
    from app.replay.risk_history import LATE_FILING_FORMS, LATE_FILING_KEY
    # LATE_FILING comes from the complete NT filing history (sec_filings), not from the store (risk_history)
    keys = sorted(k for k in TEXT_RISKS if not (meta.get("pit") and k == LATE_FILING_KEY))
    st = scan_status(store, keys, lo, end)
    meta["efts_scan"] = {"done": st["done"], "from": lo.isoformat(), "to": end.isoformat(), "truncated": st["truncated"],
                         "phrases": sorted(TEXT_RISKS), "batch": BATCH, "source": "store", "keys": st["keys"]}
    if meta.get("pit"):
        meta["late_filing"] = {"source": "sec_filings", "forms": list(LATE_FILING_FORMS)}
    glo = start - timedelta(days=ANNUAL_LOOKBACK_DAYS)
    gs = scan_status(store, sorted(GC_TEXT_KEYS + GC_FOREIGN_KEYS), glo, end)
    meta["gc_annual_scan"] = {"done": gs["done"], "from": glo.isoformat(), "to": end.isoformat(), "keys": gs["keys"]}
    return load_hits(store, sorted(set(TEXT_RISKS) | set(GC_FOREIGN_KEYS)), ciks, min(lo, glo), end)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ciks", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--efts", action="store_true", help="also scan the production TEXT_RISKS phrases (network)")
    ap.add_argument("--efts-store", help="read the production phrases from the full-text store of "
                                         "app.jobs.fulltext_backfill instead (no network; the store's completeness "
                                         "for [start - 365 days, end] becomes the scan status)")
    ap.add_argument("--source", choices=("pit", "production"), default="pit",
                    help="pit: shares / runway / actions / thesis proxy from the point-in-time tables (sec_filings, "
                         "pit_facts); production: the pre-2026-10 inputs (shares_outstanding_facts, no runway)")
    a = ap.parse_args()
    if a.efts_store and a.efts:
        ap.error("--efts and --efts-store are exclusive")
    if a.efts_store and a.source != "pit":
        # only the point-in-time path rebuilds LATE_FILING from the complete NT filing history; the store alone
        # falls short on deadline days and must never read as a complete FINANCING_TEXT scan
        ap.error("--efts-store needs --source pit")
    ensure_db_free_http()
    from app.db.session import get_engine
    ciks = sorted({c.strip().zfill(10) for c in a.ciks.split(",") if c.strip()})
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    eng = get_engine()
    meta: dict = {"ciks": ciks, "start": a.start, "end": a.end}
    pit = a.source == "pit"
    if pit:
        # the snapshot export (replay_export_snapshot --source pit) derives the corporate actions from sec_filings
        meta.update(pit=True, actions_source="sec_filings", annual_reports=True)
    with eng.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        meta["tenk_by_year"] = {str(int(y)): n for y, n in c.execute(text(
            "SELECT extract(year FROM filing_date), count(*) FROM tenk_filings GROUP BY 1 ORDER BY 1")).all()}
        meta["thesis_changes_from"] = c.execute(text("SELECT min(created_at)::date FROM thesis_changes")).scalar()
        # 10-K ingestion state per issuer (app.tenk.engine job states): not completed / given up / still queued
        from app.tenk.engine import HISTORY_YEARS
        st = dict(c.execute(text("SELECT name, state FROM processing_jobs WHERE name IN ('tenk_backfill', 'tenk_live_queue')")).all())
        bf, lq = st.get("tenk_backfill") or {}, st.get("tenk_live_queue") or {}
        want = set(ciks)
        meta["tenk_jobs"] = {"backfill_done": sorted(want & set(bf.get("done_ciks") or [])),
                             "backfill_gave_up": sorted(want & set(bf.get("gave_up") or [])),
                             "live_gave_up": sorted(want & set(lq.get("gave_up") or {})),
                             "live_queue": {k: v.get("first_seen") for k, v in (lq.get("queue") or {}).items() if k in want},
                             # market-wide: a failed / not yet published SEC daily index can hide ANY issuer's 10-K
                             "index_failed": sorted(k[len("index:"):] for k in (lq.get("gave_up") or {}) if k.startswith("index:")),
                             "index_pending": sorted(lq.get("index_pending") or []),
                             # the backfill covers a ROLLING horizon (HISTORY_YEARS back from each run); the latest
                             # possible start of what a completed issuer covers is the export date minus that horizon
                             "history_from": (date.today() - timedelta(days=365 * HISTORY_YEARS)).isoformat()}
        c.rollback()
    lo = start - timedelta(days=365)
    store_hits: dict[str, list[dict]] = {}
    if a.efts_store:
        store_hits = _store_meta(meta, Path(a.efts_store), ciks, start, end, lo)
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
            recs = _batch_rows(c, part, pit)
            c.rollback()
        if a.efts_store:
            from app.discovery.risks import TEXT_RISKS
            with eng.connect() as c:
                c.execute(text("SET TRANSACTION READ ONLY"))
                attach_store_hits(c, recs, part, store_hits, lo, pit)
                c.rollback()
            for key in TEXT_RISKS:
                efts_batches[key] = efts_batches.get(key, 0) + 1
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
                                "efts_batches": efts_batches if (a.efts or a.efts_store) else {}}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
