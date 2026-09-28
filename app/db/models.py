"""Database schema. Unique constraints make every ingestion idempotent."""
from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    LargeBinary, MetaData, Numeric, String, Table, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})


def _ts(name: str, **kw) -> Column:
    return Column(name, DateTime(timezone=True), **kw)


def _created() -> Column:
    return Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False)


companies = Table(
    "companies", metadata,
    Column("id", Integer, primary_key=True),
    Column("cik", String(10), nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("name_norm", Text, nullable=False, index=True),
    Column("shares_outstanding", BigInteger),
    Column("shares_outstanding_date", Date),
    _ts("shares_checked_at"),  # last time the point-in-time share facts were fetched
    _created(),
)

# Point-in-time cover-page share counts (dei:EntityCommonStockSharesOutstanding) with the
# filing that disclosed them, so a historical date only sees facts filed on/before it.
shares_outstanding_facts = Table(
    "shares_outstanding_facts", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False),
    Column("accession", String(25), nullable=False),
    Column("end_date", Date, nullable=False),
    Column("filed", Date, nullable=False),
    Column("form", String(16)),
    Column("val", BigInteger, nullable=False),
    UniqueConstraint("cik", "accession", "end_date", "val", name="uq_shares_facts_cik_acc_end_val"),
)
Index("ix_shares_facts_cik_filed", shares_outstanding_facts.c.cik, shares_outstanding_facts.c.filed)

tickers = Table(
    "tickers", metadata,
    Column("id", Integer, primary_key=True),
    Column("symbol", String(16), nullable=False, unique=True),
    Column("company_id", Integer, ForeignKey("companies.id"), nullable=False, index=True),
    Column("exchange", String(32)),
    Column("cusip", String(9), index=True),
    _created(),
)

persons = Table(
    "persons", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String(24), nullable=False),  # insider | politician | investor | trump_family
    Column("cik", String(10)),
    Column("name", Text, nullable=False),
    Column("name_norm", Text, nullable=False),
    _created(),
    UniqueConstraint("kind", "name_norm", "cik", name="uq_persons_kind_name_cik"),
)

filings = Table(
    "filings", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("source", String(32), nullable=False),  # sec | house | senate | usaspending
    Column("accession", String(64), nullable=False),
    Column("form_type", String(32), nullable=False),
    Column("issuer_cik", String(10), index=True),
    Column("filed_date", Date),
    _ts("accepted_at"),  # exact public timestamp when known (SEC acceptance datetime)
    Column("url", Text, nullable=False),
    Column("raw_gz", LargeBinary),  # compressed raw document (primary XML / PDF text)
    Column("raw_sha256", String(64)),
    Column("parse_status", String(16), nullable=False, server_default="pending"),
    Column("parse_error", Text),
    _created(),
    UniqueConstraint("source", "accession", name="uq_filings_source_accession"),
)
Index("ix_filings_form_filed", filings.c.form_type, filings.c.filed_date)

insider_transactions = Table(
    "insider_transactions", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("accession", String(25), nullable=False),
    Column("line_key", String(40), nullable=False),  # N<idx>/D<idx> or dataset SK
    Column("filing_id", BigInteger, ForeignKey("filings.id")),
    Column("issuer_cik", String(10), nullable=False, index=True),
    Column("ticker", String(16), index=True),
    Column("issuer_name", Text),
    Column("owner_cik", String(10), index=True),
    Column("owner_name", Text, nullable=False),
    Column("is_director", Boolean, nullable=False, server_default="false"),
    Column("is_officer", Boolean, nullable=False, server_default="false"),
    Column("is_ten_pct", Boolean, nullable=False, server_default="false"),
    Column("officer_title", Text),
    Column("is_ceo", Boolean, nullable=False, server_default="false"),
    Column("is_cfo", Boolean, nullable=False, server_default="false"),
    Column("security_title", Text),
    Column("is_derivative", Boolean, nullable=False, server_default="false"),
    Column("trans_date", Date),
    Column("trans_code", String(4)),
    Column("acq_disp", String(1)),
    Column("shares", Numeric(20, 4)),
    Column("price", Numeric(20, 6)),
    Column("value", Numeric(24, 2)),
    Column("shares_after", Numeric(20, 4)),
    Column("direct_indirect", String(1)),
    Column("nature_of_ownership", Text),
    # single = one reporting owner | footnote = joint filing, owner named in the row's ownership text |
    # joint = joint filing, not attributable to one owner (owner_cik NULL, never the "primary" owner)
    Column("owner_attribution", String(16)),
    Column("joint_owner_ciks", JSONB),
    Column("is_10b5_1", Boolean, nullable=False, server_default="false"),
    Column("category", String(32), nullable=False),
    Column("filed_date", Date, nullable=False),
    _ts("accepted_at"),
    Column("source", String(16), nullable=False),  # live | dataset
    Column("source_url", Text, nullable=False),
    _created(),
    UniqueConstraint("accession", "line_key", name="uq_insider_tx_accession_line"),
)
Index("ix_insider_tx_cat_filed", insider_transactions.c.category, insider_transactions.c.filed_date)
Index("ix_insider_tx_issuer_date", insider_transactions.c.issuer_cik, insider_transactions.c.trans_date)

