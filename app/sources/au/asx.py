"""ASX company announcements adapter (headline level).

Sources (checked 2026-09-26 from the production server, robots.txt allows them):
  * https://www.asx.com.au/asx/v2/statistics/{todayAnns,prevBusDayAnns}.do - every announcement of a day:
    code, local date/time, price-sensitive flag, headline, idsId, pages
  * https://asx.api.markitdigital.com/asx-research/1.0/companies/<code>/announcements - per-company recent
    announcements (UTC timestamp, announcementType, isPriceSensitive, documentKey)
  * https://www.asx.com.au/asx/research/ASXListedCompanies.csv - security master (name, code, GICS group)
ANNOUNCEMENT BODIES (PDF) sit behind an ASX terms page: "freely available for investors' private and personal
use; commercial use requires the express written authority of ASX". PUBLIC EDGE does not click through that
page automatically - body parsing (Appendix 3Y amounts, Form 603/604 percentages) is BLOCKED until the owner
decides (see GLOBAL_MARKET_EXPANSION_REPORT.md). Headline-level events never pretend to know a direction or an
amount that only the body contains.
"""
from __future__ import annotations

import csv
import html as htmllib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.markets.schema import CanonicalEvent

SYD = ZoneInfo("Australia/Sydney")
LIST_URL = "https://www.asx.com.au/asx/v2/statistics/{page}.do"
MARKIT_URL = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/announcements"
DOC_URL = "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId={ids}"
COMPANIES_URL = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"
LIST_HEADERS = ["ASX Code", "Date", "Price sens.", "Headline"]


@dataclass
class Announcement:
    code: str
    published_at: datetime  # UTC
    price_sensitive: bool
    headline: str
    ids_id: str
    ann_type: str | None = None  # markit announcementType
    pages: int | None = None
    source: str = "asx_list"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def list_headers(page_html: str) -> list[str]:
    t = re.search(r"<table>(.*?)</table>", page_html, re.S)
    return [_clean(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", t.group(1), re.S)] if t else []


def parse_list_page(page_html: str) -> list[Announcement]:
    """todayAnns / prevBusDayAnns table rows."""
    t = re.search(r"<table>(.*?)</table>", page_html, re.S)
    if not t:
        return []
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 4:
            continue
        code = _clean(cells[0])
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", cells[1])
        tm = re.search(r"(\d{1,2}):(\d{2})\s*([ap]m)", cells[1], re.I)
        if not (code and m and tm):
            continue
        hh = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "pm" else 0)
        local = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), hh, int(tm.group(2)), tzinfo=SYD)
        ids = re.search(r"idsId=(\d+)", cells[3])
        head = re.search(r"<a[^>]*>(.*?)<br", cells[3], re.S)
        pages = re.search(r"(\d+)\s*page", cells[3])
        out.append(Announcement(code=code, published_at=local.astimezone(timezone.utc),
                                price_sensitive="price sensitive" in cells[2].lower() or "pricesens" in cells[2] and "<img" in cells[2],
                                headline=_clean(head.group(1) if head else cells[3]), ids_id=ids.group(1) if ids else "",
                                pages=int(pages.group(1)) if pages else None))
    return out


