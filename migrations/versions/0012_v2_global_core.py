"""V2 global core: event contract, source health, security master, raw lake, manifests, rate limits,
market snapshots, entity graph, new US/CA/TR datasets, paper forward-validation fields.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28

Additive only (new nullable columns / new tables). available_at is backfilled for existing events with the
same conservative rule the backtest already used: exact timestamp when known, else the END of the public date
in New York (-> next session open), live first-seen events at their observation time.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

J = postgresql.JSONB(astext_type=sa.Text())
TS = sa.DateTime(timezone=True)

EVENT_COLS = [
    sa.Column('country', sa.String(2), server_default='US', nullable=False),
    sa.Column('exchange', sa.String(8)), sa.Column('security_id', sa.String(48)), sa.Column('issuer_id', sa.String(40)),
    sa.Column('event_family', sa.String(32)), sa.Column('accepted_at', TS), sa.Column('first_seen_at', TS),
    sa.Column('available_at', TS), sa.Column('source_document_id', sa.Text()), sa.Column('source_accession', sa.String(25)),
    sa.Column('parser_version', sa.String(32)), sa.Column('schema_version', sa.String(8), server_default='1', nullable=False),
    sa.Column('identity_confidence', sa.String(8)), sa.Column('event_confidence', sa.String(8)),
    sa.Column('needs_review', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('is_tradeable_at_event', sa.Boolean()), sa.Column('next_tradeable_at', TS),
    sa.Column('supersedes_event_id', sa.BigInteger()), sa.Column('episode_id', sa.String(64)),
    sa.Column('feature_source_event_ids', J), sa.Column('updated_at', TS),
]
HEALTH_COLS = [
    sa.Column('country', sa.String(2)), sa.Column('dataset', sa.String(48)), sa.Column('watermark', J),
    sa.Column('response_status', sa.Integer()), sa.Column('schema_fingerprint', sa.String(32)),
    sa.Column('expected_fingerprint', sa.String(32)), sa.Column('parser_version', sa.String(32)),
    sa.Column('latency_ms', sa.Integer()), sa.Column('health_status', sa.String(16), server_default='UNKNOWN', nullable=False),
    sa.Column('license_status', sa.String(24)),
]


def upgrade() -> None:
    for c in EVENT_COLS:
        op.add_column('events', c)
    op.create_index('ix_events_available_at', 'events', ['available_at'])
    op.create_index('ix_events_country_exchange_ticker', 'events', ['country', 'exchange', 'ticker', 'public_date'])
    op.execute("""UPDATE events SET
        first_seen_at = CASE WHEN is_backfill THEN NULL ELSE created_at END,
        accepted_at = CASE WHEN date_quality = 'exact' THEN public_at END,
        available_at = CASE
            WHEN date_quality = 'exact' AND public_at IS NOT NULL THEN public_at
            WHEN date_quality = 'first_seen' AND NOT is_backfill THEN created_at
            ELSE ((public_date + 1)::timestamp AT TIME ZONE 'America/New_York') - interval '1 second' END,
        event_confidence = CASE WHEN date_quality = 'estimated' THEN 'LOW' END""")
    for c in HEALTH_COLS:
        op.add_column('source_health', c)
    op.add_column('proposed_trades', sa.Column('signal_time', TS))
    op.add_column('proposed_trades', sa.Column('eligible_market_at', TS))
    for c in (sa.Column('fill_model', sa.String(8)), sa.Column('spread_bps', sa.Float()), sa.Column('participation', sa.Float()),
              sa.Column('adv_shares', sa.Float())):
        op.add_column('paper_orders', c)
    op.add_column('paper_accounts', sa.Column('book', sa.String(8), server_default='MAIN', nullable=False))

    op.create_table('global_securities',
        sa.Column('security_id', sa.String(48), primary_key=True), sa.Column('country', sa.String(2), nullable=False),
        sa.Column('exchange', sa.String(8)), sa.Column('issuer_id', sa.String(40), nullable=False),
        sa.Column('issuer_name', sa.Text(), nullable=False), sa.Column('cik', sa.String(10)), sa.Column('cusip', sa.String(9)),
        sa.Column('figi', sa.String(12)), sa.Column('composite_figi', sa.String(12)), sa.Column('isin', sa.String(12)),
        sa.Column('local_id', sa.String(40)), sa.Column('security_type', sa.String(16)), sa.Column('currency', sa.String(3)),
        sa.Column('listing_date', sa.Date()), sa.Column('delisting_date', sa.Date()),
        sa.Column('predecessor_security_id', sa.String(48)), sa.Column('successor_security_id', sa.String(48)),
        sa.Column('adr_underlying_security_id', sa.String(48)), sa.Column('primary_listing', sa.Boolean()),
        sa.Column('tradeability_status', sa.String(16), server_default='UNKNOWN', nullable=False),
        sa.Column('identity_confidence', sa.String(8), server_default='LOW', nullable=False),
        sa.Column('identity_evidence', J, server_default='[]', nullable=False),
        sa.Column('updated_at', TS, server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('security_id', name=op.f('pk_global_securities')))
    for c in ('cik', 'isin', 'figi'):
        op.create_index(f'ix_global_securities_{c}', 'global_securities', [c])
    op.create_table('security_symbol_periods',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('security_id', sa.String(48), sa.ForeignKey('global_securities.security_id', ondelete='CASCADE',
                  name='fk_security_symbol_periods_security_id_global_securities'), nullable=False),
        sa.Column('exchange', sa.String(8)), sa.Column('symbol', sa.String(16), nullable=False),
        sa.Column('valid_from', sa.Date()), sa.Column('valid_to', sa.Date()), sa.Column('source', sa.String(32), nullable=False),
        sa.Column('confidence', sa.String(8), server_default='MEDIUM', nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_security_symbol_periods')),
        sa.UniqueConstraint('security_id', 'symbol', 'valid_from', 'source', name='uq_security_symbol_periods_key'))
    op.create_index('ix_security_symbol_periods_lookup', 'security_symbol_periods', ['symbol', 'valid_from'])
    op.create_table('raw_documents',
        sa.Column('id', sa.BigInteger(), primary_key=True), sa.Column('country', sa.String(2), nullable=False),
        sa.Column('source', sa.String(48), nullable=False), sa.Column('doc_key', sa.Text(), nullable=False),
        sa.Column('url', sa.Text()), sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('storage_path', sa.Text(), nullable=False), sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('stored_bytes', sa.BigInteger(), nullable=False), sa.Column('content_type', sa.String(64)),
        sa.Column('retrieved_at', TS, nullable=False), sa.Column('parser_version', sa.String(32)),
        sa.Column('retention_class', sa.String(8), server_default='HOT', nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_raw_documents')),
        sa.UniqueConstraint('source', 'doc_key', 'sha256', name='uq_raw_documents_key'))
    op.create_index('ix_raw_documents_sha', 'raw_documents', ['sha256'])
    op.create_table('dataset_manifests',
        sa.Column('id', sa.BigInteger(), primary_key=True), sa.Column('dataset', sa.String(64), nullable=False),
        sa.Column('generated_at', TS, server_default=sa.text('now()'), nullable=False),
        sa.Column('schema_version', sa.String(16), nullable=False), sa.Column('parser_version', sa.String(32)),
        sa.Column('row_count', sa.BigInteger(), nullable=False), sa.Column('first_event', TS), sa.Column('last_event', TS),
        sa.Column('watermark', J, server_default='{}', nullable=False), sa.Column('source_health', J, server_default='{}', nullable=False),
        sa.Column('content_hash', sa.String(64)), sa.Column('export_path', sa.Text()), sa.Column('export_bytes', sa.BigInteger()),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_dataset_manifests')))
    op.create_index('ix_dataset_manifests_dataset', 'dataset_manifests', ['dataset', 'generated_at'])
    op.create_table('rate_limits',
        sa.Column('key', sa.String(48), primary_key=True), sa.Column('min_interval_s', sa.Float(), nullable=False),
        sa.Column('next_at', TS, server_default=sa.text('now()'), nullable=False), sa.Column('daily_limit', sa.Integer()),
        sa.Column('day', sa.Date()), sa.Column('used_today', sa.Integer(), server_default='0', nullable=False),
        sa.Column('hourly_limit', sa.Integer()), sa.Column('hour', TS), sa.Column('used_hour', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_429_at', TS), sa.Column('n_429', sa.Integer(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('key', name=op.f('pk_rate_limits')))
    op.create_table('source_daily_status',
        sa.Column('source', sa.String(48), nullable=False), sa.Column('day', sa.Date(), nullable=False),
        sa.Column('runs', sa.Integer(), server_default='0', nullable=False), sa.Column('failures', sa.Integer(), server_default='0', nullable=False),
        sa.Column('items', sa.Integer(), server_default='0', nullable=False), sa.Column('events', sa.Integer(), server_default='0', nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.PrimaryKeyConstraint('source', 'day', name=op.f('pk_source_daily_status')))
    op.create_table('event_market_snapshots',
        sa.Column('event_id', sa.BigInteger(), sa.ForeignKey('events.id', ondelete='CASCADE',
                  name='fk_event_market_snapshots_event_id_events'), nullable=False),
        sa.Column('captured_at', TS, nullable=False), sa.Column('symbol', sa.String(16)), sa.Column('price', sa.Float()),
        sa.Column('price_time', TS), sa.Column('bid', sa.Float()), sa.Column('ask', sa.Float()), sa.Column('volume', sa.Float()),
        sa.Column('avg_volume_20d', sa.Float()), sa.Column('market_cap', sa.Float()), sa.Column('shares_outstanding', sa.Float()),
        sa.Column('benchmark_symbol', sa.String(8)), sa.Column('benchmark_price', sa.Float()),
        sa.Column('sector_symbol', sa.String(8)), sa.Column('sector_price', sa.Float()), sa.Column('provider', sa.String(24)),
        sa.Column('lag_seconds', sa.Float()), sa.Column('status', sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint('event_id', name=op.f('pk_event_market_snapshots')))
    op.create_table('entity_edges',
        sa.Column('id', sa.BigInteger(), primary_key=True), sa.Column('src_type', sa.String(16), nullable=False),
        sa.Column('src_id', sa.String(64), nullable=False), sa.Column('rel', sa.String(32), nullable=False),
        sa.Column('dst_type', sa.String(16), nullable=False), sa.Column('dst_id', sa.String(64), nullable=False),
        sa.Column('valid_from', sa.Date()), sa.Column('valid_to', sa.Date()), sa.Column('source', sa.String(48), nullable=False),
        sa.Column('confidence', sa.String(8), nullable=False), sa.Column('evidence', J, server_default='{}', nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_entity_edges')),
        sa.UniqueConstraint('src_type', 'src_id', 'rel', 'dst_type', 'dst_id', 'source', name='uq_entity_edges_key'))
    op.create_index('ix_entity_edges_dst', 'entity_edges', ['dst_type', 'dst_id'])
    op.create_table('dataset_records',
        sa.Column('id', sa.BigInteger(), primary_key=True), sa.Column('country', sa.String(2), nullable=False),
        sa.Column('dataset', sa.String(48), nullable=False), sa.Column('record_key', sa.String(160), nullable=False),
        sa.Column('record_version', sa.String(64), server_default='', nullable=False), sa.Column('as_of_date', sa.Date()),
        sa.Column('available_at', TS, nullable=False), sa.Column('first_seen_at', TS, server_default=sa.text('now()'), nullable=False),
        sa.Column('issuer_cik', sa.String(10)), sa.Column('security_id', sa.String(48)), sa.Column('entity_name', sa.Text()),
        sa.Column('identity_confidence', sa.String(8)), sa.Column('payload', J, nullable=False), sa.Column('source_url', sa.Text()),
        sa.Column('raw_document_id', sa.BigInteger()), sa.Column('parser_version', sa.String(32), nullable=False),
        sa.Column('license_status', sa.String(24), nullable=False),
        sa.Column('needs_review', sa.Boolean(), server_default='false', nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_dataset_records')),
        sa.UniqueConstraint('dataset', 'record_key', 'record_version', name='uq_dataset_records_key'))
    op.create_index('ix_dataset_records_dataset_date', 'dataset_records', ['dataset', 'as_of_date'])
    op.create_index('ix_dataset_records_issuer', 'dataset_records', ['issuer_cik'])
    op.create_table('short_volume_daily',
        sa.Column('trade_date', sa.Date(), nullable=False), sa.Column('symbol', sa.String(16), nullable=False),
        sa.Column('short_volume', sa.BigInteger(), nullable=False),
        sa.Column('short_exempt_volume', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('total_volume', sa.BigInteger(), nullable=False), sa.Column('available_at', TS, nullable=False),
        sa.PrimaryKeyConstraint('trade_date', 'symbol', name=op.f('pk_short_volume_daily')))
    op.create_index('ix_short_volume_symbol', 'short_volume_daily', ['symbol', 'trade_date'])
    op.create_table('short_interest',
        sa.Column('country', sa.String(2), nullable=False), sa.Column('settlement_date', sa.Date(), nullable=False),
        sa.Column('symbol', sa.String(16), nullable=False), sa.Column('short_position', sa.BigInteger(), nullable=False),
        sa.Column('prev_short_position', sa.BigInteger()), sa.Column('avg_daily_volume', sa.Float()),
        sa.Column('days_to_cover', sa.Float()), sa.Column('source', sa.String(24), nullable=False),
        sa.Column('available_at', TS, nullable=False),
        sa.PrimaryKeyConstraint('country', 'settlement_date', 'symbol', name=op.f('pk_short_interest')))
    op.create_table('fails_to_deliver',
        sa.Column('settlement_date', sa.Date(), nullable=False), sa.Column('cusip', sa.String(9), nullable=False),
        sa.Column('symbol', sa.String(16)), sa.Column('quantity', sa.BigInteger(), nullable=False), sa.Column('price', sa.Float()),
        sa.Column('description', sa.Text()), sa.Column('source_file', sa.String(64), nullable=False),
        sa.Column('available_at', TS, nullable=False),
        sa.PrimaryKeyConstraint('settlement_date', 'cusip', name=op.f('pk_fails_to_deliver')))
    op.create_index('ix_ftd_symbol', 'fails_to_deliver', ['symbol', 'settlement_date'])
    op.create_table('inst_filings_13f',
        sa.Column('accession', sa.String(25), nullable=False), sa.Column('manager_cik', sa.String(10), nullable=False),
        sa.Column('manager_name', sa.Text()), sa.Column('period', sa.Date(), nullable=False),
        sa.Column('filed_date', sa.Date(), nullable=False), sa.Column('available_at', TS, nullable=False),
        sa.Column('submission_type', sa.String(12), nullable=False), sa.Column('amendment_type', sa.String(16)),
        sa.Column('entries', sa.Integer()), sa.Column('total_value', sa.Float()), sa.Column('source', sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint('accession', name=op.f('pk_inst_filings_13f')))
    op.create_index('ix_13f_manager_period', 'inst_filings_13f', ['manager_cik', 'period'])
    op.create_table('inst_holdings_13f',
        sa.Column('accession', sa.String(25), sa.ForeignKey('inst_filings_13f.accession', ondelete='CASCADE',
                  name='fk_inst_holdings_13f_accession_inst_filings_13f'), nullable=False),
        sa.Column('line', sa.Integer(), nullable=False), sa.Column('cusip', sa.String(9), nullable=False),
        sa.Column('issuer_name', sa.Text()), sa.Column('class_title', sa.Text()), sa.Column('value', sa.Float()),
        sa.Column('shares', sa.Float()), sa.Column('sh_prn', sa.String(3)), sa.Column('put_call', sa.String(4)),
        sa.Column('discretion', sa.String(8)),
        sa.PrimaryKeyConstraint('accession', 'line', name=op.f('pk_inst_holdings_13f')))
    op.create_index('ix_13f_holdings_cusip', 'inst_holdings_13f', ['cusip'])
    op.create_table('inst_cusip_quarter_13f',
        sa.Column('cusip', sa.String(9), nullable=False), sa.Column('period', sa.Date(), nullable=False),
        sa.Column('holders', sa.Integer(), nullable=False), sa.Column('shares', sa.Float()), sa.Column('value', sa.Float()),
        sa.Column('new_holders', sa.Integer()), sa.Column('exited_holders', sa.Integer()),
        sa.Column('increased_holders', sa.Integer()), sa.Column('decreased_holders', sa.Integer()),
        sa.Column('available_at', TS, nullable=False),
        sa.PrimaryKeyConstraint('cusip', 'period', name=op.f('pk_inst_cusip_quarter_13f')))


def downgrade() -> None:
    for t in ('inst_cusip_quarter_13f', 'inst_holdings_13f', 'inst_filings_13f', 'fails_to_deliver', 'short_interest',
              'short_volume_daily', 'dataset_records', 'entity_edges', 'event_market_snapshots', 'source_daily_status',
              'rate_limits', 'dataset_manifests', 'raw_documents', 'security_symbol_periods', 'global_securities'):
        op.drop_table(t)
    op.drop_column('paper_accounts', 'book')
    for c in ('fill_model', 'spread_bps', 'participation', 'adv_shares'):
        op.drop_column('paper_orders', c)
    for c in ('signal_time', 'eligible_market_at'):
        op.drop_column('proposed_trades', c)
    for c in HEALTH_COLS:
        op.drop_column('source_health', c.name)
    op.drop_index('ix_events_country_exchange_ticker', table_name='events')
    op.drop_index('ix_events_available_at', table_name='events')
    for c in EVENT_COLS:
        op.drop_column('events', c.name)
