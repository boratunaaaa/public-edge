"""Return queue: deterministic next-eligible time per event + identity-scoped, interval-bounded provider absences.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-29

Additive only: two nullable columns (returns_next_at, returns_lease) (NULL keeps the pre-0013 rule, returns_checked_at + recheck interval), one index and
one new table. No data is rewritten here; scripts/pe_return_repair.py sets returns_next_at for existing partial events
(dry-run by default).

Locking (events has ~319k rows and is written continuously by worker / backfill):
  * ADD COLUMN without a default is a catalog-only change, but it needs a brief ACCESS EXCLUSIVE lock. lock_timeout
    (5 s) makes it fail fast instead of queueing behind a long transaction and blocking every writer behind it.
  * The index is built with CREATE INDEX CONCURRENTLY in an autocommit block (no write lock; it cannot run inside a
    transaction).

Restart safety: entering the autocommit block COMMITS the column and the table before the concurrent build. If the
build fails or is interrupted, alembic stays at 0012 with those objects already present, and a failed concurrent build
leaves an INVALID index. Every step is therefore idempotent - ADD COLUMN / CREATE TABLE IF NOT EXISTS, and the index
is dropped (IF EXISTS, concurrently) before it is built - so simply re-running `alembic upgrade head` completes the
migration from any partial state (tested: tests/test_return_queue_audit.py).
"""
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS returns_next_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS returns_lease VARCHAR(32)")  # vendor-lane claim token
    op.execute("""
        CREATE TABLE IF NOT EXISTS price_absences (
            provider VARCHAR(16) NOT NULL,
            identity VARCHAR(64) NOT NULL,
            market VARCHAR(8) NOT NULL,
            symbol VARCHAR(16) NOT NULL,
            window_start DATE NOT NULL,
            window_end DATE NOT NULL,
            checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
            hits INTEGER DEFAULT '1' NOT NULL,
            PRIMARY KEY (provider, identity, market, symbol, window_start, window_end)
        )""")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = 0")  # CONCURRENTLY waits for old transactions by design; it never blocks writes
        # a previous failed / interrupted concurrent build leaves an INVALID index of this name
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_returns_queue")
        op.execute("CREATE INDEX CONCURRENTLY ix_events_returns_queue ON events (returns_status, returns_next_at)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_returns_queue")
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS price_absences")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS returns_lease")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS returns_next_at")
