"""Incremental event-return computation and historical statistics."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select, text
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


# ---------------- return queue ----------------
# Priority tiers (lower first). Inside a tier the order is a hash of the issuer: deterministic, grouped by issuer
# (price cache hits) and uncorrelated with CIK size / age - the priced subset is never the "low CIK" subset.
TIER_NEVER_ATTEMPTED, TIER_HORIZON_DUE, TIER_TEMPORARY, TIER_LONG_TERM = 1, 2, 3, 4
GAP_RECHECK_DAYS = 7  # a horizon that has elapsed but has no exit bar yet (vendor gap / lag)
VENDOR_RETRY_HOURS = 1
LANE_MAIN, LANE_VENDOR = "main", "vendor"
DUE_STATUSES = ("pending", "partial", "retry", "no_data")


def _due(now: datetime):
    """Main lane: returns_next_at when it is set, else the pre-0013 rule on returns_checked_at."""
    legacy = or_(
        events.c.returns_checked_at.is_(None),
        and_(events.c.returns_status.in_(["pending", "partial", "retry"]),
             events.c.returns_checked_at < now - timedelta(hours=RECHECK_HOURS)),
        # confirmed no-data is re-verified monthly (symbol changes, provider backfills, new SEC actions)
        and_(events.c.returns_status == "no_data", events.c.returns_checked_at < now - timedelta(days=NO_DATA_RECHECK_DAYS)),
    )
    return and_(events.c.returns_status.in_(DUE_STATUSES),
                case((events.c.returns_next_at.isnot(None), events.c.returns_next_at <= now), else_=legacy))


def queue_tier():
    """1 never attempted | 2 partial (its next horizon is due) | 3 temporary retry / young pending | 4 long-term."""
    return case((events.c.returns_checked_at.is_(None), TIER_NEVER_ATTEMPTED),
                (events.c.returns_status == "partial", TIER_HORIZON_DUE),
                (events.c.returns_status.in_(["retry", "pending"]), TIER_TEMPORARY),
                else_=TIER_LONG_TERM)


def queue_order():
    return (queue_tier(), func.md5(func.coalesce(events.c.issuer_cik, events.c.ticker, "")), events.c.id)


def next_eligible_at(entry: date, done: set[int], today: date, now: datetime) -> datetime | None:
    """When a partial event can gain its next horizon: the earliest missing horizon's end + 1 day (the exit bar is
    published the day after). A horizon that has already elapsed but is still missing is a data gap -> re-checked
    after GAP_RECHECK_DAYS. None = nothing left to compute."""
    missing = [entry + timedelta(days=h) for h in HORIZONS if h not in done]
    if not missing:
        return None
    cands = []
    future = [t for t in missing if t > today]
    if future:
        d = min(future) + timedelta(days=1)
        cands.append(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
    if any(t <= today for t in missing):
        cands.append(now + timedelta(days=GAP_RECHECK_DAYS))
    return min(cands)


def queue_select(now: datetime, *, lane: str = LANE_MAIN, backfill: bool | None = None, ciks: list[str] | None = None,
                 event_ids: list[int] | None = None, limit: int = 500):
    """The events the next batch prices, in queue order (see queue_order)."""
    if lane == LANE_VENDOR:
        due = and_(events.c.returns_status == "vendor_wait",
                   or_(events.c.returns_next_at.is_(None), events.c.returns_next_at <= now))
    else:
        due = _due(now)
    conds = [or_(events.c.ticker.isnot(None), events.c.issuer_cik.isnot(None)), events.c.superseded.is_(False),
             events.c.public_date <= now.date() - timedelta(days=2), due]
    if backfill is not None:
        conds.append(events.c.is_backfill.is_(backfill))
    if ciks:
        conds.append(events.c.issuer_cik.in_([c.zfill(10) for c in ciks]))
    if event_ids:
        conds.append(events.c.id.in_(event_ids))
    return (select(events.c.id, events.c.ticker, events.c.issuer_cik, events.c.public_date,
                   # V2: available_at is the only entry anchor (same as public_at / end-of-day for old rows)
                   func.coalesce(events.c.available_at, events.c.public_at))
            .where(and_(*conds)).order_by(*queue_order()).limit(limit))


def compute_pending(engine: Engine, limit: int = 500, time_budget_s: float = 600, backfill: bool | None = None,
                    ciks: list[str] | None = None, event_ids: list[int] | None = None, lane: str = LANE_MAIN) -> dict:
    """Compute/refresh returns for events whose horizons have (newly) elapsed.

    The price series is chosen by the security master (same CIK, never a reused symbol); an event
    that cannot be priced is recorded with WHY (missing_reason) instead of silently disappearing.
    backfill: None = all events, True/False = only historical / only live (separate processes).
    ciks / event_ids: only these issuers / events (targeted recompute after a new price source or an issuer
    re-attribution, never the whole universe).
    lane: "main" never calls the delisted-price vendor (quota 50/hour, 72 s spacing): an event that needs it moves to
    returns_status 'vendor_wait' and is priced by the "vendor" lane (compute_vendor_lane, its own thread), so normal
    events never wait behind the vendor's rate limit. Selection order: queue_order() (tiers, then issuer hash)."""
    from app.securitymaster.provider import VendorDeferred, VendorUnavailable, get_security_master
    t0 = time.time()
    today = date.today()
    now = datetime.now(timezone.utc)
    vendor_lane = lane == LANE_VENDOR
    with engine.connect() as conn:
        rows = conn.execute(queue_select(now, lane=lane, backfill=backfill, ciks=ciks, event_ids=event_ids, limit=limit)).all()
    if not rows:
        return {"processed": 0}
    try:
        with engine.begin() as conn:
            spy = _ensure_spy(conn)
    except PriceFetchError as e:
        return {"processed": 0, "error": f"benchmark unavailable: {e}"}
    done = nodata = retry = deferred = 0
    cache: dict[str, list] = {}
    otc_keys: set[str] = set()  # vendor series confirmed as OTC continuations: no cash-out at the delisting
    failed: dict[str, str] = {}
    sectors = SectorBench()
    stop = None
    for eid, ticker, cik, pdate, pat in rows:
        if time.time() - t0 > time_budget_s or stop:
            break
        with engine.begin() as conn:
            now = datetime.now(timezone.utc)
            sm = get_security_master(conn, allow_vendor=vendor_lane, skip_known_absent=True)
            sym, basis, missing = sm.price_symbol(cik, pdate, ticker)
            dl = sm.delisting(cik) if cik else None
            vendor = None
            if sym is None or basis == "historical_symbol_verified":
                # free/vendor recovery: an identity-gated series stored for THIS CIK (never by symbol alone)
                from app.pricesources.store import vendor_series
                vendor = vendor_series(conn, cik, pdate)
                if vendor is not None:
                    vbars, vbasis, vsym, votc = vendor
                    cache[f"{cik}|vendor"] = (vbars, vbasis, None)
                    if votc:
                        otc_keys.add(f"{cik}|vendor")
                    sym, basis = vsym, vbasis
            if sym is None:
                status = "no_data" if (today - pdate).days > 10 else "pending"
                nodata += status == "no_data"
                # a verified merger exit cannot price an event whose ENTRY has no predecessor price: kept apart from
                # price recovery, never filled from the successor
                why = "HISTORICAL_PRICE_STILL_MISSING" if _has_resolution(conn, cik) else _missing(missing, dl, pdate)
                conn.execute(events.update().where(events.c.id == eid).values(
                    returns_status=status, returns_checked_at=now, returns_error=None, price_symbol=None,
                    price_symbol_basis=None, missing_reason=why, returns_next_at=_recheck_at(status, now)))
                continue
            if sym in failed:
                conn.execute(events.update().where(events.c.id == eid).values(**_temporary(vendor_lane, failed[sym], now)))
                retry += 1
                continue
            key = f"{cik}|vendor" if vendor is not None else f"{cik}|{sym}"
            if key not in cache:
                cache.clear()  # rows are issuer-grouped; keep memory bounded
                try:
                    ps = sm.get_historical_price(cik, min(pdate, today) - timedelta(days=10), today, on=pdate, ticker_hint=ticker)
                    cache[key] = (ps.bars, ps.basis, ps.missing_reason)
                except VendorDeferred:
                    # needs the delisted-price vendor: handed to the vendor lane, never waited for here
                    conn.execute(events.update().where(events.c.id == eid).values(
                        returns_status="vendor_wait", returns_checked_at=now, returns_error="VENDOR_QUEUED",
                        returns_next_at=None))
                    deferred += 1
                    continue
                except PriceFetchError as e:
                    # transient: keep the event retryable, never mark it as "no data"
                    failed[sym] = str(e)[:500]
                    conn.execute(events.update().where(events.c.id == eid).values(**_temporary(vendor_lane, failed[sym], now)))
                    retry += 1
                    if isinstance(e, VendorUnavailable) and e.quota:
                        stop = "vendor quota"
                    continue
            all_bars, basis_used, series_missing = cache[key]
            bars = [b for b in all_bars if pdate - timedelta(days=10) <= b.date <= pdate + timedelta(days=400)]
            terminal = dl.date if (dl is not None and dl.date and basis_used in (
                "historical_symbol_verified", "vendor_trusted", "vendor_validated")) else None
            if terminal and basis_used.startswith("vendor") and all_bars and all_bars[-1].date < terminal - timedelta(days=15):
                terminal = None  # the source's coverage ended before the delisting: no cash-out at a stale price
            if key in otc_keys:
                terminal = None  # confirmed OTC continuation: the holding is valued on OTC quotes after the delisting
            try:
                cexit = _corporate_exit(conn, cik, cache, allow_vendor=vendor_lane)
            except VendorDeferred:
                conn.execute(events.update().where(events.c.id == eid).values(
                    returns_status="vendor_wait", returns_checked_at=now, returns_error="VENDOR_QUEUED", returns_next_at=None))
                deferred += 1
                continue
            res = compute_returns(bars, spy, pdate, pat, as_of=today, terminal_date=terminal, corporate_exit=cexit)
            if not res:
                status = "no_data" if (today - pdate).days > 10 else "pending"
                nodata += status == "no_data"
                conn.execute(events.update().where(events.c.id == eid).values(
                    returns_status=status, returns_checked_at=now, returns_error=None, price_symbol=sym,
                    price_symbol_basis=basis_used, returns_next_at=_recheck_at(status, now),
                    missing_reason=("HISTORICAL_PRICE_STILL_MISSING" if cexit is not None or _has_resolution(conn, cik)
                                    else _missing(series_missing or "NO_PRICE_DATA", dl, pdate)) if status == "no_data" else None))
                continue
            for r in res:
                vals = dict(entry_date=r.entry_date, entry_price=r.entry_price, exit_date=r.exit_date, exit_price=r.exit_price,
                            ret=r.ret, spy_ret=r.spy_ret, excess_ret=r.excess_ret, mae=r.mae, mfe=r.mfe, exit_basis=r.exit_basis)
                vals |= sectors.values(conn, cik, r.entry_date, r.exit_date, r.exit_basis, r.horizon_days, r.ret)
                conn.execute(pg_insert(event_returns).values(event_id=eid, horizon_days=r.horizon_days, **vals)
                             .on_conflict_do_update(index_elements=["event_id", "horizon_days"], set_=vals))
            status = "complete" if len(res) == len(HORIZONS) else "partial"
            nxt = None if status == "complete" else next_eligible_at(res[0].entry_date, {r.horizon_days for r in res}, today, now)
            conn.execute(events.update().where(events.c.id == eid).values(
                returns_status=status, returns_checked_at=now, returns_error=None, price_symbol=sym,
                price_symbol_basis=basis_used, missing_reason=None, returns_next_at=nxt))
            done += 1
    out = {"processed": done, "no_data": nodata, "retry": retry, "candidates": len(rows), "lane": lane}
    if deferred:
        out["vendor_queued"] = deferred
    if stop:
        out["stopped"] = stop
    return out


