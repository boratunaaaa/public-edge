"""SEC Form 13F structured data sets (quarterly zips by FILING-date window).

What is stored (disk budget): every 13F-HR / 13F-HR/A submission (inst_filings_13f); per-position rows only for
FOCUSED managers (<= FOCUSED_MAX_ENTRIES positions - where a 13F position carries information); for ALL managers
an aggregate per (CUSIP, period): number of holders, shares, value. Position changes (new / increased / decreased /
exited) are computed from the stored focused-manager rows, quarter over quarter, per manager.

Point in time: a 13F is known at its FILING date (the data set has no acceptance time -> end of the filing day,
New York). The report period is 45 days older; no trade date is ever inferred from a 13F.
Amendments: RESTATEMENT replaces the manager's report for that period, NEW HOLDINGS adds positions; both are
only known from their own filing date."""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import http
from app.core.bulk import upsert_rows
from app.core.provenance import end_of_local_day

log = logging.getLogger(__name__)
PARSER_VERSION = "13f-dataset-v1"
PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
FOCUSED_MAX_ENTRIES = 150
VALUE_IN_DOLLARS_FROM = date(2023, 1, 3)  # SEC: VALUE reported in dollars (was thousands) from 2023-01-03 filings
csv.field_size_limit(10_000_000)


