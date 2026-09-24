"""Helpers for SEC complete-submission (.txt) documents."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


@dataclass
class Submission:
    accession: str
    form_type: str
    accepted_at: datetime | None  # timezone-aware (America/New_York per SEC header)
    filed_date: date | None
    documents: list[tuple[str, str, str]]  # (type, filename, text)


def accession_from_url(url: str) -> str | None:
    m = re.search(r"(\d{10}-\d{2}-\d{6})", url)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{18})/", url)
    if m:
        a = m.group(1)
        return f"{a[:10]}-{a[10:12]}-{a[12:]}"
    return None


def full_text_url(cik: str | int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}.txt"


def index_url(cik: str | int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"


def parse_submission(text: str) -> Submission:
    acc = re.search(r"ACCESSION NUMBER:\s*(\S+)", text)
    ftype = re.search(r"CONFORMED SUBMISSION TYPE:\s*(.+)", text)
    acc_dt = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", text)
    filed = re.search(r"FILED AS OF DATE:\s*(\d{8})", text)
    accepted = None
    if acc_dt:
        # SEC acceptance datetime is Eastern time.
        accepted = datetime.strptime(acc_dt.group(1), "%Y%m%d%H%M%S").replace(tzinfo=NY)
    docs = []
    for m in re.finditer(r"<DOCUMENT>(.*?)</DOCUMENT>", text, re.S):
        body = m.group(1)
        t = re.search(r"<TYPE>([^\n<]+)", body)
        fn = re.search(r"<FILENAME>([^\n<]+)", body)
        tx = re.search(r"<TEXT>(.*)</TEXT>", body, re.S)
        docs.append(((t.group(1).strip() if t else ""), (fn.group(1).strip() if fn else ""), (tx.group(1) if tx else "")))
    return Submission(
        accession=acc.group(1).strip() if acc else "",
        form_type=ftype.group(1).strip() if ftype else "",
        accepted_at=accepted,
        filed_date=datetime.strptime(filed.group(1), "%Y%m%d").date() if filed else None,
        documents=docs,
    )


def primary_xml(sub: Submission) -> str | None:
    for _t, _fn, body in sub.documents:
        m = re.search(r"<XML>(.*?)</XML>", body, re.S)
        if m:
            return m.group(1).strip()
    return None


def strip_ns(xml: str) -> str:
    """Drop namespace prefixes/declarations so simple tag lookups work across schema versions."""
    xml = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml)
    xml = re.sub(r"<(/?)[A-Za-z0-9_]+:", r"<\1", xml)
    return xml


def to_bool(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "y", "yes"}


def to_float(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip().replace(",", "").replace("$", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def to_date(v: str | None) -> date | None:
    if not v:
        return None
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(v[:11] if fmt == "%d-%b-%Y" else v[:10], fmt).date()
        except ValueError:
            continue
    return None
