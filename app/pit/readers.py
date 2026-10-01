"""The only readers of the point-in-time source tables (pit_facts, sec_filings, pit_backfill_progress).

Every read excludes the insider ownership forms (app.pit.sources.INSIDER_FORMS) in SQL - filings by their form, facts
by their own form AND by the form of the filing they cite: the first 1,468 companies of the 2026-10-01 backfill were
stored before the job filtered them, so sec_filings is mixed and no consumer may rely on it being clean
(docs/PIT_BACKFILL_RUNBOOK.md). Production data has no XBRL fact citing an insider filing (0 of 881,597 checked), so
the fact filter changes nothing today; it keeps a future one from ever reaching DILUTION / CASH_RUNWAY."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.pit.sources import INSIDER_FORMS, PitFact

_NOT_INSIDER = "NOT (form = ANY(:insider))"


def _p(**kw) -> dict:
    return {"insider": sorted(INSIDER_FORMS), **kw}


def filings(conn: Connection, ciks: list[str], forms: list[str] | None = None) -> dict[str, list[dict]]:
    """cik -> filings (accession, form, filing_date, acceptance_at, items), oldest first; never an insider form."""
    out: dict[str, list[dict]] = {c: [] for c in ciks}
    rows = conn.execute(text(f"""SELECT cik, accession, form, filing_date, acceptance_at, items FROM sec_filings
                                 WHERE cik = ANY(:c) AND {_NOT_INSIDER}
                                   AND (CAST(:f AS text[]) IS NULL OR form = ANY(CAST(:f AS text[])))
                                 ORDER BY cik, filing_date, acceptance_at NULLS LAST, accession"""),
                        _p(c=ciks, f=forms)).mappings()
    for r in rows:
        out.setdefault(r["cik"], []).append(dict(r))
    return out


def acceptance(conn: Connection, ciks: list[str]) -> dict[str, dict[str, datetime]]:
    """cik -> {accession -> exact acceptance time} (non-insider filings of these issuers)."""
    out: dict[str, dict[str, datetime]] = {c: {} for c in ciks}
    for cik, acc, at in conn.execute(text(f"""SELECT cik, accession, acceptance_at FROM sec_filings WHERE cik = ANY(:c)
                                              AND acceptance_at IS NOT NULL AND {_NOT_INSIDER} ORDER BY cik, accession"""),
                                     _p(c=ciks)):
        out.setdefault(cik, {})[acc] = at
    return out


def facts(conn: Connection, ciks: list[str], concepts: list[str]) -> dict[str, list[PitFact]]:
    out: dict[str, list[PitFact]] = {c: [] for c in ciks}
    for r in conn.execute(text("""SELECT f.cik, f.family, f.concept, f.period_start, f.period_end, f.val, f.filed,
                                         f.accession, f.form
                                  FROM pit_facts f WHERE f.cik = ANY(:c) AND f.concept = ANY(:k)
                                    AND NOT (coalesce(f.form, '') = ANY(:insider))
                                    AND NOT EXISTS (SELECT 1 FROM sec_filings s WHERE s.cik = f.cik
                                                    AND s.accession = f.accession AND s.form = ANY(:insider))
                                  ORDER BY f.cik, f.filed, f.period_end, f.accession, f.concept, f.val"""),
                          _p(c=ciks, k=concepts)):
        out.setdefault(r.cik, []).append(PitFact(r.cik, r.family, r.concept, r.period_start, r.period_end, float(r.val),
                                                 r.filed, r.accession, r.form))
    return out


def progress(conn: Connection, ciks: list[str]) -> dict[str, dict[str, dict]]:
    """cik -> {job: {status, date}}: what the backfill verifiably holds for each issuer (absent = never processed)."""
    out: dict[str, dict[str, dict]] = {c: {} for c in ciks}
    for cik, job, st, at in conn.execute(text("""SELECT cik, job, status, updated_at FROM pit_backfill_progress
                                                 WHERE cik = ANY(:c) ORDER BY cik, job"""), {"c": ciks}):
        out.setdefault(cik, {})[job] = {"status": st, "date": at.date().isoformat() if isinstance(at, datetime) else
                                        (at.isoformat() if isinstance(at, date) else at)}
    return out


def depositary_since(conn: Connection, ciks: list[str]) -> dict[str, datetime]:
    """cik -> the first public moment of a depositary (ADR) programme filing (app.pit.sources.DEPOSITARY_FORMS)."""
    from app.pit.sources import DEPOSITARY_FORMS, filing_day_end
    out: dict[str, datetime] = {}
    for cik, rows in filings(conn, ciks, sorted(DEPOSITARY_FORMS)).items():
        ts = [r["acceptance_at"] or filing_day_end(r["filing_date"]) for r in rows]
        if ts:
            out[cik] = min(ts)
    return out
