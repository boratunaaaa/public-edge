"""Return queue: deterministic next-eligible time per event + identity-scoped confirmed provider absences.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-29

Additive only: one nullable column (NULL keeps the pre-0013 rule, returns_checked_at + recheck interval), one index and
one new table. No data is rewritten here; scripts/pe_return_repair.py sets returns_next_at for existing partial events
(dry-run by default).

Locking (events has ~319k rows and is written continuously by worker / backfill):
  * ADD COLUMN without a default is a catalog-only change, but it needs a brief ACCESS EXCLUSIVE lock. lock_timeout
    (5 s) makes it fail fast instead of queueing behind a long transaction and blocking every writer behind it; the
    migration is simply re-run.
  * The index is built with CREATE INDEX CONCURRENTLY in an autocommit block (no write lock; it cannot run inside a
    transaction). A failed concurrent build leaves an INVALID index - it is dropped IF EXISTS first, so a re-run is
    clean. Downgrade drops it CONCURRENTLY too.
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.add_column('events', sa.Column('returns_next_at', TS))
    op.create_table(
        'price_absences',
        sa.Column('provider', sa.String(16), primary_key=True),
        sa.Column('identity', sa.String(48), primary_key=True),
        sa.Column('market', sa.String(8), primary_key=True),
        sa.Column('symbol', sa.String(16), primary_key=True),
        sa.Column('window_start', sa.Date(), primary_key=True),
        sa.Column('checked_at', TS, nullable=False),
        sa.Column('hits', sa.Integer(), server_default='1', nullable=False),
    )
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = 0")  # CONCURRENTLY waits for old transactions by design; it never blocks writes
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_returns_queue")
        op.create_index('ix_events_returns_queue', 'events', ['returns_status', 'returns_next_at'],
                        postgresql_concurrently=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_returns_queue")
    op.execute("SET lock_timeout = '5s'")
    op.drop_table('price_absences')
    op.drop_column('events', 'returns_next_at')
