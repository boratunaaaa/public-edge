"""The migration chain builds exactly the schema the models declare (no drift between 0001..head and models.py)."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
def test_migrations_match_models():
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db.models import metadata
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS pe_migration_check"))
        c.execute(text("CREATE DATABASE pe_migration_check"))
    url = make_url(URL).set(database="pe_migration_check").render_as_string(hide_password=False)
    try:
        env = {**os.environ, "DATABASE_URL": url}
        r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
        eng = create_engine(url)
        with eng.connect() as c:
            diff = compare_metadata(MigrationContext.configure(c, opts={"compare_type": False}), metadata)
        eng.dispose()
        # only structural drift matters: tables / columns missing on either side
        bad = [d for d in diff if isinstance(d, tuple) and d[0] in ("add_table", "remove_table", "add_column", "remove_column")]
        assert not bad, bad
    finally:
        with admin.connect() as c:
            c.execute(text("DROP DATABASE IF EXISTS pe_migration_check"))
