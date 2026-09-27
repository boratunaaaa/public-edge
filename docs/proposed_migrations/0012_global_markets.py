"""PROPOSED - NOT APPLIED (kept outside migrations/versions so Alembic never picks it up).

Global markets (TR / CA / AU) as first-class citizens of the shared core.

Revision ID: 0012 (proposed)
Revises: 0011 (activist_reporting_persons; 0009 = corporate_action_resolution, both in production)

Why each change:
* events.country / events.exchange / events.security_id - the development mapping keeps them in payload; the core
  needs them as columns because several core queries key by TICKER only (multi_signal_check, related events in
  dispatch, the dashboard) and tickers collide across exchanges (BHP on the ASX and as a NYSE ADR). Every such
  query must become (exchange, ticker) before a second market goes live.
* global_securities / global_symbol_periods - security master with symbol validity periods (rename / reuse safe).
* source_schema_state - persisted fingerprint baseline + health per non-US source (app/markets/fingerprint.py).
* signal_features currently duplicates the whole payload; for TR (raw KAP text ~4 KB/event) store only features.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.add_column("events", sa.Column("country", sa.String(2), nullable=False, server_default="US"))
    op.add_column("events", sa.Column("exchange", sa.String(8), nullable=False, server_default="XNYS"))
    op.add_column("events", sa.Column("security_id", sa.String(40)))
    op.create_index("ix_events_exchange_ticker_date", "events", ["exchange", "ticker", "public_date"])
    op.create_table(
        "global_securities",
        sa.Column("security_id", sa.String(40), primary_key=True),
        sa.Column("country", sa.String(2), nullable=False), sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("company_id", sa.String(24), nullable=False), sa.Column("company_name", sa.Text, nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False), sa.Column("listing_date", sa.Date), sa.Column("delisting_date", sa.Date),
        sa.Column("sector", sa.Text), sa.Column("market", sa.String(40)), sa.Column("free_float", sa.Float),
        sa.Column("index_membership", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("corporate_action_history", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "global_symbol_periods",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("security_id", sa.String(40), sa.ForeignKey("global_securities.security_id", ondelete="CASCADE"), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False), sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("valid_from", sa.Date), sa.Column("valid_to", sa.Date), sa.Column("source", sa.String(24), nullable=False),
    )
    op.create_index("ix_global_symbol_periods_lookup", "global_symbol_periods", ["exchange", "symbol", "valid_from"])
    op.create_table(
        "source_schema_state",
        sa.Column("source", sa.String(40), primary_key=True),
        sa.Column("expected_fingerprint", sa.String(32)), sa.Column("last_fingerprint", sa.String(32)),
        sa.Column("last_success", sa.DateTime(timezone=True)), sa.Column("last_event", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="UNKNOWN"),
        sa.Column("notes", postgresql.JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_table("source_schema_state")
    op.drop_index("ix_global_symbol_periods_lookup", table_name="global_symbol_periods")
    op.drop_table("global_symbol_periods")
    op.drop_table("global_securities")
    op.drop_index("ix_events_exchange_ticker_date", table_name="events")
    for c in ("security_id", "exchange", "country"):
        op.drop_column("events", c)
