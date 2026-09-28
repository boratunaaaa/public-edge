"""Configuration-driven country source registry (V2 #40). Adding AU / UK / JP = adding rows here + an adapter.

status: LIVE (running in production) | READY (adapter + real-sample tests, awaiting deploy) | FORWARD_ONLY |
        AUTH_REQUIRED | BLOCKED | LICENSE_REQUIRED | PLANNED."""
from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class SourceSpec:
    country: str
    dataset: str
    source: str
    official: bool
    historical: str
    live: str
    frequency: str
    publication_lag: str
    identity_quality: str
    point_in_time_quality: str
    cost: str
    license: str
    implemented: str
    signal_candidate: bool
    context_only: bool
    blocker: str
    priority: str
    requires_key: bool = False


S = SourceSpec
REGISTRY: tuple[SourceSpec, ...] = (
    # ---------------- US - existing production
    S("US", "Form 4 insider transactions", "SEC EDGAR", True, "2004+ (backfilled 5y)", "yes (~1 min)", "continuous", "minutes (acceptance time)", "HIGH (CIK)", "HIGH (acceptance timestamp)", "free", "OPEN", "LIVE", True, False, "", "P0"),
    S("US", "Schedule 13D/13G", "SEC EDGAR", True, "5y", "yes", "continuous", "minutes", "HIGH (issuer CIK; reporting persons repaired)", "HIGH", "free", "OPEN", "LIVE", False, True, "13D = context only (2026-09-27 decision)", "P0"),
    S("US", "Form 144", "SEC EDGAR", True, "recent", "yes", "continuous", "minutes", "HIGH", "HIGH", "free", "OPEN", "LIVE", False, True, "", "P1"),
    S("US", "10-K text changes", "SEC EDGAR", True, "5y (backfill paused on LOW_DISK)", "yes", "annual per issuer", "same day", "HIGH", "HIGH", "free", "OPEN", "LIVE", True, False, "10-K history stops when free disk < 10 GB", "P1"),
    S("US", "House PTR", "House Clerk", True, "5y", "yes", "daily", "up to 45 days after trade (disclosure date used)", "MEDIUM (name -> ticker)", "HIGH (disclosure date)", "free", "OPEN", "LIVE", True, False, "", "P1"),
    S("US", "Senate PTR", "efdsearch.senate.gov", True, "-", "no", "-", "-", "-", "-", "free", "RESTRICTED_AUTOMATION", "BLOCKED", False, False, "HTTP 403 to automated clients", "P2"),
    S("US", "Federal contracts (USAspending)", "USAspending API", True, "5y", "yes (6 h)", "daily", "DoD ~90 days after signing; first-seen used", "MEDIUM (recipient -> parent mapping)", "MEDIUM (first_seen; history estimated)", "free", "OPEN", "LIVE", True, False, "connection resets (now retried + checkpointed)", "P1"),
    S("US", "Official news (8-K, PR Newswire, Business Wire, openFDA, gov)", "SEC / newswires", True, "8-K 365d PIT", "yes", "continuous", "minutes", "HIGH (8-K CIK) / MEDIUM", "HIGH", "free", "PUBLIC_WITH_TERMS", "LIVE", True, False, "", "P1"),
    S("US", "Historical delisted prices", "Tiingo free / FinSABER / Yahoo", False, "partial", "-", "-", "-", "identity-gated per CIK", "n/a", "free (Tiingo 500 symbols/month)", "FREE_API", "LIVE", False, False, "Tiingo monthly symbol quota 475/500 used in 2026-09", "P0"),
    # ---------------- US - V2
    S("US", "13F institutional holdings", "SEC Form 13F data sets", True, "2013+ (quarterly zips); 9 quarters planned (disk)", "quarterly data sets", "quarterly", "45 days after quarter end (+ data-set release)", "MEDIUM (CUSIP -> CIK via 13D covers / SEC registry)", "HIGH (filing date)", "free", "OPEN", "READY", True, False, "per-manager rows only for focused managers (disk)", "P1"),
    S("US", "Short-sale volume (Reg SHO daily)", "FINRA CDN", True, "2009+ (2024+ planned, disk)", "daily ~17-18h ET", "daily", "same day", "MEDIUM (symbol + date)", "HIGH (Last-Modified)", "free", "OPEN", "READY", True, False, "", "P1"),
    S("US", "Short interest (positions)", "FINRA equity short interest", True, "-", "-", "twice monthly", "~8 business days", "MEDIUM", "HIGH", "free", "PUBLIC_WITH_TERMS", "PLANNED", True, False, "not yet implemented (FINRA API query form)", "P2"),
    S("US", "Fails-to-deliver", "SEC", True, "2004+ (2024+ planned)", "twice monthly", "half-monthly", "~2 weeks (Last-Modified)", "MEDIUM (symbol + date; CUSIP kept)", "HIGH", "free", "OPEN", "READY", True, False, "", "P1"),
    S("US", "Clinical trials", "ClinicalTrials.gov API v2", True, "current snapshot only (registration + results dates honest)", "yes (6 h)", "continuous", "posting date", "MEDIUM (lead sponsor exact name)", "HIGH for registration/results; status changes forward-only", "free", "PUBLIC_WITH_TERMS", "READY", True, False, "no version-history API: status/phase history not reconstructable", "P1"),
    S("US", "FDA approvals", "openFDA Drugs@FDA", True, "1939+ (2018+ planned)", "yes (12 h)", "continuous", "Drugs@FDA lag; action date used", "MEDIUM (sponsor exact name)", "MEDIUM (action date, end of day)", "free (1000 req/day without key)", "OPEN", "READY", True, False, "CRLs not reliably available", "P1"),
    S("US", "FDA recalls (enforcement)", "openFDA", True, "2004+", "-", "weekly", "report date", "LOW (firm name)", "HIGH", "free", "OPEN", "PLANNED", False, True, "parser present, not wired", "P2"),
    S("US", "Lobbying (LDA)", "lda.gov API", True, "1999+", "-", "quarterly", "20 days after quarter", "MEDIUM (client name)", "HIGH", "free (key optional)", "OPEN", "BLOCKED", False, True, "403 (Akamai) from both tested networks (office + Hetzner server)", "P2"),
    S("US", "Patents", "USPTO ODP / PatentsView", True, "1976+", "-", "weekly", "grant date", "MEDIUM (assignee)", "HIGH", "free (API key)", "OPEN", "AUTH_REQUIRED", False, True, "API key required", "P2", True),
    S("US", "Macro (FRED / ALFRED vintages)", "St. Louis Fed", True, "decades, vintages", "-", "daily-monthly", "series-specific", "n/a", "HIGH only with ALFRED vintages", "free (API key)", "OPEN", "AUTH_REQUIRED", False, True, "API key required", "P2", True),
    S("US", "CFTC Commitments of Traders", "CFTC Socrata", True, "1986+", "weekly", "weekly", "Tue data, Fri release", "n/a (sector)", "HIGH (Friday rule)", "free", "OPEN", "READY", False, True, "", "P2"),
    S("US", "Federal grants", "USAspending assistance", True, "5y", "-", "daily", "first-seen", "MEDIUM", "MEDIUM", "free", "OPEN", "PLANNED", True, False, "extension of the contracts adapter", "P2"),
    S("US", "Identifier enrichment", "OpenFIGI", False, "-", "on demand", "-", "-", "90 % agreement with SEC FTD symbols (200 CUSIP sample)", "n/a", "free (25 req/min)", "FREE_API", "PLANNED", False, False, "cross-check only, never an identity authority", "P2"),
    # ---------------- TR
    S("TR", "KAP disclosures (pages)", "kap.org.tr", True, "page archive by id", "partial", "continuous", "minutes", "HIGH (KAP codes)", "HIGH (Istanbul timestamp)", "free", "RESTRICTED_AUTOMATION", "BLOCKED", True, False, "list/JSON API behind WAF; sequential-id discovery fragile", "P2"),
    S("TR", "KAP data services (official API)", "MKK API Portal", True, "service-specific", "yes", "continuous", "minutes", "HIGH", "HIGH", "free (account + approval + API key)", "PUBLIC_WITH_TERMS", "AUTH_REQUIRED", True, False, "owner must register at apiportal.mkk.com.tr", "P2", True),
    S("TR", "BIST company list", "kap.org.tr/tr/bist-sirketler (public page)", True, "current", "daily", "daily", "-", "HIGH (MKK member OID)", "first-sight periods", "free", "PUBLIC_WITH_TERMS", "READY", False, False, "", "P2"),
    S("TR", "SPK bulletins", "spk.gov.tr (PDF)", True, "2000s+", "yes", "1-3 per week", "evening of bulletin date", "MEDIUM (codes / exact names)", "HIGH", "free", "PUBLIC_WITH_TERMS", "READY", True, True, "", "P2"),
    S("TR", "Pay alım satım amounts (insider)", "KAP form attachments", True, "-", "-", "-", "-", "-", "-", "free", "RESTRICTED_AUTOMATION", "BLOCKED", True, False, "values only in PDF attachments + WAF", "P2"),
    S("TR", "Borsa Istanbul EOD prices", "Borsa Istanbul Datastore / vendors", True, "full", "yes", "daily", "EOD", "HIGH", "HIGH", "paid", "PAID_REQUIRED", "LICENSE_REQUIRED", False, False, "Yahoo .IS used for research only (zero-volume holiday bars filtered)", "P2"),
    S("TR", "TCMB EVDS macro", "TCMB EVDS", True, "decades", "yes", "daily", "same day", "n/a", "HIGH", "free (API key)", "OPEN", "AUTH_REQUIRED", False, True, "API key required", "P3", True),
    S("TR", "Public procurement (EKAP/KİK)", "ekap.kik.gov.tr", True, "-", "-", "-", "-", "LOW (tenderer names)", "-", "free", "RESTRICTED_AUTOMATION", "BLOCKED", True, False, "search behind captcha / session; no bulk dataset found", "P3"),
    S("TR", "Investment incentives", "sanayi.gov.tr teşvik lists", True, "monthly lists", "-", "monthly", "~1 month", "LOW (company names)", "MEDIUM", "free", "PUBLIC_WITH_TERMS", "BLOCKED", True, False, "yatirimtesvik.sanayi.gov.tr did not answer from either tested network", "P3"),
    S("TR", "MKK/VAP foreign flow, REKS", "MKK", True, "-", "-", "weekly", "-", "n/a", "-", "partly paid", "PAID_REQUIRED", "PLANNED", False, True, "per-stock data is a licensed MKK product", "P3"),
    S("TR", "TÜRKPATENT / EPDK / MAPEG", "official portals", True, "-", "-", "-", "-", "LOW", "-", "free", "RESTRICTED_AUTOMATION", "PLANNED", False, True, "interactive search only", "P3"),
    # ---------------- CA
    S("CA", "TSX / TSXV directory", "tsx.com", True, "current", "daily", "daily", "-", "MEDIUM (symbol, first sight)", "first-sight periods", "free", "PUBLIC_WITH_TERMS", "READY", False, False, "robots crawl-delay 6 s honoured", "P3"),
    S("CA", "News releases (mining taxonomy)", "Canada NewsWire sitemap", False, "~2 days window", "yes", "continuous", "minutes", "MEDIUM (tickers in sitemap)", "HIGH", "free", "PUBLIC_WITH_TERMS", "READY", True, False, "", "P3"),
    S("CA", "Federal contracts", "CanadaBuys contract history", True, "2009+", "monthly refresh", "monthly", "publicationDate", "LOW-MEDIUM (supplier legal names; subsidiaries need reviewed edges)", "HIGH (publication date)", "free", "OPEN", "READY", True, False, "", "P3"),
    S("CA", "Grants & contributions", "open.canada.ca proactive disclosure", True, "2005+ (2.3 GB CSV)", "quarterly", "quarterly", "unknown per record", "LOW-MEDIUM (recipient names)", "LOW: no publication date -> forward-only", "free", "OPEN", "FORWARD_ONLY", False, True, "no per-record publication date", "P3"),
    S("CA", "Macro (Valet)", "Bank of Canada", True, "decades", "daily", "daily", "same day", "n/a", "HIGH", "free", "PUBLIC_WITH_TERMS", "READY", False, True, "", "P3"),
    S("CA", "Short positions / short sale stats", "CIRO", True, "-", "-", "twice monthly / daily", "-", "MEDIUM", "HIGH", "free", "PUBLIC_WITH_TERMS", "BLOCKED", True, False, "403 to automated clients from both tested networks (office + Hetzner server)", "P3"),
    S("CA", "Insider trades (SEDI)", "SEDI", True, "-", "-", "continuous", "5 days", "HIGH", "HIGH", "licensed data product", "RESTRICTED_AUTOMATION", "BLOCKED", True, False, "bot manager; needs authorised feed", "P3"),
    S("CA", "SEDAR+ filings (NI 43-101, MD&A)", "SEDAR+", True, "-", "-", "continuous", "-", "HIGH", "HIGH", "free to view", "RESTRICTED_AUTOMATION", "BLOCKED", True, False, "bot manager", "P3"),
    S("CA", "Federal lobbying", "lobbycanada.gc.ca open data", True, "1996+", "-", "monthly", "-", "LOW", "HIGH", "free", "OPEN", "BLOCKED", False, True, "403 from both tested networks (office + Hetzner server)", "P3"),
    S("CA", "Patents (CIPO)", "CIPO bulk", True, "-", "-", "weekly", "-", "LOW", "-", "free", "OPEN", "PLANNED", False, True, "", "P3"),
)


def to_csv() -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[f.name for f in fields(SourceSpec)])
    w.writeheader()
    for s in REGISTRY:
        w.writerow(asdict(s))
    return buf.getvalue()
