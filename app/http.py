"""Polite HTTP client: per-host rate limit, retries with exponential backoff, SEC User-Agent."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from urllib.parse import urlparse

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# requests per second per host family
_HOST_RPS = {
    "sec.gov": settings.sec_max_rps,
    "api.usaspending.gov": 2.0,
    "disclosures-clerk.house.gov": 1.0,
    "query1.finance.yahoo.com": 2.0,
    "query2.finance.yahoo.com": 2.0,
    "api.telegram.org": 1.0,
    "api.tiingo.com": 0.013,  # free tier: 50 requests / hour
    "extapps2.oge.gov": 0.5,
    # global markets (development): polite rates, TSX robots.txt asks for a 6 s crawl delay
    "kap.org.tr": 0.5,
    "asx.com.au": 1.0,
    "asx.api.markitdigital.com": 1.0,
    "tsx.com": 1 / 6,
    "newswire.ca": 0.5,
    "globenewswire.com": 0.5,
    # V2 US / CA official datasets
    "finra.org": 2.0,
    "clinicaltrials.gov": 2.0,
    "api.fda.gov": 3.0,
    "lda.senate.gov": 1.0,
    "api.openfigi.com": 0.4,
    "publicreporting.cftc.gov": 2.0,
    "open.canada.ca": 1.0,
    "canadabuys.canada.ca": 1.0,
    "bankofcanada.ca": 1.0,
    "ciro.ca": 0.5,
    "fred.stlouisfed.org": 1.0,
}
_RETRY_STATUS = {429, 500, 502, 503, 504}
# host family -> shared cross-process budget (app.core.ratelimit), enabled with PE_SHARED_RATELIMIT=1
_SHARED_KEY = {
    "sec.gov": "sec", "api.tiingo.com": "tiingo", "api.usaspending.gov": "usaspending",
    "query1.finance.yahoo.com": "yahoo", "query2.finance.yahoo.com": "yahoo", "finra.org": "finra",
    "clinicaltrials.gov": "clinicaltrials", "api.fda.gov": "openfda", "lda.senate.gov": "lda",
    "api.openfigi.com": "openfigi", "publicreporting.cftc.gov": "cftc", "open.canada.ca": "canada_open",
    "canadabuys.canada.ca": "canada_open", "bankofcanada.ca": "boc_valet", "ciro.ca": "ciro",
}
_SHARED = os.environ.get("PE_SHARED_RATELIMIT") == "1"


class _Bucket:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


_buckets: dict[str, _Bucket] = {}
_buckets_lock = threading.Lock()


def _host_key(url: str) -> str:
    host = urlparse(url).hostname or ""
    for key in _HOST_RPS:
        if host == key or host.endswith("." + key):
            return key
    return host


def _bucket(url: str) -> _Bucket:
    key = _host_key(url)
    with _buckets_lock:
        if key not in _buckets:
            _buckets[key] = _Bucket(_HOST_RPS.get(key, 2.0))
        return _buckets[key]


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int | None, msg: str):
        super().__init__(f"{status} {msg} ({url})")
        self.status = status


_client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True)


def request(method: str, url: str, *, retries: int = 4, headers: dict | None = None, **kw) -> httpx.Response:
    hdrs = {"Accept-Encoding": "gzip, deflate"}
    if _host_key(url) == "sec.gov":
        hdrs["User-Agent"] = settings.sec_user_agent
    else:
        hdrs["User-Agent"] = "Mozilla/5.0 (compatible; PublicEdge/1.0)"
    if headers:
        hdrs.update(headers)
    last: Exception | None = None
    shared = _SHARED_KEY.get(_host_key(url)) if _SHARED else None
    for attempt in range(retries + 1):
        _bucket(url).wait()
        if shared:
            from app.core import ratelimit
            ratelimit.acquire(shared)  # raises QuotaExhausted -> the source records API_QUOTA, not "no data"
        try:
            r = _client.request(method, url, headers=hdrs, **kw)
            if r.status_code == 429 and shared:
                from app.core import ratelimit
                ratelimit.record_429(shared)
            if r.status_code in _RETRY_STATUS:
                raise HttpError(url, r.status_code, "retryable status")
            if r.status_code >= 400:
                raise HttpError(url, r.status_code, r.text[:200].replace("\n", " "))
            return r
        except (httpx.TransportError, HttpError) as e:
            last = e
            if isinstance(e, HttpError) and e.status not in _RETRY_STATUS:
                raise
            if attempt == retries:
                break
            sleep = min(60.0, (2 ** attempt) * 1.5) + random.uniform(0, 1)
            log.warning("http retry %s/%s %s: %s (sleep %.1fs)", attempt + 1, retries, url, e, sleep)
            time.sleep(sleep)
    assert last is not None
    raise last


def get(url: str, **kw) -> httpx.Response:
    return request("GET", url, **kw)


def post(url: str, **kw) -> httpx.Response:
    return request("POST", url, **kw)
