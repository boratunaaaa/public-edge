"""House of Representatives PTR ingestion from the official Clerk disclosure site."""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date, timedelta

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection, Engine

from app import http
from app.db.models import congress_transactions
from app.db.store import create_event, filing_exists, insert_rows, job_state, mark_filing, save_job_state, upsert_filing
from app.entities.registry import resolver
from app.parsers.house_ptr import parse_ptr_text, pdf_text
from app.parsers.sec_common import to_date
from app.signals import priority as P
from app.signals.detect import _money, multi_signal_check
from app.sources.base import RunResult, Source

log = logging.getLogger(__name__)
INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{y}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{y}/{doc}.pdf"
CLUSTER_WINDOWS = (7, 30)


def fetch_index(year: int) -> list[dict]:
    r = http.get(INDEX_URL.format(y=year), retries=3)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml = z.read(f"{year}FD.xml").decode("utf-8-sig", "replace")
    out = []
    for m in re.findall(r"<Member>(.*?)</Member>", xml, re.S):
        g = lambda t: (re.search(rf"<{t}>(.*?)</{t}>", m, re.S) or [None, None])[1]
        if (g("FilingType") or "").strip() != "P":
            continue
        name = " ".join(x for x in [g("First"), g("Last"), g("Suffix")] if x and x.strip())
        out.append({"doc_id": g("DocID").strip(), "name": name.strip(), "state_district": (g("StateDst") or "").strip(),
                    "filing_date": to_date((g("FilingDate") or "").strip()), "year": year})
    return out


def process_ptr(conn: Connection, meta: dict, pdf: bytes, *, is_backfill: bool = False) -> list[int]:
    url = PTR_URL.format(y=meta["year"], doc=meta["doc_id"])
    text = pdf_text(pdf)
    fid, _ = upsert_filing(conn, source="house", accession=meta["doc_id"], form_type="PTR", url=url,
                           filed_date=meta["filing_date"], raw=text)
    ptr = parse_ptr_text(text)
    if not ptr.text_ok:
        mark_filing(conn, fid, "no_text", "scanned/handwritten PDF - no machine-readable text")
        return []
    filed = meta["filing_date"]
    rows = []
    for ln in ptr.lines:
        delay = (filed - ln.transaction_date).days if (filed and ln.transaction_date) else None
        ticker = ln.ticker if (ln.ticker and ln.asset_type in (None, "ST", "OP")) else None
        rows.append({
            "chamber": "house", "doc_id": meta["doc_id"], "line_no": ln.line_no, "politician": meta["name"],
            "party": None, "state_district": meta["state_district"], "ticker": ticker, "asset_name": ln.asset_name[:1000],
            "asset_type": ln.asset_type, "transaction_type": ln.transaction_type, "transaction_date": ln.transaction_date,
            "notification_date": ln.notification_date, "filing_date": filed, "disclosure_delay_days": delay,
            "amount_min": ln.amount_min, "amount_max": ln.amount_max, "amount_range": ln.amount_range,
            "owner": ln.owner, "source_url": url,
        })
    insert_rows(conn, congress_transactions, rows, ["chamber", "doc_id", "line_no"])
    mark_filing(conn, fid, "ok")
    created: list[int] = []
    tickers_bought = set()
    for r in rows:
        if r["transaction_type"] != "PURCHASE" or not r["ticker"] or r["asset_type"] != "ST":
            continue
        tickers_bought.add(r["ticker"])
        eid = create_event(
            conn, event_type="CONGRESS_BUY", dedupe_key=f"CONG:house:{meta['doc_id']}:{r['line_no']}", public_date=filed,
            transaction_date=r["transaction_date"], ticker=r["ticker"],
            company_name=resolver.cik_to_name.get(resolver.ticker_to_cik.get(r["ticker"], ""), None) or r["asset_name"][:120],
            issuer_cik=resolver.ticker_to_cik.get(r["ticker"]), priority=P.congress_buy_priority(r["amount_min"]),
            title=f"Rep. {meta['name']} bought ${r['ticker']} ({r['amount_range'] or 'amount n/a'})",
            payload={"politician": meta["name"], "chamber": "house", "state_district": meta["state_district"],
                     "transaction_type": r["transaction_type"], "transaction_date": r["transaction_date"],
                     "filing_date": filed, "disclosure_delay_days": r["disclosure_delay_days"], "amount_range": r["amount_range"],
                     "amount_min": r["amount_min"], "amount_max": r["amount_max"], "owner": r["owner"], "asset": r["asset_name"]},
            source_url=url, is_backfill=is_backfill,
        )
        if eid:
            created.append(eid)
    for t in tickers_bought:
        created += congress_cluster_check(conn, t, filed, is_backfill=is_backfill)
        m = multi_signal_check(conn, t, filed, is_backfill=is_backfill)
        if m:
            created.append(m)
    return created


