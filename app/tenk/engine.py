"""10-K fetch -> fingerprint -> compare with the previous 10-K -> typed changes + TENK_CHANGE event.

Timing (no look-ahead): the event's public_at is the SEC acceptance timestamp from the submissions
JSON. EDGAR writes it with a trailing "Z" although the clock is US/Eastern; it is read as Eastern,
which can only make the entry LATER than reality, never earlier. The backtest enters at the first
tradable session after that moment. Fiscal period end and document date are never used.

Budget: 10-K documents are 1-15 MB. Live filings are processed daily (few per day). The historical
backfill is resumable (job_state cursor), runs in the LOW-priority backfill process and stops at a
per-run document / time budget, so it never delays live ingestion.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app import http
from app.db.models import tenk_changes, tenk_filings
from app.db.store import create_event, job_state, save_job_state
from app.tenk.diff import build_profile, compare, extract_items, html_to_text

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx"
MAX_DOC_BYTES = 25_000_000
ANNUAL_FORMS = {"10-K", "10-K405", "10-KT"}
CONSECUTIVE_MAX_DAYS = 550  # a "previous" 10-K older than ~18 months is not consecutive
HISTORY_YEARS = 5
MAX_CIK_FAILS = 5
MIN_FREE_BYTES = 10 * 1024**3  # the 10-K history stops well before the disk gets tight
EVENT_PRIORITY = 2  # below ALERT_MIN_PRIORITY: an unvalidated signal is recorded, not alerted
POSITIVE = {"RISK_DECREASE", "LIQUIDITY_IMPROVEMENT", "REMOVED_GOING_CONCERN", "NEW_CAPACITY_EXPANSION",
            "NEW_COMMERCIALIZATION_LANGUAGE", "NEW_MAJOR_CUSTOMER"}
NEGATIVE = {"RISK_INCREASE", "LIQUIDITY_DETERIORATION", "NEW_CUSTOMER_CONCENTRATION", "NEW_SUPPLY_RISK", "NEW_LEGAL_RISK",
            "NEW_REGULATORY_RISK", "NEW_GOING_CONCERN"}


def acceptance_to_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        naive = datetime.fromisoformat(s.replace("Z", "").split(".")[0])
    except ValueError:
        return None
    return naive.replace(tzinfo=ET).astimezone(timezone.utc)


def _annual_from_block(r: dict) -> list[dict]:
    out = []
    for i, form in enumerate(r.get("form") or []):
        if form not in ANNUAL_FORMS:
            continue
        try:
            out.append({"accession": r["accessionNumber"][i], "form": form, "filing_date": date.fromisoformat(r["filingDate"][i]),
                        "accepted_at": acceptance_to_utc(r.get("acceptanceDateTime", [None] * (i + 1))[i]),
                        "primary_doc": r["primaryDocument"][i], "period": r.get("reportDate", [None] * (i + 1))[i] or None})
        except (KeyError, IndexError, ValueError):
            continue
    return out


def annual_filings(sub: dict) -> list[dict]:
    """10-K filings from a submissions JSON (recent block only), oldest first."""
    return sorted(_annual_from_block((sub.get("filings") or {}).get("recent") or {}), key=lambda f: f["filing_date"])


ARCHIVE_URL = "https://data.sec.gov/submissions/{name}"


def all_annual_filings(sub: dict, since: date, getter=None) -> list[dict]:
    """10-Ks from the recent block PLUS every archived submissions file (filings.files) whose date range
    reaches back to `since` - long-lived filers have their older years only in those archives.
    Raises on an archive fetch error so the caller retries the company instead of storing a gap."""
    getter = getter or (lambda url: http.get(url, retries=2).json())
    out = {f["accession"]: f for f in _annual_from_block((sub.get("filings") or {}).get("recent") or {})}
    for fmeta in (sub.get("filings") or {}).get("files") or []:
        try:
            to = date.fromisoformat(fmeta.get("filingTo") or "9999-12-31")
        except ValueError:
            to = date.max
        if to < since:
            continue
        for f in _annual_from_block(getter(ARCHIVE_URL.format(name=fmeta["name"]))):
            out.setdefault(f["accession"], f)
    return sorted(out.values(), key=lambda f: f["filing_date"])


class FetchError(Exception):
    """Transient document fetch failure: the filing stays retryable."""


def fetch_text(cik: str, f: dict) -> str | None:
    """Document text; None = permanently missing (404/410); FetchError = transient (retry later)."""
    url = DOC_URL.format(cik=int(cik), acc=f["accession"].replace("-", ""), doc=f["primary_doc"])
    try:
        r = http.get(url, retries=2)
    except http.HttpError as e:
        log.info("10-K %s %s: %s", cik, f["accession"], e)
        if e.status in (404, 410):
            return None
        raise FetchError(str(e)) from e
    except Exception as e:  # timeouts / connection resets
        raise FetchError(f"{type(e).__name__}: {e}") from e
    raw = r.content[:MAX_DOC_BYTES].decode("utf-8", "ignore")
    return html_to_text(raw) if "<" in raw[:5000] else raw


def _doc_url(cik: str, f: dict) -> str:
    return DOC_URL.format(cik=int(cik), acc=f["accession"].replace("-", ""), doc=f["primary_doc"])


RETRYABLE = ("fetch_failed",)


def process_filing(engine: Engine, cik: str, ticker: str | None, name: str | None, f: dict, *, is_backfill: bool) -> dict:
    """Fingerprint one 10-K and compare it with the stored previous 10-K of the same CIK.
    A transient fetch failure is stored as retryable (status fetch_failed) and retried on the next run;
    when the filing later succeeds, its successor's comparison is recomputed too."""
    with engine.connect() as conn:
        existing = conn.execute(select(tenk_filings.c.id, tenk_filings.c.status)
                                .where(tenk_filings.c.accession == f["accession"])).first()
    if existing and existing[1] not in RETRYABLE:
        return {"skipped": "known"}
    vals = dict(cik=cik, ticker=ticker, form=f["form"], filing_date=f["filing_date"], accepted_at=f["accepted_at"],
                period=date.fromisoformat(f["period"]) if f.get("period") else None, source_url=_doc_url(cik, f),
                is_backfill=is_backfill)
    try:
        txt = fetch_text(cik, f)
    except FetchError as e:
        if not existing:
            with engine.begin() as conn:
                conn.execute(pg_insert(tenk_filings).values(accession=f["accession"], sections=[], profile={},
                                                            status="fetch_failed", **vals)
                             .on_conflict_do_nothing(index_elements=["accession"]))
        return {"status": "fetch_failed", "retryable": True, "error": str(e)[:200]}
    items = extract_items(txt) if txt else {}
    prof = build_profile(items)
    status = "ok" if len(items) >= 2 else ("no_sections" if txt else "not_found")
    with engine.begin() as conn:
        if existing:
            conn.execute(tenk_filings.update().where(tenk_filings.c.id == existing[0]).values(
                sections=sorted(items), profile=prof, status=status, **vals))
            fid = existing[0]
        else:
            fid = conn.execute(pg_insert(tenk_filings).values(accession=f["accession"], sections=sorted(items), profile=prof,
                                                              status=status, **vals)
                               .on_conflict_do_nothing(index_elements=["accession"]).returning(tenk_filings.c.id)).scalar()
        if fid is None or status != "ok":
            return {"status": status}
        row = dict(conn.execute(select(tenk_filings).where(tenk_filings.c.id == fid)).mappings().one())
        res = _compare_and_emit(conn, row, items, name)
        res["successor"] = _recompare_successor(conn, row, name)
        return res


