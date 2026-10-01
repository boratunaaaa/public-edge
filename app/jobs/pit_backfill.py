"""Point-in-time source backfills for historical replay (replay / research data only - no live decision reads them).

    python -m app.jobs.pit_backfill xbrl        [--ciks A,B | --limit N] [--apply] [--max-minutes M]
    python -m app.jobs.pit_backfill submissions [--ciks A,B | --limit N] [--apply] [--max-minutes M]

xbrl         SEC companyfacts -> pit_facts: cash, operating cash flow, debt, shares with the disclosing filing
             (accession, filed date, form); acceptance times come from sec_filings (visibility: app.pit.sources).
submissions  SEC submissions -> sec_filings: the "recent" block AND every older page (filings.files), with form,
             (insider ownership forms 3 / 4 / 5 excepted: INSIDER_FORMS)
             filing / report date, acceptance time, 8-K items, primary document; issuer identity = the requested CIK.
             production corporate_actions is NOT written (older actions would change live financing flags).

Default is a DRY RUN: fetch + parse a few companies, write nothing, print counts, bytes and a time / disk estimate.
--apply writes. Every company is its own transaction and its own progress row (pit_backfill_progress): a re-run skips
done companies (idempotent: facts / filings are upserted on their natural keys), a failing company is recorded
(attempts + error) and the job continues; after MAX_ATTEMPTS it is left alone. The job stops cleanly - everything
committed so far is kept - when free disk falls under MIN_FREE_GB or --max-minutes is reached. SEC requests use the
production HTTP client (shared cross-process SEC budget with PE_SHARED_RATELIMIT=1, retries with backoff)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app import http
from app.pit.sources import INSIDER_FORMS

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
PAGE_URL = "https://data.sec.gov/submissions/{name}"
MIN_FREE_GB = 8.0
MAX_ATTEMPTS = 3
BATCH = 1000
# insider ownership reports (~46% of all filings, app.pit.sources.INSIDER_FORMS): production ingests Form 4 through its
# own pipeline and they carry no financing / dilution information - not stored, so the complete history fits the disk


def free_gb(path: str = "/") -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def _get_json(url: str):
    """None on 404 (the issuer has no such document); other HTTP errors raise after the client's own retries."""
    try:
        return http.get(url, retries=3).json()
    except http.HttpError as e:
        if e.status == 404:
            return None
        raise


# ------------------------------------------------------------------ per-company work (pure fetch + parse)

def fetch_xbrl(cik: str) -> tuple[list[dict], int]:
    from app.pit.sources import parse_companyfacts
    r = None
    try:
        r = http.get(COMPANYFACTS_URL.format(cik=cik), retries=3)
    except http.HttpError as e:
        if e.status != 404:
            raise
        return [], 0
    facts = parse_companyfacts(cik, r.json())
    return [{"cik": f.cik, "family": f.family, "concept": f.concept, "period_start": f.start, "period_end": f.end,
             "val": f.val, "filed": f.filed, "accession": f.accession, "form": f.form} for f in facts], len(r.content)


def _rows_from_block(cik: str, block: dict, source: str) -> list[dict]:
    from app.pit.sources import acceptance_map
    acc_at = acceptance_map(block)
    n = len(block.get("accessionNumber") or [])

    def col(c, i):
        v = block.get(c) or []
        return v[i] if i < len(v) else None
    out = []
    for i in range(n):
        acc = col("accessionNumber", i)
        fd = col("filingDate", i)
        if not acc or not fd or (col("form", i) or "") in INSIDER_FORMS:
            continue
        try:
            fdate = date.fromisoformat(fd)
        except ValueError:
            continue
        rd = col("reportDate", i)
        try:
            rdate = date.fromisoformat(rd) if rd else None
        except ValueError:
            rdate = None
        out.append({"cik": cik, "accession": acc, "form": (col("form", i) or "")[:24], "filing_date": fdate,
                    "report_date": rdate, "acceptance_at": acc_at.get(acc), "items": col("items", i) or None,
                    "primary_document": col("primaryDocument", i) or None, "source_block": source})
    return out


