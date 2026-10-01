"""Populate the security master from SEC primary sources (resumable, idempotent jobs).

  * Form 3/4/5 quarterly datasets  -> point-in-time filing symbols per CIK (security_symbols),
                                      all reporting owners + joint-filing attribution for stored rows
  * EDGAR quarterly full index     -> Form 25 / 25-NSE / 15 / merger proxies / tender responses
  * EDGAR submissions JSON per CIK -> SIC sector, former names, 8-K items (1.03 bankruptcy,
                                      5.01 change in control, 3.01 listing deficiency, 3.02 PIPE ...)
  * SEC exchange ticker file       -> current listings (ACTIVE)
"""
from __future__ import annotations

import io
import logging
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app import http
from app.db.models import corporate_actions, events, insider_transactions, security_master, security_symbols
from app.db.store import job_state, save_job_state
from app.entities.registry import resolver
from app.parsers.sec_common import normalize_symbol, to_date
from app.securitymaster.sic import sector_for_sic

log = logging.getLogger(__name__)
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{n}/form.idx"

# full-index form -> corporate action
INDEX_FORMS = {
    "25-NSE": "FORM25_NSE", "25-NSE/A": "FORM25_NSE", "25": "FORM25", "25/A": "FORM25",
    "15-12B": "FORM15", "15-12G": "FORM15", "15-15D": "FORM15", "15F-12B": "FORM15",
    "DEFM14A": "MERGER_PROXY", "DEFM14C": "MERGER_PROXY", "SC 14D9": "TENDER_TARGET", "SC14D9C": "TENDER_TARGET",
}
ITEM_ACTIONS = {"1.03": "BANKRUPTCY", "5.01": "CHANGE_IN_CONTROL", "2.01": "ACQ_DISPOSITION",
                "3.01": "LISTING_DEFICIENCY", "3.02": "UNREGISTERED_SALE"}
