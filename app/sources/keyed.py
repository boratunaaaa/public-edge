"""Sources that need a (free) credential the owner creates: MKK API Portal (KAP data services), FRED/ALFRED, USPTO ODP.

Credential path: environment variables injected from the root-only secret file (/etc/public-edge/*.env, never the
repo). Without the variable the source is registered as AUTH_REQUIRED and never runs; with it, it activates on the next
worker start - config only, no code change. Keys are sent only in headers/params, never logged: errors carry the URL
without its query string."""
from __future__ import annotations

import os
from datetime import date, timedelta

from sqlalchemy.engine import Engine

from app.sources.base import PlannedSource, RunResult, Source

KEYS = {"mkk_kap": "MKK_API_KEY", "fred_alfred": "FRED_API_KEY", "uspto_patents": "USPTO_API_KEY"}


def has_key(name: str) -> bool:
    return bool(os.environ.get(KEYS[name], "").strip())


class FredAlfredSource(Source):
    """Point-in-time macro: ALFRED vintages (realtime_start = when that value was published). Regime context only."""
    name = "fred_alfred"
    interval_seconds = 12 * 3600
    critical = False
    slow = True
    country, dataset, license_status, parser_version = "US", "us_macro_alfred", "OPEN", "alfred-v1"
    stale_after = timedelta(days=4)
    SERIES = ("DGS10", "DGS2", "BAMLH0A0HYM2", "UNRATE", "CPIAUCSL", "INDPRO", "DTWEXBGS", "NFCI")
    URL = "https://api.stlouisfed.org/fred/series/observations"

    def run_live(self, engine: Engine) -> RunResult:
        from app import http
        from app.core import records
        from app.core.provenance import end_of_local_day
        key = os.environ["FRED_API_KEY"]
        n = 0
        start = (date.today() - timedelta(days=60)).isoformat()
        for sid in self.SERIES:
            obs = http.get(self.URL, params={"series_id": sid, "api_key": key, "file_type": "json", "realtime_start": start,
                                             "realtime_end": "9999-12-31", "observation_start": start}).json().get("observations", [])
            with engine.begin() as c:
                for o in obs:
                    if o.get("value") in (None, "."):
                        continue
                    vint = date.fromisoformat(o["realtime_start"])
                    n += records.put(c, country="US", dataset="us_macro_alfred", record_key=f"{sid}:{o['date']}",
                                     record_version=o["realtime_start"], as_of_date=date.fromisoformat(o["date"]),
                                     available_at=end_of_local_day(vint), payload={"series": sid, "date": o["date"],
                                     "value": float(o["value"]), "vintage": o["realtime_start"]}, parser_version=self.parser_version,
                                     license_status="OPEN", source_url=f"https://alfred.stlouisfed.org/series?seid={sid}") is not None
        return RunResult(items=n)


def keyed_sources() -> list[Source]:
    out: list[Source] = []
    out.append(FredAlfredSource() if has_key("fred_alfred") else
               PlannedSource("fred_alfred", "AUTH_REQUIRED: set FRED_API_KEY (free key) in the secret env file", "AUTH_REQUIRED"))
    # MKK / USPTO payload schemas are only visible after login: the adapters are wired once a key exists (the KAP parser
    # in app/sources/tr/kap.py is reused unchanged); until then they are reported honestly, never faked.
    out.append(PlannedSource("mkk_kap", "AUTH_REQUIRED: register at apiportal.mkk.com.tr, set MKK_API_KEY"
                             + (" (key present: adapter schema pending)" if has_key("mkk_kap") else ""), "AUTH_REQUIRED", "TR"))
    out.append(PlannedSource("uspto_patents", "AUTH_REQUIRED: set USPTO_API_KEY (USPTO ODP)"
                             + (" (key present: adapter pending)" if has_key("uspto_patents") else ""), "AUTH_REQUIRED"))
    return out
