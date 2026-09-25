"""Country news-source registry for the shared news engine (official primary sources first).

status: VERIFIED (fetched and parsed on 2026-09-26) | PARTIAL | BLOCKED | CANDIDATE (not yet tested) | LICENSED (needs a contract)
"""
from __future__ import annotations

SOURCES = {
    "TR": [
        {"name": "KAP disclosure pages", "url": "https://www.kap.org.tr/tr/Bildirim/{id}", "tier": 1, "status": "PARTIAL",
         "note": "pages served; list API / robots.txt behind WAF (HTTP 666); licensed KAP data service for production"},
        {"name": "Borsa İstanbul duyuruları (via KAP DKB)", "url": "https://www.kap.org.tr", "tier": 1, "status": "PARTIAL",
         "note": "VBTS / devre kesici notices arrive as KAP DKB disclosures"},
        {"name": "SPK Bülteni", "url": "https://spk.gov.tr/spk-bultenleri", "tier": 1, "status": "CANDIDATE",
         "note": "weekly bulletin PDFs (capital increase approvals, trading bans); reachable, parser not built"},
        {"name": "MKK", "url": "https://www.mkk.com.tr/", "tier": 1, "status": "LICENSED", "note": "investor/holding statistics; no per-company feed"},
        {"name": "Company IR pages", "url": None, "tier": 2, "status": "CANDIDATE", "note": "per issuer; mirror of KAP"},
        {"name": "Anadolu Ajansı Ekonomi", "url": "https://www.aa.com.tr/tr/ekonomi", "tier": 3, "status": "CANDIDATE", "note": ""},
    ],
    "CA": [
        {"name": "Canada NewsWire news sitemap", "url": "https://www.newswire.ca/sitemap-news.xml", "tier": 2, "status": "VERIFIED",
         "note": "title, timestamp, keywords, stock tickers"},
        {"name": "GlobeNewswire Canada RSS", "url": "https://www.globenewswire.com/RssFeed/country/Canada", "tier": 2, "status": "VERIFIED",
         "note": "20 latest items, company name only"},
        {"name": "TSX / TSXV company directory", "url": "https://www.tsx.com/json/company-directory/search/tsx/%5E*", "tier": 1,
         "status": "VERIFIED", "note": "security master, crawl-delay 6 s"},
        {"name": "SEDAR+", "url": "https://www.sedarplus.ca", "tier": 1, "status": "BLOCKED", "note": "Radware bot manager"},
        {"name": "SEDI insider reports", "url": "https://www.sedi.ca", "tier": 1, "status": "BLOCKED", "note": "Radware bot manager"},
        {"name": "Newsfile", "url": "https://www.newsfilecorp.com", "tier": 2, "status": "CANDIDATE", "note": "RSS path not found"},
    ],
    "AU": [
        {"name": "ASX announcements (daily list)", "url": "https://www.asx.com.au/asx/v2/statistics/todayAnns.do", "tier": 1,
         "status": "VERIFIED", "note": "headline, price-sensitive flag, time; bodies behind ASX personal-use terms"},
        {"name": "ASX research API (markitdigital)", "url": "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/announcements",
         "tier": 1, "status": "VERIFIED", "note": "per company, announcementType"},
        {"name": "ASX listed companies CSV", "url": "https://www.asx.com.au/asx/research/ASXListedCompanies.csv", "tier": 1,
         "status": "VERIFIED", "note": "security master"},
        {"name": "Company IR pages", "url": None, "tier": 2, "status": "CANDIDATE", "note": "mirror of ASX releases"},
    ],
}


def official_first(country: str) -> list[dict]:
    return sorted(SOURCES.get(country, []), key=lambda s: s["tier"])
