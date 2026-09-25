"""Shared PostgreSQL fixtures for the integration tests (TEST_DATABASE_URL)."""
import os

import pytest
from sqlalchemy import create_engine, text

from app.db.models import metadata

URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def engine():
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    eng = create_engine(URL)
    metadata.drop_all(eng)
    metadata.create_all(eng)
    yield eng
    metadata.drop_all(eng)


@pytest.fixture
def no_network_charts(monkeypatch):
    """Alert charts would fetch live prices; integration tests never touch the network."""
    import app.notifications.chart as chart
    monkeypatch.setattr(chart, "price_chart", lambda *a, **k: None)


@pytest.fixture
def clean(engine):
    with engine.begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(t.name for t in metadata.sorted_tables) + " RESTART IDENTITY CASCADE"))
        from app.entities.registry import resolver, sync_registry
        sync_registry(c, [
            {"cik": "0001953926", "name": "Zenas BioPharma, Inc.", "ticker": "ZBIO", "exchange": "Nasdaq"},
            {"cik": "0001737450", "name": "Opera Ltd", "ticker": "OPRA", "exchange": "Nasdaq"},
            {"cik": "0001846069", "name": "Nextdoor Holdings, Inc.", "ticker": "KIND", "exchange": "NYSE"},
            {"cik": "0000077543", "name": "Tutor Perini Corp", "ticker": "TPC", "exchange": "NYSE"},
        ])
        resolver.load(c, force=True)
    yield
