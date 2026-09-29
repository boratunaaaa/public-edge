"""Investment scoring V1: evidence-only weights, point-in-time adjustments, kept apart from the gate, research-only."""
import os
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text

from tests.test_alpha_validation import _bars, _event, _serve, _universe, offline_prices  # noqa: F401 - fixture
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
NOW = datetime(2024, 3, 1, 15, tzinfo=timezone.utc)


# ------------------------------------------------------------------ evidence registry

def test_benjamini_hochberg_step_up():
    from app.scoring.evidence import bh_survivors
    assert bh_survivors({"a": 0.001, "b": 0.02, "c": 0.04, "d": 0.5}) == {"a", "b"}  # 0.02 <= 0.05*2/4, 0.04 > 0.05*3/4
    assert bh_survivors({"a": 0.03, "b": 0.04, "c": 0.9}) == set()


def test_only_measured_significant_effects_get_a_weight():
    from app.scoring.evidence import NOT_SIGNIFICANT, VALIDATED, registry
    reg = registry()
    assert reg["INSIDER_BUY"].status == VALIDATED and reg["INSIDER_BUY"].effect == pytest.approx(0.0189, abs=1e-3)
    validated = {k for k, v in reg["INSIDER_BUY_BUCKETS"].items() if v.usable}
    assert validated == {"role_10pct_only"}  # the one bucket surviving BH across the 11 insider buckets
    assert reg["INSIDER_BUY_BUCKETS"]["value_100k_1m"].status == NOT_SIGNIFICANT  # p 0.03 alone is not enough
    assert reg["INSIDER_BUY_BUCKETS"]["value_100k_1m"].effect == 0.0
    assert reg["INSIDER_CLUSTER"].status == NOT_SIGNIFICANT and reg["INSIDER_CLUSTER"].effect == 0.0
    tk = reg["TENK_CHANGE_RECENT"]
    assert tk.status == VALIDATED and tk.effect == pytest.approx(-0.036, abs=1e-3)  # the smaller of the two directions


def test_every_declared_family_has_a_status_and_unvalidated_ones_weigh_zero():
    from app.core.signal_registry import FAMILIES as V2
    from app.paper.alpha import FAMILIES as V1
    from app.scoring.engine import family_status
    rows = {r["family"]: r for r in family_status()}
    assert set(V1) | {f.signal_id for f in V2} <= set(rows)
    weighted = {k for k, r in rows.items() if r["weight_pp"]}
    assert weighted == {"INSIDER_BUY", "TENK_CHANGE_RECENT (risk adjustment)"}
    assert all(r["weight_pp"] == 0.0 for k, r in rows.items() if k.startswith(("US_", "TR_", "CA_")))


# ------------------------------------------------------------------ scoring one event

def _score(engine, payload, et="INSIDER_OPEN_MARKET_BUY", now=NOW, tenk_at=None, cik="0001953926"):
    from app.scoring.engine import score_event
    with engine.begin() as c:
        if tenk_at is not None:
            tid = _event(c, et="TENK_CHANGE", d=tenk_at.date(), key=f"tk{cik}", payload={"direction": "POSITIVE"}, cik=cik)
            c.execute(text("UPDATE events SET available_at = :a WHERE id = :i"), {"a": tenk_at, "i": tid})
        eid = _event(c, et=et, payload=payload, key=f"e{et}{len(str(payload))}{cik}", cik=cik)
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": eid}).mappings().one())
        return score_event(c, ev, now)


@db
@pytest.mark.usefixtures("clean")
def test_a_discretionary_insider_buy_gets_the_validated_base_edge(engine):
    s = _score(engine, {"is_ceo": True, "value": 50_000, "holdings_increase": 0.05})
    assert s["cell"] == "INSIDER_BUY" and s["score_pp"] == pytest.approx(1.893, abs=0.01)
    assert s["ci_low_pp"] < s["score_pp"]
    ceo = next(c for c in s["components"] if c["component"] == "INSIDER_BUY:role_ceo")
    assert ceo["effect_pp"] == 0.0 and ceo["status"] == "NOT_SIGNIFICANT"  # matched, but not validated: no weight


@db
@pytest.mark.usefixtures("clean")
def test_a_ten_percent_owner_buy_uses_the_more_specific_validated_cell(engine):
    s = _score(engine, {"is_ten_pct": True, "value": 2_000_000})
    assert s["cell"] == "INSIDER_BUY:role_10pct_only" and s["score_pp"] == pytest.approx(8.25, abs=0.01)


@db
@pytest.mark.usefixtures("clean")
def test_planned_10b5_1_buys_and_unvalidated_families_score_zero(engine):
    s = _score(engine, {"is_ceo": True, "value": 5e6, "is_10b5_1": True})
    assert s["score_pp"] == 0.0 and s["cell"].startswith("OUTSIDE_EVIDENCE")
    for et in ("CLUSTER_INSIDER_BUY", "ACTIVIST_13D", "GOV_CONTRACT", "MATERIAL_NEWS", "CONGRESS_BUY", "INST_13F_NEW_POSITION"):
        s = _score(engine, {"n": et}, et=et)
        assert s["score_pp"] == 0.0 and s["cell"] == "NO_VALIDATED_CELL", et
        assert all(c["effect_pp"] == 0.0 for c in s["components"])


