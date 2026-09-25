"""Alpha Vantage free tier: LISTING_STATUS (delisted symbols + delisting dates) as a point-in-time
cross-check. Free keys do not serve delisted PRICE history, so AV never supplies prices here.
Without ALPHAVANTAGE_API_KEY this module does nothing. One request per month (raw CSV cached with hash)."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app import http
from app.db.models import provider_raw
from app.pricesources import quota

URL = "https://www.alphavantage.co/query"
REF = "alphavantage:LISTING_STATUS?state=delisted"


def configured() -> bool:
    return bool(os.environ.get("ALPHAVANTAGE_API_KEY", "").strip())


def parse(body: bytes) -> dict[str, dict]:
    out = {}
    for r in csv.DictReader(io.StringIO(body.decode("utf-8", "ignore"))):
        try:
            dd = date.fromisoformat(r["delistingDate"]) if r.get("delistingDate") not in (None, "", "null") else None
        except ValueError:
            dd = None
        if r.get("symbol"):
            out.setdefault(r["symbol"].upper(), []).append({"name": r.get("name"), "exchange": r.get("exchange"),
                                                            "ipo": r.get("ipoDate"), "delisted": dd})
    return out


def refresh(engine: Engine, max_age_days: int = 30) -> dict:
    if not configured():
        return {"skipped": "ALPHAVANTAGE_NOT_CONFIGURED"}
    with engine.connect() as conn:
        last = conn.execute(select(provider_raw.c.retrieved_at).where(provider_raw.c.request_ref == REF)
                            .order_by(provider_raw.c.retrieved_at.desc()).limit(1)).scalar()
    if last and last > datetime.now(timezone.utc) - timedelta(days=max_age_days):
        return {"cached": last.isoformat()}
    with engine.begin() as conn:
        ok, why = quota.can_request(conn, "alphavantage")
        if not ok:
            return {"skipped": why}
    try:
        r = http.get(URL, retries=1, params={"function": "LISTING_STATUS", "state": "delisted",
                                              "apikey": os.environ["ALPHAVANTAGE_API_KEY"].strip()})
        status, body = r.status_code, r.content
    except http.HttpError as e:
        status, body = e.status, b""
    with engine.begin() as conn:
        quota.record(conn, "alphavantage", "listing", status)
        if status == 200 and body.startswith(b"symbol"):
            conn.execute(provider_raw.insert().values(provider="alphavantage", request_ref=REF,
                                                      retrieved_at=datetime.now(timezone.utc),
                                                      sha256=hashlib.sha256(body).hexdigest(), body_gz=gzip.compress(body, 6)))
            return {"stored": True, "symbols": len(parse(body))}
    return {"stored": False, "http_status": status}


def delisted_map(engine: Engine) -> dict[str, list[dict]]:
    with engine.connect() as conn:
        b = conn.execute(select(provider_raw.c.body_gz).where(provider_raw.c.request_ref == REF)
                         .order_by(provider_raw.c.retrieved_at.desc()).limit(1)).scalar()
    return parse(gzip.decompress(b)) if b else {}
