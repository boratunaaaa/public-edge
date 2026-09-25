"""Incremental event-return computation and historical statistics."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app.backtest.engine import HORIZONS, compute_returns, summarize
from app.config import settings
from app.db.models import event_returns, events
from app.marketdata.provider import BENCHMARK, PriceFetchError, ensure_prices

log = logging.getLogger(__name__)
PRICE_HISTORY_START = date(2015, 1, 1)
RECHECK_HOURS = 20
NO_DATA_RECHECK_DAYS = 30


def _ensure_spy(conn: Connection) -> list:
    return ensure_prices(conn, BENCHMARK, PRICE_HISTORY_START, date.today())


def compute_pending(engine: Engine, limit: int = 500, time_budget_s: float = 600, backfill: bool | None = None) -> dict:
    """Compute/refresh returns for events whose horizons have (newly) elapsed.

    The price series is chosen by the security master (same CIK, never a reused symbol); an event
    that cannot be priced is recorded with WHY (missing_reason) instead of silently disappearing.
    backfill: None = all events, True/False = only historical / only live (separate processes)."""
    from app.securitymaster.provider import get_security_master
    t0 = time.time()
    today = date.today()
    now = datetime.now(timezone.utc)
    due = or_(
        and_(events.c.returns_status.in_(["pending", "partial", "retry"]),
             or_(events.c.returns_checked_at.is_(None), events.c.returns_checked_at < now - timedelta(hours=RECHECK_HOURS))),
        # confirmed no-data is re-verified monthly (symbol changes, provider backfills, new SEC actions)
        and_(events.c.returns_status == "no_data", events.c.returns_checked_at < now - timedelta(days=NO_DATA_RECHECK_DAYS)),
    )
    conds = [or_(events.c.ticker.isnot(None), events.c.issuer_cik.isnot(None)), events.c.superseded.is_(False),
             events.c.public_date <= today - timedelta(days=2), due]
    if backfill is not None:
        conds.append(events.c.is_backfill.is_(backfill))
    with engine.connect() as conn:
        rows = conn.execute(select(events.c.id, events.c.ticker, events.c.issuer_cik, events.c.public_date, events.c.public_at)
                            .where(and_(*conds)).order_by(events.c.issuer_cik, events.c.ticker, events.c.public_date)
                            .limit(limit)).all()
    if not rows:
        return {"processed": 0}
    try:
        with engine.begin() as conn:
            spy = _ensure_spy(conn)
    except PriceFetchError as e:
        return {"processed": 0, "error": f"benchmark unavailable: {e}"}
    done = nodata = retry = 0
    cache: dict[str, list] = {}
    failed: dict[str, str] = {}
    sectors = SectorBench()
    for eid, ticker, cik, pdate, pat in rows:
        if time.time() - t0 > time_budget_s:
            break
        with engine.begin() as conn:
            now = datetime.now(timezone.utc)
            sm = get_security_master(conn)
            sym, basis, missing = sm.price_symbol(cik, pdate, ticker)
            dl = sm.delisting(cik) if cik else None
            vendor = None
            if sym is None or basis == "historical_symbol_verified":
                # free/vendor recovery: an identity-gated series stored for THIS CIK (never by symbol alone)
                from app.pricesources.store import vendor_series
                vendor = vendor_series(conn, cik, pdate)
                if vendor is not None:
                    vbars, vbasis, vsym = vendor
                    cache[f"{cik}|vendor"] = (vbars, vbasis, None)
                    sym, basis = vsym, vbasis
            if sym is None:
                status = "no_data" if (today - pdate).days > 10 else "pending"
                nodata += status == "no_data"
                conn.execute(events.update().where(events.c.id == eid).values(
                    returns_status=status, returns_checked_at=now, returns_error=None, price_symbol=None,
                    price_symbol_basis=None, missing_reason=_missing(missing, dl, pdate)))
                continue
            if sym in failed:
                conn.execute(events.update().where(events.c.id == eid).values(
                    returns_status="retry", returns_checked_at=now, returns_error=failed[sym]))
                retry += 1
                continue
            key = f"{cik}|vendor" if vendor is not None else f"{cik}|{sym}"
            if key not in cache:
                cache.clear()  # rows are issuer-ordered; keep memory bounded
                try:
                    ps = sm.get_historical_price(cik, min(pdate, today) - timedelta(days=10), today, on=pdate, ticker_hint=ticker)
                    cache[key] = (ps.bars, ps.basis, ps.missing_reason)
                except PriceFetchError as e:
                    # transient: keep the event retryable, never mark it as "no data"
                    failed[sym] = str(e)[:500]
                    conn.execute(events.update().where(events.c.id == eid).values(
                        returns_status="retry", returns_checked_at=now, returns_error=failed[sym]))
                    retry += 1
                    continue
            all_bars, basis_used, series_missing = cache[key]
            bars = [b for b in all_bars if pdate - timedelta(days=10) <= b.date <= pdate + timedelta(days=400)]
            terminal = dl.date if (dl is not None and dl.date and basis_used in (
                "historical_symbol_verified", "vendor_trusted", "vendor_validated")) else None
            res = compute_returns(bars, spy, pdate, pat, as_of=today, terminal_date=terminal)
            if not res:
                status = "no_data" if (today - pdate).days > 10 else "pending"
                nodata += status == "no_data"
                conn.execute(events.update().where(events.c.id == eid).values(
                    returns_status=status, returns_checked_at=now, returns_error=None, price_symbol=sym,
                    price_symbol_basis=basis_used, missing_reason=_missing(series_missing or "NO_PRICE_DATA", dl, pdate)
                    if status == "no_data" else None))
                continue
            for r in res:
                vals = dict(entry_date=r.entry_date, entry_price=r.entry_price, exit_date=r.exit_date, exit_price=r.exit_price,
                            ret=r.ret, spy_ret=r.spy_ret, excess_ret=r.excess_ret, mae=r.mae, mfe=r.mfe, exit_basis=r.exit_basis)
                vals |= sectors.values(conn, cik, r.entry_date, r.exit_date, r.exit_basis, r.horizon_days, r.ret)
                conn.execute(pg_insert(event_returns).values(event_id=eid, horizon_days=r.horizon_days, **vals)
                             .on_conflict_do_update(index_elements=["event_id", "horizon_days"], set_=vals))
            status = "complete" if len(res) == len(HORIZONS) else "partial"
            conn.execute(events.update().where(events.c.id == eid).values(
                returns_status=status, returns_checked_at=now, returns_error=None, price_symbol=sym,
                price_symbol_basis=basis_used, missing_reason=None))
            done += 1
    return {"processed": done, "no_data": nodata, "retry": retry, "candidates": len(rows)}


class SectorBench:
    """Per-run cache: issuer sector -> SPDR sector ETF bars (fetched once per run)."""

    def __init__(self) -> None:
        self.sector: dict[str, str | None] = {}
        self.bars: dict[str, tuple[dict, list]] = {}

    def etf(self, conn: Connection, cik: str | None) -> str | None:
        from app.backtest.sector import etf_for_sector
        from app.db.models import security_master
        if not cik:
            return None
        if cik not in self.sector:
            self.sector[cik] = conn.execute(select(security_master.c.sector).where(security_master.c.cik == cik)).scalar()
        return etf_for_sector(self.sector[cik])

    def values(self, conn: Connection, cik: str | None, entry: date, exit_: date, basis: str, horizon: int, ret: float) -> dict:
        from app.backtest.sector import benchmark_return
        etf = self.etf(conn, cik)
        if not etf:
            return {"sector_etf": None, "sector_ret": None, "sector_excess": None}
        if etf not in self.bars:
            try:
                bars = ensure_prices(conn, etf, PRICE_HISTORY_START, date.today())
            except PriceFetchError:
                bars = []
            self.bars[etf] = ({b.date: b for b in bars}, bars)
        by_date, ordered = self.bars[etf]
        sret = benchmark_return(by_date, ordered, entry, exit_, basis, horizon)
        return {"sector_etf": etf, "sector_ret": sret, "sector_excess": (ret - sret) if sret is not None else None}


def fill_sector_benchmark(engine: Engine, batch: int = 20000) -> dict:
    """Sector-ETF benchmark for returns computed before the column existed (no stock refetch)."""
    from app.db.models import event_returns as er
    filled = skipped = 0
    with engine.connect() as conn:
        rows = conn.execute(select(er.c.event_id, er.c.horizon_days, er.c.entry_date, er.c.exit_date, er.c.exit_basis, er.c.ret,
                                   events.c.issuer_cik).join(events, events.c.id == er.c.event_id)
                            .where(er.c.sector_etf.is_(None)).order_by(events.c.issuer_cik).limit(batch)).all()
    if not rows:
        return {"filled": 0, "batch": 0}
    bench = SectorBench()
    with engine.begin() as conn:
        for eid, h, entry, exit_, basis, ret, cik in rows:
            v = bench.values(conn, cik, entry, exit_, basis, h, ret)
            if v["sector_etf"] is None:
                v["sector_etf"] = "-"  # sector unknown / no matching ETF: recorded, never retried in a loop
                skipped += 1
            else:
                filled += 1
            conn.execute(er.update().where(and_(er.c.event_id == eid, er.c.horizon_days == h)).values(**v))
    return {"filled": filled, "no_sector": skipped, "batch": len(rows)}


def _missing(reason: str | None, dl, pdate: date) -> str:
    """Most specific explanation for an unpriced event (security-master delisting first)."""
    if dl is not None and dl.date is not None and dl.date >= pdate:
        return {"BANKRUPTCY": "DELISTED_BANKRUPTCY", "EXCHANGE_COMPLIANCE": "DELISTED_COMPLIANCE",
                "MERGER": "DELISTED_MERGER"}.get(dl.reason, "DELISTED_OTHER")
    return reason or "NO_PRICE_DATA"


def revalidate_price_identity(engine: Engine, batch: int = 5000) -> dict:
    """Returns computed before the security master used the event's display ticker as the price key.
    Keep them only when the security master resolves the SAME symbol; otherwise drop and recompute."""
    from app.securitymaster.provider import get_security_master
    kept = recomputed = 0
    with engine.connect() as conn:
        rows = conn.execute(select(events.c.id, events.c.ticker, events.c.issuer_cik, events.c.public_date).where(and_(
            events.c.returns_status.in_(["complete", "partial"]), events.c.price_symbol.is_(None)))
            .order_by(events.c.id).limit(batch)).all()
    for i in range(0, len(rows), 500):
        with engine.begin() as conn:
            sm = get_security_master(conn)
            for eid, ticker, cik, pdate in rows[i:i + 500]:
                sym, basis, _missing_reason = sm.price_symbol(cik, pdate, ticker)
                if sym is not None and sym == ticker:
                    conn.execute(events.update().where(events.c.id == eid).values(price_symbol=sym, price_symbol_basis=basis))
                    kept += 1
                else:
                    conn.execute(event_returns.delete().where(event_returns.c.event_id == eid))
                    conn.execute(events.update().where(events.c.id == eid).values(
                        returns_status="pending", returns_checked_at=None, price_symbol=None, price_symbol_basis=None))
                    recomputed += 1
    return {"kept": kept, "sent_for_recompute": recomputed, "batch": len(rows)}


# ---------------- statistics ----------------

SUBGROUPS = {
    "all": "TRUE",
    "ceo": "(e.payload->>'is_ceo')::boolean",
    "cfo": "(e.payload->>'is_cfo')::boolean",
    "director": "(e.payload->>'is_director')::boolean AND NOT (e.payload->>'is_ceo')::boolean AND NOT (e.payload->>'is_cfo')::boolean",
    "gt_1m": "(e.payload->>'value')::float > 1000000",
    "cluster_3plus": "(e.payload->>'number_of_buyers')::int >= 3",
    "activism": "e.payload->>'item4_class' NOT IN ('PASSIVE_OR_UNCLEAR','MANAGEMENT_DISCUSSION')",
    "ratio_gt_10pct": "(e.payload->>'value_to_mcap')::float > 0.10",
}


def event_type_stats(conn: Connection, event_type: str, subgroup: str = "all", horizons=HORIZONS,
                     extra_where: str = "", params: dict | None = None) -> dict:
    cond = SUBGROUPS.get(subgroup, "TRUE")
    out = {}
    for h in horizons:
        # estimated public dates are never part of verified statistics
        q = text(f"""SELECT r.ret, r.excess_ret, r.mae, r.mfe FROM event_returns r JOIN events e ON e.id = r.event_id
                     WHERE e.event_type = :et AND r.horizon_days = :h AND e.date_quality <> 'estimated'
                       AND NOT e.superseded AND {cond} {extra_where}""")
        rows = [dict(x._mapping) for x in conn.execute(q, {"et": event_type, "h": h, **(params or {})})]
        out[h] = summarize(rows, settings.min_sample_size).as_dict()
    return out


def person_stats(conn: Connection, event_type: str, key: str, value: str, horizons=(30, 90)) -> dict:
    """Per insider (payload.owner_cik) or per politician (payload.politician) statistics."""
    if key not in ("owner_cik", "politician"):
        raise ValueError(key)
    return event_type_stats(conn, event_type, "all", horizons, extra_where=f"AND e.payload->>'{key}' = :pv", params={"pv": value})


def subgroup_for(event_type: str, payload: dict) -> str:
    if event_type == "INSIDER_OPEN_MARKET_BUY":
        if payload.get("is_ceo"):
            return "ceo"
        if payload.get("is_cfo"):
            return "cfo"
        if payload.get("is_director"):
            return "director"
    if event_type == "CLUSTER_INSIDER_BUY" and (payload.get("number_of_buyers") or 0) >= 3:
        return "cluster_3plus"
    if event_type == "ACTIVIST_13D" and payload.get("item4_class") not in ("PASSIVE_OR_UNCLEAR", "MANAGEMENT_DISCUSSION", None):
        return "activism"
    if event_type == "GOV_CONTRACT" and (payload.get("value_to_mcap") or 0) > 0.10:
        return "ratio_gt_10pct"
    return "all"


def stats_overview(conn: Connection) -> list[dict]:
    rows = conn.execute(text("""
        SELECT e.event_type || CASE WHEN e.date_quality = 'estimated' THEN ' (estimated date - unverified)' ELSE '' END event_type,
               r.horizon_days, count(*) n, avg(r.ret) mean, percentile_cont(0.5) WITHIN GROUP (ORDER BY r.ret) median,
               avg((r.ret > 0)::int)::float8 win_rate,
               (avg((r.excess_ret > 0)::int) FILTER (WHERE r.excess_ret IS NOT NULL))::float8 spy_beat,
               avg(r.excess_ret) mean_excess, stddev_samp(r.ret) std, avg(r.mae) mae, avg(r.mfe) mfe
        FROM event_returns r JOIN events e ON e.id = r.event_id
        WHERE NOT e.superseded
        GROUP BY 1, r.horizon_days ORDER BY 1, r.horizon_days""")).mappings().all()
    return [dict(r) | {"sufficient": r["n"] >= settings.min_sample_size} for r in rows]
