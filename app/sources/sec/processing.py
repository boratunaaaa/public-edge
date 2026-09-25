"""Per-filing processing for Form 4, Schedule 13D and Form 144 (shared by live + backfill)."""
from __future__ import annotations

import logging
import re

from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from app.config import TRUMP_FAMILY_CIKS, TRUMP_FAMILY_NAME_PATTERNS
from app.db.models import activist_filings, form144_filings, insider_transactions
from app.db.store import create_event, insert_rows, mark_filing, upsert_filing
from app.entities.registry import remember_cusip, resolver
from app.parsers.form4 import parse_form4
from app.parsers.form144 import parse_form144
from app.parsers.schedule13d import classify_item4, is_13d, is_amendment, parse_filer_names, parse_schedule13
from app.parsers.sec_common import Submission, index_url, parse_submission, primary_xml
from app.signals import priority as P
from app.signals.detect import _money, cluster_events_for_issuer, insider_buy_events, multi_signal_check

log = logging.getLogger(__name__)


def trump_person_for(cik: str | None, name: str | None) -> str | None:
    if cik and cik.zfill(10) in TRUMP_FAMILY_CIKS:
        return TRUMP_FAMILY_CIKS[cik.zfill(10)]
    if name:
        up = name.upper()
        for pat, person in TRUMP_FAMILY_NAME_PATTERNS.items():
            if re.search(pat, up):
                return person
    return None


def trump_event(conn: Connection, *, person: str, sub: Submission, url: str, company: str | None, issuer_cik: str | None,
                ticker: str | None, ownership: str | None, details: dict) -> int | None:
    """Documented public record only - neutral wording, no inference."""
    fdate = sub.filed_date or (sub.accepted_at.date() if sub.accepted_at else None)
    if fdate is None:
        return None
    from app.db.models import trump_family_events
    insert_rows(conn, trump_family_events, [{
        "person": person, "accession": sub.accession, "form_type": sub.form_type, "company": company,
        "issuer_cik": issuer_cik, "ticker": ticker, "filed_date": fdate, "ownership": ownership,
        "details": details, "source_url": url,
    }], ["person", "accession"])
    what = f"{sub.form_type} filing"
    title = f"{person}: {what}" + (f" - {company}" if company else "") + (f" (${ticker})" if ticker else "")
    return create_event(
        conn, event_type="TRUMP_FAMILY_SEC_EVENT", dedupe_key=f"TRUMP:{person}:{sub.accession}", public_date=fdate,
        public_at=sub.accepted_at, ticker=ticker, company_name=company, issuer_cik=issuer_cik,
        priority=P.TRUMP_FAMILY_PRIORITY, title=title,
        payload={"person": person, "filing_type": sub.form_type, "company": company, "ticker": ticker,
                 "ownership": ownership, "transaction_details": details, "accession": sub.accession},
        source_url=url,
    )


# ------------------------------------------------------------------ Form 4

