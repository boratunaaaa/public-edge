"""Store a vendor series with provenance, run the identity gate, queue only affected events."""
from __future__ import annotations

import gzip
import hashlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import price_sources, vendor_prices
from app.marketdata.provider import Bar
from app.pricesources import ACCEPTED
from app.pricesources.gate import Verdict, classify

PROVIDER_RANK = {"finsaber": 0, "tiingo": 1}  # dataset with its own CIK column first


def load_bars(conn: Connection, source_id: int) -> list[Bar]:
    return [Bar(r.date, r.open, r.high, r.low, r.close, r.adj_close, r.volume) for r in conn.execute(
        select(vendor_prices).where(vendor_prices.c.source_id == source_id).order_by(vendor_prices.c.date))]


def other_series(conn: Connection, cik: str, provider: str) -> list[list[Bar]]:
    """Second opinions for cross-validation: other providers' series of the same CIK (any verdict except
    REJECTED) and our cached Yahoo series of the CIK's current ticker."""
    out = [load_bars(conn, sid) for (sid,) in conn.execute(select(price_sources.c.id).where(and_(
        price_sources.c.cik == cik, price_sources.c.provider != provider,
        price_sources.c.validation_status != "REJECTED"))).all()]
    from app.entities.registry import resolver
    cur = resolver.ticker_for_cik(cik)
    if cur:
        from app.marketdata.provider import load_bars as cached
        y = cached(conn, cur, date(2000, 1, 1), date.today())
        if y:
            out.append(y)
    return out


def save_source(conn: Connection, *, cik: str, symbol: str, provider: str, bars: list[Bar], source_ref: str,
                identity: str, raw: bytes | None = None, vendor_name: str | None = None,
                delisting_date: date | None = None, source_end: date | None = None,
                truncated_at_delisting: bool = False, extra_checks: dict | None = None,
                otc_continuation: dict | None = None) -> tuple[int, Verdict]:
    cik = cik.zfill(10)
    bars = sorted({b.date: b for b in bars}.values(), key=lambda b: b.date)
    if delisting_date is None:
        from app.securitymaster.provider import get_security_master
        dl = get_security_master(conn).delisting(cik)
        delisting_date = dl.date if dl else None
    verdict = classify(conn, cik, symbol, bars, identity=identity, vendor_name=vendor_name,
                       delisting_date=delisting_date, other_series=other_series(conn, cik, provider),
                       source_end=source_end, truncated_at_delisting=truncated_at_delisting,
                       otc_continuation=otc_continuation)
    verdict.checks.update(extra_checks or {})
    vals = dict(source_ref=source_ref, retrieved_at=datetime.now(timezone.utc),
                raw_sha256=hashlib.sha256(raw).hexdigest() if raw else None,
                raw_gz=gzip.compress(raw, 6) if raw else None, n_bars=len(bars),
                first_date=bars[0].date if bars else None, last_date=bars[-1].date if bars else None,
                identity_basis=identity, validation_status=verdict.status, confidence=verdict.confidence,
                reasons=verdict.reasons, checks=verdict.checks)
    sid = conn.execute(pg_insert(price_sources).values(cik=cik, symbol=symbol, provider=provider, **vals)
                       .on_conflict_do_update(index_elements=["cik", "symbol", "provider"], set_=vals)
                       .returning(price_sources.c.id)).scalar_one()
    conn.execute(vendor_prices.delete().where(vendor_prices.c.source_id == sid))
    rows = [{"source_id": sid, "date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
             "adj_close": b.adj_close, "volume": b.volume} for b in bars]
    for i in range(0, len(rows), 2000):
        conn.execute(vendor_prices.insert().values(rows[i:i + 2000]))
    if verdict.status in ACCEPTED:
        queue_recompute(conn, cik)
    return sid, verdict


def queue_recompute(conn: Connection, cik: str) -> int:
    """Only this company's unpriced events are re-queued (the returns job picks up 'retry')."""
    return conn.execute(text("""UPDATE events SET returns_status = 'retry', returns_checked_at = NULL
                                WHERE issuer_cik = :c AND returns_status = 'no_data' AND NOT superseded"""),
                        {"c": cik}).rowcount


def vendor_series(conn: Connection, cik: str | None, on: date) -> tuple[list[Bar], str, str, bool] | None:
    """Best ACCEPTED series of this CIK that covers `on` (entry within 10 days): (bars, basis, symbol, otc).
    otc=True: a confirmed OTC continuation - the holding continues on OTC quotes after the exchange delisting,
    so the caller must not cash out at the delisting (see app.backtest.jobs). The caller must not apply a
    delisting terminal exit beyond the series' last bar when the series was cut by its source either."""
    if not cik:
        return None
    rows = conn.execute(select(price_sources.c.id, price_sources.c.provider, price_sources.c.symbol,
                               price_sources.c.validation_status, price_sources.c.checks).where(and_(
        price_sources.c.cik == cik.zfill(10), price_sources.c.validation_status.in_(ACCEPTED),
        price_sources.c.first_date <= on + timedelta(days=5), price_sources.c.last_date >= on))).all()
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (r.validation_status != "TRUSTED", PROVIDER_RANK.get(r.provider, 9)))
    best = rows[0]
    basis = "vendor_trusted" if best.validation_status == "TRUSTED" else "vendor_validated"
    return load_bars(conn, best.id), basis, best.symbol, bool((best.checks or {}).get("otc_continuation"))
