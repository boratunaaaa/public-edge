"""Event provenance: the single rule for WHEN a strategy could first know an event (available_at),
license vocabulary and explicit data-gap reasons (never a bare NULL)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TZ_BY_COUNTRY = {"US": NY, "CA": ZoneInfo("America/Toronto"), "TR": ZoneInfo("Europe/Istanbul"),
                 "AU": ZoneInfo("Australia/Sydney")}

LICENSE_STATUSES = ("OPEN", "PUBLIC_WITH_TERMS", "FREE_API", "PAID_REQUIRED", "RESTRICTED_AUTOMATION", "UNKNOWN")
GAP_REASONS = ("SOURCE_UNAVAILABLE", "IDENTITY_UNRESOLVED", "NOT_TRADEABLE", "PRE_LISTING", "DELIST_PRICE_MISSING",
               "CORPORATE_ACTION_COMPLEX", "API_QUOTA", "LICENSE_BLOCK", "TRUE_NO_EVENT")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")


def end_of_local_day(d: date, country: str = "US") -> datetime:
    """Last second of `d` in the market's time zone: a date-only publication is treated as after the close."""
    tz = TZ_BY_COUNTRY.get(country, NY)
    return (datetime.combine(d + timedelta(days=1), time(0), tz) - timedelta(seconds=1)).astimezone(timezone.utc)


def available_at(*, public_date: date, public_at: datetime | None = None, date_quality: str | None = None,
                 is_backfill: bool = False, first_seen_at: datetime | None = None, country: str = "US") -> datetime:
    """First moment the information was knowable.

    exact timestamp -> that timestamp; live first-seen -> our observation time; otherwise (date only, backfilled
    first-seen, estimated) -> end of the public date in the market's time zone. transaction / document /
    announcement dates are never used: an event is only as early as its publication."""
    if public_at is not None and date_quality in (None, "exact"):
        return public_at.astimezone(timezone.utc) if public_at.tzinfo else public_at.replace(tzinfo=timezone.utc)
    if date_quality == "first_seen" and not is_backfill and first_seen_at is not None:
        return first_seen_at.astimezone(timezone.utc)
    return end_of_local_day(public_date, country)


def check_entry(entry_session_open: datetime, avail: datetime) -> None:
    """Backtest guard: an entry strictly before available_at is look-ahead."""
    if entry_session_open < avail:
        raise ValueError(f"look-ahead: entry {entry_session_open.isoformat()} before available_at {avail.isoformat()}")
