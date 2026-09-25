"""Re-derive owner attribution for Form 4 rows ingested from XML before joint-filing support.

The raw XML of every live / gap-backfill Form 4 is stored (filings.raw_gz), so the owner list,
ownership text and footnotes can be re-read exactly as filed - no network needed."""
from __future__ import annotations

import gzip
import logging

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Engine

from app.db.models import filings, insider_transactions
from app.parsers.form4 import parse_form4
from app.signals.owners import OwnerRef, attribute, joint_display_name, joint_roles, save_filing_owners

log = logging.getLogger(__name__)


def reattribute_xml_rows(engine: Engine, batch: int = 2000) -> dict:
    with engine.connect() as conn:
        accs = [a for (a,) in conn.execute(select(insider_transactions.c.accession).where(and_(
            insider_transactions.c.source.in_(["live", "backfill_xml"]), insider_transactions.c.owner_attribution.is_(None)))
            .distinct().limit(batch)).all()]
    fixed = joint = 0
    for acc in accs:
        with engine.begin() as conn:
            raw = conn.execute(select(filings.c.raw_gz).where(and_(filings.c.source == "sec", filings.c.accession == acc))).scalar()
            if not raw:
                conn.execute(update(insider_transactions).where(insider_transactions.c.accession == acc)
                             .values(owner_attribution="unknown"))
                continue
            try:
                doc = parse_form4(gzip.decompress(raw).decode("utf-8", "replace"))
            except Exception as e:
                log.warning("reattribute %s: %s", acc, e)
                conn.execute(update(insider_transactions).where(insider_transactions.c.accession == acc)
                             .values(owner_attribution="unknown"))
                continue
            refs = [OwnerRef(o.cik, o.name, o.is_director, o.is_officer, o.is_ten_pct, o.officer_title, o.is_ceo, o.is_cfo)
                    for o in doc.owners if o.cik]
            filed = conn.execute(select(insider_transactions.c.filed_date).where(insider_transactions.c.accession == acc)).scalar()
            save_filing_owners(conn, acc, refs, doc.issuer_cik, filed)
            for t in doc.transactions:
                who, how = attribute(refs, t.ownership_text(doc.footnotes))
                vals = {"nature_of_ownership": t.nature_of_ownership, "owner_attribution": how}
                if len(refs) > 1:
                    roles = joint_roles(refs) if who is None else {
                        "is_director": who.is_director, "is_officer": who.is_officer, "is_ten_pct": who.is_ten_pct,
                        "officer_title": who.officer_title, "is_ceo": who.is_ceo, "is_cfo": who.is_cfo}
                    vals |= {"owner_cik": who.cik if who else None,
                             "owner_name": who.name if who else joint_display_name(refs), **roles,
                             "joint_owner_ciks": [o.cik for o in refs]}
                    joint += who is None
                conn.execute(update(insider_transactions).where(and_(
                    insider_transactions.c.accession == acc, insider_transactions.c.line_key == t.line_key)).values(**vals))
            fixed += 1
    return {"filings": fixed, "joint_rows": joint, "remaining_batch": len(accs) == batch}