def _d(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except (ValueError, AttributeError):
        return None


def dataset_urls() -> list[str]:
    page = http.get(PAGE).text
    return ["https://www.sec.gov" + u for u in dict.fromkeys(re.findall(r'href="(/files/[^"]+_form13f\.zip)"', page))]


def _member(z: zipfile.ZipFile, name: str) -> str:
    """Data sets differ in layout (folder prefix / case): match the table by file name, case-insensitively."""
    for n in z.namelist():
        if n.rsplit("/", 1)[-1].upper() == name.upper():
            return n
    raise KeyError(f"{name} not in data set (members: {z.namelist()[:12]})")


def _rows(z: zipfile.ZipFile, name: str):
    with z.open(_member(z, name)) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t", quoting=csv.QUOTE_NONE)


def parse_dataset(zbytes_or_path, *, min_period: date | None = None, sink=None, flush_every: int = 20_000) -> dict:
    """Stream a data-set zip into (filings, focused holdings, per-CUSIP aggregates). Memory is bounded by the
    aggregate dict (CUSIP x period) and the focused managers' rows."""
    z = zipfile.ZipFile(io.BytesIO(zbytes_or_path) if isinstance(zbytes_or_path, bytes) else zbytes_or_path)
    subs = {}
    for r in _rows(z, "SUBMISSION.tsv"):
        if r["SUBMISSIONTYPE"] not in ("13F-HR", "13F-HR/A"):
            continue  # 13F-NT (notice) has no holdings
        per, filed = _d(r["PERIODOFREPORT"]), _d(r["FILING_DATE"])
        if per is None or filed is None or (min_period and per < min_period):
            continue
        subs[r["ACCESSION_NUMBER"]] = {"accession": r["ACCESSION_NUMBER"], "manager_cik": r["CIK"].zfill(10), "period": per,
                                       "filed_date": filed, "submission_type": r["SUBMISSIONTYPE"]}
    for r in _rows(z, "COVERPAGE.tsv"):
        s = subs.get(r["ACCESSION_NUMBER"])
        if s:
            s["manager_name"] = (r["FILINGMANAGER_NAME"] or "").strip()[:200] or None
            s["amendment_type"] = (r.get("AMENDMENTTYPE") or "").strip().upper() or None
            s["report_type"] = (r.get("REPORTTYPE") or "").strip().upper()
    for r in _rows(z, "SUMMARYPAGE.tsv"):
        s = subs.get(r["ACCESSION_NUMBER"])
        if s:
            s["entries"] = int(r["TABLEENTRYTOTAL"]) if (r["TABLEENTRYTOTAL"] or "").strip().isdigit() else None
            try:
                s["total_value"] = float(r["TABLEVALUETOTAL"])
            except (TypeError, ValueError):
                s["total_value"] = None
    focused = {a for a, s in subs.items() if s.get("entries") and s["entries"] <= FOCUSED_MAX_ENTRIES
               and "NOTICE" not in s.get("report_type", "")}
    agg: dict[tuple[str, date], list] = defaultdict(lambda: [set(), 0.0, 0.0, None])  # holders, shares, value, last filing
    holdings = []
    for r in _rows(z, "INFOTABLE.tsv"):
        s = subs.get(r["ACCESSION_NUMBER"])
        if not s:
            continue
        cusip = (r["CUSIP"] or "").strip().upper()
        if len(cusip) != 9 or (r["PUTCALL"] or "").strip():
            continue  # options are not ownership
        try:
            value = float(r["VALUE"] or 0)
            shares = float(r["SSHPRNAMT"] or 0)
        except ValueError:
            continue
        if s["filed_date"] < VALUE_IN_DOLLARS_FROM:
            value *= 1000.0
        if (r["SSHPRNAMTTYPE"] or "").strip().upper() != "SH":
            continue  # principal amounts (bonds) are not share positions
        if s["submission_type"] == "13F-HR":
            a = agg[(cusip, s["period"])]  # aggregates count originals only: an amendment never double-counts a holder
            a[0].add(int(s["manager_cik"]))
            a[1] += shares
            a[2] += value
            a[3] = max(a[3], s["filed_date"]) if a[3] else s["filed_date"]
        if r["ACCESSION_NUMBER"] in focused:
            holdings.append({"accession": r["ACCESSION_NUMBER"], "line": int(r["INFOTABLE_SK"]) % 2_000_000_000, "cusip": cusip,
                             "issuer_name": (r["NAMEOFISSUER"] or "").strip()[:200], "class_title": (r["TITLEOFCLASS"] or "").strip()[:60],
                             "value": value, "shares": shares, "sh_prn": "SH", "put_call": None,
                             "discretion": (r["INVESTMENTDISCRETION"] or "").strip()[:8] or None})
            if sink is not None and len(holdings) >= flush_every:
                sink(holdings)
                holdings = []
    if sink is not None and holdings:
        sink(holdings)
        holdings = []
    aggregates = [{"cusip": c, "period": p, "holders": len(v[0]), "shares": v[1], "value": v[2],
                   "available_at": end_of_local_day(v[3])} for (c, p), v in agg.items() if v[3] is not None]
    return {"filings": list(subs.values()), "holdings": holdings, "aggregates": aggregates, "focused": len(focused)}


def store(engine: Engine, parsed: dict, source: str) -> dict:
    fil = [{k: s.get(k) for k in ("accession", "manager_cik", "manager_name", "period", "filed_date", "submission_type",
                                  "amendment_type", "entries", "total_value")} | {"available_at": end_of_local_day(s["filed_date"])}
           for s in parsed["filings"]]
    with engine.begin() as c:
        nf = upsert_rows(c, "inst_filings_13f", fil, ("accession",), {"source": source})
        nh = upsert_rows(c, "inst_holdings_13f", parsed["holdings"], ("accession", "line"), {})
        # a later data set can hold more filings for the same (cusip, period): holders accumulate by max
        for i in range(0, len(parsed["aggregates"]), 5000):
            chunk = parsed["aggregates"][i:i + 5000]
            c.execute(text("""
                INSERT INTO inst_cusip_quarter_13f (cusip, period, holders, shares, value, available_at)
                SELECT * FROM unnest(CAST(:c AS text[]), CAST(:p AS date[]), CAST(:h AS int[]), CAST(:s AS float8[]),
                                     CAST(:v AS float8[]), CAST(:a AS timestamptz[]))
                ON CONFLICT (cusip, period) DO UPDATE SET holders = inst_cusip_quarter_13f.holders + EXCLUDED.holders,
                  shares = inst_cusip_quarter_13f.shares + EXCLUDED.shares, value = inst_cusip_quarter_13f.value + EXCLUDED.value,
                  available_at = GREATEST(inst_cusip_quarter_13f.available_at, EXCLUDED.available_at)"""),
                      {"c": [x["cusip"] for x in chunk], "p": [x["period"] for x in chunk], "h": [x["holders"] for x in chunk],
                       "s": [x["shares"] for x in chunk], "v": [x["value"] for x in chunk], "a": [x["available_at"] for x in chunk]})
    return {"filings_new": nf, "holdings_new": nh, "aggregates": len(parsed["aggregates"])}


def position_changes(conn, manager_cik: str, period: date) -> list[dict]:
    """Per-CUSIP change of one focused manager vs its previous reported quarter (effective report = latest
    RESTATEMENT/original known at the later filing's date). Values: NEW | INCREASE | DECREASE | EXIT | HOLD."""
    rows = conn.execute(text("""
        WITH f AS (SELECT accession, period, filed_date, submission_type, amendment_type FROM inst_filings_13f
                   WHERE manager_cik = :m AND period <= :p AND period >= :p - 120),
        eff AS (SELECT DISTINCT ON (period) period, accession, filed_date FROM f
                WHERE submission_type = '13F-HR' OR amendment_type = 'RESTATEMENT'
                ORDER BY period, filed_date DESC, accession DESC),
        two AS (SELECT * FROM eff ORDER BY period DESC LIMIT 2)
        SELECT e.period, e.filed_date, h.cusip, max(h.issuer_name) issuer_name, sum(h.shares) shares, sum(h.value) value
        FROM two e JOIN inst_holdings_13f h ON h.accession = e.accession
        GROUP BY 1, 2, 3"""), {"m": manager_cik, "p": period}).mappings().all()
    periods = sorted({r["period"] for r in rows})
    if not periods or periods[-1] != period:
        return []
    cur = {r["cusip"]: r for r in rows if r["period"] == period}
    prev = {r["cusip"]: r for r in rows if len(periods) > 1 and r["period"] == periods[0]}
    tot = sum(r["value"] or 0 for r in cur.values()) or None
    out = []
    for cu in set(cur) | set(prev):
        a, b = cur.get(cu), prev.get(cu)
        if not prev:
            kind = "FIRST_REPORT"
        elif a and not b:
            kind = "NEW"
        elif b and not a:
            kind = "EXIT"
        elif a["shares"] > b["shares"] * 1.1:
            kind = "INCREASE"
        elif a["shares"] < b["shares"] * 0.9:
            kind = "DECREASE"
        else:
            kind = "HOLD"
        out.append({"cusip": cu, "change": kind, "shares": a["shares"] if a else 0, "prev_shares": b["shares"] if b else 0,
                    "weight": (a["value"] / tot) if (a and tot) else 0.0, "issuer_name": (a or b)["issuer_name"],
                    "filed_date": (a or b)["filed_date"] if a else None})
    return out


def ingest_dataset(engine: Engine, url: str, *, min_period: date | None = None, tmp_dir: str = "/tmp") -> dict:
    """Download (streamed to disk), parse, store - once per data-set URL (job state)."""
    import os
    import tempfile

    from app.db.store import job_state, save_job_state
    with engine.connect() as c:
        st = job_state(c, "form13f_datasets")
    done = set(st.get("done", []))
    if url in done:
        return {"url": url, "status": "ALREADY_INGESTED"}
    fd, path = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)
    os.close(fd)
    src = url.rsplit("/", 1)[-1][:32]
    n_hold = 0
    try:
        http.download(url, path)
        head = parse_dataset(path, min_period=min_period, sink=lambda rows: None)  # filings first (holdings FK)
        store(engine, {"filings": head["filings"], "holdings": [], "aggregates": []}, source=src)

        def sink(rows):
            nonlocal n_hold
            with engine.begin() as c:
                n_hold += upsert_rows(c, "inst_holdings_13f", rows, ("accession", "line"), {})
        parsed = parse_dataset(path, min_period=min_period, sink=sink)
        out = store(engine, {"filings": [], "holdings": [], "aggregates": parsed["aggregates"]}, source=src)
        out["holdings_new"] = n_hold
    finally:
        os.unlink(path)
    with engine.begin() as c:
        st = job_state(c, "form13f_datasets")
        save_job_state(c, "form13f_datasets", {**st, "done": sorted(set(st.get("done", [])) | {url})})
    return {"url": url, **out, "focused_managers": parsed["focused"]}
