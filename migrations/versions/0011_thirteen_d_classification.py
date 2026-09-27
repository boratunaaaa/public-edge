"""thirteen_d_classification

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('thirteen_d_classification',
    sa.Column('accession', sa.String(length=25), nullable=False),
    sa.Column('issuer_cik', sa.String(length=10), nullable=True),
    sa.Column('investor_key', sa.String(length=160), nullable=True),
    sa.Column('filing_class', sa.String(length=32), nullable=False),
    sa.Column('confidence', sa.String(length=8), nullable=False),
    sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('flags', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('version', sa.String(length=24), nullable=False),
    sa.Column('classified_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('accession', name=op.f('pk_thirteen_d_classification'))
    )
    for col in ('issuer_cik', 'investor_key', 'filing_class'):
        op.create_index(op.f(f'ix_thirteen_d_classification_{col}'), 'thirteen_d_classification', [col], unique=False)


def downgrade() -> None:
    for col in ('issuer_cik', 'investor_key', 'filing_class'):
        op.drop_index(op.f(f'ix_thirteen_d_classification_{col}'), table_name='thirteen_d_classification')
    op.drop_table('thirteen_d_classification')