@db
@pytest.mark.usefixtures("clean")
def test_the_10k_adjustment_is_point_in_time_and_windowed(engine):
    base = {"is_ceo": True, "value": 50_000}
    recent = _score(engine, base, tenk_at=NOW - timedelta(days=20), cik="0000000011")
    assert recent["score_pp"] == pytest.approx(1.893 - 3.601, abs=0.02)
    later = _score(engine, base, tenk_at=NOW + timedelta(days=5), cik="0000000012")  # public after the scoring moment
    assert later["score_pp"] == pytest.approx(1.893, abs=0.01)
    stale = _score(engine, base, tenk_at=NOW - timedelta(days=120), cik="0000000013")  # outside the measured window
    assert stale["score_pp"] == pytest.approx(1.893, abs=0.01)


# ------------------------------------------------------------------ kept apart from the gate, research-only

def _live_setup(engine, monkeypatch):
    monkeypatch.setenv("PAPER_RESEARCH_MODE", "1")
    today = date.today()
    with engine.begin() as c:
        _universe(c)
        zb = _bars(c, "ZBIO", today - timedelta(days=90), 60, 10.0, 0.01)
        spy = _bars(c, "SPY", today - timedelta(days=90), 60, 400.0, 0.5)
        eid = _event(c, d=today - timedelta(days=1), payload={"is_ten_pct": True, "value": 2e6})
    _serve(monkeypatch, ZBIO=zb, SPY=spy)
    return eid


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_scan_records_the_score_after_the_gate_and_changes_nothing(engine, monkeypatch):
    from app.db.models import proposed_trades, signal_scores
    from app.paper.proposals import scan_new_events
    eid = _live_setup(engine, monkeypatch)
    out = scan_new_events(engine, days=5)
    assert out["created"] == 1 and out["scored"] == 1
    with engine.connect() as c:
        p = c.execute(select(proposed_trades)).mappings().one()
        s = c.execute(select(signal_scores)).mappings().one()
    assert p["research_only"] and "score" not in (p["gate"] or {})  # the proposal is the gate's alone
    assert p["max_position_pct"] == 0.05 and p["suggested_position_pct"] == 0.03  # sizing untouched by a +8 pp score
    assert s["event_id"] == eid and s["research_only"] and s["vetoes"] == [] and s["score_pp"] == pytest.approx(8.25, abs=0.01)
    assert scan_new_events(engine, days=5)["checked"] == 0  # already proposed: nothing re-scored twice


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_scoring_failure_never_stops_a_proposal(engine, monkeypatch):
    from app.paper.proposals import scan_new_events
    from app.scoring import engine as se
    _live_setup(engine, monkeypatch)
    monkeypatch.setattr(se, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = scan_new_events(engine, days=5)
    assert out["created"] == 1 and out["score_errors"] == 1


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_vetoes_are_stored_apart_from_the_score(engine, monkeypatch):
    from app.db.models import signal_scores
    from app.paper.proposals import scan_new_events
    _live_setup(engine, monkeypatch)
    with engine.begin() as c:
        c.execute(text("INSERT INTO discovery_risks (cik, risk, severity) VALUES ('0001953926', 'GOING_CONCERN', 'HIGH')"))
    out = scan_new_events(engine, days=5)
    assert out["created"] == 0 and out["rejected"].get("SEVERE_FINANCING_RISK") == 1
    with engine.connect() as c:
        s = c.execute(select(signal_scores)).mappings().one()
    assert "SEVERE_FINANCING_RISK" in s["vetoes"] and s["score_pp"] == pytest.approx(8.25, abs=0.01)  # score is not a veto


def test_scoring_is_never_read_by_the_gate_or_the_ledger():
    import inspect

    from app.paper import ledger, proposals
    assert "scoring" not in inspect.getsource(proposals.evaluate_gate)
    assert "scoring" not in inspect.getsource(proposals.size_limits)
    assert "scoring" not in inspect.getsource(ledger)


@db
def test_migration_0015_round_trip():
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    name = "pe_mig_0015"
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    url = URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        seen = []
        for args in (["upgrade", "head"], ["downgrade", "0014"], ["upgrade", "head"]):
            p = subprocess.run(["alembic", *args], cwd=root, env=env, capture_output=True, text=True, timeout=300)
            assert p.returncode == 0, p.stderr[-2000:]
            eng = create_engine(url)
            with eng.connect() as c:
                seen.append((c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
                             c.execute(text("SELECT to_regclass('public.signal_scores') IS NOT NULL")).scalar()))
            eng.dispose()
        assert seen == [("0015", True), ("0014", False), ("0015", True)]
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
