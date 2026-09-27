"""Canada: news-release adapter (Canada NewsWire news sitemap) + TSX/TSXV directory security master.

Access (checked 2026-09-26):
  * https://www.newswire.ca/sitemap-news.xml?page=N - Google-News sitemap (robots.txt allows it): URL, title,
    publication timestamp WITH offset, keywords and <news:stock_tickers> (e.g. "TorontoVE:DPF", "CNSX:ABC")
  * https://www.tsx.com/json/company-directory/search/{tsx,tsxv}/^* - listed issuers (robots crawl-delay 6 s)
  * GlobeNewswire /RssFeed/country/Canada - 20 latest items, company name only (no ticker)
BLOCKED: SEDI (insider reports) and SEDAR+ answer a Radware bot-manager page to automated clients; not bypassed.
Insider coverage for Canada is therefore NONE until an authorised feed exists (see sedi.py).
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.markets.schema import CanonicalEvent

SITEMAP_URL = "https://www.newswire.ca/sitemap-news.xml?page={page}"
TSX_DIR_URL = "https://www.tsx.com/json/company-directory/search/{board}/%5E*"
EXCHANGE_MAP = {"toronto": "XTSE", "tsx": "XTSE", "torontove": "XTSX", "tsxv": "XTSX", "tsx-v": "XTSX", "cnsx": "XCNQ", "cse": "XCNQ"}


@dataclass
class Release:
    url: str
    title: str
    published_at: datetime
    keywords: list[str]
    tickers: list[tuple[str, str]]  # (exchange MIC, symbol) - Canadian listings only
    other_tickers: list[str] = field(default_factory=list)
    release_id: str = ""


def parse_sitemap(xml: str) -> list[Release]:
    out = []
    for u in re.findall(r"<url>(.*?)</url>", xml, re.S):
        loc = re.search(r"<loc>(.*?)</loc>", u)
        title = re.search(r"<news:title>(.*?)</news:title>", u, re.S)
        pub = re.search(r"<news:publication_date>(.*?)</news:publication_date>", u)
        if not (loc and title and pub):
            continue
        kw = re.search(r"<news:keywords>(.*?)</news:keywords>", u, re.S)
        tk = re.search(r"<news:stock_tickers>(.*?)</news:stock_tickers>", u, re.S)
        ca, other = [], []
        for t in (tk.group(1).split(",") if tk else []):
            if ":" not in t:
                continue
            ex, sym = t.strip().split(":", 1)
            mic = EXCHANGE_MAP.get(ex.strip().lower())
            (ca.append((mic, sym.strip())) if mic else other.append(t.strip()))
        rid = re.search(r"-(\d{6,})\.html", loc.group(1))
        out.append(Release(url=loc.group(1), title=htmllib.unescape(htmllib.unescape(title.group(1))).strip(),
                           published_at=datetime.fromisoformat(pub.group(1)), release_id=rid.group(1) if rid else loc.group(1),
                           keywords=[k.strip() for k in htmllib.unescape(kw.group(1)).split(",") if k.strip()] if kw else [],
                           tickers=ca, other_tickers=other))
    return out


# ------------------------------------------------------------------ classification (critical minerals first)
MINERALS = r"uranium|rare earths?|lithium|copper|nickel|gold|silver|graphite|cobalt|vanadium|tungsten|antimony|zinc|niobium|scandium|helium"
RULES: list[tuple[str, re.Pattern]] = [
    ("MERGER", re.compile(r"\bmerger\b|arrangement agreement|plan of arrangement|amalgamation|business combination", re.I)),
    ("ACQUISITION", re.compile(r"\bacquisition\b|\bacquires?\b|to acquire", re.I)),
    ("PRIVATE_PLACEMENT", re.compile(r"private placement|non-brokered|brokered (offering|financing)|flow-through", re.I)),
    ("RIGHTS_ISSUE", re.compile(r"rights offering", re.I)),
    ("DILUTION", re.compile(r"shares[- ]for[- ]debt", re.I)),
    ("CAPITAL_RAISE", re.compile(r"bought deal|public offering|registered direct offering|at-the-market|\bATM\b program|financing|"
                                 r"unit offering|convertible debenture", re.I)),
    ("SHARE_BUYBACK", re.compile(r"normal course issuer bid|\bNCIB\b|substantial issuer bid|share repurchase", re.I)),
    ("RESOURCE_UPDATE", re.compile(r"mineral resource|resource estimate|\bNI 43-101\b|reserve estimate", re.I)),
    ("FEASIBILITY_STUDY", re.compile(r"feasibility study|\bPEA\b|preliminary economic assessment|\bPFS\b|\bDFS\b", re.I)),
    ("MINING_DISCOVERY", re.compile(r"drill(s|ing)?\b.{0,60}(g/t|%|intercept|metres|meters)|\bg/t\b|intersects? \d|"
                                    r"(discovery|high[- ]grade|assays?|mineralization|mineralisation)", re.I)),
    ("OFFTAKE_AGREEMENT", re.compile(r"offtake", re.I)),
    ("PERMIT", re.compile(r"permit|licen[cs]e (granted|approved)|environmental assessment approval", re.I)),
    ("PRODUCTION_START", re.compile(r"commences? (commercial )?production|first (gold|pour|production|concentrate)|production testing", re.I)),
    ("GOVERNMENT_SUPPORT", re.compile(r"(government|federal|provincial|critical minerals infrastructure fund|strategic innovation fund).{0,60}(funding|grant|investment|loan)", re.I)),
    ("STRATEGIC_PARTNERSHIP", re.compile(r"strategic (investment|partnership|alliance)|joint venture|memorandum of understanding|\bMOU\b", re.I)),
    ("GOVERNMENT_CONTRACT", re.compile(r"(department of national defence|government of canada|DND|federal government).{0,60}(contract|award)", re.I)),
    ("NEW_CONTRACT", re.compile(r"\bcontract\b|\bawarded\b|purchase order|supply agreement", re.I)),
    ("REGULATORY_APPROVAL", re.compile(r"health canada|\bFDA\b|approval (received|granted)|receives approval", re.I)),
    ("CLINICAL_RESULT", re.compile(r"clinical (trial|study|data|results)|phase (1|2|3|i{1,3})\b|topline", re.I)),
    ("MANAGEMENT_CHANGE", re.compile(r"appoints?|appointment|resign|steps down|new (ceo|cfo|president|chair)", re.I)),
    ("DIVIDEND", re.compile(r"(declares?|announces?|increases?)\b[^.]{0,60}\b(dividends?|distributions? (for|of|rate))\b|"
                            r"\b(monthly|quarterly|special|september|october|november|december) (dividend|distribution)", re.I)),
    ("EARNINGS", re.compile(r"(first|second|third|fourth|q[1-4]) quarter|results for|financial results|annual results", re.I)),
    ("GUIDANCE", re.compile(r"guidance|outlook", re.I)),
]
ADMIN = re.compile(r"record date and meeting date|annual general meeting|sustainability report|conference|webcast|to present at|"
                   r"to participate|investor day|notice of", re.I)


def classify(title: str, keywords: list[str] | None = None) -> tuple[str, dict] | None:
    if ADMIN.search(title):
        return None
    mining_ctx = re.search(MINERALS + r"|\bg/t\b|\bmine\b|\bproject\b|\bdrill", title, re.I)
    for fam, rx in RULES:
        if rx.search(title):
            if fam == "MINING_DISCOVERY" and not mining_ctx:
                continue  # "intersection", "discovery" outside a mining context is not a mineral discovery
            f = {}
            mins = sorted(set(m.lower() for m in re.findall(MINERALS, title, re.I)))
            if mins:
                f["minerals"] = mins
            if fam in ("PRIVATE_PLACEMENT", "CAPITAL_RAISE") and re.search(r"flow-through", title, re.I):
                f["flow_through"] = True
            if fam in ("PRIVATE_PLACEMENT", "CAPITAL_RAISE") and re.search(r"clos(es|ing|ed)", title, re.I):
                f["stage"] = "CLOSED"
            return fam, f
    return None


def to_canonical(r: Release) -> list[CanonicalEvent]:
    c = classify(r.title, r.keywords)
    if c is None or not r.tickers:
        return []  # only issuers with a Canadian listing
    fam, f = c
    mic, sym = r.tickers[0]
    return [CanonicalEvent(country="CA", exchange=mic, source="newswire_ca", source_id=r.release_id, family=fam, ticker=sym,
                           company_name=None, published_at=r.published_at, source_url=r.url, title=r.title, price_sensitive=None,
                           fields={**f, "all_tickers": [f"{m}:{s}" for m, s in r.tickers], "foreign_tickers": r.other_tickers},
                           raw={"title": r.title, "keywords": r.keywords}, confidence="MEDIUM", security_id=f"{mic}:{sym}")]


def parse_directory(payload: dict, board: str) -> list[dict]:
    mic = "XTSE" if board == "tsx" else "XTSX"
    return [{"ticker": r["symbol"], "company_name": r["name"], "exchange": mic, "country": "CA",
             "instruments": [i["symbol"] for i in r.get("instruments") or []]} for r in (payload or {}).get("results") or []]
