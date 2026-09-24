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
    Column("amount_basis", String(24)),  # amount_at_first_seen | current_total_at_backfill
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
    _created(),
)
Index("ix_events_type_public", events.c.event_type, events.c.public_date)
Index("ix_events_returns_status", events.c.returns_status)

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