def process_form4(conn: Connection, sub: Submission, cik_for_url: str, *, is_backfill: bool = False) -> list[int]:
    url = index_url(cik_for_url, sub.accession)
    xml = primary_xml(sub)
    fid, _new = upsert_filing(conn, source="sec", accession=sub.accession, form_type=sub.form_type, url=url,
                              filed_date=sub.filed_date, accepted_at=sub.accepted_at, raw=xml)
    if not xml:
        mark_filing(conn, fid, "error", "no XML document")
        return []
    try:
        doc = parse_form4(xml)
    except Exception as e:  # malformed XML must not stop the pipeline
        mark_filing(conn, fid, "error", f"parse: {e}")
        return []
    ticker = doc.ticker or resolver.ticker_for_cik(doc.issuer_cik)
    if (not is_backfill) and doc.ticker and resolver.ticker_for_cik(doc.issuer_cik) and doc.ticker not in resolver.ticker_to_cik:
        ticker = resolver.ticker_for_cik(doc.issuer_cik)  # stale/odd symbol on the form -> official mapping
    owner = doc.primary_owner if doc.owners else None
    filed = sub.filed_date or (sub.accepted_at.date() if sub.accepted_at else None)
    rows = []
    for t in doc.transactions:
        rows.append({
            "accession": sub.accession, "line_key": t.line_key, "filing_id": fid, "issuer_cik": doc.issuer_cik,
            "ticker": ticker, "issuer_name": doc.issuer_name, "owner_cik": owner.cik if owner else None,
            "owner_name": owner.name if owner else "", "is_director": owner.is_director if owner else False,
            "is_officer": owner.is_officer if owner else False, "is_ten_pct": owner.is_ten_pct if owner else False,
            "officer_title": owner.officer_title if owner else None, "is_ceo": owner.is_ceo if owner else False,
            "is_cfo": owner.is_cfo if owner else False, "security_title": t.security_title, "is_derivative": t.is_derivative,
            "trans_date": t.trans_date, "trans_code": t.trans_code, "acq_disp": t.acq_disp, "shares": t.shares,
            "price": t.price, "value": t.value, "shares_after": t.shares_after, "direct_indirect": t.direct_indirect,
            "is_10b5_1": t.is_10b5_1, "category": t.category, "filed_date": filed, "accepted_at": sub.accepted_at,
            "source": "backfill_xml" if is_backfill else "live", "source_url": url,
        })
    insert_rows(conn, insider_transactions, rows, ["accession", "line_key"])
    mark_filing(conn, fid, "ok")
    created: list[int] = []
    # Amendments re-state earlier forms; they are stored but do not create new buy events.
    if sub.form_type.upper() == "4" and filed:
        created += insider_buy_events(conn, sub.accession, is_backfill=is_backfill)
        if any(t.category == "OPEN_MARKET_BUY" and not t.is_derivative for t in doc.transactions):
            created += cluster_events_for_issuer(conn, doc.issuer_cik, filed, is_backfill=is_backfill)
    for o in doc.owners:
        person = trump_person_for(o.cik, o.name)
        if person:
            det = [{"code": t.trans_code, "category": t.category, "date": str(t.trans_date), "shares": t.shares,
                    "price": t.price, "shares_after": t.shares_after, "derivative": t.is_derivative} for t in doc.transactions]
            eid = trump_event(conn, person=person, sub=sub, url=url, company=doc.issuer_name, issuer_cik=doc.issuer_cik,
                              ticker=ticker, ownership=(f"{doc.transactions[-1].shares_after:,.0f} shares after last reported transaction"
                                                        if doc.transactions and doc.transactions[-1].shares_after else None),
                              details={"transactions": det, "relationship": {"director": o.is_director, "officer": o.is_officer,
                                                                             "ten_pct": o.is_ten_pct, "title": o.officer_title}})
            if eid:
                created.append(eid)
            break
    if created and ticker and filed:
        m = multi_signal_check(conn, ticker, filed, is_backfill=is_backfill)
        if m:
            created.append(m)
    return created


# ------------------------------------------------------------------ Schedule 13D

