"""Historical SEC backfill.

Form 3/4/5: SEC "Insider Transactions Data Sets" (official quarterly TSV extracts).
  Only open-market purchases (P) and sales (S) are stored to respect the shared server disk.
  The datasets have filing DATE only (no time) -> backtest entry is the next session's open.
Schedule 13D: EDGAR quarterly full-index -> each filing's complete submission text.
"""
from __future__ import annotations

import csv
import io
import logging
import shutil
import zipfile
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import http
from app.db.models import insider_transactions
from app.db.store import filing_exists, insert_rows, job_state, save_job_state
from app.entities.registry import resolver
from app.parsers.form4 import PLAN_RE, classify, role_flags
from app.parsers.sec_common import index_url, normalize_symbol, to_date, to_float
from app.signals.detect import cluster_events_for_issuer, insider_buy_events
from app.sources.sec import edgar
from app.sources.sec.processing import process_text

log = logging.getLogger(__name__)
csv.field_size_limit(10**8)
DATASET_URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip"
FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{n}/form.idx"
MIN_FREE_BYTES = 3 * 1024**3


def disk_ok(path: str = "/") -> bool:
    return shutil.disk_usage(path).free > MIN_FREE_BYTES


def quarters(years: int, today: date | None = None) -> list[str]:
    today = today or date.today()
    out = []
    for y in range(today.year - years, today.year + 1):
        for q in range(1, 5):
            qs = date(y, 3 * q - 2, 1)
            if qs > today:
                continue
            out.append(f"{y}q{q}")
    return out