def congress_cluster_check(conn: Connection, ticker: str, as_of: date, *, is_backfill: bool = False) -> list[int]:
    """Several members bought the same ticker (by transaction date) - known once the latest report is public."""
    created = []
    best = 1
    for w in CLUSTER_WINDOWS:
        lo = as_of - timedelta(days=w + 45)  # reports lag trades by up to 45 days
        rows = conn.execute(select(congress_transactions).where(and_(
            congress_transactions.c.ticker == ticker, congress_transactions.c.transaction_type == "PURCHASE",
            congress_transactions.c.filing_date <= as_of, congress_transactions.c.transaction_date >= lo,
        ))).mappings().all()
        if not rows:
            continue
        rows = sorted(rows, key=lambda r: r["transaction_date"] or date.min)
        # best window ending at the latest trade date
        end = rows[-1]["transaction_date"]
        if end is None:
            continue
        inwin = [r for r in rows if r["transaction_date"] and (end - r["transaction_date"]).days < w]
        members = sorted({r["politician"] for r in inwin})
        if len(members) >= 2 and len(members) > best:
            best = len(members)
            total_min = sum(float(r["amount_min"] or 0) for r in inwin)
            eid = create_event(
                conn, event_type="CONGRESS_CLUSTER_BUY", dedupe_key=f"CONGCL:{ticker}:{w}:{min(r['transaction_date'] for r in inwin)}:{len(members)}",
                public_date=as_of, transaction_date=end, ticker=ticker, priority=P.congress_cluster_priority(len(members)),
                title=f"{len(members)} members of Congress bought ${ticker} within {w}d",
                payload={"window_days": w, "members": members, "n_members": len(members), "aggregate_min": total_min,
                         "trades": [{"politician": r["politician"], "chamber": r["chamber"], "date": r["transaction_date"],
                                     "amount_range": r["amount_range"], "owner": r["owner"]} for r in inwin]},
                source_url=inwin[-1]["source_url"], is_backfill=is_backfill,
            )
            if eid:
                created.append(eid)
    return created


class HousePtrSource(Source):
    name = "congress_house"
    interval_seconds = 3 * 3600
    slow = True

    def _run_year(self, engine: Engine, year: int, *, is_backfill: bool) -> RunResult:
        res = RunResult()
        idx = fetch_index(year)
        with engine.connect() as conn:
            resolver.load(conn)
            todo = [m for m in idx if not filing_exists(conn, "house", m["doc_id"])]
        for m in todo:
            try:
                pdf = http.get(PTR_URL.format(y=year, doc=m["doc_id"]), retries=2).content
                with engine.begin() as conn:
                    res.events += process_ptr(conn, m, pdf, is_backfill=is_backfill)
                res.items += 1
            except Exception as e:
                log.warning("house PTR %s failed: %s", m["doc_id"], e)
        return res

    def run_live(self, engine: Engine) -> RunResult:
        y = date.today().year
        res = self._run_year(engine, y, is_backfill=False)
        if date.today().month == 1:  # late filings for the previous year
            r2 = self._run_year(engine, y - 1, is_backfill=False)
            res.items += r2.items
            res.events += r2.events
        return res

    def run_backfill(self, engine: Engine, years: int = 5) -> RunResult:
        name = "backfill_house"
        with engine.begin() as conn:
            st = job_state(conn, name)
        done = set(st.get("done", []))
        total = RunResult()
        for y in range(date.today().year - years, date.today().year):
            if str(y) in done:
                continue
            r = self._run_year(engine, y, is_backfill=True)
            total.items += r.items
            done.add(str(y))
            with engine.begin() as conn:
                save_job_state(conn, name, {"done": sorted(done), "last_year": y, "docs": r.items}, status="running")
        with engine.begin() as conn:
            save_job_state(conn, name, {"done": sorted(done)}, status="done")
        return total


class SenateSource(Source):
    """Official Senate eFD (efdsearch.senate.gov) is behind Akamai bot protection that returns
    HTTP 403 to all non-browser clients (verified from the server and a residential IP on
    2026-09-24). The previously popular third-party mirrors are also 403. Registered disabled so
    the gap is visible instead of silently missing."""
    name = "congress_senate"
    enabled = False
    disabled_reason = "efdsearch.senate.gov returns HTTP 403 (Akamai bot protection) to automated clients; no official machine-readable alternative"

    def run_live(self, engine: Engine) -> RunResult:
        return RunResult()
