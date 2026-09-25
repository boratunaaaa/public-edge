"""Delisting classification from SEC primary-source corporate actions (pure functions, testable).

Why it matters: a stock that disappears from the price feed is not a neutral observation.
  * performance delistings (bankruptcy, exchange compliance) are losses - Shumway (1997) documents
    strongly negative delisting returns that price databases silently drop;
  * merger/tender delistings usually end at the deal price, direction unknown without that price.
Dropping them all (what a live-ticker price feed does) biases a hit rate upwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

DELIST_FORMS = {"FORM25_NSE", "FORM25"}
MERGER_MARKERS = {"MERGER_PROXY", "TENDER_TARGET", "CHANGE_IN_CONTROL"}
PERFORMANCE_REASONS = {"BANKRUPTCY", "EXCHANGE_COMPLIANCE"}


@dataclass(frozen=True)
class Delisting:
    date: date | None
    reason: str  # BANKRUPTCY | MERGER | EXCHANGE_COMPLIANCE | VOLUNTARY | UNKNOWN | NOT_DELISTED
    confidence: str  # HIGH | MEDIUM | LOW
    source: str

    @property
    def is_performance(self) -> bool:
        return self.reason in PERFORMANCE_REASONS


def classify_delisting(actions: list[dict], *, currently_listed: bool, last_symbol_seen: date | None = None) -> Delisting:
    """actions: [{action, action_date, accession}] for one CIK.

    Delisting date = latest Form 25 / 25-NSE that is not followed by renewed listed activity.
    A company that is currently in the SEC exchange ticker file is ACTIVE regardless of old notices
    (a Form 25 for one class, a transfer between exchanges, ...)."""
    if currently_listed:
        return Delisting(None, "NOT_DELISTED", "HIGH", "sec_registry_current")
    acts = sorted(actions, key=lambda a: a["action_date"])
    notices = [a for a in acts if a["action"] in DELIST_FORMS]
    dereg = [a for a in acts if a["action"] == "FORM15"]
    if notices:
        n = notices[-1]
        d, conf, src = n["action_date"], "HIGH", f"{n['action']} {n.get('accession') or ''}".strip()
    elif dereg:
        n = dereg[-1]
        d, conf, src = n["action_date"], "MEDIUM", f"FORM15 {n.get('accession') or ''}".strip()
    elif last_symbol_seen is not None:
        return Delisting(last_symbol_seen, "UNKNOWN", "LOW", "last_filing_symbol_seen")
    else:
        return Delisting(None, "UNKNOWN", "LOW", "no_delisting_record")

    def near(kind: set[str], before_days: int, after_days: int = 30) -> dict | None:
        lo, hi = d - timedelta(days=before_days), d + timedelta(days=after_days)
        hits = [a for a in acts if a["action"] in kind and lo <= a["action_date"] <= hi]
        return hits[-1] if hits else None

    if near({"BANKRUPTCY"}, 730):
        return Delisting(d, "BANKRUPTCY", conf, src + " + 8-K 1.03")
    m = near(MERGER_MARKERS, 365)
    if m:
        return Delisting(d, "MERGER", conf, src + f" + {m['action']}")
    if any(a["action"] == "FORM25_NSE" for a in notices[-1:]) and near({"LISTING_DEFICIENCY"}, 540):
        return Delisting(d, "EXCHANGE_COMPLIANCE", conf, src + " + 8-K 3.01")
    if notices and notices[-1]["action"] == "FORM25":
        return Delisting(d, "VOLUNTARY", "MEDIUM" if conf == "HIGH" else conf, src)
    return Delisting(d, "UNKNOWN", "MEDIUM" if conf == "HIGH" else conf, src)
