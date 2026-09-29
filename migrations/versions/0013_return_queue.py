"""Return queue: deterministic next-eligible time per event + confirmed provider absences.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-29

Additive only: one nullable column (NULL keeps the pre-0013 rule, returns_checked_at + recheck interval) and one
new table. No data is rewritten here; scripts/pe_return_repair.py sets returns_next_at for existing partial events
(dry-run by default).
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column('events', sa.Column('returns_next_at', TS))
    op.create_index('ix_events_returns_queue', 'events', ['returns_status', 'returns_next_at'])
    op.create_table(
        'price_absences',
        sa.Column('provider', sa.String(16), primary_key=True),
        sa.Column('symbol', sa.String(16), primary_key=True),
        sa.Column('checked_at', TS, nullable=False),
        sa.Column('hits', sa.Integer(), server_default='1', nullable=False),
    )


def downgrade() -> None:
    op.drop_table('price_absences')
    op.drop_index('ix_events_returns_queue', table_name='events')
    op.drop_column('events', 'returns_next_at')
