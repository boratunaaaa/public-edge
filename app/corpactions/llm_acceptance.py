"""Acceptance policy for LLM-extracted merger terms (from the 100-case stratified pilot, 2026-09-26).

The LLM is never authoritative. A field value is usable only as follows:
  exchange_ratio, cash_per_share  AUTO_ACCEPTED when the deterministic evidence gate passed (verbatim quote located in
                                  the SEC document, the value inside it). Pilot: 0 false acceptances out of 74.
  successor, effective_date       never auto-accepted (pilot false-acceptance 5-10 %): VALIDATED only with independent
                                  primary-source evidence - successor = the Form 425 filer / S-4 registrant named in
                                  the completion 8-K; effective date stated in the target's completion 8-K and within
                                  10 days of its Form 25 / completion 8-K - else PENDING_REVIEW.
  fund mergers                    BLOCKED (manual only): multi-fund prospectuses defeated both the LLM and the gate.
"""
from __future__ import annotations

from datetime import date

AUTO_FIELDS = ("exchange_ratio", "cash_per_share")
REVIEW_FIELDS = ("successor", "effective_date")
DATE_TOLERANCE_DAYS = 10


def decide(field: str, value, *, gate_passed: bool, is_fund: bool = False, primary: dict | None = None) -> str:
    """AUTO_ACCEPTED | VALIDATED | PENDING_REVIEW | REJECTED | BLOCKED | EMPTY"""
    if value is None:
        return "EMPTY"
    if is_fund:
        return "BLOCKED"
    if not gate_passed:
        return "REJECTED"
    if field in AUTO_FIELDS:
        return "AUTO_ACCEPTED"
    primary = primary or {}
    if field == "successor":
        ciks = set(primary.get("successor_ciks") or [])  # 425 filer / S-4 registrant CIKs named in the completion 8-K
        return "VALIDATED" if primary.get("value_cik") and primary["value_cik"] in ciks else "PENDING_REVIEW"
    if field == "effective_date":
        anchors = [a for a in (primary.get("anchors") or []) if a]  # Form 25 / completion 8-K dates
        stated = bool(primary.get("stated_in_completion_8k"))
        d = value if isinstance(value, date) else date.fromisoformat(str(value))
        near = any(abs((d - a).days) <= DATE_TOLERANCE_DAYS for a in anchors)
        return "VALIDATED" if (stated and near) else "PENDING_REVIEW"
    return "PENDING_REVIEW"