congress_transactions = Table(
    "congress_transactions", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("chamber", String(8), nullable=False),
    Column("doc_id", String(32), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("politician", Text, nullable=False),
    Column("party", String(16)),  # data field only - NEVER used in scoring
    Column("state_district", String(8)),
    Column("ticker", String(16), index=True),
    Column("asset_name", Text, nullable=False),
    Column("asset_type", String(8)),
    Column("transaction_type", String(16), nullable=False),  # PURCHASE | SALE | SALE_PARTIAL | EXCHANGE
    Column("transaction_date", Date),
    Column("notification_date", Date),
    Column("filing_date", Date, nullable=False),
    Column("disclosure_delay_days", Integer),
    Column("amount_min", Numeric(16, 2)),
    Column("amount_max", Numeric(16, 2)),
    Column("amount_range", Text),
    Column("owner", String(16)),  # self | spouse | joint | dependent
    Column("source_url", Text, nullable=False),
    _created(),
    UniqueConstraint("chamber", "doc_id", "line_no", name="uq_congress_tx_doc_line"),
)

government_awards = Table(
    "government_awards", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("award_key", String(160), nullable=False, unique=True),  # USAspending generated_internal_id
    Column("award_id", String(80)),
    Column("recipient_name", Text, nullable=False),
    Column("recipient_uei", String(16)),
    Column("parent_name", Text),
    Column("amount", Numeric(20, 2), nullable=False),
    Column("award_date", Date),
    Column("start_date", Date),
    Column("end_date", Date),
    Column("agency", Text),
    Column("subagency", Text),
    Column("description", Text),
    Column("ticker", String(16), index=True),
    Column("company_id", Integer, ForeignKey("companies.id")),
    Column("market_cap", Numeric(22, 2)),
    Column("value_to_mcap", Float),
    Column("mcap_level", String(8)),
    Column("first_seen_date", Date),  # date this system first observed the record (documented)
    Column("public_date_basis", String(16)),  # first_seen | estimated
    Column("amount_basis", String(32)),  # amount_at_first_seen | current_total_at_backfill (25 chars)
    Column("recipient_cik", String(10)),
    # VERIFIED | HIGH_CONFIDENCE | MEDIUM | LOW - how the recipient was mapped to a listed parent
    Column("mapping_confidence", String(16)),
    Column("mapping_source", Text),
    Column("source_url", Text, nullable=False),
    _created(),
)

activist_filings = Table(
    "activist_filings", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("accession", String(25), nullable=False, unique=True),
    Column("filing_id", BigInteger, ForeignKey("filings.id")),
    Column("form_type", String(24), nullable=False),  # SCHEDULE 13D | SCHEDULE 13D/A | SC 13D ...
    Column("is_13d", Boolean, nullable=False),
    Column("is_amendment", Boolean, nullable=False),
    Column("issuer_cik", String(10), index=True),
    Column("issuer_name", Text),
    Column("ticker", String(16), index=True),
    Column("cusip", String(9)),
    Column("investor_name", Text),
    Column("investor_cik", String(10)),
    Column("percent", Float),
    Column("shares", Numeric(20, 2)),
    Column("prev_percent", Float),
    Column("percent_change", Float),
    Column("event_date", Date),
    Column("filed_date", Date, nullable=False),
    _ts("accepted_at"),
    Column("item4_text", Text),
    Column("item4_class", String(32)),
    Column("item4_labels", JSONB),
    Column("source_url", Text, nullable=False),
    _created(),
)

form144_filings = Table(
    "form144_filings", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("accession", String(25), nullable=False, unique=True),
    Column("filing_id", BigInteger, ForeignKey("filings.id")),
    Column("issuer_cik", String(10), index=True),
    Column("issuer_name", Text),
    Column("ticker", String(16), index=True),
    Column("insider_name", Text),
    Column("relationship", Text),
    Column("shares", Numeric(20, 2)),
    Column("est_value", Numeric(22, 2)),
    Column("shares_outstanding", Numeric(22, 2)),
    Column("broker", Text),
    Column("approx_sale_date", Date),
    Column("prior_sales_3m", JSONB),
    Column("filed_date", Date, nullable=False),
    _ts("accepted_at"),
    Column("source_url", Text, nullable=False),
    _created(),
)

trump_family_events = Table(
    "trump_family_events", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("person", Text, nullable=False),
    Column("accession", String(25), nullable=False),
    Column("form_type", String(32), nullable=False),
    Column("company", Text),
    Column("issuer_cik", String(10)),
    Column("ticker", String(16)),
    Column("filed_date", Date, nullable=False),
    Column("ownership", Text),
    Column("details", JSONB),
    Column("source_url", Text, nullable=False),
    _created(),
    UniqueConstraint("person", "accession", name="uq_trump_family_events_person_acc"),
)

market_prices = Table(
    "market_prices", metadata,
    Column("symbol", String(16), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("adj_close", Float),
    Column("volume", BigInteger),
)

events = Table(
    "events", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("event_type", String(40), nullable=False),
    Column("dedupe_key", String(200), nullable=False, unique=True),
    Column("ticker", String(16), index=True),
    Column("company_name", Text),
    Column("issuer_cik", String(10)),
    Column("public_date", Date, nullable=False),  # date the information became public (filing/publication)
    _ts("public_at"),  # exact public timestamp when known
    Column("transaction_date", Date),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("title", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("is_backfill", Boolean, nullable=False, server_default="false"),
    # exact = public timestamp known | date = public date known | first_seen = our observation date
    # estimated = inferred (never used in verified stats or multi-signal)
    Column("date_quality", String(12), nullable=False, server_default="date"),
    Column("returns_status", String(16), nullable=False, server_default="pending"),  # pending|partial|complete|retry|no_data
    _ts("returns_checked_at"),
    Column("returns_error", Text),
    # legacy duplicates are never deleted: they are marked superseded and point at the canonical row
    Column("superseded", Boolean, nullable=False, server_default="false"),
    Column("canonical_id", BigInteger),
    Column("superseded_reason", String(64)),
    # price series identity (security master) - may differ from the filing-time display ticker
    Column("price_symbol", String(16)),
    Column("price_symbol_basis", String(32)),
    # why a mature event has no SPY-relative return (DELISTED_BANKRUPTCY, TICKER_REUSED, NO_PRICE_DATA...)
    Column("missing_reason", String(32)),
    _created(),
    # ---- V2 global event contract (migration 0012) ----
    Column("country", String(2), nullable=False, server_default="US"),
    Column("exchange", String(8)),
    Column("security_id", String(48)),
    Column("issuer_id", String(40)),
    Column("event_family", String(32)),
    _ts("accepted_at"),  # source acceptance timestamp (SEC acceptance, KAP publication)
    _ts("first_seen_at"),  # our first observation (live) - never used as a historical entry for backfill
    _ts("available_at"),  # first moment a strategy could know the event: the ONLY entry-time anchor
    Column("source_document_id", Text),
    Column("source_accession", String(25)),
    Column("parser_version", String(32)),
    Column("schema_version", String(8), nullable=False, server_default="1"),
    Column("identity_confidence", String(8)),
    Column("event_confidence", String(8)),
    Column("needs_review", Boolean, nullable=False, server_default="false"),
    Column("is_tradeable_at_event", Boolean),
    _ts("next_tradeable_at"),
    Column("supersedes_event_id", BigInteger),
    Column("episode_id", String(64)),
    Column("feature_source_event_ids", JSONB),  # derived events: the primary events they were built from
    _ts("updated_at"),
)
Index("ix_events_type_public", events.c.event_type, events.c.public_date)
Index("ix_events_issuer", events.c.issuer_cik)
Index("ix_events_returns_status", events.c.returns_status)
Index("ix_events_available_at", events.c.available_at)
Index("ix_events_country_exchange_ticker", events.c.country, events.c.exchange, events.c.ticker, events.c.public_date)

event_returns = Table(
    "event_returns", metadata,
    Column("event_id", BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column("horizon_days", Integer, primary_key=True),
    Column("entry_date", Date, nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("exit_date", Date, nullable=False),
    Column("exit_price", Float, nullable=False),
    Column("ret", Float, nullable=False),
    Column("spy_ret", Float),
    Column("excess_ret", Float),
    Column("mae", Float),
    Column("mfe", Float),
    Column("exit_basis", String(20), nullable=False, server_default="horizon"),  # horizon | delisting_terminal | merger_cash | merger_stock | merger_mixed
    # sector-matched benchmark (SPDR sector ETF of the issuer's SIC sector) over the same holding period
    Column("sector_etf", String(8)),
    Column("sector_ret", Float),
    Column("sector_excess", Float),
    _created(),
)

signal_features = Table(
    "signal_features", metadata,
    Column("event_id", BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    Column("features", JSONB, nullable=False),
    _created(),
)

notifications = Table(
    "notifications", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("dedupe_key", String(200), nullable=False, unique=True),
    Column("event_id", BigInteger, ForeignKey("events.id", ondelete="SET NULL")),
    Column("kind", String(24), nullable=False),  # event | source_error | system
    # sending (claimed) -> sent | failed (retry after next_retry_at) -> dead ; merged = folded into another alert
    Column("status", String(16), nullable=False),
    Column("error", Text),
    Column("attempts", Integer, nullable=False, server_default="0"),
    _ts("claimed_at"),
    _ts("next_retry_at"),
    _ts("sent_at"),
    _created(),
)

source_health = Table(
    "source_health", metadata,
    Column("source", String(48), primary_key=True),
    _ts("last_run"),
    _ts("last_success"),
    _ts("last_event"),
    _ts("last_error_at"),
    Column("last_error", Text),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
    Column("alerted", Boolean, nullable=False, server_default="false"),
    Column("status", String(16), nullable=False, server_default="UNKNOWN"),
    Column("items_last_run", Integer, nullable=False, server_default="0"),
    # ---- V2 (migration 0012) ----
    Column("country", String(2)),
    Column("dataset", String(48)),
    Column("watermark", JSONB),
    Column("response_status", Integer),
    Column("schema_fingerprint", String(32)),
    Column("expected_fingerprint", String(32)),
    Column("parser_version", String(32)),
    Column("latency_ms", Integer),
    # HEALTHY | STALE | DEGRADED | BLOCKED | SCHEMA_CHANGED | AUTH_REQUIRED | DISABLED | UNKNOWN
    Column("health_status", String(16), nullable=False, server_default="UNKNOWN"),
    Column("license_status", String(24)),
)

processing_jobs = Table(
    "processing_jobs", metadata,
    Column("name", String(80), primary_key=True),
    Column("status", String(16), nullable=False, server_default="idle"),
    Column("state", JSONB, nullable=False, server_default="{}"),
    _ts("last_started"),
    _ts("last_finished"),
    Column("last_error", Text),
    Column("runs", Integer, nullable=False, server_default="0"),
)


# =====================================================================  V2: discovery + news

universe_snapshots = Table(  # point-in-time universe (kept per date -> no survivorship rewrite)
    "universe_snapshots", metadata,
    Column("as_of", Date, primary_key=True),
    Column("cik", String(10), primary_key=True),
    Column("ticker", String(16), nullable=False),
    Column("name", Text),
    Column("price", Float),
    Column("shares", BigInteger),
    Column("shares_end", Date),
    Column("market_cap", Float),
    Column("bucket", String(16)),  # MICRO | EARLY_SMALL | SMALL | GROWTH | ALREADY_SCALED | BELOW_UNIVERSE
    Column("adv_dollar_30d", Float),
)
Index("ix_universe_bucket", universe_snapshots.c.as_of, universe_snapshots.c.bucket)

fundamentals_quarterly = Table(
    "fundamentals_quarterly", metadata,
    Column("cik", String(10), primary_key=True),
    Column("concept", String(64), primary_key=True),
    Column("period_end", Date, primary_key=True),
    Column("period_start", Date),
    Column("val", Float, nullable=False),
    Column("accession", String(25)),
)

discovery_companies = Table(
    "discovery_companies", metadata,
    Column("cik", String(10), primary_key=True),
    Column("ticker", String(16), index=True),
    Column("name", Text),
    Column("market_cap", Float),
    Column("bucket", String(16)),
    Column("themes", JSONB, nullable=False, server_default="[]"),
    Column("stage", String(24)),
    Column("interest_score", Float),
    Column("interest_level", String(8)),  # LOW | MEDIUM | HIGH
    Column("features", JSONB, nullable=False, server_default="{}"),
    Column("customers", JSONB, nullable=False, server_default="[]"),
    Column("metrics", JSONB, nullable=False, server_default="{}"),
    Column("financing_risk_score", Float),
    Column("financing_flags", JSONB, nullable=False, server_default="[]"),
    _ts("first_discovered_at"),
    _ts("last_scanned_at"),
)

discovery_evidence = Table(
    "discovery_evidence", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False, index=True),
    Column("kind", String(24), nullable=False),  # bottleneck | theme | customer | stage | risk
    Column("label", String(48), nullable=False),
    Column("relation", String(24)),  # customer | supplier | partner | competitor | investor | ...
    Column("agreement", String(40)),  # binding_contract | purchase_order | framework | mou | loi | pilot | ...
    Column("counterparty", Text),
    Column("counterparty_category", String(32)),
    Column("hedged", Boolean, nullable=False, server_default="false"),
    Column("sentence", Text, nullable=False),
    Column("sentence_hash", String(40), nullable=False),
    Column("source_url", Text, nullable=False),
    Column("accession", String(25)),
    Column("form", String(16)),
    Column("filed_date", Date),
    _created(),
    UniqueConstraint("cik", "kind", "label", "sentence_hash", name="uq_discovery_evidence_key"),
)

discovery_risks = Table(
    "discovery_risks", metadata,
    Column("cik", String(10), primary_key=True),
    Column("risk", String(40), primary_key=True),
    Column("severity", String(8), nullable=False),  # LOW | MEDIUM | HIGH
    Column("value", Text),
    Column("source_url", Text),
    _ts("detected_at"),
)

discovery_stage_history = Table(
    "discovery_stage_history", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False, index=True),
    Column("stage", String(24), nullable=False),
    Column("previous_stage", String(24)),
    Column("evidence", JSONB, nullable=False, server_default="[]"),
    _created(),
)

company_thesis = Table(
    "company_thesis", metadata,
    Column("cik", String(10), primary_key=True),
    Column("ticker", String(16)),
    Column("sections", JSONB, nullable=False),  # {field: {"text": ..., "evidence": [urls/ids]}}
    Column("thesis_last_updated", DateTime(timezone=True), nullable=False),
)

thesis_changes = Table(
    "thesis_changes", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False, index=True),
    Column("ticker", String(16)),
    Column("news_cluster_id", BigInteger),
    Column("change", String(24), nullable=False),  # THESIS_STRENGTHENING | THESIS_WEAKENING | THESIS_INVALIDATION | NEUTRAL
    Column("explanation", Text, nullable=False),
    Column("evidence", JSONB, nullable=False, server_default="[]"),
    _created(),
    UniqueConstraint("cik", "news_cluster_id", name="uq_thesis_changes_cik_cluster"),
)

watchlist = Table(
    "watchlist", metadata,
    Column("cik", String(10), primary_key=True),
    Column("ticker", String(16)),
    Column("category", String(20), nullable=False),  # DISCOVERY | HIGH_INTEREST | MONITOR | THESIS_WEAKENING | ARCHIVED
    Column("reason", Text, nullable=False),
    Column("manual_override", Boolean, nullable=False, server_default="false"),
    _ts("updated_at"),
)

watchlist_history = Table(
    "watchlist_history", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False, index=True),
    Column("from_category", String(20)),
    Column("to_category", String(20), nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor", String(16), nullable=False),  # auto | manual
    _created(),
)

news_items = Table(
    "news_items", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("source_tier", Integer, nullable=False),
    Column("url", Text, nullable=False),
    Column("guid", String(300), nullable=False),
    Column("headline", Text, nullable=False),
    Column("snippet", Text),
    Column("publisher", Text),
    Column("original_published_at", DateTime(timezone=True)),  # first published time as first observed
    Column("updated_at", DateTime(timezone=True)),  # later "updated" timestamps - never used for entry
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    # HIGH = authoritative timestamp (SEC acceptance / wire timestamp consistent with first sight)
    # MEDIUM = publisher timestamp without contradiction | LOW = missing / future-dated / seen much later
    Column("timestamp_confidence", String(8)),
    Column("canonical_url", Text),
    Column("lead_signature", JSONB),  # MinHash of the headline+lead shingles (semantic copy detection)
    Column("source_lineage", String(40)),  # originating wire / filer (SEC, PRNEWSWIRE, BUSINESSWIRE, ...)
    Column("tickers", JSONB, nullable=False, server_default="[]"),
    Column("ciks", JSONB, nullable=False, server_default="[]"),
    Column("entities", JSONB, nullable=False, server_default="[]"),
    Column("cluster_id", BigInteger, index=True),
    UniqueConstraint("source", "guid", name="uq_news_items_source_guid"),
)
Index("ix_news_items_published", news_items.c.original_published_at)

news_story_clusters = Table(
    "news_story_clusters", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("ticker", String(16), index=True),
    Column("cik", String(10)),
    Column("primary_item_id", BigInteger, nullable=False),
    Column("public_at", DateTime(timezone=True), nullable=False),  # earliest trustworthy public time
    Column("headline", Text, nullable=False),
    Column("canonical_story", Text),  # normalized headline of the primary (most authoritative) copy
    Column("first_published_at", DateTime(timezone=True)),
    Column("first_seen_at", DateTime(timezone=True)),
    Column("syndicated_count", Integer, nullable=False, server_default="0"),
    Column("timestamp_confidence", String(8)),
    Column("n_items", Integer, nullable=False, server_default="1"),
    Column("sources", JSONB, nullable=False, server_default="[]"),
    Column("title_tokens", JSONB, nullable=False, server_default="[]"),
    Column("classes", JSONB, nullable=False, server_default="[]"),
    Column("agreement", String(40)),
    Column("binding", Boolean),
    Column("contract_value", Float),
    Column("annual_revenue", Float),
    Column("value_to_revenue", Float),
    Column("market_cap", Float),
    Column("value_to_mcap", Float),
    Column("mcap_bucket", String(16)),
    Column("materiality", String(8)),
    Column("event_id", BigInteger),
    _created(),
)
Index("ix_news_clusters_public", news_story_clusters.c.public_at)

entity_aliases = Table(
    "entity_aliases", metadata,
    Column("alias_norm", String(120), primary_key=True),
    Column("canonical", Text, nullable=False),
    Column("category", String(32), nullable=False),
    Column("parent", Text),
    Column("ticker", String(16)),
)

llm_usage = Table(
    "llm_usage", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("provider", String(32), nullable=False),
    Column("model", String(64), nullable=False),
    Column("purpose", String(48), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    _created(),
)


# =====================================================================  data integrity (2026-09-25)

# Every reporting owner of a Form 4 (joint filings list several; transactions are not per-owner).
insider_filing_owners = Table(
    "insider_filing_owners", metadata,
    Column("accession", String(25), primary_key=True),
    Column("owner_cik", String(10), primary_key=True),
    Column("owner_index", Integer, nullable=False),
    Column("owner_name", Text, nullable=False),
    Column("is_director", Boolean, nullable=False, server_default="false"),
    Column("is_officer", Boolean, nullable=False, server_default="false"),
    Column("is_ten_pct", Boolean, nullable=False, server_default="false"),
    Column("officer_title", Text),
    Column("is_ceo", Boolean, nullable=False, server_default="false"),
    Column("is_cfo", Boolean, nullable=False, server_default="false"),
    Column("is_entity", Boolean, nullable=False, server_default="false"),
    Column("issuer_cik", String(10), index=True),
    Column("filed_date", Date),
)

# Person/entity graph: which reporting entities share one economic (beneficial) owner.
beneficial_owner_links = Table(
    "beneficial_owner_links", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("entity_key", String(40), nullable=False),  # owner CIK (or NAME:<norm>)
    Column("entity_name", Text, nullable=False),
    Column("beneficial_owner_key", String(40), nullable=False),
    Column("beneficial_owner_name", Text, nullable=False),
    Column("relationship_type", String(24), nullable=False),  # JOINT_FILER | NAMED_TRUST | FAMILY_VEHICLE | CONTROLLED_ENTITY
    Column("confidence", String(8), nullable=False),  # HIGH | MEDIUM | LOW
    Column("source", Text, nullable=False),  # accession / rule that produced the link
    Column("issuer_cik", String(10)),
    _created(),
    UniqueConstraint("entity_key", "beneficial_owner_key", "relationship_type", name="uq_bo_links_entity_owner_type"),
)
Index("ix_bo_links_entity", beneficial_owner_links.c.entity_key)

# Security master (SEC primary sources + public listing metadata).
security_master = Table(
    "security_master", metadata,
    Column("cik", String(10), primary_key=True),
    Column("name", Text),
    Column("sic", String(8)),
    Column("sic_description", Text),
    Column("sector", String(40)),  # SIC division
    Column("exchanges", JSONB, nullable=False, server_default="[]"),
    Column("current_tickers", JSONB, nullable=False, server_default="[]"),
    Column("former_names", JSONB, nullable=False, server_default="[]"),
    Column("status", String(16), nullable=False, server_default="UNKNOWN"),  # ACTIVE | DELISTED | UNKNOWN
    Column("delisting_date", Date),
    Column("delisting_reason", String(32)),  # BANKRUPTCY | MERGER | EXCHANGE_COMPLIANCE | VOLUNTARY | UNKNOWN
    Column("delisting_confidence", String(8)),
    Column("delisting_source", Text),
    _ts("submissions_fetched_at"),
    _ts("updated_at"),
)

# Point-in-time symbol history: the symbol a CIK used on its own filings between two dates.
security_symbols = Table(
    "security_symbols", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False),
    Column("symbol", String(16), nullable=False),
    Column("first_seen", Date, nullable=False),
    Column("last_seen", Date, nullable=False),
    Column("n_obs", Integer, nullable=False, server_default="1"),
    Column("source", String(24), nullable=False),  # form345_filing | sec_registry | listing_file
    UniqueConstraint("cik", "symbol", "source", name="uq_security_symbols_cik_sym_src"),
)
Index("ix_security_symbols_symbol", security_symbols.c.symbol)
Index("ix_security_symbols_cik", security_symbols.c.cik)

# Public listing periods per symbol (exchange symbol directories / listing files).
listing_periods = Table(
    "listing_periods", metadata,
    Column("symbol", String(16), primary_key=True),
    Column("exchange", String(24), primary_key=True),
    Column("start_date", Date),
    Column("end_date", Date),
    Column("asset_type", String(16)),
    Column("source", String(24), nullable=False),
)

corporate_actions = Table(
    "corporate_actions", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False, index=True),
    # FORM25 | FORM25_NSE | FORM15 | BANKRUPTCY | CHANGE_IN_CONTROL | ACQ_DISPOSITION | LISTING_DEFICIENCY |
    # UNREGISTERED_SALE | SHELF_REGISTRATION | PROSPECTUS_424B | MERGER_PROXY | TENDER_OFFER | SPLIT | REVERSE_SPLIT
    Column("action", String(24), nullable=False),
    Column("action_date", Date, nullable=False),
    Column("accession", String(25), nullable=False, server_default=""),
    Column("source", String(24), nullable=False),  # sec_full_index | sec_submissions | yahoo_events
    Column("details", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("cik", "action", "accession", "action_date", name="uq_corporate_actions_key"),
)

# XBRL facts with the filing date that disclosed them (point-in-time fundamentals).
fundamental_facts = Table(
    "fundamental_facts", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False),
    Column("concept", String(64), nullable=False),
    Column("period_start", Date),
    Column("period_end", Date, nullable=False),
    Column("val", Float, nullable=False),
    Column("filed", Date, nullable=False),
    Column("accession", String(25), nullable=False),
    Column("form", String(16)),
    UniqueConstraint("cik", "concept", "period_start", "period_end", "accession", name="uq_fundamental_facts_key"),
)
Index("ix_fundamental_facts_cik_filed", fundamental_facts.c.cik, fundamental_facts.c.concept, fundamental_facts.c.filed)

# Audit trail for every legacy cleanup (dry runs included).
data_cleanup_audit = Table(
    "data_cleanup_audit", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("run_id", String(40), nullable=False, index=True),
    Column("action", String(40), nullable=False),
    Column("table_name", String(40), nullable=False),
    Column("row_id", BigInteger),
    Column("before", JSONB),
    Column("after", JSONB),
    Column("reason", Text, nullable=False),
    Column("dry_run", Boolean, nullable=False),
    _created(),
)

# Cached hit-rate quality verdicts (TRUSTED | PARTIAL | BLOCKED) per peer group / horizon.
metrics_quality = Table(
    "metrics_quality", metadata,
    Column("metric_key", String(160), primary_key=True),
    Column("status", String(8), nullable=False),
    Column("reasons", JSONB, nullable=False, server_default="[]"),
    Column("stats", JSONB, nullable=False, server_default="{}"),
    _ts("computed_at"),
)

walkforward_results = Table(
    "walkforward_results", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("run_id", String(40), nullable=False, index=True),
    Column("study", String(64), nullable=False),
    Column("fold", Integer, nullable=False),
    Column("train_start", Date, nullable=False),
    Column("train_end", Date, nullable=False),
    Column("validation_end", Date, nullable=False),
    Column("test_end", Date, nullable=False),
    Column("chosen", JSONB, nullable=False, server_default="{}"),
    Column("metrics", JSONB, nullable=False, server_default="{}"),
    _created(),
)

discovery_validation = Table(
    "discovery_validation", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("sample_id", String(40), nullable=False),
    Column("cik", String(10), nullable=False),
    Column("ticker", String(16)),
    Column("level_at_sample", String(8), nullable=False),
    Column("score_at_sample", Float),
    Column("evidence_snapshot", JSONB, nullable=False, server_default="{}"),
    # TRUE_DISCOVERY | PLAUSIBLE | FALSE_POSITIVE | INSUFFICIENT_DATA (NULL = not yet labelled)
    Column("label", String(20)),
    Column("reason_codes", JSONB, nullable=False, server_default="[]"),
    Column("notes", Text),
    Column("labeler", String(40)),
    _ts("labeled_at"),
    _created(),
    UniqueConstraint("sample_id", "cik", name="uq_discovery_validation_sample_cik"),
)

# OGE public financial disclosures (primary source: extapps2.oge.gov).
oge_documents = Table(
    "oge_documents", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("doc_url", Text, nullable=False, unique=True),
    Column("filer_name", Text, nullable=False),
    Column("doc_type", String(40), nullable=False),  # 278 Transaction | Annual (YYYY) | ...
    Column("doc_date", DateTime(timezone=True)),  # OGE posting timestamp
    Column("agency", Text),
    Column("title", Text),
    Column("sha256", String(64)),
    Column("n_pages", Integer),
    Column("text_gz", LargeBinary),
    Column("parse_status", String(16), nullable=False, server_default="pending"),
    Column("parse_note", Text),
    _created(),
)

oge_transactions = Table(
    "oge_transactions", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("doc_id", BigInteger, ForeignKey("oge_documents.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("line_no", Integer, nullable=False),
    Column("description", Text, nullable=False),
    Column("tx_type", String(16)),  # PURCHASE | SALE | EXCHANGE | UNKNOWN
    Column("tx_date", Date),
    Column("amount_min", Numeric(16, 2)),
    Column("amount_max", Numeric(16, 2)),
    Column("amount_text", Text),
    Column("ticker", String(16)),  # only when the filer wrote an explicit exchange ticker
    Column("parse_confidence", String(8), nullable=False),  # HIGH | MEDIUM | LOW (OCR text layer)
    UniqueConstraint("doc_id", "line_no", name="uq_oge_transactions_doc_line"),
)

# ------------------------------------------------------------------ 10-K text changes (Lazy Prices)

tenk_filings = Table(
    "tenk_filings", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False),
    Column("ticker", String(16)),  # ticker AT the filing date (security master), never today's
    Column("accession", String(25), nullable=False, unique=True),
    Column("form", String(10), nullable=False),
    Column("filing_date", Date, nullable=False),
    _ts("accepted_at"),  # SEC acceptance timestamp = public time (entry basis)
    Column("period", Date),  # informational only; never used for timing
    Column("source_url", Text, nullable=False),
    Column("sections", JSONB, nullable=False, server_default="[]"),
    Column("profile", JSONB, nullable=False, server_default="{}"),  # sentence fingerprints per section
    Column("status", String(16), nullable=False),  # ok | no_sections | fetch_failed | extract_failed
    Column("prev_accession", String(25)),
    Column("n_changes", Integer),
    Column("is_backfill", Boolean, nullable=False, server_default="false"),
    _created(),
)
Index("ix_tenk_filings_cik_date", tenk_filings.c.cik, tenk_filings.c.filing_date)

tenk_changes = Table(
    "tenk_changes", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("filing_id", BigInteger, ForeignKey("tenk_filings.id", ondelete="CASCADE"), nullable=False),
    Column("cik", String(10), nullable=False, index=True),
    Column("ticker", String(16)),
    Column("filing_date", Date, nullable=False),
    _ts("public_timestamp"),
    Column("section", String(32), nullable=False),
    Column("change_type", String(40), nullable=False),
    Column("materiality", String(8), nullable=False),  # HIGH | MEDIUM | LOW
    Column("n_new", Integer, nullable=False, server_default="0"),
    Column("n_removed", Integer, nullable=False, server_default="0"),
    Column("old_text_hash", String(40)),
    Column("new_text_hash", String(40)),
    Column("examples", JSONB, nullable=False, server_default="[]"),
    Column("source_url", Text, nullable=False),
    Column("prev_accession", String(25)),
    _created(),
    UniqueConstraint("filing_id", "change_type", name="uq_tenk_changes_filing_type"),
)

# ------------------------------------------------------------------ insider person track record (point-in-time snapshot)

insider_profiles = Table(
    "insider_profiles", metadata,
    Column("person_id", String(64), primary_key=True),  # owner CIK, or N:<normalized name> when the filer has none
    Column("name", Text, nullable=False),
    Column("issuer_count", Integer, nullable=False),
    Column("completed_buy_count", Integer, nullable=False),
    Column("episode_count", Integer, nullable=False),
    Column("unique_company_count", Integer, nullable=False),
    Column("median_30d_excess", Float),
    Column("median_90d_excess", Float),
    Column("median_180d_excess", Float),
    Column("spy_beat_rate_90d", Float),
    Column("sector_beat_rate_90d", Float),
    Column("ci_low", Float),
    Column("ci_high", Float),
    Column("unresolved_share", Float),
    Column("data_quality", String(16), nullable=False),  # OK | PARTIAL | BLOCKED
    Column("label", String(24), nullable=False),  # INSUFFICIENT_HISTORY | HISTORICALLY_STRONG | MIXED | HISTORICALLY_WEAK
    Column("last_updated", DateTime(timezone=True), nullable=False),
)

# ------------------------------------------------------------------ paper trading (virtual money only, never a broker)

paper_accounts = Table(
    "paper_accounts", metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("strategy", String(16), nullable=False),  # A_30D | B_90D | C_180D | D_THESIS | E_STOP_TIME
    Column("starting_capital", Float, nullable=False),
    Column("commission_per_trade", Float, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("book", String(8), nullable=False, server_default="MAIN"),  # MAIN (TRUSTED families) | RESEARCH
    _created(),
)

paper_cash = Table(  # cash ledger: every movement, balance reproducible
    "paper_cash", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), nullable=False, index=True),
    Column("on_date", Date, nullable=False),
    Column("delta", Float, nullable=False),
    Column("balance_after", Float, nullable=False),
    Column("reason", String(32), nullable=False),  # DEPOSIT | BUY | SELL | COMMISSION
    Column("ref_id", BigInteger),
    _created(),
)

proposed_trades = Table(
    "proposed_trades", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("code", String(12), nullable=False, unique=True),  # short approval code shown in Telegram
    Column("ticker", String(16), nullable=False),
    Column("cik", String(10)),
    Column("event_id", BigInteger, ForeignKey("events.id")),
    Column("signal_family", String(32), nullable=False),
    Column("event_type", String(40), nullable=False),
    _ts("proposal_created_at", nullable=False),
    Column("reference_date", Date, nullable=False),
    Column("entry_reference_price", Float, nullable=False),
    Column("suggested_position_pct", Float, nullable=False),
    Column("max_position_pct", Float, nullable=False),
    Column("stop_reference", Float),
    Column("thesis", Text, nullable=False),
    Column("risks", JSONB, nullable=False, server_default="[]"),
    Column("reasons", JSONB, nullable=False, server_default="[]"),
    Column("data_quality", String(16), nullable=False),
    Column("gate", JSONB, nullable=False, server_default="{}"),
    Column("approval_status", String(10), nullable=False, server_default="PENDING"),  # PENDING|APPROVED|REJECTED|EXPIRED
    _ts("expires_at", nullable=False),
    _ts("decided_at"),
    Column("decided_by", String(40)),
    Column("research_only", Boolean, nullable=False, server_default="false"),
    Column("notified", Boolean, nullable=False, server_default="false"),
    # ---- V2 forward-validation timeline (migration 0012) ----
    _ts("signal_time"),  # event available_at
    _ts("eligible_market_at"),  # first market open at/after approval
)
Index("ix_proposed_trades_status", proposed_trades.c.approval_status, proposed_trades.c.ticker)

paper_orders = Table(
    "paper_orders", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), nullable=False, index=True),
    Column("proposal_id", BigInteger, ForeignKey("proposed_trades.id")),
    Column("position_id", BigInteger),  # set for SELL orders
    Column("ticker", String(16), nullable=False),
    Column("side", String(4), nullable=False),  # BUY | SELL
    _ts("requested_at", nullable=False),
    Column("not_before", Date, nullable=False),  # first session the order may fill (never the decision day)
    Column("target_pct", Float),
    Column("status", String(10), nullable=False, server_default="PENDING"),  # PENDING | FILLED | CANCELLED
    Column("fill_date", Date),
    Column("fill_price", Float),
    Column("shares", Float),
    Column("slippage_bps", Float),
    Column("commission", Float),
    Column("note", Text),
    # ---- V2 fill realism (migration 0012) ----
    Column("fill_model", String(8)),  # LIQUID | NORMAL | ILLIQUID
    Column("spread_bps", Float),
    Column("participation", Float),  # order shares / fill-day volume
    Column("adv_shares", Float),
)

