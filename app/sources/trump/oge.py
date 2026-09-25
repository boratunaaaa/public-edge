"""OGE public financial disclosures of Trump-family officials (primary source).

Pipeline (reproducible; every step keeps its input):
  1. index   - OGE's official REST endpoint behind the "Officials' Individual Disclosures Search
               Collection" (extapps2.oge.gov/201/Presiden.nsf/API.xsp/v2/rest), filtered to filers
               named Trump;
  2. fetch   - the PDF itself; its SHA-256 is stored so the parsed text can be traced to the file;
  3. text    - the PDF's embedded text layer (pdfplumber). The President's 278-T reports are scans
               with an OCR layer, so text can be noisy ("lourchaso" for "Purchase");
  4. parse   - transaction rows (278-T): description, type, date, amount bracket. Each row gets a
               parse confidence (HIGH / MEDIUM / LOW) instead of a silent guess;
  5. event   - one TRUMP_OGE_DISCLOSURE event per new document (documents found on the first run
               are stored as history and never alerted).
No inference: a ticker is attached only when the filer wrote an explicit "(TICKER)" that exists in
the SEC ticker file; issuer names are never fuzzy-matched to companies.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app import http
from app.db.models import oge_documents, oge_transactions
from app.db.store import create_event
from app.entities.registry import resolver
from app.signals import priority as P
from app.sources.base import RunResult, Source

log = logging.getLogger(__name__)
INDEX_URL = "https://extapps2.oge.gov/201/Presiden.nsf/API.xsp/v2/rest"
MAX_DOCS_PER_RUN = 3
ALERT_MAX_AGE_DAYS = 14  # an older document found later (first load, interrupted load) is history, never an alert
FILER_RE = re.compile(r"^\s*trump\s*,", re.I)
HREF_RE = re.compile(r"href='([^']+)'>([^<]+)<", re.I)
BRACKETS = [(1_001, 15_000), (15_001, 50_000), (50_001, 100_000), (100_001, 250_000), (250_001, 500_000),
            (500_001, 1_000_000), (1_000_001, 5_000_000), (5_000_001, 25_000_000), (25_000_001, 50_000_000)]
TYPE_RULES = [("PURCHASE", re.compile(r"urch", re.I)), ("SALE", re.compile(r"\bsal[eo]\b|\bsold\b", re.I)),
              ("EXCHANGE", re.compile(r"xchang", re.I))]
ROW_RE = re.compile(r"^\s*(\d{1,3})\s+(.+?)\s+(\S*(?:urch|sal[eo]|xchang)\S*)\s+([\d/Il|]{5,11})\s+(\S+)\s+(.+)$", re.I)
TICKER_IN_DESC = re.compile(r"\(([A-Z]{1,5}(?:\.[A-Z])?)\)")


def parse_index(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if not FILER_RE.match(r.get("name") or ""):
            continue
        m = HREF_RE.search(r.get("type") or "")
        if not m:
            continue
        try:
            dd = datetime.fromisoformat(r["docDate"]).replace(tzinfo=timezone.utc) if r.get("docDate") else None
        except ValueError:
            dd = None
        out.append({"doc_url": m.group(1).replace(" ", "%20"), "doc_type": m.group(2).strip()[:40], "filer_name": r["name"].strip(),
                    "agency": r.get("agency"), "title": r.get("title"), "doc_date": dd})
    return out


def fix_date(raw: str) -> tuple[date | None, bool]:
    """(date, repaired). OCR often reads a slash as '1' or drops it: '7/812026' -> 7/8/2026."""
    s = raw.replace("I", "1").replace("l", "1").replace("|", "1")
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))), False
        except ValueError:
            return None, False
    for pat in (r"(\d{1,2})/(\d{1,2}?)1(\d{4})", r"(\d{1,2})/(\d{1,2}?)(\d{4})", r"(\d{1,2}?)1(\d{1,2})/(\d{4})"):
        m = re.fullmatch(pat, s)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(1)), int(m.group(2))), True
            except ValueError:
                continue
    return None, False


def parse_amount(raw: str) -> tuple[float | None, float | None, bool]:
    """(min, max, exact_bracket). Standard OGE value brackets only."""
    txt = raw.replace("S", "$").replace("O", "0").replace("o", "0")
    if re.search(r"over\s*\$?\s*50", txt, re.I):
        return 50_000_001.0, None, True
    parts = [re.sub(r"\D", "", p) for p in txt.split("$") if re.search(r"\d", p)]
    nums = [int(p) for p in parts if p][:2]
    if len(nums) == 2 and tuple(nums) in BRACKETS:
        return float(nums[0]), float(nums[1]), True
    for lo, hi in BRACKETS:  # snap on the lower bound (the upper is most often OCR-damaged)
        if nums and nums[0] == lo:
            return float(lo), float(hi), False
    return None, None, False


def parse_transactions(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        no, desc, typ_raw, date_raw, _notice, amount_raw = m.groups()
        tx_type = next((t for t, rx in TYPE_RULES if rx.search(typ_raw)), "UNKNOWN")
        d, repaired = fix_date(date_raw)
        lo, hi, exact = parse_amount(amount_raw)
        conf = "HIGH" if (d and not repaired and exact and tx_type != "UNKNOWN") else \
            "MEDIUM" if (d and lo is not None and tx_type != "UNKNOWN") else "LOW"
        tick = None
        tm = TICKER_IN_DESC.search(desc)
        if tm and tm.group(1) in resolver.ticker_to_cik:
            tick = tm.group(1)
        rows.append({"line_no": int(no), "description": desc.strip()[:1000], "tx_type": tx_type, "tx_date": d,
                     "amount_min": lo, "amount_max": hi, "amount_text": amount_raw.strip()[:120], "ticker": tick,
                     "parse_confidence": conf})
    # line numbers restart per page in some forms: keep the first occurrence of each number
    seen, out = set(), []
    for r in rows:
        if r["line_no"] in seen:
            continue
        seen.add(r["line_no"])
        out.append(r)
    return out


def fetch_index(attempts: int = 3, pause_s: float = 20.0) -> list[dict]:
    """The Domino REST service intermittently answers 400 'Error while rendering service' (verified
    2026-09-25: the next request succeeds). Treat that specific error as transient."""
    import time
    for i in range(attempts):
        try:
            return http.get(INDEX_URL, retries=2).json().get("data") or []
        except http.HttpError as e:
            if e.status != 400 or "rendering" not in str(e).lower() or i == attempts - 1:
                raise
            time.sleep(pause_s)
    return []


def pdf_text(pdf: bytes) -> tuple[str, int]:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return "\n".join((p.extract_text() or "") for p in doc.pages), len(doc.pages)


class TrumpOgeSource(Source):
    name = "trump_oge"
    interval_seconds = 3600
    slow = True
    critical = False

    def run_live(self, engine: Engine) -> RunResult:
        res = RunResult()
        docs = parse_index(fetch_index())
        with engine.connect() as conn:
            resolver.load(conn)
            known = {u for (u,) in conn.execute(select(oge_documents.c.doc_url)).all()}
            first_run = not known
        # newest first (a fresh filing is never stuck behind the historical backlog); scanned PDFs take
        # minutes to extract, so each run handles a few and the backlog drains over later runs
        todo = sorted((d for d in docs if d["doc_url"] not in known),
                      key=lambda d: d["doc_date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for d in todo[:MAX_DOCS_PER_RUN]:
            old = d["doc_date"] is not None and d["doc_date"] < datetime.now(timezone.utc) - timedelta(days=ALERT_MAX_AGE_DAYS)
            try:
                res.events += self._ingest(engine, d, is_backfill=first_run or old)
                res.items += 1
            except Exception as e:
                log.warning("OGE %s failed: %s", d["doc_url"], e)
        return res

    def _ingest(self, engine: Engine, d: dict, *, is_backfill: bool) -> list[int]:
        pdf = http.get(d["doc_url"], retries=2).content
        sha = hashlib.sha256(pdf).hexdigest()
        try:
            text, pages = pdf_text(pdf)
            status, note = "parsed", None
        except Exception as e:  # keep the document record; parsing can be retried from the stored hash
            text, pages, status, note = "", None, "error", f"{type(e).__name__}: {e}"[:500]
        txs = parse_transactions(text) if "Transaction" in d["doc_type"] else []
        with engine.begin() as conn:
            did = conn.execute(pg_insert(oge_documents).values(
                **d, sha256=sha, n_pages=pages, text_gz=gzip.compress(text.encode("utf-8"), 6) if text else None,
                parse_status=status, parse_note=note).on_conflict_do_nothing(index_elements=["doc_url"])
                .returning(oge_documents.c.id)).scalar()
            if did is None:
                return []
            if txs:
                conn.execute(pg_insert(oge_transactions).values([{"doc_id": did, **t} for t in txs])
                             .on_conflict_do_nothing(index_elements=["doc_id", "line_no"]))
            n_buy = sum(1 for t in txs if t["tx_type"] == "PURCHASE")
            n_sell = sum(1 for t in txs if t["tx_type"] == "SALE")
            conf = {c: sum(1 for t in txs if t["parse_confidence"] == c) for c in ("HIGH", "MEDIUM", "LOW")}
            top = sorted([t for t in txs if t["amount_min"]], key=lambda t: -(t["amount_min"] or 0))[:5]
            pub = (d["doc_date"] or datetime.now(timezone.utc))
            eid = create_event(
                conn, event_type="TRUMP_OGE_DISCLOSURE", dedupe_key=f"OGE:{sha}", public_date=pub.date(), public_at=d["doc_date"],
                priority=P.TRUMP_FAMILY_PRIORITY if not is_backfill else 0,
                title=f"{d['filer_name']}: OGE {d['doc_type']} ({len(txs)} transactions)",
                payload={"person": d["filer_name"], "doc_type": d["doc_type"], "doc_date": pub.isoformat(), "pages": pages,
                         "n_transactions": len(txs), "purchases": n_buy, "sales": n_sell, "parse_confidence": conf,
                         "top_transactions": [{k: t[k] for k in ("description", "tx_type", "tx_date", "amount_min", "amount_max",
                                                                  "parse_confidence")} for t in top],
                         "sha256": sha, "source": "OGE (primary)"},
                source_url=d["doc_url"], is_backfill=is_backfill, date_quality="exact" if d["doc_date"] else "first_seen")
        return [eid] if eid else []