def process_13d(conn: Connection, sub: Submission, cik_for_url: str, *, is_backfill: bool = False, header_text: str = "") -> list[int]:
    url = index_url(cik_for_url, sub.accession)
    xml = primary_xml(sub)
    raw = xml if xml else None
    fid, _new = upsert_filing(conn, source="sec", accession=sub.accession, form_type=sub.form_type, url=url,
                              filed_date=sub.filed_date, accepted_at=sub.accepted_at,
                              raw=None if is_backfill else raw)
    try:
        d = parse_schedule13(sub)
    except Exception as e:
        mark_filing(conn, fid, "error", f"parse: {e}")
        return []
    names = parse_filer_names(header_text)
    issuer_cik = d.issuer_cik or (names["SUBJECT COMPANY"][0][1] if names["SUBJECT COMPANY"] else None)
    issuer_name = d.issuer_name or (names["SUBJECT COMPANY"][0][0] if names["SUBJECT COMPANY"] else None)
    lead = d.lead
    investor_name = (lead.name if lead and lead.name else None) or (names["FILED BY"][0][0] if names["FILED BY"] else None)
    investor_cik = (lead.cik if lead and lead.cik else None) or (names["FILED BY"][0][1] if names["FILED BY"] else None)
    ticker = resolver.ticker_for_cik(issuer_cik) or resolver.ticker_for_cusip(d.cusip)
    if ticker and d.cusip:
        remember_cusip(conn, ticker, d.cusip)
    pct = d.max_percent
    prev = None
    prev_class = None
    if issuer_cik and investor_cik:
        prev_row = conn.execute(select(activist_filings.c.percent, activist_filings.c.item4_class).where(
            activist_filings.c.issuer_cik == issuer_cik, activist_filings.c.investor_cik == investor_cik,
            activist_filings.c.accession != sub.accession, activist_filings.c.filed_date <= (sub.filed_date or activist_filings.c.filed_date))
            .order_by(desc(activist_filings.c.filed_date)).limit(1)).first()
        if prev_row:
            prev, prev_class = prev_row
    change = (pct - prev) if (pct is not None and prev is not None) else None
    cls, labels = classify_item4(d.item4_text)
    filed = sub.filed_date or (sub.accepted_at.date() if sub.accepted_at else None)
    thirteen_d = is_13d(sub.form_type)
    amend = is_amendment(sub.form_type)
    insert_rows(conn, activist_filings, [{
        "accession": sub.accession, "filing_id": fid, "form_type": sub.form_type, "is_13d": thirteen_d, "is_amendment": amend,
        "issuer_cik": issuer_cik, "issuer_name": issuer_name, "ticker": ticker, "cusip": d.cusip,
        "investor_name": investor_name, "investor_cik": investor_cik, "percent": pct, "shares": d.max_shares,
        "prev_percent": prev, "percent_change": change, "event_date": d.event_date, "filed_date": filed,
        "accepted_at": sub.accepted_at, "item4_text": (d.item4_text or "")[:20000] or None, "item4_class": cls,
        "item4_labels": labels, "source_url": url,
    }], ["accession"])
    mark_filing(conn, fid, "ok")
    created: list[int] = []
    # Amendments are not independent events.  Create a new signal only for a material ownership
    # move (>=1 percentage point) or a changed Item 4 purpose; all amendments remain stored/auditable.
    material_amendment = (not amend) or (prev is None and prev_class is None) or \
        (change is not None and abs(change) >= 1.0) or (prev_class is not None and cls != prev_class)
    if thirteen_d and filed and material_amendment:
        prio = P.activist_priority(item4_class=cls, is_amendment=amend, percent=pct, percent_change=change)
        pct_s = f"{pct:.1f}%" if pct is not None else "n/a"
        eid = create_event(
            conn, event_type="ACTIVIST_13D", dedupe_key=f"13D:{sub.accession}", public_date=filed, public_at=sub.accepted_at,
            transaction_date=d.event_date, ticker=ticker, company_name=issuer_name, issuer_cik=issuer_cik, priority=prio,
            title=f"{'13D/A' if amend else '13D'} {investor_name or '?'} {pct_s} of {('$' + ticker) if ticker else issuer_name} - {cls}",
            payload={"investor": investor_name, "investor_cik": investor_cik, "percent": pct, "shares": d.max_shares,
                     "previous_percent": prev, "change": change, "item4_class": cls, "item4_labels": labels,
                     "previous_item4_class": prev_class,
                     "item4_excerpt": (d.item4_text or "")[:600], "is_amendment": amend, "form_type": sub.form_type,
                     "cusip": d.cusip, "accession": sub.accession, "event_date": d.event_date},
            source_url=url, is_backfill=is_backfill,
        )
        if eid:
            created.append(eid)
    for pname, pcik in names.get("FILED BY", []) + [(p.name, p.cik) for p in d.persons]:
        person = trump_person_for(pcik, pname)
        if person:
            eid = trump_event(conn, person=person, sub=sub, url=url, company=issuer_name, issuer_cik=issuer_cik, ticker=ticker,
                              ownership=f"{pct}% of class" if pct is not None else None,
                              details={"investor": pname, "percent": pct, "shares": d.max_shares, "item4_class": cls})
            if eid:
                created.append(eid)
            break
    if created and ticker and filed:
        m = multi_signal_check(conn, ticker, filed, is_backfill=is_backfill)
        if m:
            created.append(m)
    return created