FORM_ACTIONS = {"S-3": "SHELF_REGISTRATION", "S-3ASR": "SHELF_REGISTRATION", "F-3": "SHELF_REGISTRATION",
                "S-1": "REGISTRATION_S1", "424B5": "PROSPECTUS_424B", "424B3": "PROSPECTUS_424B", "424B4": "PROSPECTUS_424B",
                "424B7": "PROSPECTUS_424B", "25-NSE": "FORM25_NSE", "25": "FORM25", "15-12B": "FORM15", "15-12G": "FORM15",
                "DEFM14A": "MERGER_PROXY", "SC 14D9": "TENDER_TARGET",
                # a depositary (ADR) programme: its market cap is unknown (app.marketdata.market_cap)
                "F-6": "DEPOSITARY_PROGRAM", "F-6EF": "DEPOSITARY_PROGRAM", "F-6 POS": "DEPOSITARY_PROGRAM",
                "F-6/A": "DEPOSITARY_PROGRAM"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ symbols from Form 3/4/5 datasets

def aggregate_symbols(submission_rows) -> dict[tuple[str, str], list]:
    """{(cik, symbol): [first, last, n]} from Form 3/4/5 SUBMISSION rows."""
    agg: dict[tuple[str, str], list] = {}
    for s in submission_rows:
        sym = normalize_symbol(s.get("ISSUERTRADINGSYMBOL"))
        d = to_date(s.get("FILING_DATE"))
        cik = (s.get("ISSUERCIK") or "").strip()
        if not sym or not d or not cik:
            continue
        k = (cik.zfill(10), sym[:16])
        cur = agg.get(k)
        if cur is None:
            agg[k] = [d, d, 1]
        else:
            cur[0], cur[1], cur[2] = min(cur[0], d), max(cur[1], d), cur[2] + 1
    return agg


def save_symbols(conn, agg: dict[tuple[str, str], list], source: str) -> int:
    rows = [{"cik": c, "symbol": s, "first_seen": v[0], "last_seen": v[1], "n_obs": v[2], "source": source}
            for (c, s), v in agg.items()]
    for i in range(0, len(rows), 1000):
        st = pg_insert(security_symbols).values(rows[i:i + 1000])
        conn.execute(st.on_conflict_do_update(index_elements=["cik", "symbol", "source"], set_={
            "first_seen": text("LEAST(security_symbols.first_seen, excluded.first_seen)"),
            "last_seen": text("GREATEST(security_symbols.last_seen, excluded.last_seen)"),
            "n_obs": security_symbols.c.n_obs + st.excluded.n_obs}))
    return len(rows)


def rescan_form345_quarter(engine: Engine, q: str) -> dict:
    """Second pass over an already-loaded quarter: filing symbols for every issuer, all reporting
    owners, ownership text and joint-filing attribution for the stored rows."""
    from app.sources.sec.backfill import (_download_dataset, _tsv, dataset_owner_refs, dataset_row_owner,
                                          dataset_row_text)
    from app.signals.owners import save_filing_owners
    z = zipfile.ZipFile(io.BytesIO(_download_dataset(q)))
    subs = list(_tsv(z, "SUBMISSION.tsv"))
    agg = aggregate_symbols(subs)
    sub_by_acc = {s["ACCESSION_NUMBER"]: s for s in subs if s.get("DOCUMENT_TYPE") == "4"}
    trans = [t for t in _tsv(z, "NONDERIV_TRANS.tsv") if t.get("TRANS_CODE") in ("P", "S") and t["ACCESSION_NUMBER"] in sub_by_acc]
    need = {t["ACCESSION_NUMBER"] for t in trans}
    owners: dict[str, list[dict]] = defaultdict(list)
    for o in _tsv(z, "REPORTINGOWNER.tsv"):
        if o["ACCESSION_NUMBER"] in need:
            owners[o["ACCESSION_NUMBER"]].append(o)
    notes: dict[str, dict[str, str]] = defaultdict(dict)
    for fn in _tsv(z, "FOOTNOTES.tsv"):
        if fn.get("ACCESSION_NUMBER") in need:
            notes[fn["ACCESSION_NUMBER"]][fn.get("FOOTNOTE_ID") or ""] = fn.get("FOOTNOTE_TXT", "") or ""
    with engine.connect() as conn:
        stored = {(a, lk) for a, lk in conn.execute(select(insider_transactions.c.accession, insider_transactions.c.line_key).where(
            and_(insider_transactions.c.accession.in_(list(need)), insider_transactions.c.source == "dataset"))).all()} if need else set()
    changed = multi = 0
    with engine.begin() as conn:
        save_symbols(conn, agg, "form345_filing")
        refs_by_acc = {a: dataset_owner_refs(owners.get(a, [])) for a in need}
        for a in {a for a, _ in stored}:  # owners only for filings this system actually stores
            s = sub_by_acc[a]
            save_filing_owners(conn, a, refs_by_acc[a], (s.get("ISSUERCIK") or "").zfill(10), to_date(s.get("FILING_DATE")))
            multi += len(refs_by_acc[a]) > 1
        for t in trans:
            a, lk = t["ACCESSION_NUMBER"], f"DS{t['NONDERIV_TRANS_SK']}"
            if (a, lk) not in stored:
                continue
            vals = dataset_row_owner(refs_by_acc[a], dataset_row_text(t, notes.get(a, {})))
            vals["nature_of_ownership"] = t.get("NATURE_OF_OWNERSHIP") or None
            if len(refs_by_acc[a]) <= 1:
                # single-owner rows keep their stored identity; only the ownership text is added
                vals = {"nature_of_ownership": vals["nature_of_ownership"], "owner_attribution": "single"}
            conn.execute(update(insider_transactions).where(and_(
                insider_transactions.c.accession == a, insider_transactions.c.line_key == lk)).values(**vals))
            changed += 1
    return {"quarter": q, "symbols": len(agg), "rows_updated": changed, "multi_owner_filings": multi}


def rescan_form345(engine: Engine, years: int) -> dict:
    from app.sources.sec.backfill import quarters
    name = "securitymaster_form345_rescan"
    with engine.begin() as conn:
        st = job_state(conn, name)
    done = set(st.get("done", []))
    out = {}
    for q in quarters(years + 1):
        if q in done:
            continue
        try:
            out[q] = rescan_form345_quarter(engine, q)
        except http.HttpError as e:
            if e.status == 404:
                continue
            raise
        done.add(q)
        with engine.begin() as conn:
            save_job_state(conn, name, {"done": sorted(done), "last": out[q]}, status="running")
        log.info("form345 rescan %s: %s", q, out[q])
    with engine.begin() as conn:
        save_job_state(conn, name, {"done": sorted(done)}, status="done")
    return {"quarters": len(out)}


# ------------------------------------------------------------------ corporate actions from the full index

def parse_full_index(txt: str) -> list[dict]:
    out = []
    for line in txt.splitlines():
        form = line[:17].strip()
        if form not in INDEX_FORMS:
            continue
        parts = line.split()
        path = parts[-1]
        if not path.startswith("edgar/"):
            continue
        try:
            d = date.fromisoformat(parts[-2])
            cik = parts[-3].zfill(10)
        except ValueError:
            continue
        acc = path.rsplit("/", 1)[-1].replace(".txt", "")
        out.append({"cik": cik, "action": INDEX_FORMS[form], "action_date": d, "accession": acc,
                    "source": "sec_full_index", "details": {"form": form}})
    return out


def save_actions(conn, rows: list[dict]) -> int:
    n = 0
    for i in range(0, len(rows), 1000):
        res = conn.execute(pg_insert(corporate_actions).values(rows[i:i + 1000]).on_conflict_do_nothing(
            index_elements=["cik", "action", "accession", "action_date"]).returning(corporate_actions.c.id))
        n += len(res.fetchall())
    return n


def load_full_index_actions(engine: Engine, years: int) -> dict:
    name = "securitymaster_full_index"
    with engine.begin() as conn:
        st = job_state(conn, name)
    done = set(st.get("done", []))
    today = date.today()
    cur_q = f"{today.year}q{(today.month - 1) // 3 + 1}"
    total = 0
    for y in range(today.year - years, today.year + 1):
        for n in range(1, 5):
            key = f"{y}q{n}"
            if date(y, 3 * n - 2, 1) > today or (key in done and key != cur_q):
                continue  # the current quarter is re-read every run (it keeps growing)
            try:
                rows = parse_full_index(http.get(FULL_INDEX_URL.format(y=y, n=n), retries=2).text)
            except http.HttpError as e:
                if e.status in (403, 404):
                    continue
                raise
            with engine.begin() as conn:
                total += save_actions(conn, rows)
            done.add(key)
            with engine.begin() as conn:
                save_job_state(conn, name, {"done": sorted(done), "new_actions": total}, status="running")
    with engine.begin() as conn:
        save_job_state(conn, name, {"done": sorted(done), "new_actions": total}, status="done")
    return {"new_actions": total}


# ------------------------------------------------------------------ submissions JSON per CIK

def parse_submissions(cik: str, d: dict) -> tuple[dict, list[dict]]:
    cik = cik.zfill(10)
    master = {"cik": cik, "name": d.get("name"), "sic": str(d.get("sic") or "")[:8] or None,
              "sic_description": d.get("sicDescription"), "sector": sector_for_sic(d.get("sic")),
              "exchanges": [x for x in (d.get("exchanges") or []) if x], "current_tickers": [x for x in (d.get("tickers") or []) if x],
              "former_names": d.get("formerNames") or []}
    rec = (d.get("filings") or {}).get("recent") or {}
    forms, dates, items, accs = rec.get("form") or [], rec.get("filingDate") or [], rec.get("items") or [], rec.get("accessionNumber") or []
    acts = []
    for i, form in enumerate(forms):
        try:
            fd = date.fromisoformat(dates[i])
        except (IndexError, ValueError):
            continue
        acc = accs[i] if i < len(accs) else ""
        if form in ("8-K", "8-K/A"):
            for it in str(items[i] if i < len(items) else "").split(","):
                it = it.strip()
                if it in ITEM_ACTIONS:
                    acts.append({"cik": cik, "action": ITEM_ACTIONS[it], "action_date": fd, "accession": acc,
                                 "source": "sec_submissions", "details": {"form": form, "item": it}})
        elif form in FORM_ACTIONS:
            acts.append({"cik": cik, "action": FORM_ACTIONS[form], "action_date": fd, "accession": acc,
                         "source": "sec_submissions", "details": {"form": form}})
    return master, acts


def refresh_submissions(engine: Engine, ciks: list[str], max_n: int = 2000, stale_days: int = 30) -> dict:
    """Fetch submissions JSON for CIKs whose master row is missing or stale."""
    ciks = sorted({c.zfill(10) for c in ciks if c})
    with engine.connect() as conn:
        fresh = {c for (c,) in conn.execute(select(security_master.c.cik).where(and_(
            security_master.c.cik.in_(ciks), security_master.c.submissions_fetched_at > _now() - timedelta(days=stale_days)))).all()}
    todo = [c for c in ciks if c not in fresh][:max_n]
    ok = missing = 0
    for cik in todo:
        try:
            d = http.get(SUBMISSIONS_URL.format(cik=cik), retries=2).json()
        except http.HttpError as e:
            if e.status == 404:
                missing += 1
                with engine.begin() as conn:
                    conn.execute(pg_insert(security_master).values(cik=cik, submissions_fetched_at=_now(), updated_at=_now())
                                 .on_conflict_do_update(index_elements=["cik"], set_={"submissions_fetched_at": _now()}))
                continue
            log.warning("submissions %s: %s", cik, e)
            continue
        master, acts = parse_submissions(cik, d)
        with engine.begin() as conn:
            vals = {k: v for k, v in master.items() if k != "cik"} | {"submissions_fetched_at": _now(), "updated_at": _now()}
            conn.execute(pg_insert(security_master).values(cik=cik, **vals).on_conflict_do_update(index_elements=["cik"], set_=vals))
            save_actions(conn, acts)
        ok += 1
    return {"fetched": ok, "not_found": missing, "remaining": max(0, len([c for c in ciks if c not in fresh]) - len(todo))}


# ------------------------------------------------------------------ status

def sync_registry_symbols(engine: Engine) -> int:
    """Current SEC exchange listings as observed today (source sec_registry)."""
    today = date.today()
    with engine.begin() as conn:
        resolver.load(conn, force=True)
        agg = {(c, t[:16]): [today, today, 1] for c, t in resolver.cik_to_ticker.items()}
        rows = [{"cik": c, "symbol": s, "first_seen": v[0], "last_seen": v[1], "n_obs": 1, "source": "sec_registry"}
                for (c, s), v in agg.items()]
        for i in range(0, len(rows), 1000):
            st = pg_insert(security_symbols).values(rows[i:i + 1000])
            conn.execute(st.on_conflict_do_update(index_elements=["cik", "symbol", "source"],
                                                  set_={"last_seen": st.excluded.last_seen}))
    return len(rows)


def update_status(engine: Engine, ciks: list[str] | None = None) -> dict:
    """ACTIVE / DELISTED (+date, reason, confidence) for every CIK in the master."""
    from app.securitymaster.provider import SecSecurityMaster
    with engine.connect() as conn:
        targets = ciks or [c for (c,) in conn.execute(select(security_master.c.cik)).all()]
    counts: dict[str, int] = defaultdict(int)
    for i in range(0, len(targets), 500):
        with engine.begin() as conn:
            sm = SecSecurityMaster(conn)
            for cik in targets[i:i + 500]:
                dl = sm.delisting(cik)
                status = "ACTIVE" if dl.reason == "NOT_DELISTED" else ("DELISTED" if dl.date else "UNKNOWN")
                conn.execute(pg_insert(security_master).values(
                    cik=cik, status=status, delisting_date=dl.date, delisting_reason=None if status == "ACTIVE" else dl.reason,
                    delisting_confidence=dl.confidence, delisting_source=dl.source, updated_at=_now()).on_conflict_do_update(
                    index_elements=["cik"], set_={"status": status, "delisting_date": dl.date,
                                                  "delisting_reason": None if status == "ACTIVE" else dl.reason,
                                                  "delisting_confidence": dl.confidence, "delisting_source": dl.source,
                                                  "updated_at": _now()}))
                counts[status] += 1
    return dict(counts)


def event_issuer_ciks(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [c for (c,) in conn.execute(select(events.c.issuer_cik).where(events.c.issuer_cik.isnot(None)).distinct()).all()]


def run_security_master(engine: Engine, years: int, submissions_budget: int = 15000) -> dict:
    """Full refresh pipeline; every step is resumable."""
    from app.securitymaster.vendors import load_listing_periods
    out: dict = {}
    out["registry_symbols"] = sync_registry_symbols(engine)
    try:
        with engine.begin() as conn:
            out["listing_periods"] = load_listing_periods(conn)
    except Exception as e:  # optional public file; SEC sources stay authoritative
        log.warning("listing periods unavailable: %s", e)
    out["full_index"] = load_full_index_actions(engine, years + 1)
    out["form345_rescan"] = rescan_form345(engine, years)
    out["submissions"] = refresh_submissions(engine, event_issuer_ciks(engine), max_n=submissions_budget)
    out["status"] = update_status(engine)
    return out