def _recheck_at(status: str, now: datetime) -> datetime:
    """pending (young event, prices may still arrive) -> RECHECK_HOURS; no_data -> monthly re-verification."""
    return now + (timedelta(days=NO_DATA_RECHECK_DAYS) if status == "no_data" else timedelta(hours=RECHECK_HOURS))


def _temporary(vendor_lane: bool, error: str, now: datetime) -> dict:
    """A temporary provider failure. Main lane: `retry` after RECHECK_HOURS. Vendor lane: the event stays in
    vendor_wait (it still needs the vendor) and is offered again after VENDOR_RETRY_HOURS."""
    if vendor_lane:
        return {"returns_status": "vendor_wait", "returns_checked_at": now, "returns_error": error,
                "returns_next_at": now + timedelta(hours=VENDOR_RETRY_HOURS)}
    return {"returns_status": "retry", "returns_checked_at": now, "returns_error": error,
            "returns_next_at": now + timedelta(hours=RECHECK_HOURS)}


def compute_vendor_lane(engine: Engine, limit: int = 60, time_budget_s: float = 3600) -> dict:
    """Delisted-vendor lane (own thread): the vendor's shared rate limiter / quota paces this lane only."""
    return compute_pending(engine, limit=limit, time_budget_s=time_budget_s, backfill=None, lane=LANE_VENDOR)


