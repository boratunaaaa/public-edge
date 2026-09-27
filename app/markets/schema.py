"""Common global event schema: every market adapter emits CanonicalEvent; the shared core (events table,
backtest, news, risk, dashboard) consumes it. The raw market-specific disclosure is never lost: it travels
in `raw` next to the normalized fields.

The mapping onto the existing core `events` row (no schema change needed for development):
  event_type  = "<COUNTRY>_<FAMILY>"   (e.g. "TR_INSIDER_BUY", "AU_CAPITAL_RAISE")  - US event types unchanged
  dedupe_key  = "<country>:<source>:<source_id>:<family>:<ticker>"
  issuer_cik  = NULL (non-US issuers have no CIK) ; ticker = local exchange code
  payload     = {"country", "exchange", "family", "security_id", "price_sensitive", "raw", "fields", ...}
A proposed migration (docs/proposed_migrations/0011_global_markets.py, NOT applied) adds first-class columns.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

FAMILIES = (
    "INSIDER_BUY", "INSIDER_SELL", "MAJOR_HOLDER_BUY", "MAJOR_HOLDER_SELL", "THRESHOLD_CROSSING",
    "NEW_CONTRACT", "GOVERNMENT_CONTRACT", "EXPORT_CONTRACT", "CAPACITY_EXPANSION", "NEW_FACTORY",
    "NEW_PRODUCTION_LINE", "MAJOR_CUSTOMER", "COMMERCIALIZATION", "STRATEGIC_PARTNERSHIP", "GOVERNMENT_SUPPORT",
    "MINING_DISCOVERY", "RESOURCE_UPDATE", "CLINICAL_RESULT", "REGULATORY_APPROVAL", "SHARE_BUYBACK",
    "CAPITAL_RAISE", "RIGHTS_ISSUE", "PRIVATE_PLACEMENT", "DILUTION", "MERGER", "ACQUISITION",
    "MANAGEMENT_CHANGE", "GUIDANCE", "EARNINGS", "TRADING_RESTRICTION", "OTHER_MATERIAL_DISCLOSURE",
    # market-structure families that exist in the adapters but carry no directional meaning
    "DIVIDEND", "PERIODIC_REPORT", "OFFTAKE_AGREEMENT", "PERMIT", "PRODUCTION_START", "FEASIBILITY_STUDY",
    "DIRECTOR_INTEREST_CHANGE",  # ASX headline level: direction only in the Appendix 3Y body
)
COUNTRIES = {"US": "XNYS", "TR": "XIST", "CA": "XTSE", "AU": "XASX"}


@dataclass
class CanonicalEvent:
    country: str  # TR | CA | AU (US stays on its native types)
    exchange: str  # XIST | XTSE | XTSX | XCNQ | XASX
    source: str  # kap | asx | globenewswire | ...
    source_id: str  # disclosure / announcement id at the source
    family: str
    ticker: str | None
    company_name: str | None
    published_at: datetime  # publication timestamp, timezone-aware (UTC normalised)
    source_url: str
    title: str
    price_sensitive: bool | None = None
    fields: dict = field(default_factory=dict)  # normalized, family-specific (amount, holder, pct, price, ...)
    raw: dict = field(default_factory=dict)  # market-specific raw event (never dropped)
    confidence: str = "MEDIUM"  # HIGH (structured form) | MEDIUM (headline/rule) | LOW
    security_id: str | None = None  # global security master id: "<exchange>:<local id>"

    def __post_init__(self):
        if self.family not in FAMILIES:
            raise ValueError(f"unknown family {self.family}")
        if self.country not in COUNTRIES:
            raise ValueError(f"unknown country {self.country}")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        self.published_at = self.published_at.astimezone(timezone.utc)

    @property
    def event_type(self) -> str:
        return f"{self.country}_{self.family}"

    @property
    def dedupe_key(self) -> str:
        # one document can cover several securities (e.g. an issuer's multi-ETF distribution notice): one event each
        return f"{self.country.lower()}:{self.source}:{self.source_id}:{self.family}:{self.ticker or '-'}"[:200]

    def core_kwargs(self, local_date: date) -> dict:
        """Arguments for app.db.store.create_event (the shared core)."""
        return dict(event_type=self.event_type, dedupe_key=self.dedupe_key, public_date=local_date, public_at=self.published_at,
                    title=self.title[:500], ticker=self.ticker, company_name=self.company_name, issuer_cik=None,
                    source_url=self.source_url, priority=1, date_quality="exact",
                    payload={"country": self.country, "exchange": self.exchange, "family": self.family, "source": self.source,
                             "source_id": self.source_id, "security_id": self.security_id, "price_sensitive": self.price_sensitive,
                             "confidence": self.confidence, "fields": self.fields, "raw": self.raw})

    def to_json(self) -> dict:
        d = asdict(self)
        d["published_at"] = self.published_at.isoformat()
        d["event_type"], d["dedupe_key"] = self.event_type, self.dedupe_key
        return d


def content_hash(*parts: str) -> str:
    """Duplicate-disclosure key: the same text re-published under another id is one event."""
    return hashlib.sha1("\x1f".join(p.strip().lower() for p in parts if p).encode()).hexdigest()