# ------------------------------------------------------------------ Form 144

def process_144(conn: Connection, sub: Submission, cik_for_url: str, *, is_backfill: bool = False) -> list[int]:
    url = index_url(cik_for_url, sub.accession)
    xml = primary_xml(sub)
    fid, _new = upsert_filing(conn, source="sec", accession=sub.accession, form_type=sub.form_type, url=url,
                              filed_date=sub.filed_date, accepted_at=sub.accepted_at, raw=xml)
    if not xml:
        mark_filing(conn, fid, "error", "no XML (paper Form 144)")
        return []
    try:
        f = parse_form144(xml)
    except Exception as e:
        mark_filing(conn, fid, "error", f"parse: {e}")
        return []
    ticker = resolver.ticker_for_cik(f.issuer_cik)
    filed = sub.filed_date or (sub.accepted_at.date() if sub.accepted_at else None)
    insert_rows(conn, form144_filings, [{
        "accession": sub.accession, "filing_id": fid, "issuer_cik": f.issuer_cik, "issuer_name": f.issuer_name,
        "ticker": ticker, "insider_name": f.insider_name, "relationship": f.relationship, "shares": f.shares,
        "est_value": f.est_value, "shares_outstanding": f.shares_outstanding, "broker": f.broker,
        "approx_sale_date": f.approx_sale_date, "prior_sales_3m": f.prior_sales, "filed_date": filed,
        "accepted_at": sub.accepted_at, "source_url": url,
    }], ["accession"])
    mark_filing(conn, fid, "ok")
    created: list[int] = []
    if filed and P.form144_is_major(f.est_value, f.shares, f.shares_outstanding):
        pct = (f.shares / f.shares_outstanding) if (f.shares and f.shares_outstanding) else None
        eid = create_event(
            conn, event_type="FORM144_MAJOR_SALE", dedupe_key=f"144:{sub.accession}", public_date=filed,
            public_at=sub.accepted_at, ticker=ticker, company_name=f.issuer_name, issuer_cik=f.issuer_cik, priority=3,
            title=f"Form 144: {f.insider_name} proposes to sell {_money(f.est_value)} of {('$' + ticker) if ticker else f.issuer_name}",
            payload={"insider": f.insider_name, "relationship": f.relationship, "shares": f.shares, "est_value": f.est_value,
                     "pct_of_outstanding": pct, "broker": f.broker, "approx_sale_date": f.approx_sale_date,
                     "prior_sales_3m": f.prior_sales, "planned_10b5_1": bool(f.plan_adoption_dates),
                     "plan_adoption_dates": f.plan_adoption_dates, "accession": sub.accession,
                     "note": "Information signal only - a proposed sale is not by itself a negative signal."},
            source_url=url, is_backfill=is_backfill,
        )
        if eid:
            created.append(eid)
    person = trump_person_for(None, f.insider_name)
    if person:
        eid = trump_event(conn, person=person, sub=sub, url=url, company=f.issuer_name, issuer_cik=f.issuer_cik, ticker=ticker,
                          ownership=None, details={"proposed_shares": f.shares, "est_value": f.est_value, "broker": f.broker})
        if eid:
            created.append(eid)
    return created


def process_text(conn: Connection, text: str, cik_for_url: str, *, is_backfill: bool = False) -> list[int]:
    sub = parse_submission(text)
    ft = sub.form_type.upper()
    header = text.split("</SEC-HEADER>")[0] if "</SEC-HEADER>" in text else text[:20000]
    if ft in ("4", "4/A"):
        return process_form4(conn, sub, cik_for_url, is_backfill=is_backfill)
    if ft.startswith(("SC 13D", "SCHEDULE 13D", "SC 13G", "SCHEDULE 13G")):
        return process_13d(conn, sub, cik_for_url, is_backfill=is_backfill, header_text=header)
    if ft in ("144", "144/A"):
        return process_144(conn, sub, cik_for_url, is_backfill=is_backfill)
    return []
