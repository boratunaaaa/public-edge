"""Live SEC ingestion via the EDGAR latest-filings feed."""
from __future__ import annotations

import logging

from sqlalchemy.engine import Engine

from app.db.store import filing_exists
from app.entities.registry import resolver
from app.sources.base import RunResult, Source
from app.sources.sec import edgar
from app.sources.sec.processing import process_text

log = logging.getLogger(__name__)


class _SecFeedSource(Source):
    feed_types: tuple[str, ...] = ()
    owner = "include"
    accept_forms: tuple[str, ...] = ()

    def run_live(self, engine: Engine) -> RunResult:
        with engine.connect() as conn:
            resolver.load(conn)

            def known(acc: str) -> bool:
                return filing_exists(conn, "sec", acc)

            entries = []
            for ft in self.feed_types:
                entries += edgar.fetch_new(ft, known, owner=self.owner)
        entries = [e for e in entries if e.form_type.upper() in self.accept_forms]
        res = RunResult()
        errors = 0
        done: set[str] = set()
        for e in sorted(entries, key=lambda x: x.updated or 0):
            if e.accession in done:
                continue
            done.add(e.accession)
            try:
                text = edgar.fetch_full_text(e.cik, e.accession)
                with engine.begin() as conn:
                    res.events += process_text(conn, text, e.cik)
                res.items += 1
            except Exception as ex:  # one bad filing never blocks the feed
                errors += 1
                log.exception("%s: failed %s: %s", self.name, e.accession, ex)
        if entries and errors == len(done):
            raise RuntimeError(f"all {errors} filings failed")
        return res


class SecForm4Source(_SecFeedSource):
    name = "sec_form4"
    interval_seconds = 60
    feed_types = ("4",)
    owner = "only"
    accept_forms = ("4", "4/A")


class Sec13DSource(_SecFeedSource):
    name = "sec_13d"
    interval_seconds = 120
    # structured (post Dec-2024) and legacy form names
    feed_types = ("SCHEDULE 13D", "SC 13D")
    accept_forms = ("SCHEDULE 13D", "SCHEDULE 13D/A", "SC 13D", "SC 13D/A")


class SecForm144Source(_SecFeedSource):
    name = "sec_form144"
    interval_seconds = 300
    feed_types = ("144",)
    accept_forms = ("144", "144/A")
