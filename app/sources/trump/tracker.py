"""Trump-family PUBLIC SEC record tracker.

Tracks only documented SEC filings where Donald J. Trump, Donald J. Trump Jr. or Eric Trump
is a reporting person. It records facts from the filing; it does not interpret motives.
Form 4 / 13D / 144 filings found via the main feeds are also matched by name (trusts).
"""
from __future__ import annotations

import logging

from sqlalchemy.engine import Engine

from app import http
from app.config import TRUMP_FAMILY_CIKS
from app.db.store import filing_exists, upsert_filing
from app.entities.registry import resolver
from app.parsers.schedule13d import parse_filer_names
from app.parsers.sec_common import index_url, parse_submission
from app.sources.base import RunResult, Source
from app.sources.sec import edgar
from app.sources.sec.processing import process_text, trump_event

log = logging.getLogger(__name__)
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
HANDLED_FORMS = ("4", "4/A", "144", "144/A")


def list_filings(cik: str) -> list[dict]:
    d = http.get(SUBMISSIONS_URL.format(cik=cik)).json()
    rec = d.get("filings", {}).get("recent", {})
    keys = ["accessionNumber", "form", "filingDate", "primaryDocument"]
    out = [dict(zip(keys, vals)) for vals in zip(*(rec.get(k, []) for k in keys))]
    return out


class TrumpFamilySource(Source):
    name = "trump_family"
    interval_seconds = 1800

    def _run(self, engine: Engine, *, is_backfill: bool) -> RunResult:
        res = RunResult()
        with engine.connect() as conn:
            resolver.load(conn)
        for cik, person in TRUMP_FAMILY_CIKS.items():
            for f in list_filings(cik):
                acc = f["accessionNumber"]
                with engine.connect() as conn:
                    if filing_exists(conn, "sec", acc):
                        continue
                try:
                    text = edgar.fetch_full_text(cik, acc)
                    with engine.begin() as conn:
                        form = f["form"].upper()
                        if form in HANDLED_FORMS or form.startswith(("SC 13D", "SCHEDULE 13D", "SC 13G", "SCHEDULE 13G")):
                            res.events += process_text(conn, text, cik, is_backfill=is_backfill)
                        else:
                            sub = parse_submission(text)
                            header = text.split("</SEC-HEADER>")[0]
                            names = parse_filer_names(header)
                            subj = names["SUBJECT COMPANY"] or names["ISSUER"]
                            company, icik = (subj[0] if subj else (None, None))
                            url = index_url(cik, acc)
                            upsert_filing(conn, source="sec", accession=acc, form_type=sub.form_type or f["form"], url=url,
                                          issuer_cik=icik, filed_date=sub.filed_date, accepted_at=sub.accepted_at)
                            eid = trump_event(conn, person=person, sub=sub, url=url, company=company, issuer_cik=icik,
                                              ticker=resolver.ticker_for_cik(icik), ownership=None,
                                              details={"form": f["form"], "primary_document": f["primaryDocument"]})
                            if eid:
                                res.events.append(eid)
                    res.items += 1
                except Exception as e:
                    log.warning("trump tracker %s %s failed: %s", person, acc, e)
        return res

    def run_live(self, engine: Engine) -> RunResult:
        return self._run(engine, is_backfill=False)

    def run_backfill(self, engine: Engine, years: int = 10) -> RunResult:
        return self._run(engine, is_backfill=True)