def _tsv(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t", quoting=csv.QUOTE_NONE)


def load_form345_quarter(engine: Engine, q: str) -> dict:
    r = http.get(DATASET_URL.format(q=q), retries=3)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    subs = {}
    for s in _tsv(z, "SUBMISSION.tsv"):
        if s.get("DOCUMENT_TYPE") not in ("4",):
            continue
        subs[s["ACCESSION_NUMBER"]] = s
    trans = [t for t in _tsv(z, "NONDERIV_TRANS.tsv") if t.get("TRANS_CODE") in ("P", "S") and t["ACCESSION_NUMBER"] in subs]
    need = {t["ACCESSION_NUMBER"] for t in trans}
    owners: dict[str, dict] = {}
    for o in _tsv(z, "REPORTINGOWNER.tsv"):
        a = o["ACCESSION_NUMBER"]
        if a in need and a not in owners:
            owners[a] = o
    plan_acc: set[str] = set()
    for fn in _tsv(z, "FOOTNOTES.tsv"):
        a = fn.get("ACCESSION_NUMBER")
        if a in need and PLAN_RE.search(fn.get("FOOTNOTE_TXT", "") or ""):
            plan_acc.add(a)
    with engine.connect() as conn:
        resolver.load(conn)
        existing = {a for (a,) in conn.execute(select(insider_transactions.c.accession).where(
            insider_transactions.c.accession.in_(list(need)), insider_transactions.c.source != "dataset")).all()} if need else set()
    rows = []
    for t in trans:
        a = t["ACCESSION_NUMBER"]
        if a in existing:
            continue  # already ingested from the live XML
        s = subs[a]
        o = owners.get(a, {})
        rel = (o.get("RPTOWNER_RELATIONSHIP") or "").lower()
        title = o.get("RPTOWNER_TITLE") or None
        is_ceo, is_cfo = role_flags(" ".join(x for x in (title, o.get("RPTOWNER_TXT")) if x))
        shares = to_float(t.get("TRANS_SHARES"))
        price = to_float(t.get("TRANS_PRICEPERSHARE"))
        issuer = (s.get("ISSUERCIK") or "").zfill(10)
        sym = normalize_symbol(s.get("ISSUERTRADINGSYMBOL"))
        ticker = resolver.ticker_for_cik(issuer) or sym
        cik_url = issuer.lstrip("0") or "0"
        rows.append({
            "accession": a, "line_key": f"DS{t['NONDERIV_TRANS_SK']}", "issuer_cik": issuer, "ticker": ticker,
            "issuer_name": s.get("ISSUERNAME"), "owner_cik": (o.get("RPTOWNERCIK") or "").zfill(10) or None,
            "owner_name": o.get("RPTOWNERNAME") or "", "is_director": "director" in rel, "is_officer": "officer" in rel,
            "is_ten_pct": "tenpercent" in rel.replace(" ", "") or "10%" in rel, "officer_title": title,
            "is_ceo": is_ceo, "is_cfo": is_cfo, "security_title": t.get("SECURITY_TITLE"), "is_derivative": False,
            "trans_date": to_date(t.get("TRANS_DATE")), "trans_code": t.get("TRANS_CODE"),
            "acq_disp": t.get("TRANS_ACQUIRED_DISP_CD"), "shares": shares, "price": price,
            "value": round(shares * price, 2) if (shares is not None and price is not None) else None,
            "shares_after": to_float(t.get("SHRS_OWND_FOLWNG_TRANS")), "direct_indirect": t.get("DIRECT_INDIRECT_OWNERSHIP"),
            "is_10b5_1": (s.get("AFF10B5ONE", "").lower() in ("1", "true")) or a in plan_acc,
            "category": classify(t.get("TRANS_CODE"), False), "filed_date": to_date(s.get("FILING_DATE")),
            "accepted_at": None, "source": "dataset", "source_url": index_url(cik_url, a),
        })
    with engine.begin() as conn:
        n = insert_rows(conn, insider_transactions, rows, ["accession", "line_key"])
    buys = {r["accession"] for r in rows if r["trans_code"] == "P"}
    events_created = insider_events_from_dataset(engine, buys)
    return {"quarter": q, "rows": len(rows), "inserted": n, "buy_filings": len(buys), "events": events_created}


def insider_events_from_dataset(engine: Engine, accessions: set[str]) -> int:
    """Create backfill INSIDER_OPEN_MARKET_BUY + CLUSTER events for the given dataset filings."""
    if not accessions:
        return 0
    accs = []
    with engine.connect() as conn:
        lst = list(accessions)
        for i in range(0, len(lst), 1000):
            q = select(insider_transactions.c.accession, insider_transactions.c.issuer_cik, insider_transactions.c.filed_date).where(
                insider_transactions.c.category == "OPEN_MARKET_BUY", insider_transactions.c.accession.in_(lst[i:i + 1000])).distinct()
            accs += conn.execute(q).all()
    n = 0
    issuer_dates: dict[str, set[date]] = defaultdict(set)
    for acc, issuer, filed in accs:
        with engine.begin() as conn:
            n += len(insider_buy_events(conn, acc, is_backfill=True))
        issuer_dates[issuer].add(filed)
    for issuer, dates in issuer_dates.items():
        for d in sorted(dates):
            with engine.begin() as conn:
                n += len(cluster_events_for_issuer(conn, issuer, d, is_backfill=True))
    return n


def backfill_form345(engine: Engine, years: int) -> dict:
    name = "backfill_form345"
    with engine.begin() as conn:
        st = job_state(conn, name)
    done = set(st.get("done", []))
    for q in quarters(years):
        if q in done:
            continue
        if not disk_ok():
            with engine.begin() as conn:
                save_job_state(conn, name, st, status="paused", error="low disk")
            return st
        try:
            res = load_form345_quarter(engine, q)
        except http.HttpError as e:
            if e.status == 404:  # current quarter not published yet
                log.info("form345 %s not published yet", q)
                continue
            raise
        done.add(q)
        st = {"done": sorted(done), "last": res}
        with engine.begin() as conn:
            save_job_state(conn, name, st, status="running")
        log.info("form345 %s: %s", q, res)
    with engine.begin() as conn:
        save_job_state(conn, name, st, status="done")
    return st


# ---------------------------------------------------------------- 13D

SC13D_FORMS = {"SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A"}


def list_13d_filings(year: int, qtr: int) -> list[tuple[str, str, str]]:
    """(form, cik, accession) from the quarterly form index, deduped by accession."""
    txt = http.get(FULL_INDEX_URL.format(y=year, n=qtr), retries=3).text
    out: dict[str, tuple[str, str, str]] = {}
    for line in txt.splitlines():
        for form in SC13D_FORMS:
            if line.startswith(form + " ") and not line.startswith(form + "/"):
                parts = line.split()
                path = parts[-1]
                if not path.startswith("edgar/"):
                    continue
                cik = path.split("/")[2]
                acc = path.rsplit("/", 1)[-1].replace(".txt", "")
                out.setdefault(acc, (form, cik, acc))
                break
    return list(out.values())


def backfill_13d(engine: Engine, years: int) -> dict:
    name = "backfill_13d"
    with engine.begin() as conn:
        st = job_state(conn, name)
    done = set(st.get("done", []))
    today = date.today()
    for y in range(today.year - years, today.year + 1):
        for qn in range(1, 5):
            key = f"{y}q{qn}"
            if key in done or date(y, 3 * qn - 2, 1) > today:
                continue
            items = list_13d_filings(y, qn)
            ok = err = 0
            for form, cik, acc in items:
                if not disk_ok():
                    with engine.begin() as conn:
                        save_job_state(conn, name, {**st, "done": sorted(done)}, status="paused", error="low disk")
                    return st
                with engine.connect() as conn:
                    if filing_exists(conn, "sec", acc):
                        continue
                    resolver.load(conn)
                try:
                    text = edgar.fetch_full_text(cik, acc)
                    with engine.begin() as conn:
                        process_text(conn, text, cik, is_backfill=True)
                    ok += 1
                except Exception as e:
                    err += 1
                    log.warning("13D backfill %s failed: %s", acc, e)
            # current quarter stays open so later filings get picked up next time
            if date(y, 3 * qn, 28) < today:
                done.add(key)
            st = {"done": sorted(done), "last": {"quarter": key, "filings": len(items), "ok": ok, "errors": err}}
            with engine.begin() as conn:
                save_job_state(conn, name, st, status="running")
            log.info("13D backfill %s: %s", key, st["last"])
    with engine.begin() as conn:
        save_job_state(conn, name, st, status="done")
    return st
