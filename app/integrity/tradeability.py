"""Events whose security was not tradeable on the event date (never exchange-traded structures such as
non-traded BDCs / interval funds, or events before the security's first listing).

They are FLAGGED, never deleted: payload.tradeability = NOT_TRADEABLE | PRE_LISTING (+ evidence), every change
is written to data_cleanup_audit, and the quality layer leaves them out of the denominator. This is not a
survivorship exclusion: on that date nobody could have bought the security.
"""
from __future__ import annotations

import json

from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from app.db.models import data_cleanup_audit, events

STATUSES = ("NOT_TRADEABLE", "PRE_LISTING")


def mark(conn: Connection, marks: list[tuple[int, str, dict]], run_id: str, dry_run: bool = True) -> dict:
    """marks: (event_id, status, evidence). Only live (not superseded) events without a computed return change."""
    out = {s: 0 for s in STATUSES} | {"skipped_has_return": 0, "skipped_missing": 0, "unchanged": 0}
    for eid, status, evidence in marks:
        if status not in STATUSES:
            raise ValueError(status)
        ev = conn.execute(select(events.c.id, events.c.payload, events.c.returns_status, events.c.superseded)
                          .where(events.c.id == eid)).mappings().first()
        if ev is None or ev["superseded"]:
            out["skipped_missing"] += 1
            continue
        if ev["returns_status"] in ("complete", "partial"):
            out["skipped_has_return"] += 1  # priced after all: evidence contradicts the flag
            continue
        before = (ev["payload"] or {}).get("tradeability")
        if before == status:
            out["unchanged"] += 1
            continue
        out[status] += 1
        if dry_run:
            continue
        conn.execute(text("""UPDATE events SET payload = COALESCE(payload, '{}'::jsonb)
                                 || jsonb_build_object('tradeability', CAST(:s AS text), 'tradeability_evidence', CAST(:e AS jsonb))
                             WHERE id = :i"""), {"s": status, "e": json.dumps(evidence), "i": eid})
        conn.execute(data_cleanup_audit.insert().values(
            run_id=run_id, action="FLAG_TRADEABILITY", table_name="events", row_id=eid, before={"tradeability": before},
            after={"tradeability": status, "evidence": evidence}, reason=status, dry_run=False))
    return out
