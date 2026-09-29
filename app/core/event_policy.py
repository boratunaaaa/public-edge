"""Two separate event eligibilities - never derive one from the other.

RETURN eligibility (may raw returns be computed / kept?) - blocked only when the event's IDENTITY is unsafe:
  * identity_confidence LOW or NONE (13F prefix-only / name-contradicted CUSIP, the 2026-09-28 V2 identity audit
    "TICKER_OWNED_BY_OTHER_ISSUER", any future issuer / security ambiguity), or
  * an explicit identity-review marker (payload.identity_review, missing_reason IDENTITY_REVIEW).
  A return priced for the wrong company is wrong data, so no lane (main, vendor, retry, partial) touches such an event.

VALIDATION eligibility (may the event count in statistics, placebo, research, promotion, paper?) - blocked by
  needs_review for ANY reason (identity, data quality, context, manual review). An identity-safe review event keeps
  its raw returns up to date but never enters a verdict path until the review flag is cleared.
"""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import and_, func, or_

from app.db.models import events

UNSAFE_IDENTITY_CONFIDENCE = ("LOW", "NONE")
IDENTITY_REVIEW_REASON = "IDENTITY_REVIEW"
IDENTITY_REVIEW_KEY = "identity_review"


def identity_unsafe_clause():
    """SQL: the event's identity is not safe enough to price (see module doc)."""
    return or_(func.coalesce(events.c.identity_confidence, "").in_(UNSAFE_IDENTITY_CONFIDENCE),
               events.c.payload.has_key(IDENTITY_REVIEW_KEY),
               func.coalesce(events.c.missing_reason, "") == IDENTITY_REVIEW_REASON)


def return_eligible_clause():
    """SQL: every return lane selects only these (plus its own due / lane conditions)."""
    return and_(events.c.superseded.is_(False), ~identity_unsafe_clause())


def is_identity_unsafe(ev: Mapping) -> bool:
    """Python twin of identity_unsafe_clause for a fetched row (identity_confidence, payload, missing_reason)."""
    payload = ev.get("payload") or {}
    return ((ev.get("identity_confidence") or "") in UNSAFE_IDENTITY_CONFIDENCE
            or (isinstance(payload, Mapping) and IDENTITY_REVIEW_KEY in payload)
            or (ev.get("missing_reason") or "") == IDENTITY_REVIEW_REASON)


def validation_excluded(ev: Mapping) -> bool:
    """Verdict paths (statistics, placebo, research, promotion, paper): any review flag excludes."""
    return bool(ev.get("needs_review")) or bool(ev.get("superseded"))