paper_positions = Table(
    "paper_positions", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), nullable=False, index=True),
    Column("proposal_id", BigInteger, ForeignKey("proposed_trades.id")),
    Column("ticker", String(16), nullable=False),
    Column("cik", String(10)),
    Column("shares", Float, nullable=False),
    Column("entry_date", Date, nullable=False),
    Column("entry_price", Float, nullable=False),  # incl. slippage
    Column("cost", Float, nullable=False),  # incl. commission
    Column("adj_factor", Float, nullable=False, server_default="1"),  # unused (kept): valuation uses one fresh series per run
    Column("stop_price", Float),
    Column("planned_exit_date", Date),
    Column("sector_etf", String(8)),
    Column("status", String(8), nullable=False, server_default="OPEN"),  # OPEN | CLOSING | CLOSED
    Column("exit_date", Date),
    Column("exit_price", Float),
    Column("exit_reason", String(24)),  # TIME | STOP | THESIS_INVALIDATION
    Column("realized_pnl", Float),
    Column("last_price", Float),
    Column("last_price_date", Date),
)

paper_trades = Table(  # executed fills
    "paper_trades", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), nullable=False, index=True),
    Column("order_id", BigInteger, ForeignKey("paper_orders.id"), nullable=False, unique=True),
    Column("position_id", BigInteger, ForeignKey("paper_positions.id")),
    Column("ticker", String(16), nullable=False),
    Column("side", String(4), nullable=False),
    Column("trade_date", Date, nullable=False),
    Column("price", Float, nullable=False),
    Column("shares", Float, nullable=False),
    Column("gross", Float, nullable=False),
    Column("commission", Float, nullable=False),
    Column("slippage_bps", Float, nullable=False),
    _created(),
)