def _has_resolution(conn: Connection, cik: str | None) -> bool:
    from app.corpactions import resolution_for
    return resolution_for(conn, cik) is not None


def _corporate_exit(conn: Connection, cik: str | None, cache: dict, allow_vendor: bool = True):
    """Verified merger consideration for this issuer (CONFIRMED + SIMPLE), valued with the successor's real price."""
    from app.corpactions import exit_value, resolution_for, successor_window
    res = resolution_for(conn, cik)
    if res is None:
        return None
    key = f"cae|{res['id']}"
    if key not in cache:
        bars = []
        if float(res["share_ratio"] or 0) > 0:
            # the successor's price through the same identity path as every event (same CIK, verified symbol) -
            # never a bare ticker, which may belong to another security at the closing date
            from app.securitymaster.provider import VERIFIED_BASES, get_security_master
            lo, hi = successor_window(res)
            try:
                ps = get_security_master(conn, allow_vendor=allow_vendor).get_historical_price(
                    res["successor_cik"], lo, min(hi, date.today()), on=res["effective_date"])
                bars = ps.bars if ps.basis in VERIFIED_BASES else None
            except PriceFetchError:
                bars = None
            if not res["successor_cik"]:
                bars = None
        cache[key] = exit_value(res, bars) if bars is not None else None
    return cache[key]


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
                        returns_status="pending", returns_checked_at=None, price_symbol=None, price_symbol_basis=None,
                        returns_next_at=None))
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