def parse_markit(code: str, payload: dict) -> list[Announcement]:
    out = []
    for it in ((payload or {}).get("data") or {}).get("items") or []:
        try:
            ts = datetime.fromisoformat(it["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        dk = it.get("documentKey") or ""
        parts = dk.split("-")
        out.append(Announcement(code=code.upper(), published_at=ts, price_sensitive=bool(it.get("isPriceSensitive")),
                                headline=_clean(it.get("headline") or ""), ids_id=parts[1] if len(parts) > 1 else dk,
                                ann_type=it.get("announcementType"), source="asx_markit"))
    return out


def markit_fingerprint(payload: dict) -> list[str]:
    items = ((payload or {}).get("data") or {}).get("items") or [{}]
    return sorted(set((payload.get("data") or {}).keys())) + sorted(items[0].keys()) if payload else []


# ------------------------------------------------------------------ headline classification
AMOUNT = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(m|mn|million|b|bn|billion|k)\b", re.I)
ADMIN = re.compile(r"proxy form|notice of (annual )?general meeting|letter to shareholders|investor presentation|"
                   r"change of (registered )?address|company secretary|cleansing (notice|statement)|appendix 2a|"
                   r"notification regarding unquoted|application for quotation|pause in trading|corporate governance|"
                   r"constitution|esg|sustainability|webinar|conference call|results of meeting", re.I)
RULES: list[tuple[str, re.Pattern, dict]] = [
    ("DIRECTOR_INTEREST_CHANGE", re.compile(r"change of director'?s'? interest|appendix 3y", re.I), {"subtype": "3Y"}),
    ("DIRECTOR_INTEREST_CHANGE", re.compile(r"initial director'?s'? interest|appendix 3x", re.I), {"subtype": "3X"}),
    ("DIRECTOR_INTEREST_CHANGE", re.compile(r"final director'?s'? interest|appendix 3z", re.I), {"subtype": "3Z"}),
    ("THRESHOLD_CROSSING", re.compile(r"becoming a substantial holder", re.I), {"direction": "UP", "form": "603"}),
    ("THRESHOLD_CROSSING", re.compile(r"ceasing to be a substantial holder", re.I), {"direction": "DOWN", "form": "605"}),
    ("THRESHOLD_CROSSING", re.compile(r"change (in|of) substantial holding", re.I), {"direction": "UNKNOWN", "form": "604"}),
    ("TRADING_RESTRICTION", re.compile(r"trading halt|suspension from (official )?quotation|voluntary suspension", re.I), {}),
    ("MERGER", re.compile(r"scheme (implementation|booklet|meeting)|merger|bidder'?s statement|target'?s statement|takeover", re.I), {}),
    ("ACQUISITION", re.compile(r"\bacqui(re|res|red|sition)\b", re.I), {}),
    ("RIGHTS_ISSUE", re.compile(r"entitlement offer|rights issue|share purchase plan|\bSPP\b", re.I), {}),
    ("PRIVATE_PLACEMENT", re.compile(r"placement", re.I), {}),
    ("CAPITAL_RAISE", re.compile(r"capital rais|raises? \$|equity raising|proposed issue of securities|appendix 3b", re.I), {}),
    ("SHARE_BUYBACK", re.compile(r"buy-?back|appendix 3[cef]\b", re.I), {}),
    ("OFFTAKE_AGREEMENT", re.compile(r"offtake", re.I), {}),
    ("FEASIBILITY_STUDY", re.compile(r"(definitive|pre-?)?feasibility study|\bDFS\b|\bPFS\b|scoping study", re.I), {}),
    ("RESOURCE_UPDATE", re.compile(r"mineral resource|ore reserve|resource (estimate|upgrade|update)|\bMRE\b|\bJORC\b", re.I), {}),
    ("MINING_DISCOVERY", re.compile(r"drill(ing)? results?|assays?|intercepts?|high[- ]grade|discovery|mineralisation", re.I), {}),
    ("PRODUCTION_START", re.compile(r"first (production|shipment|ore|concentrate)|commercial production|production commence", re.I), {}),
    ("GOVERNMENT_SUPPORT", re.compile(r"(government|federal|state|export finance|critical minerals facility).{0,40}(grant|funding|loan)", re.I), {}),
    ("GOVERNMENT_CONTRACT", re.compile(r"(department of defence|government|defence|army|navy).{0,40}(contract|award|order)", re.I), {}),
    ("NEW_CONTRACT", re.compile(r"\bcontract\b|\bawarded\b|\bagreement signed\b|\border\b", re.I), {}),
    ("STRATEGIC_PARTNERSHIP", re.compile(r"strategic (partnership|alliance|investment)|joint venture|\bMOU\b|memorandum of understanding", re.I), {}),
    ("REGULATORY_APPROVAL", re.compile(r"\b(TGA|FDA|EMA)\b.{0,30}(approv|clearance)|approval (granted|received)", re.I), {}),
    ("PERMIT", re.compile(r"(mining lease|licen[cs]e|permit|environmental approval).{0,30}(grant|approv|issued)", re.I), {}),
    ("CLINICAL_RESULT", re.compile(r"clinical (trial|study|results?)|phase (1|2|3|i{1,3})\b|topline", re.I), {}),
    ("MANAGEMENT_CHANGE", re.compile(r"appointment|resignation|retirement|(new|interim) (ceo|cfo|chair|managing director)|director appointment", re.I), {}),
    ("GUIDANCE", re.compile(r"guidance|outlook|earnings update", re.I), {}),
    ("DIVIDEND", re.compile(r"dividend|distribution", re.I), {}),
    ("EARNINGS", re.compile(r"half[- ]year|full[- ]year|annual report|appendix 4[de]|financial (report|statements)|results? for", re.I), {}),
    ("PERIODIC_REPORT", re.compile(r"quarterly|appendix (4c|5b)|activities report|cash ?flow report", re.I), {}),
]


FOLLOW_UP = re.compile(r"cleansing (notice|statement)|appendix 4g|corporate governance statement", re.I)
NEGATED = re.compile(r"terminat|withdraw|cancel|lapse|not proceed", re.I)
SUBJECT_PREFIX = re.compile(r"^([A-Z0-9]{3,4}):\s*")


def classify(headline: str, price_sensitive: bool | None = None, ann_type: str | None = None) -> tuple[str, dict] | None:
    """(family, fields) or None for administrative notices. Headline evidence only (MEDIUM confidence)."""
    h = headline or ""
    if FOLLOW_UP.search(h):
        return None  # a follow-up filing of an event already counted (e.g. the cleansing notice after a placement)
    subj = SUBJECT_PREFIX.match(h)
    if subj:
        h = h[subj.end():]
    if NEGATED.search(h) and re.search(r"joint venture|agreement|partnership|offtake|contract|\bMOU\b|merger|scheme|acquisition", h, re.I):
        return ("OTHER_MATERIAL_DISCLOSURE", {"negated": True, **({"subject_code": subj.group(1)} if subj else {})}) \
            if price_sensitive else None
    for fam, rx, extra in RULES:
        if rx.search(h):
            fields = dict(extra)
            m = AMOUNT.search(h)
            if m and fam in ("CAPITAL_RAISE", "PRIVATE_PLACEMENT", "RIGHTS_ISSUE", "NEW_CONTRACT", "GOVERNMENT_CONTRACT",
                             "GOVERNMENT_SUPPORT", "ACQUISITION", "OFFTAKE_AGREEMENT"):
                mult = {"m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "k": 1e3}[m.group(2).lower()]
                fields["amount_aud"] = float(m.group(1)) * mult
            if fam == "THRESHOLD_CROSSING":
                # ASX lodges the same notice under both codes: "... from X" = filed by holder X about this company;
                # "... for Y" = filed under the HOLDER's code about company Y (the event belongs to Y)
                fr = re.search(r"\bfrom\s+(.+)$", h, re.I)
                fo = re.search(r"\bfor\s+([A-Z0-9]{3,4})\s*$", h)
                if fr:
                    fields["holder"] = fr.group(1).strip()
                elif fo:
                    fields["subject_code"] = fo.group(1)
                    fields["holder_is_announcer"] = True
            if subj:
                fields["subject_code"] = subj.group(1)
            if fam == "DIRECTOR_INTEREST_CHANGE":
                fields.setdefault("direction", "UNKNOWN")  # buy/sell is only in the Appendix 3Y body
            if fam == "CAPITAL_RAISE" and re.search(r"proposed issue of securities|appendix 3b", h, re.I):
                fields["subtype"] = "APPENDIX_3B"  # may be options / performance rights: not necessarily cash raised
            return fam, fields
    if ADMIN.search(h):
        return None
    if price_sensitive:
        return "OTHER_MATERIAL_DISCLOSURE", {}
    return None


def to_canonical(a: Announcement, company_name: str | None = None) -> CanonicalEvent | None:
    c = classify(a.headline, a.price_sensitive, a.ann_type)
    if c is None:
        return None
    fam, fields = c
    ticker = fields.get("subject_code") or a.code  # the company the notice is ABOUT
    if fields.get("holder_is_announcer"):
        fields["holder"] = a.code
    return CanonicalEvent(country="AU", exchange="XASX", source="asx", source_id=a.ids_id or f"{a.code}:{a.published_at.isoformat()}",
                          family=fam, ticker=ticker, company_name=company_name, published_at=a.published_at,
                          source_url=DOC_URL.format(ids=a.ids_id) if a.ids_id else MARKIT_URL.format(code=a.code.lower()),
                          title=f"{a.code}: {a.headline}", price_sensitive=a.price_sensitive, fields=fields,
                          raw={"headline": a.headline, "ann_type": a.ann_type, "pages": a.pages, "ids_id": a.ids_id, "feed": a.source},
                          confidence="LOW" if fields.get("subtype") == "APPENDIX_3B" else "MEDIUM", security_id=f"XASX:{ticker}")


def parse_companies_csv(text: str) -> list[dict]:
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Company name"))
    return [{"company_name": r["Company name"], "ticker": r["ASX code"], "sector": r["GICS industry group"],
             "exchange": "XASX", "country": "AU"} for r in csv.DictReader(io.StringIO("\n".join(lines[start:])))]


def list_date(page_html: str) -> date | None:
    m = re.search(r"published on\s+(\d{1,2} \w+ \d{4})", _clean(page_html))
    return datetime.strptime(m.group(1), "%d %B %Y").date() if m else None


def dedupe(events: list[CanonicalEvent]) -> list[CanonicalEvent]:
    """ASX lodges a substantial-holder notice / scheme document under BOTH codes; after subject resolution both
    copies carry the same dedupe key -> one event, fields merged (e.g. the holder name from the other copy)."""
    by: dict[str, CanonicalEvent] = {}
    for e in sorted(events, key=lambda x: x.published_at):
        k = e.dedupe_key
        if k in by:
            for f, v in e.fields.items():
                by[k].fields.setdefault(f, v)
            by[k].raw.setdefault("also_lodged_under", []).append(e.raw.get("headline"))
            continue
        by[k] = e
    return list(by.values())
