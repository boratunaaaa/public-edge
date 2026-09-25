"""News sources. Tier 1 = official (SEC, company press releases on wires, government)."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import http
from app.db.models import watchlist
from app.db.store import filing_exists, upsert_filing
from app.entities.registry import resolver
from app.news.extract import clean_text
from app.news.lead import headline_and_lead, html_lines
from app.news.pipeline import RawItem, ingest

SPAC_RE = re.compile(r"\b(acquisition (corp|co|company|ltd|inc)|blank check|capital acquisition|\btrust\b|royalty trust|spac)\b", re.I)
from app.parsers.sec_common import index_url, parse_submission
from app.sources.base import RunResult, Source
from app.sources.sec import edgar

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}


def parse_rss(xml: str) -> list[dict]:
    out = []
    for it in re.findall(r"<item\b[^>]*>(.*?)</item>", xml or "", re.S):
        def g(tag):
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", it, re.S)
            if not m:
                return None
            v = m.group(1).strip()
            v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
            return v
        pub = None
        raw = g("pubDate") or g("dc:date")
        if raw:
            try:
                pub = parsedate_to_datetime(raw.replace(" UT", " +0000"))
            except (TypeError, ValueError):
                try:
                    pub = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    pub = None
        link = clean_text(g("link"))
        out.append({"title": clean_text(g("title")), "link": link, "guid": clean_text(g("guid")) or link,
                    "description": g("description") or "", "published_at": pub, "source": clean_text(g("source"))})
    return out


class RssNewsSource(Source):
    critical = False

    def __init__(self, name: str, urls: list[str], tier: int, interval: int = 300, english_only: bool = True):
        self.name = name
        self.urls = urls
        self.tier = tier
        self.interval_seconds = interval
        self.english_only = english_only

    def run_live(self, engine: Engine) -> RunResult:
        res = RunResult()
        with engine.connect() as conn:
            resolver.load(conn)
        errors = 0
        for url in self.urls:
            try:
                xml = http.get(url, headers=BROWSER_UA, retries=2).text
            except Exception as e:
                errors += 1
                log.warning("%s feed failed %s: %s", self.name, url, e)
                continue
            for r in parse_rss(xml):
                if self.english_only and re.search(r"/(fr|de|es|it|ja|zh|pt|nl)/\?|/(fr|de|es|it|ja|zh-CN|pt)/", r["link"] or ""):
                    continue
                with engine.begin() as conn:
                    res.events += ingest(conn, RawItem(self.name, self.tier, r["guid"], r["link"], r["title"], r["description"],
                                                       r["published_at"], publisher=r["source"] or None))
                res.items += 1
        if errors == len(self.urls):
            raise RuntimeError("all feeds failed")
        return res


PRN_FEEDS = ["https://www.prnewswire.com/rss/news-releases-list.rss",
             "https://www.prnewswire.com/rss/all-news-releases-list.rss"]
BW_FEEDS = ["https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==",  # technology
            "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWQ=="]
GOV_FEEDS = ["https://www.energy.gov/newsroom/rss.xml", "https://www.nasa.gov/news-release/feed/"]


class Sec8kNewsSource(Source):
    """8-K current reports: official company news with the exact SEC acceptance timestamp.
    Exhibit 99.x (press release) text is used for classification."""
    name = "news_sec_8k"
    interval_seconds = 120
    critical = True

    def run_live(self, engine: Engine) -> RunResult:
        with engine.connect() as conn:
            resolver.load(conn)
            entries = edgar.fetch_new("8-K", lambda a: filing_exists(conn, "sec8k", a), max_pages=3)
        res = RunResult()
        for e in entries:
            if e.form_type.upper() not in ("8-K", "8-K/A"):
                continue
            try:
                with engine.connect() as conn:
                    if filing_exists(conn, "sec8k", e.accession):
                        continue
                ticker = resolver.ticker_for_cik(e.cik)
                if not ticker:
                    with engine.begin() as conn:  # remember it so the feed walk stops here next time
                        upsert_filing(conn, source="sec8k", accession=e.accession, form_type=e.form_type,
                                      url=index_url(e.cik, e.accession), issuer_cik=e.cik.zfill(10))
                    continue
                text = edgar.fetch_full_text(e.cik, e.accession)
                with engine.begin() as conn:
                    res.events += process_8k(conn, text, e.cik, ticker)
                res.items += 1
            except Exception as ex:
                log.warning("8-K %s failed: %s", e.accession, ex)
        return res


ITEM_NAMES = {"1.01": "Material Definitive Agreement", "1.02": "Termination of Material Agreement", "2.01": "Acquisition/Disposition",
              "2.02": "Results of Operations", "2.03": "Direct Financial Obligation", "3.01": "Delisting Notice",
              "3.02": "Unregistered Equity Sale", "3.03": "Material Modification of Rights", "4.02": "Non-Reliance on Financials",
              "5.02": "Officer/Director Change", "5.03": "Charter/Bylaw Amendment", "7.01": "Regulation FD", "8.01": "Other Events"}


def process_8k(conn, text: str, cik: str, ticker: str, *, backfill: bool = False, universe_quality: str | None = None) -> list[int]:
    sub = parse_submission(text)
    header = text.split("</SEC-HEADER>")[0]
    items = re.findall(r"ITEM INFORMATION:\s*(.+)", header)
    url = index_url(cik, sub.accession)
    upsert_filing(conn, source="sec8k", accession=sub.accession, form_type=sub.form_type, url=url, issuer_cik=cik.zfill(10),
                  filed_date=sub.filed_date, accepted_at=sub.accepted_at)
    name = resolver.cik_to_name.get(cik.zfill(10), "") or ""
    if SPAC_RE.search(name):
        return []  # blank-check / trust vehicles: corporate actions are not operating-company news
    ex99 = next((body for t, _fn, body in sub.documents if t.upper().startswith("EX-99")), None)
    item_list = "; ".join(i.strip() for i in items)
    if ex99:
        head, lead = headline_and_lead(ex99)
    else:
        # no press release: the item names are the only reliable description
        head, lead = None, ""
    headline = head or f"8-K: {item_list[:250]}"
    snippet = lead  # classification sees only the headline + lead paragraph, never boilerplate
    if any("3.01" in i or i.strip().startswith(("Notice of Delisting", "Deficiency")) for i in items):
        snippet += " delisting notice"  # Item 3.01 is a risk fact regardless of wording
    # the whole press release is discovery evidence (customers, production, capacity ...)
    if ex99:
        from app.discovery.evidence import extract_from_text
        extract_from_text(conn, cik.zfill(10), " ".join(html_lines(ex99))[:60000], url=url, accession=sub.accession,
                          form=sub.form_type, filed=sub.filed_date, max_sentences=400)
    return ingest(conn, RawItem("sec_8k", 1, sub.accession, url, headline, snippet, sub.accepted_at,
                                publisher="SEC EDGAR", tickers=[ticker], ciks=[cik.zfill(10)], exact_time=True,
                                backfill=backfill, universe_quality=universe_quality))


class OpenFdaSource(Source):
    """FDA drug approvals (openFDA drugsfda). Only a date is published -> treated as after-close."""
    name = "news_openfda"
    interval_seconds = 6 * 3600
    critical = False
    slow = True
    URL = "https://api.fda.gov/drug/drugsfda.json"

    def run_live(self, engine: Engine) -> RunResult:
        res = RunResult()
        end = date.today()
        start = end - timedelta(days=10)
        q = f"submissions.submission_status_date:[{start:%Y%m%d} TO {end:%Y%m%d}]+AND+submissions.submission_status:AP"
        try:
            data = http.get(f"{self.URL}?search={q}&limit=100", retries=2).json()
        except http.HttpError as e:
            if e.status == 404:  # openFDA returns 404 for "no matches"
                return res
            raise
        with engine.connect() as conn:
            resolver.load(conn)
        for r in data.get("results", []):
            sponsor = r.get("sponsor_name") or ""
            ticker = resolver.ticker_for_name(sponsor)
            if not ticker:
                continue
            brand = ", ".join((p.get("brand_name") or "") for p in (r.get("products") or [])[:2])
            for s in r.get("submissions") or []:
                if s.get("submission_status") != "AP" or not s.get("submission_status_date"):
                    continue
                # original approvals and new-indication (efficacy) supplements only;
                # labeling / manufacturing / chemistry supplements are routine
                is_orig = s.get("submission_type") == "ORIG"
                is_eff = "efficacy" in (s.get("submission_class_code_description") or "").lower()
                if not (is_orig or is_eff):
                    continue
                d = datetime.strptime(s["submission_status_date"], "%Y%m%d").date()
                if d < start:
                    continue
                stype = f"{s.get('submission_type')} {s.get('submission_class_code_description') or ''}".strip()
                guid = f"{r.get('application_number')}:{s.get('submission_type')}{s.get('submission_number')}"
                # date-only publication: assume it became public after the close (conservative)
                pub = datetime.combine(d, time(20, 0), tzinfo=NY)
                with engine.begin() as conn:
                    res.events += ingest(conn, RawItem(
                        self.name, 1, guid, f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={(r.get('application_number') or '')[3:]}",
                        f"FDA approval: {brand or r.get('application_number')} ({sponsor}) - {stype}",
                        f"FDA approved {brand} {stype} for {sponsor}.", pub, publisher="openFDA",
                        tickers=[ticker], ciks=[resolver.ticker_to_cik.get(ticker)]))
                res.items += 1
        return res


class YahooWatchlistNewsSource(Source):
    """Per-ticker headlines for watchlist companies (aggregator = tier 4 unless the link is a known outlet)."""
    name = "news_yahoo_watchlist"
    interval_seconds = 2 * 3600
    critical = False
    slow = True
    URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US"
    TIER2 = ("reuters.com", "apnews.com", "ft.com", "wsj.com", "bloomberg.com", "cnbc.com", "barrons.com", "marketwatch.com")

    def run_live(self, engine: Engine) -> RunResult:
        res = RunResult()
        with engine.connect() as conn:
            resolver.load(conn)
            tickers = [r[0] for r in conn.execute(select(watchlist.c.ticker).where(
                watchlist.c.category.in_(["DISCOVERY", "HIGH_INTEREST", "MONITOR", "THESIS_WEAKENING"]),
                watchlist.c.ticker.isnot(None))).all()][:200]
        for t in tickers:
            try:
                xml = http.get(self.URL.format(t=t), headers=BROWSER_UA, retries=1).text
            except Exception as e:
                log.warning("yahoo rss %s: %s", t, e)
                continue
            for r in parse_rss(xml):
                tier = 2 if any(d in (r["link"] or "") for d in self.TIER2) else 4
                with engine.begin() as conn:
                    res.events += ingest(conn, RawItem(self.name, tier, r["guid"], r["link"], r["title"], clean_text(r["description"]),
                                                       r["published_at"], publisher=r["source"] or "Yahoo Finance",
                                                       tickers=[t], ciks=[resolver.ticker_to_cik.get(t)]))
                res.items += 1
        return res


def news_sources() -> list[Source]:
    return [
        Sec8kNewsSource(),
        RssNewsSource("news_prnewswire", PRN_FEEDS, tier=1, interval=180),
        RssNewsSource("news_businesswire", BW_FEEDS, tier=1, interval=300),
        RssNewsSource("news_gov", GOV_FEEDS, tier=1, interval=1800),
        OpenFdaSource(),
        YahooWatchlistNewsSource(),
    ]
