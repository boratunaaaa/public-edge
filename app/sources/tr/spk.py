"""SPK (Capital Markets Board) weekly bulletins - an official, freely reachable Turkish source.

Extracted (company level only - person names, national ids and IBANs are never stored):
  * A. "Halka Açık Ortaklıkların Pay İhraçları": approved share issues -> TR_RIGHTS_ISSUE_APPROVAL (bedelli, dilution)
    and TR_BONUS_ISSUE_APPROVAL (bedelsiz: iç kaynak / kâr payı), with current / new capital and amounts.
  * "SUÇ DUYURUSU, İDARİ PARA CEZASI ..." sections: BIST codes named in the section -> TR_SPK_ENFORCEMENT (risk).
Point in time: bulletin date, end of day Istanbul (bulletins are posted in the evening).
Identity: a "(CODE)" in the text resolves through the TR security master (symbol + date); a company name only through
an exact normalized name match on the KAP member list; otherwise the record is stored with needs_review."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from app import http

PARSER_VERSION = "spk-bulletin-v1"
LIST_URL = "https://spk.gov.tr/spk-bultenleri/{year}-yili-spk-bultenleri"
NUM = r"(\d{1,3}(?:\.\d{3})+|\d+|-)"
ROW = re.compile(rf"^(?P<name>.*?)\s*{NUM}\s+{NUM}\s+{NUM}\s+{NUM}\s+{NUM}(?:\s+(?P<sale>.+?))?$")
CODE = re.compile(r"\(([A-ZÇĞİÖŞÜ]{4,6})\)")
SECTION = re.compile(r"^([A-H])\.\s+(.+)$")


@dataclass
class Bulletin:
    number: str
    date: date
    issues: list[dict] = field(default_factory=list)
    enforcement_codes: list[str] = field(default_factory=list)


def _n(x: str) -> float | None:
    return None if x in ("-", "") else float(x.replace(".", ""))


def parse_text(txt: str) -> Bulletin:
    m = re.search(r"(\d{4}/\d{1,3})\s+(\d{2}/\d{2}/\d{4})", txt)
    if not m:
        raise ValueError("not an SPK bulletin (no number/date header)")
    b = Bulletin(m.group(1), datetime.strptime(m.group(2), "%d/%m/%Y").date())
    lines = [ln.strip() for ln in txt.splitlines()]
    section, sub = None, None
    pending_name: list[str] = []
    for i, ln in enumerate(lines):
        s = SECTION.match(ln)
        if s and s.group(2).isupper():
            section, sub, pending_name = s.group(2), None, []
            continue
        ss = re.match(r"^(\d)\.\s+(.+)$", ln)
        if ss:
            sub, pending_name = ss.group(2), []
            continue
        if section and "SUÇ DUYURUSU" in section:
            for code in CODE.findall(ln):
                if code not in b.enforcement_codes:
                    b.enforcement_codes.append(code)
            continue
        if section and "İHRAÇ" in section and sub and "Pay İhraçları" in sub:
            r = ROW.match(ln)
            if r and r.group(2) != "-" and _n(r.group(2)) and _n(r.group(3)):
                name = " ".join(pending_name + ([r.group("name")] if r.group("name") else []))
                if not name.endswith(("AŞ", "A.Ş.", "AŞ.")) and i + 1 < len(lines) and not ROW.match(lines[i + 1]):
                    name = (name + " " + lines[i + 1]).strip()  # name wrapped after the numbers
                cur, new, rights, internal, dividend = (_n(r.group(k)) for k in (2, 3, 4, 5, 6))
                b.issues.append({"company": name.strip(), "current_capital": cur, "new_capital": new, "rights": rights,
                                 "bonus_internal": internal, "bonus_dividend": dividend,
                                 "sale_type": (r.group("sale") or "").strip() or None})
                pending_name = []
            elif ln and not r and not ln.startswith(("[", "Bedelli", "Mevcut", "Ortaklık", "Sermaye", "Artırımı", "NOT")):
                if b.issues and b.issues[-1]["company"].endswith(ln):
                    continue
                pending_name = [ln] if not pending_name else pending_name + [ln]
                if len(pending_name) > 2:
                    pending_name = pending_name[-1:]
    return b


def events_of(b: Bulletin) -> list[tuple[str, dict]]:
    out = []
    for x in b.issues:
        if x["rights"]:
            out.append(("TR_RIGHTS_ISSUE_APPROVAL", {**x, "dilution_pct": x["rights"] / x["current_capital"]}))
        bonus = (x["bonus_internal"] or 0) + (x["bonus_dividend"] or 0)
        if bonus:
            out.append(("TR_BONUS_ISSUE_APPROVAL", {**x, "bonus_pct": bonus / x["current_capital"]}))
    for code in b.enforcement_codes:
        out.append(("TR_SPK_ENFORCEMENT", {"code": code}))
    return out


def bulletin_urls(year: int) -> list[str]:
    page = http.get(LIST_URL.format(year=year)).text
    return list(dict.fromkeys(re.findall(rf"https://spk\.gov\.tr/data/[a-f0-9]+/{year}-\d+\.pdf", page)))


def fetch_text(url: str) -> str:
    import io

    import pdfplumber
    r = http.get(url)
    with pdfplumber.open(io.BytesIO(r.content)) as p:
        return "\n".join((pg.extract_text() or "") for pg in p.pages)


def store(conn, b: Bulletin, url: str, *, is_backfill: bool) -> list[int]:
    from app.core import records
    from app.core.provenance import end_of_local_day
    from app.db.store import create_event
    from app.entities.names import normalize_company
    from app.markets import master_db
    avail = end_of_local_day(b.date, "TR")
    names = master_db.name_index(conn, "TR")
    ids = []
    for et, info in events_of(b):
        sid = None
        conf = "MEDIUM"
        if et == "TR_SPK_ENFORCEMENT":
            sid = master_db.resolve(conn, info["code"], b.date, country="TR")
            label = info["code"]
            if sid is None:
                # the TR master starts at first sight (2026-09-28): for a bulletin of the last 365 days, the code's
                # CURRENT unique holder is accepted with LOW identity confidence (BIST code reuse is rare, not impossible)
                cur = master_db.resolve(conn, info["code"], date.today(), country="TR")
                if cur and (date.today() - b.date).days <= 365:
                    sid, conf = cur, "LOW"
        else:
            hits = names.get(normalize_company(info["company"]), set())
            sid = next(iter(hits)) if len(hits) == 1 else None
            label = info["company"]
        key = f"{b.number}:{et}:{label}"
        records.put(conn, country="TR", dataset="tr_spk_bulletin", record_key=key, as_of_date=b.date, available_at=avail,
                    payload={**info, "bulletin": b.number, "type": et}, parser_version=PARSER_VERSION, license_status="PUBLIC_WITH_TERMS",
                    security_id=sid, entity_name=label, identity_confidence=conf if sid else None, needs_review=sid is None,
                    source_url=url)
        if not sid:
            continue
        ticker = sid.split(":")[-1]
        eid = create_event(conn, event_type=et, dedupe_key=f"SPK:{key}", public_date=b.date, ticker=ticker, company_name=label,
                           title=f"SPK {b.number}: {label} - {et.replace('TR_', '').replace('_', ' ').lower()}", payload=info,
                           source_url=url, is_backfill=is_backfill, country="TR", exchange="XIST", security_id=sid,
                           event_family={"TR_SPK_ENFORCEMENT": "TR_SPK_SANCTION", "TR_RIGHTS_ISSUE_APPROVAL": "TR_DILUTION_RISK",
                                         "TR_BONUS_ISSUE_APPROVAL": "TR_BONUS_ISSUE"}[et],
                           identity_confidence=conf, parser_version=PARSER_VERSION, available_at_override=avail, date_quality="date")
        if eid:
            ids.append(eid)
    return ids


from datetime import timedelta as _td  # noqa: E402

from app.sources.base import RunResult, Source  # noqa: E402


class SpkBulletinSource(Source):
    """Weekly+ official bulletins (PDF). Company-level facts only."""
    name = "tr_spk_bulletin"
    interval_seconds = 6 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "TR", "tr_spk_bulletin", "PUBLIC_WITH_TERMS", PARSER_VERSION
    stale_after = _td(days=10)

    def run_live(self, engine, year: int | None = None, is_backfill: bool = False) -> RunResult:
        from sqlalchemy import text
        year = year or date.today().year
        with engine.connect() as c:
            done = set(c.execute(text("SELECT DISTINCT source_url FROM dataset_records WHERE dataset = 'tr_spk_bulletin'")).scalars())
        res = RunResult(watermark={"year": year})
        from app.sources.tr import members
        rows = members.fetch()  # TR security master first: codes in bulletins resolve point in time
        with engine.begin() as c:
            res.watermark["tr_securities"] = members.sync(c, rows, date.today())
        for url in bulletin_urls(year):
            if url in done:
                continue
            b = parse_text(fetch_text(url))
            with engine.begin() as c:
                res.events += store(c, b, url, is_backfill=is_backfill)
            res.items += 1
        return res
