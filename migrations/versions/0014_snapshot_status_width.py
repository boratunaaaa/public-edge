"""event_market_snapshots.status VARCHAR(16) -> VARCHAR(24): the documented status IDENTITY_UNRESOLVED has 19 characters.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-29

Since 2026-09-28 13:29 one live event whose symbol could not be resolved failed every capture with
StringDataRightTruncation and blocked all later snapshots. Widening a VARCHAR is a catalog-only change in PostgreSQL
(no table rewrite, existing rows unchanged); it needs a brief ACCESS EXCLUSIVE lock, so lock_timeout makes it fail fast
instead of queueing writers (re-run to retry). Downgrade narrows back and fails loudly if a longer value exists.
"""
from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE event_market_snapshots ALTER COLUMN status TYPE VARCHAR(24)")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE event_market_snapshots ALTER COLUMN status TYPE VARCHAR(16)")
