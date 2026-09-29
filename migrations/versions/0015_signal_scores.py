"""Research-only investment score V1 (app.scoring.engine): signal_scores, one row per event and model version.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-29

Additive only (a new table + index); nothing existing is read or changed by it, and old code ignores it.
"""
from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS signal_scores (
            event_id BIGINT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
            model_version VARCHAR(32) NOT NULL,
            evidence_version VARCHAR(64) NOT NULL,
            scored_at TIMESTAMP WITH TIME ZONE NOT NULL,
            score_pp DOUBLE PRECISION NOT NULL,
            ci_low_pp DOUBLE PRECISION NOT NULL,
            cell VARCHAR(64) NOT NULL,
            components JSONB NOT NULL,
            vetoes JSONB DEFAULT '[]' NOT NULL,
            research_only BOOLEAN DEFAULT 'true' NOT NULL,
            PRIMARY KEY (event_id, model_version)
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_signal_scores_scored_at ON signal_scores (scored_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS signal_scores")
