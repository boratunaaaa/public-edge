"""V2 API: health / provenance / signals / manifests answer from the database."""
from datetime import date

import pytest


@pytest.fixture
def client(engine, clean, monkeypatch):
    from fastapi.testclient import TestClient
    import app.api.main as M
    monkeypatch.setattr(M, "get_engine", lambda: engine)
    return TestClient(M.app)


def test_v2_endpoints(client, engine):
    from app.db.store import create_event
    with engine.begin() as c:
        a = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key="A", public_date=date(2026, 1, 5), title="a",
                         payload={}, source_url="https://www.sec.gov/a", is_backfill=True)
        m = create_event(c, event_type="MULTI_SIGNAL_EVENT", dedupe_key="M", public_date=date(2026, 1, 6), title="m",
                         payload={}, source_url="https://www.sec.gov/m", is_backfill=True, feature_source_event_ids=[a])
    h = client.get("/api/v2/health").json()
    assert "quotas" in h and "disk" in h
    p = client.get(f"/api/v2/provenance/{m}").json()
    assert p["components"][0]["source_url"] == "https://www.sec.gov/a" and p["event"]["available_at"]
    assert client.get("/api/v2/provenance/999999").status_code == 404
    s = client.get("/api/v2/signals").json()
    assert any(f["signal_id"] == "US_FTD_SPIKE" for f in s["families"])
    assert client.get("/api/v2/manifests").json() == {"manifests": []}
