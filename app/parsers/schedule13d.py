"""Schedule 13D / 13G parser (structured XML since Dec-2024, legacy HTML/text before)."""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date

from lxml import etree

from app.parsers.sec_common import Submission, primary_xml, strip_ns, to_date, to_float

# Item-4 purpose classes, most specific first. Deterministic keyword rules keep the
# classification auditable; each class records which phrases triggered it.
ITEM4_RULES: list[tuple[str, list[str]]] = [
    ("SALE_OF_COMPANY", [r"sale of the (issuer|company)", r"sell the (issuer|company)", r"take[- ]private", r"going[- ]private",
                         r"acquire (all|the remaining)", r"proposal to acquire", r"non-binding (proposal|indication)", r"tender offer"]),
    ("MERGER", [r"\bmerger\b", r"business combination", r"amalgamation"]),
    ("STRATEGIC_ALTERNATIVES", [r"strategic alternatives", r"strategic review", r"explore (a|the) (sale|strategic)"]),
    ("BOARD_SEAT", [r"nominat\w+ .{0,40}(director|board)", r"board (seat|representation)", r"appoint\w* .{0,30}to the board",
                    r"proxy contest", r"solicit\w* proxies", r"cooperation agreement"]),
    ("BOARD_CHANGE", [r"(change|reconstitut\w+|refresh\w*) .{0,20}board", r"remov\w+ .{0,30}director", r"management changes?"]),
    # Only a *request* by the filer counts - an issuer's own repurchase does not.
    ("BUYBACK_REQUEST", [r"(urg|request|recommend|call\w* (on|for)|propos|encourag|advocat|demand)\w* .{0,80}(repurchase|buy[- ]?back)"]),
    ("ASSET_SALE", [r"(sale|divest\w*|spin[- ]?off) of .{0,40}(assets|business|segment|division|subsidiar)", r"\bspin[- ]?off\b"]),
    ("CAPITAL_ALLOCATION", [r"capital allocation", r"special dividend", r"capital (structure|return)", r"return (of )?capital"]),
    ("MANAGEMENT_DISCUSSION", [r"(engage|discussions?|conversations?|dialogue) with (management|the board|members of)",
                               r"communicat\w+ with (management|the board)"]),
]
PASSIVE_HINTS = [r"investment purposes", r"no (present )?(plans|proposals)", r"passive"]
_ACTIVE_CLASSES = {c for c, _ in ITEM4_RULES}


def classify_item4(text: str | None) -> tuple[str, list[str]]:
    """Return (primary_class, all_matched_classes)."""
    if not text or not text.strip():
        return "PASSIVE_OR_UNCLEAR", []
    t = " ".join(text.lower().split())
    matched: list[str] = []
    for cls, pats in ITEM4_RULES:
        if any(re.search(p, t) for p in pats):
            matched.append(cls)
    if not matched:
        if any(re.search(p, t) for p in PASSIVE_HINTS):
            return "PASSIVE_OR_UNCLEAR", []
        # Text present but no rule hit: treat as unclear rather than inventing activism.
        return "PASSIVE_OR_UNCLEAR", []
    return matched[0], matched


def is_13d(form_type: str) -> bool:
    f = form_type.upper().replace("SCHEDULE", "SC").strip()
    return f.startswith("SC 13D")


def is_amendment(form_type: str) -> bool:
    return form_type.strip().upper().endswith("/A")


@dataclass
class ReportingPerson:
    name: str
    cik: str | None
    percent: float | None
    shares: float | None


@dataclass
class Schedule13:
    form_type: str
    issuer_cik: str | None
    issuer_name: str | None
    cusip: str | None
    event_date: date | None
    persons: list[ReportingPerson] = field(default_factory=list)
    item4_text: str | None = None

    @property
    def lead(self) -> ReportingPerson | None:
        return self.persons[0] if self.persons else None

    @property
    def max_percent(self) -> float | None:
        vals = [p.percent for p in self.persons if p.percent is not None]
        return max(vals) if vals else None

    @property
    def max_shares(self) -> float | None:
        vals = [p.shares for p in self.persons if p.shares is not None]
        return max(vals) if vals else None


def _t(el, path):
    x = el.find(path)
    return None if x is None else ((x.text or "").strip() or None)


