"""Forward market snapshots: every documented status fits the table (0014) and one failing event never blocks the
later events of the capture window."""
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def _live_event(c, key, ticker, cik, minutes_ago):
    from app.db.store import create_event
    now = datetime.now(timezone.utc)
    eid = create_event(c, event_type="ACTIVIST_13D", dedupe_key=key, public_date=now.date(), ticker=ticker, issuer_cik=cik,
                       title="t", payload={}, source_url="u", public_at=now - timedelta(minutes=minutes_ago), is_backfill=False)
    c.execute(text("UPDATE events SET available_at = :a WHERE id = :i"), {"a": now - timedelta(minutes=minutes_ago), "i": eid})
    return eid


@db
@pytest.mark.usefixtures("clean")
def test_an_unresolved_identity_is_stored_as_identity_unresolved(engine, monkeypatch):
    from app.core import snapshots
    from app.securitymaster import provider as sm
    with engine.begin() as c:
        eid = _live_event(c, "dbhl", "DBHL", "0002031283", 30)
    monkeypatch.setattr(sm.SecSecurityMaster, "price_symbol", lambda self, cik, on, ticker=None: (None, None, None))
    assert snapshots.capture_pending(engine) == {"IDENTITY_UNRESOLVED": 1}
    with engine.connect() as c:
        assert c.execute(text("SELECT status, symbol FROM event_market_snapshots WHERE event_id = :i"),
                         {"i": eid}).one() == ("IDENTITY_UNRESOLVED", None)


@db
@pytest.mark.usefixtures("clean")
def test_one_failing_event_does_not_block_the_rest_of_the_window(engine, monkeypatch):
    from app.core import snapshots
    with engine.begin() as c:
        bad = _live_event(c, "bad", "ZBIO", "0001953926", 60)  # oldest: first in the window
        good = _live_event(c, "good", "OPRA", "0001737450", 30)
    def capture(conn, ev, now=None):
        if ev["id"] == bad:
            raise RuntimeError("boom")
        return "NO_QUOTE"
    monkeypatch.setattr(snapshots, "capture", capture)
    assert snapshots.capture_pending(engine) == {"FAILED": 1, "NO_QUOTE": 1}  # `good` was still captured
    assert good != bad


@db
def test_migration_0014_widens_the_status_column_and_round_trips():
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    name = "pe_mig_0014"
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    url = URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    q = ("SELECT character_maximum_length FROM information_schema.columns "
         "WHERE table_name = 'event_market_snapshots' AND column_name = 'status'")
    try:
        widths = []
        for args in (["upgrade", "head"], ["downgrade", "0013"], ["upgrade", "head"]):
            p = subprocess.run(["alembic", *args], cwd=root, env=env, capture_output=True, text=True, timeout=300)
            assert p.returncode == 0, p.stderr[-2000:]
            eng = create_engine(url)
            with eng.connect() as c:
                widths.append((c.execute(text("SELECT version_num FROM alembic_version")).scalar(), c.execute(text(q)).scalar()))
            eng.dispose()
        assert widths == [("0014", 24), ("0013", 16), ("0014", 24)]
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
