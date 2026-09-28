"""FINRA Reg SHO daily short-sale VOLUME and SEC fails-to-deliver.

Short volume is NOT short interest: it is the share of the day's (FINRA-reported, off-exchange + consolidated)
volume executed as short sales, dominated by market-maker hedging. It is kept in its own table and never mixed
with short positions (app short_interest).

available_at: the file's HTTP Last-Modified (FINRA posts the day's file ~17:00-18:00 ET; SEC posts a half-month FTD
file ~2 weeks after its last settlement date). Live runs use our first observation if that is earlier; a missing
Last-Modified falls back to the end-of-day rule of the publication date, never to the trade/settlement date."""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import http
from app.core import rawlake
from app.core.bulk import upsert_rows
from app.core.provenance import end_of_local_day
from app.marketdata.calendar import is_trading_day
from app.sources.base import RunResult, Source

log = logging.getLogger(__name__)
PARSER_VERSION = "short-data-v1"
FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
FTD_PAGE = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"
FTD_URL = "https://www.sec.gov/files/data/fails-deliver-data/{name}.zip"
FINRA_HEADER = ["Date", "Symbol", "ShortVolume", "ShortExemptVolume", "TotalVolume", "Market"]
FTD_HEADER = ["SETTLEMENT DATE", "CUSIP", "SYMBOL", "QUANTITY (FAILS)", "DESCRIPTION", "PRICE"]


class FormatChanged(RuntimeError):
    pass


def _last_modified(resp) -> datetime | None:
    lm = resp.headers.get("last-modified")
    try:
        return parsedate_to_datetime(lm).astimezone(timezone.utc) if lm else None
    except (TypeError, ValueError):
        return None