def parse_schedule13_xml(xml: str, form_type: str) -> Schedule13:
    root = etree.fromstring(strip_ns(xml).encode("utf-8"), parser=etree.XMLParser(recover=True, huge_tree=True))
    fd = root.find(".//formData") if root.find(".//formData") is not None else root
    cover = fd.find(".//coverPageHeader")
    issuer_cik = None
    issuer_name = None
    cusip = None
    ev = None
    if cover is not None:
        issuer_cik = (_t(cover, ".//issuerCIK") or _t(cover, ".//issuerCik"))
        issuer_name = _t(cover, ".//issuerName")
        cusip = _t(cover, ".//issuerCusipNumber") or _t(cover, ".//issuerCUSIP")
        ev = to_date(_t(cover, "dateOfEvent") or _t(cover, ".//eventDateRequiresFilingThisStatement"))
    persons = []
    for rp in fd.iter("reportingPersonInfo"):
        persons.append(ReportingPerson(
            name=_t(rp, "reportingPersonName") or "",
            cik=(_t(rp, "reportingPersonCIK") or None),
            percent=to_float(_t(rp, "percentOfClass") or _t(rp, ".//classPercent")),
            shares=to_float(_t(rp, "aggregateAmountOwned") or _t(rp, ".//reportingPersonBeneficiallyOwnedAggregateNumberOfShares")),
        ))
    purpose = None
    item4 = fd.find(".//item4")
    if item4 is not None:
        purpose = " ".join("".join(item4.itertext()).split()) or None
    return Schedule13(
        form_type=form_type,
        issuer_cik=issuer_cik.zfill(10) if issuer_cik else None,
        issuer_name=issuer_name,
        cusip=(cusip or "").replace(" ", "")[:9] or None,
        event_date=ev,
        persons=persons,
        item4_text=purpose,
    )


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    return re.sub(r"[ \t\xa0]+", " ", s)


_PCT_RE = re.compile(r"percent\s+of\s+class\s+represented\s+by\s+amount\s+in\s+row\s*\(?\d+\)?\s*[:.]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%", re.I | re.S)
_ITEM4_RE = re.compile(r"item\s*4\.?\s*[-–—:.]?\s*purpose\s+of\s+(the\s+)?transaction\.?(.*?)(item\s*5\.?\s*[-–—:.]?\s*interest)", re.I | re.S)
_CUSIP_RE = re.compile(r"\b([0-9A-Z]{6}\s?[0-9A-Z]{2}\s?[0-9])\b\s*(?:\(CUSIP|\n?\s*\(?CUSIP)", re.I)


def parse_schedule13_legacy(text_or_html: str, form_type: str) -> Schedule13:
    txt = html_to_text(text_or_html) if "<" in text_or_html[:5000] else text_or_html
    pcts = [float(p) for p in _PCT_RE.findall(txt) if 0 < float(p) <= 100]
    m4 = _ITEM4_RE.search(txt)
    item4 = " ".join(m4.group(2).split())[:20000] if m4 else None
    cm = _CUSIP_RE.search(txt)
    persons = [ReportingPerson(name="", cik=None, percent=max(pcts) if pcts else None, shares=None)] if pcts else []
    return Schedule13(form_type=form_type, issuer_cik=None, issuer_name=None,
                      cusip=cm.group(1).replace(" ", "") if cm else None, event_date=None,
                      persons=persons, item4_text=item4)


def parse_schedule13(sub: Submission) -> Schedule13:
    xml = primary_xml(sub)
    if xml and ("<formData" in xml or "coverPageHeader" in xml):
        return parse_schedule13_xml(xml, sub.form_type)
    main = next((body for t, _fn, body in sub.documents if t.upper().startswith(("SC 13", "SCHEDULE 13"))), None)
    if main is None and sub.documents:
        main = sub.documents[0][2]
    return parse_schedule13_legacy(main or "", sub.form_type)


def parse_filer_names(header_text: str) -> dict[str, list[tuple[str, str]]]:
    """Extract SUBJECT COMPANY / FILED BY blocks from the SEC header: {role: [(name, cik)]}."""
    out: dict[str, list[tuple[str, str]]] = {"SUBJECT COMPANY": [], "FILED BY": [], "REPORTING-OWNER": [], "ISSUER": []}
    for m in re.finditer(r"^(SUBJECT COMPANY|FILED BY|REPORTING-OWNER|ISSUER):\s*$(.*?)(?=^\S[^\n]*:\s*$|\Z)", header_text, re.M | re.S):
        blk = m.group(2)
        n = re.search(r"COMPANY CONFORMED NAME:\s*(.+)", blk)
        c = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", blk)
        if n and c:
            out[m.group(1)].append((n.group(1).strip(), c.group(1).zfill(10)))
    return out