paper_performance = Table(
    "paper_performance", metadata,
    Column("account_id", Integer, ForeignKey("paper_accounts.id"), primary_key=True),
    Column("on_date", Date, primary_key=True),
    Column("nav", Float, nullable=False),
    Column("cash", Float, nullable=False),
    Column("positions_value", Float, nullable=False),
    Column("spy_nav", Float),  # same capital held in SPY from the account's first trade
    Column("n_open", Integer, nullable=False),
)

# ------------------------------------------------------------------ free / vendor historical price sources

# One row per (company, symbol, provider) series, with provenance and the identity-gate verdict.
# Only TRUSTED and VALIDATED_FALLBACK series may feed the backtest.
price_sources = Table(
    "price_sources", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("cik", String(10), nullable=False, index=True),
    Column("symbol", String(16), nullable=False),
    Column("provider", String(24), nullable=False),  # tiingo | finsaber | yahoo | ...
    Column("source_ref", Text, nullable=False),  # URL without credentials, or dataset path + file hash
    _ts("retrieved_at", nullable=False),
    Column("raw_sha256", String(64)),
    Column("raw_gz", LargeBinary),  # compressed raw response (re-validation without re-querying)
    Column("n_bars", Integer, nullable=False, server_default="0"),
    Column("first_date", Date),
    Column("last_date", Date),
    Column("identity_basis", String(16), nullable=False),  # cik | name | symbol
    Column("validation_status", String(20), nullable=False),  # TRUSTED | VALIDATED_FALLBACK | LOW_CONFIDENCE | REJECTED
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("reasons", JSONB, nullable=False, server_default="[]"),
    Column("checks", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("cik", "symbol", "provider", name="uq_price_sources_cik_symbol_provider"),
)

vendor_prices = Table(
    "vendor_prices", metadata,
    Column("source_id", BigInteger, ForeignKey("price_sources.id", ondelete="CASCADE"), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("adj_close", Float),
    Column("volume", BigInteger),
)

# Free-tier quota ledger: unique symbols per calendar month and individual requests (hour/day limits).
provider_usage = Table(
    "provider_usage", metadata,
    Column("provider", String(24), primary_key=True),
    Column("month", String(7), primary_key=True),  # YYYY-MM
    Column("symbol", String(16), primary_key=True),
    _ts("first_used_at", nullable=False),
)

provider_requests = Table(
    "provider_requests", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("provider", String(24), nullable=False),
    _ts("at", nullable=False),
    Column("kind", String(16), nullable=False),  # meta | prices | listing | test
    Column("status", Integer),
)
Index("ix_provider_requests_provider_at", provider_requests.c.provider, provider_requests.c.at)

# Raw responses of low-limit free sources (never re-queried needlessly): hash + source + timestamp, gzip.
provider_raw = Table(
    "provider_raw", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("provider", String(24), nullable=False),
    Column("request_ref", Text, nullable=False),  # never contains a credential
    _ts("retrieved_at", nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("body_gz", LargeBinary, nullable=False),
)
Index("ix_provider_raw_provider_ref", provider_raw.c.provider, provider_raw.c.request_ref)

# Verified merger / acquisition consideration used to RESOLVE A POSITION'S EXIT (never to fabricate historical
# prices of the predecessor). Only CONFIRMED + SIMPLE rows are used by the backtest.
corporate_action_resolution = Table(
    "corporate_action_resolution", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("security_id", String(32), nullable=False),  # CIK:<predecessor cik>
    Column("action_type", String(24), nullable=False),  # STOCK_MERGER | CASH_MERGER | MIXED_MERGER
    Column("announcement_date", Date),
    Column("effective_date", Date, nullable=False),
    Column("predecessor_cik", String(10), nullable=False, index=True),
    Column("successor_cik", String(10)),
    Column("predecessor_ticker", String(16)),
    Column("successor_ticker", String(16)),
    Column("cash_component", Numeric(18, 6)),
    Column("share_ratio", Numeric(18, 8)),
    Column("currency", String(3), nullable=False, server_default="USD"),
    Column("source_accession", String(25)),
    Column("evidence", JSONB, nullable=False, server_default="[]"),  # primary-document quotes with offsets
    Column("validation_status", String(20), nullable=False),  # CONFIRMED | CONFLICT | INSUFFICIENT
    Column("complexity", String(32), nullable=False, server_default="SIMPLE"),  # SIMPLE | COMPLEX_CORPORATE_ACTION
    Column("validation_version", String(40)),
    Column("notes", Text),
    _created(),
    UniqueConstraint("predecessor_cik", "effective_date", name="uq_corporate_action_resolution_pred_eff"),
)

# Every reporting person of a Schedule 13D / 13G (group filings keep all members; the filing's investor fields hold
# only the lead). identity_key = CIK, else NAME:<normalized name> - never a fuzzy merge.
activist_reporting_persons = Table(
    "activist_reporting_persons", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("accession", String(25), nullable=False, index=True),
    Column("issuer_cik", String(10), index=True),
    Column("group_id", String(40)),  # GRP:<accession> when several persons report as a group
    Column("person_index", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("name_norm", Text, nullable=False),
    Column("cik", String(10), index=True),
    Column("identity_key", String(160), nullable=False, index=True),
    Column("kind", String(12), nullable=False, server_default="UNKNOWN"),  # INDIVIDUAL | ENTITY | UNKNOWN
    Column("type_codes", JSONB, nullable=False, server_default="[]"),  # cover-page "type of reporting person"
    Column("percent", Float),
    Column("is_lead", Boolean, nullable=False, server_default="false"),
    Column("confidence", String(12), nullable=False),  # HIGH | MEDIUM | NAME | AMBIGUOUS
    Column("cik_evidence", Text),
    Column("source", String(32), nullable=False),  # xml_reportingPersonInfo | cover_page | sec_header
    _created(),
    UniqueConstraint("accession", "person_index", name="uq_activist_reporting_persons_acc_idx"),
)

# What a Schedule 13D actually is (a 13D is not by itself activism). One row per filing, re-derivable (version).
thirteen_d_classification = Table(
    "thirteen_d_classification", metadata,
    Column("accession", String(25), primary_key=True),
    Column("issuer_cik", String(10), index=True),
    Column("investor_key", String(160), index=True),
    Column("filing_class", String(32), nullable=False, index=True),  # ACTIVIST_13D | INSIDER_13D | CONTROL_HOLDER_13D | PASSIVE_OR_STRUCTURAL_13D | AMBIGUOUS_13D
    Column("confidence", String(8), nullable=False),  # HIGH | MEDIUM | LOW
    Column("reasons", JSONB, nullable=False, server_default="[]"),
    Column("flags", JSONB, nullable=False, server_default="{}"),  # structured Item 4 flags
    Column("evidence", JSONB, nullable=False, server_default="{}"),
    Column("version", String(24), nullable=False),
    Column("classified_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


# =====================================================================  V2 (migration 0012): global core

# One canonical security per (country, exchange listing). A ticker is NOT an identity: symbols live in
# security_symbol_periods with validity intervals, and every symbol lookup is (exchange?, symbol, date).
global_securities = Table(
    "global_securities", metadata,
    Column("security_id", String(48), primary_key=True),  # e.g. US:CIK0000320193:COM | TR:ISIN:TRAASELS91H2
    Column("country", String(2), nullable=False),
    Column("exchange", String(8)),  # MIC when known (XNYS, XNAS, XIST, XTSE, XTSX, XCNQ, XASX)
    Column("issuer_id", String(40), nullable=False),  # US: CIK | TR: MKK/KAP member id | CA: SEDAR+ profile
    Column("issuer_name", Text, nullable=False),
    Column("cik", String(10)),
    Column("cusip", String(9)),
    Column("figi", String(12)),
    Column("composite_figi", String(12)),
    Column("isin", String(12)),
    Column("local_id", String(40)),  # MKK/BIST or SEDAR+ identifier
    Column("security_type", String(16)),  # COMMON | ADR | PREFERRED | UNIT | WARRANT | ETF | FUND | UNKNOWN
    Column("currency", String(3)),
    Column("listing_date", Date),
    Column("delisting_date", Date),
    Column("predecessor_security_id", String(48)),
    Column("successor_security_id", String(48)),
    Column("adr_underlying_security_id", String(48)),
    Column("primary_listing", Boolean),
    Column("tradeability_status", String(16), nullable=False, server_default="UNKNOWN"),  # ACTIVE|SUSPENDED|DELISTED|UNKNOWN
    Column("identity_confidence", String(8), nullable=False, server_default="LOW"),  # HIGH|MEDIUM|LOW
    Column("identity_evidence", JSONB, nullable=False, server_default="[]"),
    _ts("updated_at", server_default=func.now(), nullable=False),
)
Index("ix_global_securities_cik", global_securities.c.cik)
Index("ix_global_securities_isin", global_securities.c.isin)
Index("ix_global_securities_figi", global_securities.c.figi)

security_symbol_periods = Table(
    "security_symbol_periods", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("security_id", String(48), ForeignKey("global_securities.security_id", ondelete="CASCADE"), nullable=False),
    Column("exchange", String(8)),
    Column("symbol", String(16), nullable=False),
    Column("valid_from", Date),  # NULL = unknown start (never "forever": resolution then needs other evidence)
    Column("valid_to", Date),  # NULL = still valid
    Column("source", String(32), nullable=False),
    Column("confidence", String(8), nullable=False, server_default="MEDIUM"),
    UniqueConstraint("security_id", "symbol", "valid_from", "source", name="uq_security_symbol_periods_key"),
)
Index("ix_security_symbol_periods_lookup", security_symbol_periods.c.symbol, security_symbol_periods.c.valid_from)

# Raw data lake index: the compressed original lives on disk (content-addressed), the row is its provenance.
raw_documents = Table(
    "raw_documents", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("country", String(2), nullable=False),
    Column("source", String(48), nullable=False),
    Column("doc_key", Text, nullable=False),  # source-native id (accession, NCT id, file name, URL path)
    Column("url", Text),
    Column("sha256", String(64), nullable=False),
    Column("storage_path", Text, nullable=False),
    Column("bytes", BigInteger, nullable=False),
    Column("stored_bytes", BigInteger, nullable=False),
    Column("content_type", String(64)),
    _ts("retrieved_at", nullable=False),
    Column("parser_version", String(32)),
    Column("retention_class", String(8), nullable=False, server_default="HOT"),  # HOT | COLD | EVIDENCE (never expires)
    UniqueConstraint("source", "doc_key", "sha256", name="uq_raw_documents_key"),
)
Index("ix_raw_documents_sha", raw_documents.c.sha256)

dataset_manifests = Table(
    "dataset_manifests", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("dataset", String(64), nullable=False),
    _ts("generated_at", nullable=False, server_default=func.now()),
    Column("schema_version", String(16), nullable=False),
    Column("parser_version", String(32)),
    Column("row_count", BigInteger, nullable=False),
    Column("first_event", DateTime(timezone=True)),
    Column("last_event", DateTime(timezone=True)),
    Column("watermark", JSONB, nullable=False, server_default="{}"),
    Column("source_health", JSONB, nullable=False, server_default="{}"),
    Column("content_hash", String(64)),  # hash of the exported rows (reproducibility check)
    Column("export_path", Text),
    Column("export_bytes", BigInteger),
)
Index("ix_dataset_manifests_dataset", dataset_manifests.c.dataset, dataset_manifests.c.generated_at)

# Cross-process rate limit / quota coordination (worker, backfill and intelligence share one budget per key).
rate_limits = Table(
    "rate_limits", metadata,
    Column("key", String(48), primary_key=True),
    Column("min_interval_s", Float, nullable=False),
    _ts("next_at", nullable=False, server_default=func.now()),
    Column("daily_limit", Integer),
    Column("day", Date),
    Column("used_today", Integer, nullable=False, server_default="0"),
    Column("hourly_limit", Integer),
    Column("hour", DateTime(timezone=True)),
    Column("used_hour", Integer, nullable=False, server_default="0"),
    Column("last_429_at", DateTime(timezone=True)),
    Column("n_429", Integer, nullable=False, server_default="0"),
)

# Source failure vs true zero: one row per source and UTC day.
source_daily_status = Table(
    "source_daily_status", metadata,
    Column("source", String(48), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("runs", Integer, nullable=False, server_default="0"),
    Column("failures", Integer, nullable=False, server_default="0"),
    Column("items", Integer, nullable=False, server_default="0"),
    Column("events", Integer, nullable=False, server_default="0"),
    # OK_EVENTS | TRUE_NO_EVENT | SOURCE_UNAVAILABLE | API_QUOTA | LICENSE_BLOCK | AUTH_REQUIRED | PARTIAL
    Column("status", String(20), nullable=False),
)

# Market state at (or right after) available_at, captured live -> survivorship-safe forward dataset.
event_market_snapshots = Table(
    "event_market_snapshots", metadata,
    Column("event_id", BigInteger, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
    _ts("captured_at", nullable=False),
    Column("symbol", String(16)),
    Column("price", Float),
    Column("price_time", DateTime(timezone=True)),
    Column("bid", Float),
    Column("ask", Float),
    Column("volume", Float),
    Column("avg_volume_20d", Float),
    Column("market_cap", Float),
    Column("shares_outstanding", Float),
    Column("benchmark_symbol", String(8)),
    Column("benchmark_price", Float),
    Column("sector_symbol", String(8)),
    Column("sector_price", Float),
    Column("provider", String(24)),
    Column("lag_seconds", Float),  # captured_at - available_at
    Column("status", String(16), nullable=False),  # CAPTURED | NO_QUOTE | NOT_TRADEABLE | IDENTITY_UNRESOLVED
    # forward gold dataset (V2 #22)
    _ts("available_at"),
    Column("security_id", String(48)),
    Column("exchange", String(8)),
    Column("sector", String(40)),
    Column("source_event_ids", JSONB),
    Column("signal_family", String(48)),
    Column("signal_version", String(16)),
    Column("data_quality_state", String(16)),
)

# Timestamped relationships (issuer/security/person/fund/agency/contract/grant/patent/trial/drug/subsidiary).
entity_edges = Table(
    "entity_edges", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("src_type", String(16), nullable=False),
    Column("src_id", String(64), nullable=False),
    Column("rel", String(32), nullable=False),  # SUBSIDIARY_OF | SPONSOR_OF | ASSIGNEE_OF | CLIENT_OF | RECIPIENT_OF | HOLDS ...
    Column("dst_type", String(16), nullable=False),
    Column("dst_id", String(64), nullable=False),
    Column("valid_from", Date),
    Column("valid_to", Date),
    Column("source", String(48), nullable=False),
    Column("confidence", String(8), nullable=False),
    Column("evidence", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("src_type", "src_id", "rel", "dst_type", "dst_id", "source", name="uq_entity_edges_key"),
)
Index("ix_entity_edges_dst", entity_edges.c.dst_type, entity_edges.c.dst_id)

# Generic point-in-time records of lower-volume datasets (trials, FDA, lobbying, grants, patents, macro, COT,
# CA / TR official datasets). High-volume series get their own tables below.
dataset_records = Table(
    "dataset_records", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("country", String(2), nullable=False),
    Column("dataset", String(48), nullable=False),
    Column("record_key", String(160), nullable=False),
    Column("record_version", String(64), nullable=False, server_default=""),  # source's last-update stamp
    Column("as_of_date", Date),  # the date the record is about (decision date, report date...)
    _ts("available_at", nullable=False),  # first moment a strategy could know it
    _ts("first_seen_at", nullable=False, server_default=func.now()),
    Column("issuer_cik", String(10)),
    Column("security_id", String(48)),
    Column("entity_name", Text),
    Column("identity_confidence", String(8)),
    Column("payload", JSONB, nullable=False),
    Column("source_url", Text),
    Column("raw_document_id", BigInteger),
    Column("parser_version", String(32), nullable=False),
    Column("license_status", String(24), nullable=False),
    Column("needs_review", Boolean, nullable=False, server_default="false"),
    UniqueConstraint("dataset", "record_key", "record_version", name="uq_dataset_records_key"),
)
Index("ix_dataset_records_dataset_date", dataset_records.c.dataset, dataset_records.c.as_of_date)
Index("ix_dataset_records_issuer", dataset_records.c.issuer_cik)

short_volume_daily = Table(  # FINRA Reg SHO daily short-sale VOLUME (not short interest)
    "short_volume_daily", metadata,
    Column("trade_date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("short_volume", BigInteger, nullable=False),
    Column("short_exempt_volume", BigInteger, nullable=False, server_default="0"),
    Column("total_volume", BigInteger, nullable=False),
    _ts("available_at", nullable=False),
)
Index("ix_short_volume_symbol", short_volume_daily.c.symbol, short_volume_daily.c.trade_date)

short_interest = Table(  # short POSITIONS (FINRA equity short interest, CIRO consolidated short position report)
    "short_interest", metadata,
    Column("country", String(2), primary_key=True),
    Column("settlement_date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("short_position", BigInteger, nullable=False),
    Column("prev_short_position", BigInteger),
    Column("avg_daily_volume", Float),
    Column("days_to_cover", Float),
    Column("source", String(24), nullable=False),
    _ts("available_at", nullable=False),
)

fails_to_deliver = Table(  # SEC fails-to-deliver (published twice a month, ~2 weeks after the half-month)
    "fails_to_deliver", metadata,
    Column("settlement_date", Date, primary_key=True),
    Column("cusip", String(9), primary_key=True),
    Column("symbol", String(16)),
    Column("quantity", BigInteger, nullable=False),
    Column("price", Float),
    Column("description", Text),
    Column("source_file", String(64), nullable=False),
    _ts("available_at", nullable=False),
)
Index("ix_ftd_symbol", fails_to_deliver.c.symbol, fails_to_deliver.c.settlement_date)

inst_filings_13f = Table(
    "inst_filings_13f", metadata,
    Column("accession", String(25), primary_key=True),
    Column("manager_cik", String(10), nullable=False),
    Column("manager_name", Text),
    Column("period", Date, nullable=False),  # report calendar quarter end
    Column("filed_date", Date, nullable=False),
    _ts("available_at", nullable=False),
    Column("submission_type", String(12), nullable=False),  # 13F-HR | 13F-HR/A
    Column("amendment_type", String(16)),  # RESTATEMENT | NEW HOLDINGS
    Column("entries", Integer),
    Column("total_value", Float),
    Column("source", String(32), nullable=False),
)
Index("ix_13f_manager_period", inst_filings_13f.c.manager_cik, inst_filings_13f.c.period)

inst_holdings_13f = Table(
    "inst_holdings_13f", metadata,
    Column("accession", String(25), ForeignKey("inst_filings_13f.accession", ondelete="CASCADE"), primary_key=True),
    Column("line", Integer, primary_key=True),
    Column("cusip", String(9), nullable=False),
    Column("issuer_name", Text),
    Column("class_title", Text),
    Column("value", Float),  # USD (the source's thousands before 2023 are normalised to dollars)
    Column("shares", Float),
    Column("sh_prn", String(3)),
    Column("put_call", String(4)),
    Column("discretion", String(8)),
)
Index("ix_13f_holdings_cusip", inst_holdings_13f.c.cusip)

inst_cusip_quarter_13f = Table(  # all managers aggregated per CUSIP and quarter (per-manager rows only for focused funds)
    "inst_cusip_quarter_13f", metadata,
    Column("cusip", String(9), primary_key=True),
    Column("period", Date, primary_key=True),
    Column("holders", Integer, nullable=False),
    Column("shares", Float),
    Column("value", Float),
    Column("new_holders", Integer),
    Column("exited_holders", Integer),
    Column("increased_holders", Integer),
    Column("decreased_holders", Integer),
    _ts("available_at", nullable=False),  # latest original filing counted (deadline effect: 45 days after quarter end)
)

# Multiple-testing ledger: every evaluated hypothesis (family x variant x horizon) is recorded, so the false-discovery
# control counts ALL tests ever run, not only the ones that looked good.
hypothesis_log = Table(
    "hypothesis_log", metadata,
    Column("id", BigInteger, primary_key=True),
    Column("family", String(48), nullable=False),
    Column("variant", String(64), nullable=False, server_default="base"),
    _ts("run_at", nullable=False, server_default=func.now()),
    Column("horizon", Integer, nullable=False),
    Column("n_events", Integer),
    Column("n_companies", Integer),
    Column("in_sample_rate", Float),
    Column("out_of_sample_rate", Float),
    Column("p_value", Float),
    Column("adjusted_p", Float),  # Benjamini-Hochberg over every hypothesis in the ledger
    Column("verdict", String(24)),
    Column("params", JSONB, nullable=False, server_default="{}"),
)
Index("ix_hypothesis_log_family", hypothesis_log.c.family, hypothesis_log.c.run_at)
