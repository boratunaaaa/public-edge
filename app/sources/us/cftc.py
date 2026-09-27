"""CFTC Commitments of Traders (legacy, futures only) - sector / regime context, never a company signal.

Point in time: positions as of Tuesday, released Friday 15:30 ET (holiday weeks: later). available_at = the Friday
of the report week end-of-day (New York) -> first usable session is the following Monday."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.engine import Engine

from app import http
from app.core import records
from app.core.provenance import end_of_local_day
from app.sources.base import RunResult, Source

PARSER_VERSION = "cftc-cot-legacy-v1"
API = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
# commodity_name -> sector tag (only commodities with listed-company exposure)
COMMODITIES = {"CRUDE OIL": "energy", "NATURAL GAS": "energy", "COPPER": "base_metals", "GOLD": "precious_metals",
               "SILVER": "precious_metals", "PLATINUM": "precious_metals", "CORN": "agriculture", "SOYBEANS": "agriculture",
               "WHEAT": "agriculture", "LUMBER": "materials"}


def parse_row(r: dict) -> dict | None:
    name = (r.get("commodity_name") or "").strip().upper()
    if name not in COMMODITIES:
        return None
    d = datetime.strptime(r["report_date_as_yyyy_mm_dd"][:10], "%Y-%m-%d").date()
    f = lambda k: float(r.get(k) or 0)  # noqa: E731
    oi = f("open_interest_all")
    net_spec = f("noncomm_positions_long_all") - f("noncomm_positions_short_all")
    net_comm = f("comm_positions_long_all") - f("comm_positions_short_all")
    return {"report_date": d, "market_code": r.get("cftc_contract_market_code"), "market": r.get("market_and_exchange_names"),
            "commodity": name, "sector": COMMODITIES[name], "open_interest": oi, "net_noncommercial": net_spec,
            "net_commercial": net_comm, "net_noncommercial_pct_oi": (net_spec / oi) if oi else None}


def available_at(report_date: date):
    return end_of_local_day(report_date + timedelta(days=(4 - report_date.weekday()) % 7 or 7))


def sync(engine: Engine, start: date, end: date) -> int:
    n, off = 0, 0
    while True:
        rows = http.get(API, params={"$where": f"report_date_as_yyyy_mm_dd between '{start}T00:00:00' and '{end}T00:00:00'",
                                     "$limit": 5000, "$offset": off, "$order": "report_date_as_yyyy_mm_dd"}).json()
        with engine.begin() as c:
            for r in rows:
                p = parse_row(r)
                if p:
                    n += records.put(c, country="US", dataset="us_cftc_cot", record_key=f"{p['market_code']}:{p['report_date']}",
                                     as_of_date=p["report_date"], available_at=available_at(p["report_date"]), payload=p,
                                     parser_version=PARSER_VERSION, license_status="OPEN", entity_name=p["market"],
                                     source_url="https://publicreporting.cftc.gov/d/6dca-aqww") is not None
        off += len(rows)
        if len(rows) < 5000:
            return n


class CftcCotSource(Source):
    name = "us_cftc_cot"
    interval_seconds = 12 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "US", "us_cftc_cot", "OPEN", PARSER_VERSION
    stale_after = timedelta(days=9)

    def run_live(self, engine: Engine) -> RunResult:
        return RunResult(items=sync(engine, date.today() - timedelta(days=21), date.today()))

    def run_backfill(self, engine: Engine, start: date = date(2015, 1, 1)) -> RunResult:
        return RunResult(items=sync(engine, start, date.today()))
