"""Free-tier quota ledger (per provider): unique symbols per calendar month + request rate windows."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from app.db.models import provider_requests, provider_usage

LIMITS = {  # Tiingo free ("Starter") tier as published: 500 unique symbols / month, 50 req / hour, 1000 req / day
    "tiingo": {"symbols_month": 500, "per_hour": 50, "per_day": 1000, "safety": 0.95},
    "alphavantage": {"symbols_month": None, "per_hour": None, "per_day": 25, "safety": 0.8},
}


def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def usage(conn: Connection, provider: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sym = conn.execute(select(func.count()).select_from(provider_usage).where(and_(
        provider_usage.c.provider == provider, provider_usage.c.month == month_key(now)))).scalar_one()
    def since(td):
        return conn.execute(select(func.count()).select_from(provider_requests).where(and_(
            provider_requests.c.provider == provider, provider_requests.c.at > now - td))).scalar_one()
    return {"symbols_month": sym, "per_hour": since(timedelta(hours=1)), "per_day": since(timedelta(days=1))}


def can_request(conn: Connection, provider: str, symbol: str | None = None, n: int = 1, now: datetime | None = None) -> tuple[bool, str]:
    lim = LIMITS[provider]
    u = usage(conn, provider, now)
    s = lim["safety"]
    for k in ("per_hour", "per_day"):
        if lim[k] and u[k] + n > int(lim[k] * s):
            return False, f"{k.upper()}_LIMIT"
    if symbol and lim["symbols_month"]:
        used = conn.execute(select(provider_usage.c.symbol).where(and_(
            provider_usage.c.provider == provider, provider_usage.c.month == month_key(now),
            provider_usage.c.symbol == symbol))).first()
        if not used and u["symbols_month"] + 1 > int(lim["symbols_month"] * s):
            return False, "MONTHLY_SYMBOL_LIMIT"
    return True, "OK"


def record(conn: Connection, provider: str, kind: str, status: int | None, symbol: str | None = None,
           now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    conn.execute(provider_requests.insert().values(provider=provider, at=now, kind=kind, status=status))
    if symbol:
        conn.execute(pg_insert(provider_usage).values(provider=provider, month=month_key(now), symbol=symbol, first_used_at=now)
                     .on_conflict_do_nothing(index_elements=["provider", "month", "symbol"]))


def summary(conn: Connection, provider: str) -> dict:
    lim = LIMITS[provider]
    u = usage(conn, provider)
    return {"unique_symbols_this_month": u["symbols_month"],
            "remaining_symbols_this_month": (lim["symbols_month"] - u["symbols_month"]) if lim["symbols_month"] else None,
            "requests_last_hour": u["per_hour"], "requests_last_day": u["per_day"]}
