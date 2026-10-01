"""Two separate event eligibilities - never derive one from the other.

RETURN eligibility (may raw returns be computed / kept?) - blocked only when the event's IDENTITY is unsafe:
  * identity_confidence LOW or NONE (13F prefix-only / name-contradicted CUSIP, the 2026-09-28 V2 identity audit
    "TICKER_OWNED_BY_OTHER_ISSUER", any future issuer / security ambiguity), or
  * an explicit identity-review marker (payload.identity_review, missing_reason IDENTITY_REVIEW).
  A return priced for the wrong company is wrong data, so no lane (main, vendor, retry, partial) touches such an event.

VALIDATION eligibility (may the event count in statistics, placebo, research, promotion, paper?) - blocked by
  needs_review for ANY reason (identity, data quality, context, manual review). An identity-safe review event keeps
  its raw returns up to date but never enters a verdict path until the review flag is cleared.

PAPER SOURCE validity (may a proposal built on the event be created / approved, may its BUY orders fill?) - the
  event must be neither superseded, nor identity-unsafe, nor under review, checked AGAIN at every step under the
  event row lock (source_block_reason / source_blocked_clause): an event that becomes excluded after the proposal
  was created stops its pending proposal and unfilled BUY orders. SELL / exit orders of open positions are exempt.
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


SOURCE_SUPERSEDED, SOURCE_IDENTITY_UNSAFE, SOURCE_NEEDS_REVIEW, SOURCE_MISSING = (
    "SUPERSEDED", "IDENTITY_UNSAFE", "NEEDS_REVIEW", "MISSING")
# columns source_block_reason needs from an event row
SOURCE_COLUMNS = (events.c.superseded, events.c.needs_review, events.c.identity_confidence, events.c.payload,
                  events.c.missing_reason)


def source_block_reason(ev: Mapping | None) -> str | None:
    """Why a paper proposal / BUY order may no longer rest on this event (None = still valid). Most specific first."""
    if ev is None:
        return SOURCE_MISSING
    if ev.get("superseded"):
        return SOURCE_SUPERSEDED
    if is_identity_unsafe(ev):
        return SOURCE_IDENTITY_UNSAFE
    if ev.get("needs_review"):
        return SOURCE_NEEDS_REVIEW
    return None


def source_blocked_clause():
    """SQL twin of source_block_reason (for an existing row)."""
    return or_(events.c.superseded.is_(True), events.c.needs_review.is_(True), identity_unsafe_clause())
