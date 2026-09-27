"""openFDA: Drugs@FDA approvals (original applications and efficacy supplements) and drug enforcement (recalls).

Point in time: an approval is public on its action date (FDA letter / company release the same day) -> end of the
action date (New York); event_confidence MEDIUM because Drugs@FDA itself is updated with a lag. Recalls use the
enforcement report_date. Complete response letters are not reliably available through openFDA and are not claimed.
Sponsor -> issuer by exact normalized name (SEC security master); unmapped sponsors are not events."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.engine import Engine

from app import http
from app.core import records
from app.core.provenance import end_of_local_day
from app.db.store import create_event
from app.markets.identity import name_to_cik
from app.sources.base import RunResult, Source

log = logging.getLogger(__name__)
PARSER_VERSION = "openfda-v1"
DRUGSFDA = "https://api.fda.gov/drug/drugsfda.json"
ENFORCEMENT = "https://api.fda.gov/drug/enforcement.json"


def _d(s: str | None) -> date | None:
    try:
        return datetime.strptime(s, "%Y%m%d").date() if s else None
    except ValueError:
        return None


def approvals(result: dict, start: date, end: date) -> list[dict]:
    """Material approvals of one Drugs@FDA application within [start, end]."""
    out = []
    of = result.get("openfda") or {}
    for s in result.get("submissions", []) or []:
        if s.get("submission_status") != "AP":
            continue
        d = _d(s.get("submission_status_date"))
        if not d or not (start <= d <= end):
            continue
        kind = "FDA_APPROVAL" if s.get("submission_type") == "ORIG" else (
            "FDA_EFFICACY_SUPPLEMENT" if s.get("submission_class_code") == "EFFICACY" else None)
        if not kind:
            continue  # labeling / manufacturing supplements are not catalysts
        out.append({"kind": kind, "date": d, "application_number": result.get("application_number"),
                    "sponsor_name": result.get("sponsor_name"), "submission_number": s.get("submission_number"),
                    "submission_class": s.get("submission_class_code"), "review_priority": s.get("review_priority"),
                    "brand_names": (of.get("brand_name") or [])[:5], "generic_names": (of.get("generic_name") or [])[:5],
                    "docs": [dd.get("url") for dd in (s.get("application_docs") or [])][:3]})
    return out


def store_approval(conn, a: dict, *, is_backfill: bool) -> int | None:
    cik, conf, why = name_to_cik(conn, a["sponsor_name"])
    key = f"{a['application_number']}:{a['submission_number']}"
    avail = end_of_local_day(a["date"])
    records.put(conn, country="US", dataset="us_fda_approvals", record_key=key, as_of_date=a["date"], available_at=avail,
                payload=a, parser_version=PARSER_VERSION, license_status="OPEN", issuer_cik=cik, entity_name=a["sponsor_name"],
                identity_confidence=conf if cik else None, needs_review=cik is None,
                source_url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={a['application_number'][3:]}")
    if not cik:
        return None
    from app.entities.registry import resolver
    return create_event(conn, event_type=a["kind"], dedupe_key=f"FDA:{key}", public_date=a["date"], ticker=resolver.ticker_for_cik(cik),
                        issuer_cik=cik, company_name=a["sponsor_name"],
                        title=f"{a['sponsor_name']}: FDA {'approval' if a['kind'] == 'FDA_APPROVAL' else 'efficacy supplement'} "
                              f"{', '.join(a['brand_names'][:2]) or a['application_number']}",
                        payload=a, source_url=a["docs"][0] if a["docs"] else "https://open.fda.gov/apis/drug/drugsfda/",
                        is_backfill=is_backfill, event_family="US_FDA_EVENT", identity_confidence=conf, event_confidence="MEDIUM",
                        parser_version=PARSER_VERSION, date_quality="date")


def recall(result: dict) -> dict | None:
    if result.get("classification") != "Class I":
        return None
    d = _d(result.get("report_date"))
    return {"kind": "FDA_RECALL", "date": d, "recall_number": result.get("recall_number"), "firm": result.get("recalling_firm"),
            "reason": (result.get("reason_for_recall") or "")[:400], "product": (result.get("product_description") or "")[:300],
            "status": result.get("status")} if d else None


def fetch(url: str, search: str, limit: int = 1000, skip: int = 0) -> dict:
    try:
        return http.get(url, params={"search": search, "limit": limit, "skip": skip}).json()
    except http.HttpError as e:
        if e.status == 404:  # openFDA: no matches
            return {"results": []}
        raise


def sync_approvals(engine: Engine, start: date, end: date, *, is_backfill: bool) -> dict:
    n = ev = 0
    skip = 0
    search = f"submissions.submission_status_date:[{start:%Y%m%d} TO {end:%Y%m%d}]"
    while True:
        page = fetch(DRUGSFDA, search, skip=skip)
        rows = page.get("results", [])
        for r in rows:
            for a in approvals(r, start, end):
                with engine.begin() as c:
                    ev += store_approval(c, a, is_backfill=is_backfill) is not None
                n += 1
        skip += len(rows)
        if len(rows) < 1000 or skip >= 25000:
            break
    return {"approvals": n, "events": ev}


class OpenFdaSource(Source):
    name = "us_openfda"
    interval_seconds = 12 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "US", "us_fda_approvals", "OPEN", PARSER_VERSION
    stale_after = timedelta(days=3)

    def run_live(self, engine: Engine) -> RunResult:
        today = date.today()
        out = sync_approvals(engine, today - timedelta(days=21), today, is_backfill=False)
        return RunResult(items=out["approvals"], watermark={"through": str(today)})

    def run_backfill(self, engine: Engine, start: date = date(2018, 1, 1)) -> RunResult:
        res = RunResult()
        d = start
        while d < date.today():
            e = min(d + timedelta(days=31), date.today())
            res.items += sync_approvals(engine, d, e, is_backfill=True)["approvals"]
            d = e + timedelta(days=1)
        return res
