"""activist_reporting_persons

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('activist_reporting_persons',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('accession', sa.String(length=25), nullable=False),
    sa.Column('issuer_cik', sa.String(length=10), nullable=True),
    sa.Column('group_id', sa.String(length=40), nullable=True),
    sa.Column('person_index', sa.Integer(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('name_norm', sa.Text(), nullable=False),
    sa.Column('cik', sa.String(length=10), nullable=True),
    sa.Column('identity_key', sa.String(length=160), nullable=False),
    sa.Column('kind', sa.String(length=12), server_default='UNKNOWN', nullable=False),
    sa.Column('type_codes', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('percent', sa.Float(), nullable=True),
    sa.Column('is_lead', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('confidence', sa.String(length=12), nullable=False),
    sa.Column('cik_evidence', sa.Text(), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_activist_reporting_persons')),
    sa.UniqueConstraint('accession', 'person_index', name='uq_activist_reporting_persons_acc_idx')
    )
    for col in ('accession', 'issuer_cik', 'cik', 'identity_key'):
        op.create_index(op.f(f'ix_activist_reporting_persons_{col}'), 'activist_reporting_persons', [col], unique=False)


def downgrade() -> None:
    for col in ('accession', 'issuer_cik', 'cik', 'identity_key'):
        op.drop_index(op.f(f'ix_activist_reporting_persons_{col}'), table_name='activist_reporting_persons')
    op.drop_table('activist_reporting_persons')
