"""government_awards.amount_basis varchar(24) -> varchar(32)

'current_total_at_backfill' is 25 characters: every historical USAspending insert failed.
Widening a varchar is a metadata-only change in PostgreSQL (no rewrite, no data change).

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("government_awards", "amount_basis", type_=sa.String(32), existing_type=sa.String(24))


def downgrade() -> None:
    op.alter_column("government_awards", "amount_basis", type_=sa.String(24), existing_type=sa.String(32))
