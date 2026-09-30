"""Point-in-time sources for historical replay: pit_facts (XBRL cash / OCF / debt / shares with the disclosing filing),
sec_filings (complete SEC submissions history with acceptance times), pit_backfill_progress (resumable jobs).

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-01

Additive only: three new tables and their indexes; nothing existing is read or changed, and no live decision reads
them (replay / research only). lock_timeout keeps the DDL from queueing behind writers.
"""
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS pit_facts (
            id BIGSERIAL PRIMARY KEY,
            cik VARCHAR(10) NOT NULL,
            family VARCHAR(16) NOT NULL,
            concept VARCHAR(96) NOT NULL,
            period_start DATE,
            period_end DATE NOT NULL,
            val DOUBLE PRECISION NOT NULL,
            filed DATE NOT NULL,
            accession VARCHAR(25) NOT NULL,
            form VARCHAR(16),
            run_id VARCHAR(40) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL
        )""")
    # one row per (issuer, concept, period, filing): a re-run never duplicates (instants have no start: NULLS NOT DISTINCT)
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_pit_facts_fact ON pit_facts
                  (cik, concept, period_end, period_start, accession, val) NULLS NOT DISTINCT""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_pit_facts_cik_family ON pit_facts (cik, family, filed)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS sec_filings (
            cik VARCHAR(10) NOT NULL,
            accession VARCHAR(25) NOT NULL,
            form VARCHAR(24) NOT NULL,
            filing_date DATE NOT NULL,
            report_date DATE,
            acceptance_at TIMESTAMP WITH TIME ZONE,
            items TEXT,
            primary_document TEXT,
            source_block VARCHAR(64) NOT NULL,
            run_id VARCHAR(40) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (cik, accession)
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sec_filings_cik_date ON sec_filings (cik, filing_date)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS pit_backfill_progress (
            job VARCHAR(24) NOT NULL,
            cik VARCHAR(10) NOT NULL,
            status VARCHAR(12) NOT NULL,
            rows INTEGER DEFAULT 0 NOT NULL,
            attempts INTEGER DEFAULT 0 NOT NULL,
            error TEXT,
            run_id VARCHAR(40) NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (job, cik)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pit_backfill_progress")
    op.execute("DROP TABLE IF EXISTS sec_filings")
    op.execute("DROP TABLE IF EXISTS pit_facts")
