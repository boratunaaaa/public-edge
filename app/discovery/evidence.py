"""Evidence collection: official SEC documents -> sentence-level, source-linked evidence rows."""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app import http
from app.db.models import discovery_evidence, universe_snapshots
from app.db.store import job_state, save_job_state
from app.discovery import language as L
from app.discovery.sec_data import doc_url, efts_search
from app.parsers.schedule13d import html_to_text

log = logging.getLogger(__name__)

# exact phrases for EDGAR full-text search (the sentence classifier then decides what they mean)
QUERY_PHRASES = [
    "sole source", "sole supplier", "only domestic producer", "only U.S. producer", "supply constrained",
    "capacity constrained", "demand exceeds our capacity", "record backlog", "first commercial production",
    "commercial-scale production", "first commercial shipment", "customer qualification", "design win",
    "long-term supply agreement", "take-or-pay", "offtake agreement", "capacity reservation", "pilot production",
    "mass production", "production ramp", "new manufacturing line", "hyperscaler", "Defense Production Act",
    "critical mineral", "first-of-a-kind", "low-rate initial production",
]
FORMS = ["10-K", "10-Q", "8-K", "S-1", "424B4"]
BUSINESS_RE = re.compile(r"^(We|The Company|[A-Z][A-Za-z0-9&.\- ]{2,40}) (is a|is an|are a|are an|designs|develops|manufactures|provides|produces|makes)\b")
MAX_DOC_BYTES = 4_000_000


def extract_from_text(conn: Connection, cik: str, text: str, *, url: str, accession: str | None, form: str | None,
                      filed: date | None, max_sentences: int = 4000) -> int:
    """Store every sentence that carries a commercial signal, a counterparty relation or (10-K) a
    business description. Returns the number of new evidence rows."""
    rows = []
    for i, s in enumerate(L.sentences(text)[:max_sentences]):
        labels = L.signals_in(s)
        rels = L.relations_in(s)
        base = dict(cik=cik, sentence=s[:1500], sentence_hash=L.sentence_hash(s), source_url=url, accession=accession,
                    form=form, filed_date=filed)
        for lab in labels:
            rows.append(dict(base, kind="bottleneck" if lab not in ("CLINICAL", "PRE_REVENUE", "RESEARCH_STAGE") else "stage",
                             label=lab, relation=None, agreement=None, counterparty=None, counterparty_category=None,
                             hedged=bool(L.HEDGE_RE.search(s))))
        for r in rels:
            rows.append(dict(base, kind="customer", label=r.category, relation=r.relation, agreement=r.agreement,
                             counterparty=r.counterparty, counterparty_category=r.category, hedged=r.hedged))
        if form and form.startswith("10-K") and i < 400 and BUSINESS_RE.match(s) and len(s) < 500:
            rows.append(dict(base, kind="business", label="WHAT_THEY_DO", relation=None, agreement=None, counterparty=None,
                             counterparty_category=None, hedged=False))
    if not rows:
        return 0
    n = 0
    for i in range(0, len(rows), 500):
        res = conn.execute(pg_insert(discovery_evidence).values(rows[i:i + 500])
                           .on_conflict_do_nothing(index_elements=["cik", "kind", "label", "sentence_hash"])
                           .returning(discovery_evidence.c.id))
        n += len(res.fetchall())
    return n


def fetch_doc_text(url: str) -> str:
    r = http.get(url, retries=2)
    raw = r.content[:MAX_DOC_BYTES].decode("utf-8", "replace")
    return html_to_text(raw) if "<" in raw[:3000] else raw


def universe_ciks(conn: Connection, lo: float = 50e6, hi: float = 10e9) -> dict[str, dict]:
    latest = conn.execute(select(universe_snapshots.c.as_of).order_by(universe_snapshots.c.as_of.desc()).limit(1)).scalar()
    if latest is None:
        return {}
    rows = conn.execute(select(universe_snapshots).where(universe_snapshots.c.as_of == latest)).mappings().all()
    return {r["cik"]: dict(r) for r in rows if r["market_cap"] and lo <= r["market_cap"] < hi}


def collect(engine: Engine, *, days_initial: int = 365, max_docs: int = 800) -> dict:
    """Incremental: first run looks back `days_initial`, later runs from the last run date."""
    name = "discovery_evidence"
    with engine.begin() as conn:
        st = job_state(conn, name)
        uni = universe_ciks(conn)
    if not uni:
        return {"skipped": "no universe snapshot yet"}
    end = date.today()
    start = date.fromisoformat(st["last_end"]) - timedelta(days=2) if st.get("last_end") else end - timedelta(days=days_initial)
    seen_docs: set[str] = set(st.get("recent_docs", [])[-5000:])
    hits: dict[str, dict] = {}
    for ph in QUERY_PHRASES:
        try:
            for h in efts_search(ph, FORMS, start, end, max_hits=300):
                if h["cik"] in uni and h["filename"]:
                    hits.setdefault(f"{h['adsh']}:{h['filename']}", h)
        except Exception as e:
            log.warning("evidence query %r failed: %s", ph, e)
    # discovery priority band ($100M-$2B) first, then newest filing first (stable two-key sort)
    order = sorted(hits.values(), key=lambda h: h["file_date"] or "", reverse=True)
    order.sort(key=lambda h: not (100e6 <= uni[h["cik"]]["market_cap"] <= 2e9))
    docs = new_rows = 0
    touched: set[str] = set()
    for h in order:
        key = f"{h['adsh']}:{h['filename']}"
        if key in seen_docs:
            continue
        if docs >= max_docs:
            break
        url = doc_url(h["cik"], h["adsh"], h["filename"])
        try:
            text = fetch_doc_text(url)
            with engine.begin() as conn:
                new_rows += extract_from_text(conn, h["cik"], text, url=url, accession=h["adsh"], form=h["form"],
                                              filed=date.fromisoformat(h["file_date"]) if h["file_date"] else None)
            touched.add(h["cik"])
        except Exception as e:
            log.warning("evidence doc %s failed: %s", url, e)
        seen_docs.add(key)
        docs += 1
    complete = docs < max_docs
    with engine.begin() as conn:
        save_job_state(conn, name, {"last_end": end.isoformat() if complete else st.get("last_end", (end - timedelta(days=days_initial)).isoformat()),
                                    "recent_docs": sorted(seen_docs)[-5000:], "last_docs": docs, "last_rows": new_rows,
                                    "pending_hits": len(order) - docs})
    return {"hits": len(hits), "docs": docs, "new_evidence": new_rows, "companies": len(touched), "complete": complete}