def fetch_submissions(cik: str) -> tuple[list[dict], int, int]:
    """(rows of recent + every older page, bytes, pages). The same accession on two blocks is kept once, with the
    first non-empty value of every field (app.pit.sources.merge_submission_pages semantics)."""
    d = _get_json(SUBMISSIONS_URL.format(cik=cik))
    if d is None:
        return [], 0, 0
    nbytes = len(json.dumps(d))
    blocks = [((d.get("filings") or {}).get("recent") or {}, "recent")]
    files = (d.get("filings") or {}).get("files") or []
    for f in files:
        name = f.get("name")
        if not name:
            continue
        page = _get_json(PAGE_URL.format(name=name))
        if page is None:
            raise RuntimeError(f"older submissions page {name} listed but not found")  # never a silent gap
        nbytes += len(json.dumps(page))
        blocks.append((page, name[:64]))
    rows: dict[str, dict] = {}
    for block, source in blocks:
        for r in _rows_from_block(cik, block, source):
            if r["accession"] not in rows:
                rows[r["accession"]] = r
            else:
                rows[r["accession"]] = {k: (v if v not in (None, "") else r[k]) for k, v in rows[r["accession"]].items()}
    return list(rows.values()), nbytes, len(files)


# ------------------------------------------------------------------ storage (one transaction per company)

def store(engine: Engine, job: str, cik: str, rows: list[dict], run_id: str) -> int:
    from app.db.models import pit_backfill_progress, pit_facts, sec_filings
    with engine.begin() as c:
        c.execute(text("SET LOCAL lock_timeout = '10s'"))
        for i in range(0, len(rows), BATCH):
            part = [{**r, "run_id": run_id} for r in rows[i:i + BATCH]]
            if job == "xbrl":
                c.execute(pg_insert(pit_facts).values(part).on_conflict_do_nothing(
                    index_elements=["cik", "concept", "period_end", "period_start", "accession", "val"]))
            else:
                stmt = pg_insert(sec_filings).values(part)
                # a later run may learn an acceptance time / items an earlier run lacked: fill, never overwrite
                c.execute(stmt.on_conflict_do_update(index_elements=["cik", "accession"], set_={
                    "acceptance_at": text("coalesce(sec_filings.acceptance_at, excluded.acceptance_at)"),
                    "items": text("coalesce(sec_filings.items, excluded.items)"),
                    "report_date": text("coalesce(sec_filings.report_date, excluded.report_date)"),
                    "primary_document": text("coalesce(sec_filings.primary_document, excluded.primary_document)")}))
        c.execute(pg_insert(pit_backfill_progress).values(job=job, cik=cik, status="done" if rows else "not_found",
                                                          rows=len(rows), attempts=1, run_id=run_id)
                  .on_conflict_do_update(index_elements=["job", "cik"], set_={
                      "status": "done" if rows else "not_found", "rows": len(rows), "error": None, "run_id": run_id,
                      "attempts": pit_backfill_progress.c.attempts + 1, "updated_at": datetime.now(timezone.utc)}))
    return len(rows)


def record_failure(engine: Engine, job: str, cik: str, err: str, run_id: str) -> None:
    from app.db.models import pit_backfill_progress
    with engine.begin() as c:
        c.execute(pg_insert(pit_backfill_progress).values(job=job, cik=cik, status="failed", attempts=1, error=err[:500],
                                                          run_id=run_id)
                  .on_conflict_do_update(index_elements=["job", "cik"], set_={
                      "status": "failed", "error": err[:500], "run_id": run_id,
                      "attempts": pit_backfill_progress.c.attempts + 1, "updated_at": datetime.now(timezone.utc)}))


def todo(engine: Engine, job: str, ciks: list[str] | None, limit: int | None) -> list[str]:
    """Companies still to do: not done / not_found, and failed fewer than MAX_ATTEMPTS times; CIK order."""
    with engine.connect() as c:
        universe = ciks or [r[0] for r in c.execute(text("SELECT cik FROM security_master ORDER BY cik"))]
        skip = {r[0] for r in c.execute(text("""SELECT cik FROM pit_backfill_progress WHERE job = :j
                                                AND (status IN ('done', 'not_found') OR attempts >= :m)"""),
                                        {"j": job, "m": MAX_ATTEMPTS})}
    out = [c.zfill(10) for c in universe if c.zfill(10) not in skip]
    return out[:limit] if limit else out


# ------------------------------------------------------------------ driver

