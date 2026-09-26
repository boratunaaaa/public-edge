"""corporate_action_resolution

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('corporate_action_resolution',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('security_id', sa.String(length=32), nullable=False),
    sa.Column('action_type', sa.String(length=24), nullable=False),
    sa.Column('announcement_date', sa.Date(), nullable=True),
    sa.Column('effective_date', sa.Date(), nullable=False),
    sa.Column('predecessor_cik', sa.String(length=10), nullable=False),
    sa.Column('successor_cik', sa.String(length=10), nullable=True),
    sa.Column('predecessor_ticker', sa.String(length=16), nullable=True),
    sa.Column('successor_ticker', sa.String(length=16), nullable=True),
    sa.Column('cash_component', sa.Numeric(18, 6), nullable=True),
    sa.Column('share_ratio', sa.Numeric(18, 8), nullable=True),
    sa.Column('currency', sa.String(length=3), server_default='USD', nullable=False),
    sa.Column('source_accession', sa.String(length=25), nullable=True),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('validation_status', sa.String(length=20), nullable=False),
    sa.Column('complexity', sa.String(length=32), server_default='SIMPLE', nullable=False),
    sa.Column('validation_version', sa.String(length=40), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_corporate_action_resolution')),
    sa.UniqueConstraint('predecessor_cik', 'effective_date', name='uq_corporate_action_resolution_pred_eff')
    )
    op.create_index(op.f('ix_corporate_action_resolution_predecessor_cik'), 'corporate_action_resolution', ['predecessor_cik'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_corporate_action_resolution_predecessor_cik'), table_name='corporate_action_resolution')
    op.drop_table('corporate_action_resolution')