def _prev_of(conn, row: dict) -> dict | None:
    r = conn.execute(select(tenk_filings).where(and_(
        tenk_filings.c.cik == row["cik"], tenk_filings.c.status == "ok", tenk_filings.c.filing_date < row["filing_date"],
        tenk_filings.c.filing_date >= row["filing_date"] - timedelta(days=CONSECUTIVE_MAX_DAYS)))
        .order_by(tenk_filings.c.filing_date.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


def _compare_and_emit(conn, row: dict, items: dict | None, name: str | None) -> dict:
    prev = _prev_of(conn, row)
    if not prev:
        return {"status": "first"}
    changes = compare(prev["profile"], row["profile"], items)
    fid, cik, ticker = row["id"], row["cik"], row["ticker"]
    conn.execute(tenk_filings.update().where(tenk_filings.c.id == fid).values(prev_accession=prev["accession"],
                                                                               n_changes=len(changes)))
    for c in changes:
        conn.execute(pg_insert(tenk_changes).values(
            filing_id=fid, cik=cik, ticker=ticker, filing_date=row["filing_date"], public_timestamp=row["accepted_at"],
            section=c.section, change_type=c.change_type, materiality=c.materiality, n_new=c.n_new, n_removed=c.n_removed,
            old_text_hash=c.old_text_hash, new_text_hash=c.new_text_hash, examples=c.examples,
            source_url=row["source_url"], prev_accession=prev["accession"],
        ).on_conflict_do_nothing(index_elements=["filing_id", "change_type"]))
    material = [c for c in changes if c.materiality in ("HIGH", "MEDIUM")]
    if not material:
        return {"status": "no_material_change", "changes": len(changes)}
    pos = sum(c.change_type in POSITIVE for c in material)
    neg = sum(c.change_type in NEGATIVE for c in material)
    direction = "POSITIVE" if pos > neg else "NEGATIVE" if neg > pos else "MIXED"
    types = sorted({c.change_type for c in material})
    acc = row["accession"]
    eid = create_event(
        conn, event_type="TENK_CHANGE", dedupe_key=f"tenk:{acc}", public_date=(
            row["accepted_at"].astimezone(ET).date() if row["accepted_at"] else row["filing_date"]),
        public_at=row["accepted_at"], ticker=ticker, company_name=name, issuer_cik=cik, is_backfill=row["is_backfill"],
        priority=EVENT_PRIORITY, date_quality="exact" if row["accepted_at"] else "date", source_url=row["source_url"],
        title=f"{ticker or cik} 10-K değişimi: {', '.join(types)}",
        payload={"accession": acc, "prev_accession": prev["accession"], "direction": direction,
                 "change_types": types, "high": sum(c.materiality == "HIGH" for c in material),
                 "changes": [{"type": c.change_type, "section": c.section, "materiality": c.materiality,
                              "n_new": c.n_new, "n_removed": c.n_removed} for c in material]})
    return {"status": "event" if eid else "event_exists", "changes": len(changes), "direction": direction}


def _recompare_successor(conn, row: dict, name: str | None) -> str | None:
    """A predecessor that arrives late (retried fetch, archive backfill) changes its successor's
    comparison. A successor stored as 'first' is simply compared now; one compared against an older
    filing is superseded (renamed + audited, never deleted) and re-created from its stored fingerprint."""
    nxt = conn.execute(select(tenk_filings).where(and_(
        tenk_filings.c.cik == row["cik"], tenk_filings.c.status == "ok", tenk_filings.c.filing_date > row["filing_date"],
        tenk_filings.c.filing_date <= row["filing_date"] + timedelta(days=CONSECUTIVE_MAX_DAYS)))
        .order_by(tenk_filings.c.filing_date).limit(1)).mappings().first()
    if not nxt:
        return None
    nxt = dict(nxt)
    if nxt["prev_accession"] == row["accession"]:
        return None
    if nxt["prev_accession"] is None:
        return _compare_and_emit(conn, nxt, None, name)["status"]
    from sqlalchemy import text as sql
    run_id = f"tenk-late-predecessor-{date.today().isoformat()}"
    # unique suffix per supersession: the same filing can be re-based more than once (10-KT + 10-K pairs)
    tag = f"#{nxt['id'] % 65536:04x}"  # row ids differ per re-creation; 20 + 5 = 25 chars
    conn.execute(sql("UPDATE tenk_filings SET status = 'superseded', accession = left(accession, 20) || :t WHERE id = :i"),
                 {"i": nxt["id"], "t": tag})
    conn.execute(sql("""UPDATE events SET superseded = true, superseded_reason = 'TENK_PREDECESSOR_ARRIVED',
                        dedupe_key = left(dedupe_key || '#rp' || :t, 200) WHERE dedupe_key = :k AND NOT superseded"""),
                 {"k": f"tenk:{nxt['accession']}", "t": f"{nxt['id']}"})
    conn.execute(sql("""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, before, after, reason, dry_run)
                        VALUES (:r, 'supersede', 'tenk_filings', :i, CAST(:b AS jsonb), '{"status": "superseded"}'::jsonb,
                                :why, false)"""),
                 {"r": run_id, "i": nxt["id"], "b": f'{{"accession": "{nxt["accession"]}", "prev": "{nxt["prev_accession"]}"}}',
                  "why": f"late predecessor {row['accession']} arrived; comparison recomputed"})
    new = {k: v for k, v in nxt.items() if k not in ("id", "created_at", "prev_accession", "n_changes")}
    nid = conn.execute(tenk_filings.insert().values(**new).returning(tenk_filings.c.id)).scalar_one()
    return _compare_and_emit(conn, {**new, "id": nid}, None, name)["status"]


def fetch_submissions(cik: str) -> dict:
    return http.get(SUBMISSIONS_URL.format(cik=cik.zfill(10)), retries=2).json()


def process_cik(engine: Engine, cik: str, ticker: str | None, name: str | None, *, history_from: date,
                live_from: date, max_docs: int) -> dict:
    """All not-yet-stored 10-Ks of one CIK filed on/after history_from (plus the one before it, so the
    first of them has a predecessor). Filings before live_from are stored as backfill."""
    cik = cik.zfill(10)
    try:
        sub = fetch_submissions(cik)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:300]}
    since = history_from - timedelta(days=CONSECUTIVE_MAX_DAYS)
    try:
        fl = [f for f in all_annual_filings(sub, since) if f["filing_date"] >= since]
    except Exception as e:  # an archive file failed: retry the whole company later, never store a gap
        return {"error": f"archive: {type(e).__name__}: {e}"[:300]}
    with engine.connect() as conn:
        known = {a for (a,) in conn.execute(select(tenk_filings.c.accession).where(and_(
            tenk_filings.c.cik == cik, tenk_filings.c.status.notin_(RETRYABLE)))).all()}
    pending = [f for f in fl if f["accession"] not in known]
    todo = pending[:max_docs]  # oldest first: every predecessor is stored before its successor
    res = {"docs": 0, "events": 0, "failed": 0, "remaining": len(pending) - len(todo)}
    for f in todo:  # oldest first, so each one finds its predecessor
        tk = ticker
        if tk is None or f["filing_date"] < live_from:  # the ticker AT the filing date, never today's
            with engine.connect() as conn:
                from app.securitymaster.provider import get_security_master
                tk = get_security_master(conn).resolve_ticker_at_date(cik, f["filing_date"]) or tk
        r = process_filing(engine, cik, tk, name or sub.get("name"), f, is_backfill=f["filing_date"] < live_from)
        res["docs"] += 1
        res["failed"] += bool(r.get("retryable"))
        res["events"] += r.get("status") == "event"
    return res