def run(engine: Engine, job: str, *, ciks: list[str] | None = None, limit: int | None = None, apply: bool = False,
        max_minutes: float | None = None, min_free_gb: float = MIN_FREE_GB, sample: int = 5) -> dict:
    if job not in ("xbrl", "submissions"):
        raise ValueError(job)
    run_id = f"pit-{job}-{datetime.now(timezone.utc):%Y%m%dT%H%M}-{uuid.uuid4().hex[:4]}"
    queue = todo(engine, job, ciks, limit if apply else (limit or sample))
    t0, out = time.time(), {"job": job, "mode": "APPLY" if apply else "DRY_RUN", "run_id": run_id, "queued": len(queue),
                            "done": 0, "not_found": 0, "failed": 0, "rows": 0, "bytes": 0, "pages": 0, "errors": {},
                            "exact_acceptance": 0, "value_conflicts": 0}
    for cik in queue:
        if free_gb() < min_free_gb:
            out["stopped"] = f"free disk {free_gb():.1f} GB < {min_free_gb} GB"
            break
        if max_minutes and (time.time() - t0) / 60 >= max_minutes:
            out["stopped"] = f"time budget {max_minutes} min reached"
            break
        try:
            if job == "xbrl":
                rows, nbytes = fetch_xbrl(cik)
                pages = 0
            else:
                rows, nbytes, pages = fetch_submissions(cik)
        except Exception as e:  # noqa: BLE001 - one company never stops the job
            out["failed"] += 1
            if len(out["errors"]) < 20:
                out["errors"][cik] = f"{type(e).__name__}: {e}"[:200]
            if apply:
                record_failure(engine, job, cik, f"{type(e).__name__}: {e}", run_id)
            continue
        out["bytes"] += nbytes
        out["pages"] += pages
        out["rows"] += len(rows)
        if job == "xbrl" and rows:  # drift monitor: facts whose filing has an exact acceptance time in sec_filings
            with engine.connect() as c:
                known = set(c.execute(text("SELECT accession FROM sec_filings WHERE cik = :c AND acceptance_at IS NOT NULL"),
                                      {"c": cik}).scalars())
            out["exact_acceptance"] += sum(1 for r in rows if r["accession"] in known)
            # the same fact (concept, period, filing) with two values: both are stored, made visible here
            keys = [(r["concept"], r["period_start"], r["period_end"], r["accession"]) for r in rows]
            out["value_conflicts"] += len(keys) - len(set(keys))
        out["done" if rows else "not_found"] += 1
        if apply:
            store(engine, job, cik, rows, run_id)
    out["seconds"] = round(time.time() - t0, 1)
    if job == "xbrl":
        out["exact_acceptance_share"] = round(out["exact_acceptance"] / out["rows"], 4) if out["rows"] else None
    n = out["done"] + out["not_found"]
    if n:
        with engine.connect() as c:
            remaining = len(todo(engine, job, ciks, None)) if apply else len(ciks or []) or c.execute(
                text("SELECT count(*) FROM security_master")).scalar()
        per = out["seconds"] / n
        out["estimate"] = {"companies": remaining, "hours": round(per * remaining / 3600, 2),
                           "rows": int(out["rows"] / n * remaining),
                           "db_gb": round(out["rows"] / n * remaining * (260 if job == "xbrl" else 330) / 1024 ** 3, 2),
                           "transfer_gb": round(out["bytes"] / n * remaining / 1024 ** 3, 1)}
    if apply:  # companies abandoned after MAX_ATTEMPTS: never silently lost
        with engine.connect() as c:
            out["gave_up"] = c.execute(text("""SELECT count(*) FROM pit_backfill_progress WHERE job = :j
                                               AND status = 'failed' AND attempts >= :m"""),
                                       {"j": job, "m": MAX_ATTEMPTS}).scalar()
    out["free_gb"] = round(free_gb(), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=("xbrl", "submissions"))
    ap.add_argument("--ciks")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-minutes", type=float)
    a = ap.parse_args()
    if a.apply and os.environ.get("PE_SHARED_RATELIMIT") != "1":
        # without it app.http falls back to a process-local bucket and SEC sees live workers + this job
        ap.error("--apply needs PE_SHARED_RATELIMIT=1 (one SEC budget shared with the live workers)")
    from app.db.session import get_engine
    ciks = [c.strip().zfill(10) for c in a.ciks.split(",")] if a.ciks else None
    print(json.dumps(run(get_engine(), a.job, ciks=ciks, limit=a.limit, apply=a.apply, max_minutes=a.max_minutes),
                     indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
