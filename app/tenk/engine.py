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
HISTORY_YEARS = 6
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


def annual_filings(sub: dict) -> list[dict]:
    """10-K filings from a submissions JSON (recent block), oldest first."""
    r = (sub.get("filings") or {}).get("recent") or {}
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
    return sorted(out, key=lambda f: f["filing_date"])


def fetch_text(cik: str, f: dict) -> str | None:
    url = DOC_URL.format(cik=int(cik), acc=f["accession"].replace("-", ""), doc=f["primary_doc"])
    try:
        r = http.get(url, retries=2)
    except http.HttpError as e:
        log.info("10-K %s %s: %s", cik, f["accession"], e)
        return None
    raw = r.content[:MAX_DOC_BYTES].decode("utf-8", "ignore")
    return html_to_text(raw) if "<" in raw[:5000] else raw


def _doc_url(cik: str, f: dict) -> str:
    return DOC_URL.format(cik=int(cik), acc=f["accession"].replace("-", ""), doc=f["primary_doc"])


def process_filing(engine: Engine, cik: str, ticker: str | None, name: str | None, f: dict, *, is_backfill: bool) -> dict:
    """Fingerprint one 10-K and compare it with the stored previous 10-K of the same CIK."""
    with engine.connect() as conn:
        if conn.execute(select(tenk_filings.c.id).where(tenk_filings.c.accession == f["accession"])).first():
            return {"skipped": "known"}
    txt = fetch_text(cik, f)
    items = extract_items(txt) if txt else {}
    prof = build_profile(items)
    status = "ok" if len(items) >= 2 else ("no_sections" if txt else "fetch_failed")
    with engine.begin() as conn:
        fid = conn.execute(pg_insert(tenk_filings).values(
            cik=cik, ticker=ticker, accession=f["accession"], form=f["form"], filing_date=f["filing_date"],
            accepted_at=f["accepted_at"], period=date.fromisoformat(f["period"]) if f.get("period") else None,
            source_url=_doc_url(cik, f), sections=sorted(items), profile=prof, status=status, is_backfill=is_backfill,
        ).on_conflict_do_nothing(index_elements=["accession"]).returning(tenk_filings.c.id)).scalar()
        if fid is None or status != "ok":
            return {"status": status}
        prev = conn.execute(select(tenk_filings).where(and_(
            tenk_filings.c.cik == cik, tenk_filings.c.status == "ok", tenk_filings.c.filing_date < f["filing_date"],
            tenk_filings.c.filing_date >= f["filing_date"] - timedelta(days=CONSECUTIVE_MAX_DAYS)))
            .order_by(tenk_filings.c.filing_date.desc()).limit(1)).mappings().first()
        if not prev:
            return {"status": "first"}
        changes = compare(prev["profile"], prof, items)
        conn.execute(tenk_filings.update().where(tenk_filings.c.id == fid).values(prev_accession=prev["accession"],
                                                                                   n_changes=len(changes)))
        for c in changes:
            conn.execute(pg_insert(tenk_changes).values(
                filing_id=fid, cik=cik, ticker=ticker, filing_date=f["filing_date"], public_timestamp=f["accepted_at"],
                section=c.section, change_type=c.change_type, materiality=c.materiality, n_new=c.n_new, n_removed=c.n_removed,
                old_text_hash=c.old_text_hash, new_text_hash=c.new_text_hash, examples=c.examples,
                source_url=_doc_url(cik, f), prev_accession=prev["accession"],
            ).on_conflict_do_nothing(index_elements=["filing_id", "change_type"]))
        material = [c for c in changes if c.materiality in ("HIGH", "MEDIUM")]
        if not material:
            return {"status": "no_material_change", "changes": len(changes)}
        pos = sum(c.change_type in POSITIVE for c in material)
        neg = sum(c.change_type in NEGATIVE for c in material)
        direction = "POSITIVE" if pos > neg else "NEGATIVE" if neg > pos else "MIXED"
        types = sorted({c.change_type for c in material})
        eid = create_event(
            conn, event_type="TENK_CHANGE", dedupe_key=f"tenk:{f['accession']}", public_date=(
                f["accepted_at"].astimezone(ET).date() if f["accepted_at"] else f["filing_date"]),
            public_at=f["accepted_at"], ticker=ticker, company_name=name, issuer_cik=cik, is_backfill=is_backfill,
            priority=EVENT_PRIORITY, date_quality="exact" if f["accepted_at"] else "date", source_url=_doc_url(cik, f),
            title=f"{ticker or cik} 10-K değişimi: {', '.join(types)}",
            payload={"accession": f["accession"], "prev_accession": prev["accession"], "direction": direction,
                     "change_types": types, "high": sum(c.materiality == "HIGH" for c in material),
                     "changes": [{"type": c.change_type, "section": c.section, "materiality": c.materiality,
                                  "n_new": c.n_new, "n_removed": c.n_removed} for c in material]})
        return {"status": "event" if eid else "event_exists", "changes": len(changes), "direction": direction}


def process_cik(engine: Engine, cik: str, ticker: str | None, name: str | None, *, history_from: date,
                live_from: date, max_docs: int) -> dict:
    """All not-yet-stored 10-Ks of one CIK filed on/after history_from (plus the one before it, so the
    first of them has a predecessor). Filings before live_from are stored as backfill."""
    cik = cik.zfill(10)
    try:
        sub = http.get(SUBMISSIONS_URL.format(cik=cik), retries=2).json()
    except http.HttpError as e:
        return {"error": str(e)}
    fl = [f for f in annual_filings(sub) if f["filing_date"] >= history_from - timedelta(days=CONSECUTIVE_MAX_DAYS)]
    with engine.connect() as conn:
        known = {a for (a,) in conn.execute(select(tenk_filings.c.accession).where(tenk_filings.c.cik == cik)).all()}
    todo = [f for f in fl if f["accession"] not in known][-max_docs:]
    res = {"docs": 0, "events": 0}
    for f in todo:  # oldest first, so each one finds its predecessor
        tk = ticker
        if tk is None or f["filing_date"] < live_from:  # the ticker AT the filing date, never today's
            with engine.connect() as conn:
                from app.securitymaster.provider import get_security_master
                tk = get_security_master(conn).resolve_ticker_at_date(cik, f["filing_date"]) or tk
        r = process_filing(engine, cik, tk, name or sub.get("name"), f, is_backfill=f["filing_date"] < live_from)
        res["docs"] += 1
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
    out = {"ciks": len(ciks), "docs": 0, "events": 0}
    for cik in sorted(ciks):
        if out["docs"] >= max_docs:
            break
        u = uni.get(cik) or {}
        r = process_cik(engine, cik, u.get("ticker"), u.get("name"), history_from=today - timedelta(days=days + 2),
                        live_from=today - timedelta(days=days + 2), max_docs=2)
        out["docs"] += r.get("docs", 0)
        out["events"] += r.get("events", 0)
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
    from app.securitymaster.provider import get_security_master
    name = "tenk_backfill"
    t0 = time.time()
    with engine.begin() as conn:
        st = job_state(conn, name)
    done = set(st.get("done_ciks") or [])
    finished_q = set(st.get("done_quarters") or [])
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
            if "error" in r:
                continue
            done.add(cik)
            out["ciks"] += 1
            out["docs"] += r["docs"]
            out["events"] += r["events"]
        if complete and filers:
            finished_q.add(qk)
        with engine.begin() as conn:
            save_job_state(conn, name, {"done_ciks": sorted(done), "done_quarters": sorted(finished_q)}, status="idle")
    out["quarters_done"] = len(finished_q)
    return out