def parse_finra(txt: str) -> list[dict]:
    lines = [ln for ln in txt.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines or lines[0].split("|") != FINRA_HEADER:
        raise FormatChanged(f"FINRA short volume header changed: {lines[:1]}")
    out = []
    for ln in lines[1:]:
        p = ln.split("|")
        if len(p) != 6 or not re.fullmatch(r"\d{8}", p[0]):
            continue  # trailer / footer
        total = float(p[4])
        if total <= 0:
            continue
        out.append({"trade_date": datetime.strptime(p[0], "%Y%m%d").date(), "symbol": p[1].strip().upper()[:16],
                    "short_volume": round(float(p[2])), "short_exempt_volume": round(float(p[3] or 0)),
                    "total_volume": round(total)})
    return out


def parse_ftd(txt: str) -> list[dict]:
    lines = [ln for ln in txt.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines or [h.strip() for h in lines[0].split("|")] != FTD_HEADER:
        raise FormatChanged(f"SEC FTD header changed: {lines[:1]}")
    out = {}
    for ln in lines[1:]:
        p = ln.split("|")
        if len(p) < 6 or not re.fullmatch(r"\d{8}", p[0].strip()):
            continue
        try:
            qty = int(p[3])
        except ValueError:
            continue
        price = None
        try:
            price = float(p[5]) if p[5].strip() not in ("", ".") else None
        except ValueError:
            pass
        r = {"settlement_date": datetime.strptime(p[0].strip(), "%Y%m%d").date(), "cusip": p[1].strip()[:9],
             "symbol": (p[2].strip().upper() or None) and p[2].strip().upper()[:16], "quantity": qty, "price": price,
             "description": p[4].strip()[:200] or None}
        out[(r["settlement_date"], r["cusip"])] = r  # a (date, cusip) pair is unique in the file
    return list(out.values())


def ingest_finra_day(engine: Engine, d: date, *, first_seen: datetime | None = None, keep_raw: bool = False) -> dict:
    url = FINRA_URL.format(d=d)
    try:
        r = http.get(url, retries=2)
    except http.HttpError as e:
        if e.status in (403, 404):
            return {"date": str(d), "status": "NOT_PUBLISHED"}
        raise
    rows = parse_finra(r.text)
    lm = _last_modified(r)
    avail = min(x for x in (lm, first_seen) if x) if (lm or first_seen) else end_of_local_day(d)
    avail = max(avail, datetime.combine(d, datetime.min.time(), timezone.utc) + timedelta(hours=20))  # never before 16:00 ET
    with engine.begin() as c:
        if keep_raw:
            rawlake.store(c, country="US", source="finra_short_volume", doc_key=url.rsplit("/", 1)[-1], content=r.content,
                          url=url, content_type="text/plain", parser_version=PARSER_VERSION)
        n = upsert_rows(c, "short_volume_daily", rows, ("trade_date", "symbol"), {"available_at": avail})
    return {"date": str(d), "rows": len(rows), "new": n, "available_at": avail.isoformat()}


def ftd_files() -> list[str]:
    page = http.get(FTD_PAGE).text
    return sorted(set(re.findall(r"/files/data/fails-deliver-data/(cnsfails\d{6}[ab])\.zip", page)), reverse=True)


def ingest_ftd_file(engine: Engine, name: str, *, first_seen: datetime | None = None) -> dict:
    url = FTD_URL.format(name=name)
    r = http.get(url, retries=2)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        txt = z.read(z.namelist()[0]).decode("latin-1")
    rows = parse_ftd(txt)
    lm = _last_modified(r)
    last_settle = max((x["settlement_date"] for x in rows), default=None)
    avail = min(x for x in (lm, first_seen) if x) if (lm or first_seen) else None
    if avail is None:
        return {"file": name, "status": "NO_PUBLICATION_TIME"}  # never guess: a file without a time is not stored
    if last_settle and avail.date() <= last_settle:
        return {"file": name, "status": "PUBLICATION_BEFORE_DATA", "available_at": avail.isoformat()}
    with engine.begin() as c:
        rawlake.store(c, country="US", source="sec_ftd", doc_key=name, content=r.content, url=url,
                      content_type="application/zip", parser_version=PARSER_VERSION, retention="COLD")
        n = upsert_rows(c, "fails_to_deliver", rows, ("settlement_date", "cusip"), {"source_file": name, "available_at": avail})
    return {"file": name, "rows": len(rows), "new": n, "available_at": avail.isoformat()}


class FinraShortVolumeSource(Source):
    name = "finra_short_volume"
    interval_seconds = 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "US", "short_volume_daily", "OPEN", PARSER_VERSION
    stale_after = timedelta(days=4)

    def run_live(self, engine: Engine) -> RunResult:
        now = datetime.now(timezone.utc)
        with engine.connect() as c:
            have = {r[0] for r in c.execute(text("SELECT DISTINCT trade_date FROM short_volume_daily "
                                                  "WHERE trade_date >= CURRENT_DATE - 10"))}
        res = RunResult(watermark={})
        for k in range(7, -1, -1):
            d = (now - timedelta(days=k)).date()
            if d in have or not is_trading_day(d):
                continue
            out = ingest_finra_day(engine, d, first_seen=now)
            if out.get("rows"):
                res.items += 1
                res.watermark["last_trade_date"] = str(d)
                from app.jobs.v2_steps import after_finra
                after_finra(engine, [d], is_backfill=d < now.date() - timedelta(days=2))
        return res

    def run_backfill(self, engine: Engine, start: date = date(2024, 1, 2)) -> RunResult:
        with engine.connect() as c:
            have = {r[0] for r in c.execute(text("SELECT DISTINCT trade_date FROM short_volume_daily"))}
        res = RunResult()
        d = start
        from app.jobs.v2_steps import disk_ok
        while d < date.today():
            if is_trading_day(d) and d not in have:
                if not disk_ok():  # historical work pauses; live ingestion is unaffected
                    res.watermark = {"paused": "LOW_DISK", "at": str(d)}
                    break
                res.items += bool(ingest_finra_day(engine, d).get("rows"))
            d += timedelta(days=1)
        return res


class SecFtdSource(Source):
    name = "sec_ftd"
    interval_seconds = 6 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "US", "fails_to_deliver", "OPEN", PARSER_VERSION
    stale_after = timedelta(days=25)  # two files a month
    live_events = True

    def run_live(self, engine: Engine, backfill_since: str | None = None) -> RunResult:
        """Live: only the two newest published files (history belongs to the HISTORICAL_LOW backfill, which passes
        backfill_since). Events are live only for a file published within the last 21 days."""
        now = datetime.now(timezone.utc)
        with engine.connect() as c:
            done = {r[0] for r in c.execute(text("SELECT DISTINCT source_file FROM fails_to_deliver"))}
        files = ftd_files()
        if backfill_since is None:
            files = files[:2]
            backfill_since = ""
        res = RunResult(watermark={})
        for name in files:
            if name in done or name < backfill_since:
                continue
            out = ingest_ftd_file(engine, name, first_seen=now)
            res.items += 1
            res.watermark.setdefault("newest_file", name)
            log.info("ftd %s", out)
            fresh = out.get("available_at") and (now - datetime.fromisoformat(out["available_at"])).days <= 21
            if out.get("rows") and self.live_events and fresh:
                from app.jobs.v2_steps import after_ftd
                after_ftd(engine, [name], is_backfill=False)
        return res