def daily_10k_ciks(d: date) -> set[str]:
    url = DAILY_INDEX_URL.format(y=d.year, q=(d.month - 1) // 3 + 1, d=d.strftime("%Y%m%d"))
    try:
        txt = http.get(url, retries=2).text
    except http.HttpError as e:
        if e.status in (403, 404):
            return set()
        raise
    out = set()
    for line in txt.splitlines():
        if line[:12].strip() in ANNUAL_FORMS:
            parts = line.split()
            if len(parts) >= 4 and parts[-1].startswith("edgar/"):
                out.add(parts[-3].zfill(10))
    return out


def _universe(conn) -> dict[str, dict]:
    from app.discovery.evidence import universe_ciks
    return universe_ciks(conn, 0, 1e13)


def run_live(engine: Engine, days: int = 4, max_docs: int = 60) -> dict:
    """New 10-Ks of listed companies from the last `days` daily indexes."""
    today = date.today()
    with engine.connect() as conn:
        uni = _universe(conn)
    ciks: set[str] = set()
    for i in range(days):
        ciks |= daily_10k_ciks(today - timedelta(days=i)) & set(uni)
    out = {"ciks": len(ciks), "docs": 0, "events": 0, "failed": 0, "errors": 0}
    for cik in sorted(ciks):
        if out["docs"] >= max_docs:
            break
        u = uni.get(cik) or {}
        r = process_cik(engine, cik, u.get("ticker"), u.get("name"), history_from=today - timedelta(days=days + 2),
                        live_from=today - timedelta(days=days + 2), max_docs=2)
        out["docs"] += r.get("docs", 0)
        out["events"] += r.get("events", 0)
        out["failed"] += r.get("failed", 0)
        out["errors"] += "error" in r
    out["retry_queue"] = retry_failed(engine, uni)
    return out


def retry_failed(engine: Engine, uni: dict | None = None, max_ciks: int = 40) -> dict:
    """Persistent retry queue: every filing stored as fetch_failed is retried here, independent of the
    daily-index window and of the backfill's done list, until it succeeds (or is permanently missing)."""
    with engine.connect() as conn:
        rows = conn.execute(select(tenk_filings.c.cik, tenk_filings.c.filing_date, tenk_filings.c.is_backfill)
                            .where(tenk_filings.c.status.in_(RETRYABLE)).order_by(tenk_filings.c.filing_date)).all()
    first: dict[str, date] = {}
    for cik, d, _ in rows:
        first.setdefault(cik, d)
    out = {"queued": len(rows), "ciks": 0, "recovered": 0, "still_failed": 0}
    live_from = date.today() - timedelta(days=6)
    for cik in list(first)[:max_ciks]:
        u = (uni or {}).get(cik) or {}
        r = process_cik(engine, cik, u.get("ticker"), u.get("name"), history_from=first[cik] + timedelta(days=CONSECUTIVE_MAX_DAYS),
                        live_from=live_from, max_docs=HISTORY_YEARS + 2)
        out["ciks"] += 1
        out["still_failed"] += r.get("failed", 0) + ("error" in r)
    with engine.connect() as conn:
        left = conn.execute(select(tenk_filings.c.id).where(tenk_filings.c.status.in_(RETRYABLE))).all()
    out["recovered"] = max(0, len(rows) - len(left))
    out["remaining"] = len(left)
    return out


FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/form.idx"


def quarter_10k_ciks(y: int, q: int) -> list[tuple[str, date]]:
    try:
        txt = http.get(FULL_INDEX_URL.format(y=y, q=q), retries=2).text
    except http.HttpError as e:
        if e.status in (403, 404):
            return []
        raise
    out = {}
    for line in txt.splitlines():
        if line[:12].strip() in ANNUAL_FORMS:
            parts = line.split()
            try:
                out.setdefault(parts[-3].zfill(10), date.fromisoformat(parts[-2]))
            except (ValueError, IndexError):
                continue
    return sorted(out.items())


def _quarters(years: int) -> list[str]:
    today = date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(years * 4 + 1):
        out.append(f"{y}Q{q}")
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    return out


def run_backfill(engine: Engine, max_docs: int = 120, time_budget_s: float = 900, years: int = HISTORY_YEARS) -> dict:
    """Resumable, survivorship-free historical pass. The universe is point-in-time: every 10-K filer in
    the EDGAR full index of a quarter that had an exchange ticker AT THAT filing date (delisted and
    merged companies included). Quarters newest first; per CIK all 10-Ks of the window at once."""
    import shutil
    from app.securitymaster.provider import get_security_master
    if shutil.disk_usage("/").free < MIN_FREE_BYTES:
        return {"skipped": "LOW_DISK", "free_gb": round(shutil.disk_usage("/").free / 1024**3, 1)}
    name = "tenk_backfill"
    t0 = time.time()
    with engine.begin() as conn:
        st = job_state(conn, name)
    done = set(st.get("done_ciks") or [])
    finished_q = set(st.get("done_quarters") or [])
    fails: dict[str, int] = dict(st.get("fail_counts") or {})
    gave_up = set(st.get("gave_up") or [])
    history_from = date.today() - timedelta(days=365 * years)
    live_from = date.today() - timedelta(days=6)
    out = {"ciks": 0, "docs": 0, "events": 0, "not_listed": 0}
    for qk in _quarters(years):
        if qk in finished_q:
            continue
        if out["docs"] >= max_docs or time.time() - t0 > time_budget_s:
            break
        filers = quarter_10k_ciks(int(qk[:4]), int(qk[-1]))
        complete = True
        for cik, fdate in filers:
            if cik in done:
                continue
            if out["docs"] >= max_docs or time.time() - t0 > time_budget_s:
                complete = False
                break
            with engine.connect() as conn:
                tk = get_security_master(conn).resolve_ticker_at_date(cik, fdate)
            if not tk:
                out["not_listed"] += 1
                done.add(cik)  # not exchange-listed at that date (funds, ABS trusts, private debt issuers)
                continue
            r = process_cik(engine, cik, None, None, history_from=history_from, live_from=live_from, max_docs=years + 2)
            if "error" in r or r.get("failed"):
                # keep the quarter open so this company is retried on the next run; after MAX_CIK_FAILS
                # attempts it is recorded as given up (visible in job_state), not silently dropped
                fails[cik] = fails.get(cik, 0) + 1
                out["failed"] = out.get("failed", 0) + 1
                out["docs"] += r.get("docs", 0)
                if fails[cik] >= MAX_CIK_FAILS:
                    gave_up.add(cik)
                    done.add(cik)
                else:
                    complete = False
                continue
            fails.pop(cik, None)
            out["docs"] += r["docs"]
            out["events"] += r["events"]
            if r.get("remaining"):
                complete = False  # the per-run document cap left older reports: continue this company next run
                out["partial"] = out.get("partial", 0) + 1
                continue
            done.add(cik)
            out["ciks"] += 1
        if complete and filers:
            finished_q.add(qk)
        with engine.begin() as conn:
            save_job_state(conn, name, {"done_ciks": sorted(done), "done_quarters": sorted(finished_q),
                                        "fail_counts": fails, "gave_up": sorted(gave_up)}, status="idle")
    out["quarters_done"] = len(finished_q)
    out["gave_up"] = len(gave_up)
    return out


def supersede_old_classifier(engine: Engine, dry_run: bool = True) -> dict:
    """Filings fingerprinted by an older classifier are re-processed, never deleted: the old filing row
    is renamed (accession + '#v<n>') and marked superseded, its TENK_CHANGE event is superseded (kept,
    excluded from statistics) and every change is written to data_cleanup_audit."""
    import uuid

    from sqlalchemy import text as sql

    from app.tenk.diff import CLASSIFIER_VERSION
    run_id = f"tenk-reclassify-{date.today().isoformat()}-{uuid.uuid4().hex[:6]}"
    with engine.begin() as conn:
        old = conn.execute(sql("""SELECT id, accession, COALESCE((profile->'_meta'->>'v')::int, 1) v FROM tenk_filings
                                  WHERE status <> 'superseded' AND COALESCE((profile->'_meta'->>'v')::int, 1) < :v"""),
                           {"v": CLASSIFIER_VERSION}).mappings().all()
        evs = conn.execute(sql("""SELECT id, dedupe_key FROM events WHERE event_type = 'TENK_CHANGE' AND NOT superseded
                                  AND dedupe_key = ANY(:k)"""), {"k": [f"tenk:{r['accession']}" for r in old]}).mappings().all()
        out = {"run_id": run_id, "dry_run": dry_run, "filings": len(old), "events": len(evs)}
        if dry_run:
            return out
        for r in old:
            new_acc = f"{r['accession']}#v{r['v']}"[:25]
            conn.execute(sql("UPDATE tenk_filings SET status = 'superseded', accession = :a WHERE id = :i"), {"a": new_acc, "i": r["id"]})
            conn.execute(sql("""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, before, after, reason, dry_run)
                                VALUES (:r, 'supersede', 'tenk_filings', :i, CAST(:b AS jsonb), CAST(:a AS jsonb), :why, false)"""),
                         {"r": run_id, "i": r["id"], "b": f'{{"accession": "{r["accession"]}", "status": "ok"}}',
                          "a": f'{{"accession": "{new_acc}", "status": "superseded"}}',
                          "why": f"10-K classifier v{r['v']} -> v{CLASSIFIER_VERSION} (going-concern boilerplate fix)"})
        for e in evs:
            conn.execute(sql("""UPDATE events SET superseded = true, superseded_reason = 'TENK_CLASSIFIER_OLD',
                                dedupe_key = left(dedupe_key || '#old', 200) WHERE id = :i"""), {"i": e["id"]})
            conn.execute(sql("""INSERT INTO data_cleanup_audit (run_id, action, table_name, row_id, before, after, reason, dry_run)
                                VALUES (:r, 'supersede', 'events', :i, CAST(:b AS jsonb), '{"superseded": true}'::jsonb, :why, false)"""),
                         {"r": run_id, "i": e["id"], "b": f'{{"dedupe_key": "{e["dedupe_key"]}"}}',
                          "why": "TENK_CHANGE from an older 10-K classifier; re-created by the current one"})
        save_job_state(conn, "tenk_backfill", {}, status="idle")  # re-walk the quarters; unchanged filings are skipped
    return out
