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


def test_only_ten_pct_any_carries_a_weight_in_evidence_v3():
    from app.scoring.evidence import EVIDENCE_ONLY, HOLDOUT_REQUIRED, NOT_ROBUST, NOT_SIGNIFICANT, VETO, registry
    reg = registry()
    base = reg["INSIDER_BUY"]  # significant at 60 days, but +0.5 pp (p 0.56) with a 90-day lookback: not robust
    assert base.status == NOT_ROBUST and base.effect == 0.0 and "lookback90_family" in base.why
    assert base.measured == pytest.approx(0.0201, abs=1e-3)
    b = reg["INSIDER_BUY_BUCKETS"]
    assert {k: round(v.effect, 4) for k, v in b.items() if v.usable} == {"ten_pct_any": 0.0502}
    assert b["role_10pct_only"].status == EVIDENCE_ONLY and b["role_10pct_only"].measured == pytest.approx(0.0752, abs=1e-3)
    holdout = {k for k, v in b.items() if v.status == HOLDOUT_REQUIRED}
    assert holdout == {"serial_3plus_issuers_2y", "owner_history_negative", "indirect_only", "first_buy_same_issuer_5y",
                       "value_100k_1m", "incr_10_50pct"}
    assert all(b[k].effect == 0.0 for k in holdout)
    assert b["role_ceo"].status == NOT_SIGNIFICANT and b["role_ceo"].effect == 0.0
    assert reg["INSIDER_CLUSTER"].status == NOT_SIGNIFICANT and reg["INSIDER_CLUSTER"].effect == 0.0
    tk = reg["TENK_NEGATIVE_RECENT"]  # a risk: a veto, never a weight
    assert tk.status == VETO and tk.effect == 0.0 and tk.measured < 0


def test_an_active_label_never_overrides_the_numbers():
    from app.scoring.evidence import NOT_SIGNIFICANT, VALIDATED, _bucket
    ok = {"role": "ACTIVE", "companies": 500, "effect": 0.05, "bh_p": 0.01, "boot_ci95": [0.01, 0.09]}
    assert _bucket("x", ok, "v").status == VALIDATED
    for bad in ({"companies": 80}, {"bh_p": 0.06}, {"boot_ci95": [-0.001, 0.09]}):
        e = _bucket("x", {**ok, **bad}, "v")
        assert e.status == NOT_SIGNIFICANT and e.effect == 0.0, bad


def test_the_evidence_file_is_reproducible_from_the_committed_research_outputs():
    import json
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    try:
        from build_scoring_evidence_v3 import OUT, build
    finally:
        sys.path.pop(0)
    assert json.loads(json.dumps(build(), sort_keys=True, default=str)) == json.loads(OUT.read_text())


def test_every_declared_family_has_a_status_and_unvalidated_ones_weigh_zero():
    from app.core.signal_registry import FAMILIES as V2
    from app.paper.alpha import FAMILIES as V1
    from app.scoring.engine import family_status
    rows = {r["family"]: r for r in family_status()}
    assert set(V1) | {f.signal_id for f in V2} <= set(rows)
    assert {k for k, r in rows.items() if r["weight_pp"]} == set()  # no family-level weight
    assert rows["INSIDER_BUY"]["buckets_validated"] == {"ten_pct_any": 5.02}
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
def test_a_plain_insider_buy_scores_zero_because_the_base_effect_is_not_robust(engine):
    s = _score(engine, {"is_ceo": True, "value": 50_000, "holdings_increase": 0.05})
    assert s["cell"] == "NO_VALIDATED_CELL" and s["score_pp"] == 0.0 and s["vetoes"] == []
    base = next(c for c in s["components"] if c["component"] == "INSIDER_BUY")
    assert base["status"] == "NOT_ROBUST" and base["effect_pp"] == 0.0
    ceo = next(c for c in s["components"] if c["component"] == "INSIDER_BUY:role_ceo")
    assert ceo["effect_pp"] == 0.0 and ceo["status"] == "NOT_SIGNIFICANT"  # matched, but not validated: no weight


@db
@pytest.mark.usefixtures("clean")
def test_a_ten_percent_owner_gets_ten_pct_any_once_never_stacked(engine):
    only = {"is_ten_pct": True, "is_director": False, "is_ceo": False, "is_cfo": False, "value": 200_000,
            "holdings_increase": 0.2}  # also in role_10pct_only, value_100k_1m and incr_10_50pct
    s = _score(engine, only)
    assert s["cell"] == "INSIDER_BUY:ten_pct_any" and s["score_pp"] == pytest.approx(5.02, abs=0.01)
    comp = {c["component"]: c for c in s["components"]}
    assert comp["INSIDER_BUY:role_10pct_only"]["status"] == "EVIDENCE_ONLY" and comp["INSIDER_BUY:role_10pct_only"]["effect_pp"] == 0
    for k in ("value_100k_1m", "incr_10_50pct", "serial_3plus_issuers_2y", "owner_history_negative", "indirect_only",
              "first_buy_same_issuer_5y"):
        assert comp[f"INSIDER_BUY:{k}"]["status"] == "HOLDOUT_REQUIRED" and comp[f"INSIDER_BUY:{k}"]["effect_pp"] == 0, k
    assert sum(c["effect_pp"] for c in s["components"]) == pytest.approx(5.02, abs=0.01)  # exactly one weighted cell
    director = _score(engine, {**only, "is_director": True}, cik="0000000021")  # 10 % owner AND director
    assert director["cell"] == "INSIDER_BUY:ten_pct_any" and director["score_pp"] == pytest.approx(5.02, abs=0.01)


@pytest.mark.parametrize("flags, owner_cell", [
    ({"is_ten_pct": True}, False),  # competing flags missing: NOT NULL is unknown in the research SQL
    ({"is_ten_pct": True, "is_director": None, "is_ceo": False, "is_cfo": False}, False),
    ({"is_ten_pct": True, "is_director": False, "is_ceo": False, "is_cfo": False}, True),
    ({"is_ten_pct": "true", "is_director": "false", "is_ceo": "false", "is_cfo": "false"}, True),  # JSON text booleans
    ({"is_ten_pct": True, "is_director": True, "is_ceo": False, "is_cfo": False}, False),
    ({"is_ten_pct": None, "is_director": False, "is_ceo": False, "is_cfo": False}, False),
])
def test_bucket_membership_follows_sql_null_semantics(flags, owner_cell):
    from app.scoring.engine import BUCKETS
    assert (BUCKETS["role_10pct_only"](flags) is True) is owner_cell


@db
@pytest.mark.usefixtures("clean")
def test_an_event_not_public_at_the_scoring_moment_scores_zero(engine):
    from app.scoring.engine import score_event
    owner = {"is_ten_pct": True, "is_director": False, "is_ceo": False, "is_cfo": False, "value": 2e6}
    with engine.begin() as c:
        eid = _event(c, payload=owner, key="future")
        c.execute(text("UPDATE events SET available_at = :a WHERE id = :i"), {"a": NOW + timedelta(hours=1), "i": eid})
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": eid}).mappings().one())
        fut = score_event(c, ev, NOW)
        ev2 = {**ev, "available_at": None, "public_at": None}
        none = score_event(c, ev2, NOW)
    assert (fut["score_pp"], fut["cell"]) == (0.0, "NOT_PUBLIC_YET")
    assert (none["score_pp"], none["cell"]) == (0.0, "NO_PUBLIC_TIME")


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
@pytest.mark.parametrize("direction, types, at_days, vetoes", [
    ("NEGATIVE", ["NEW_RISK_FACTORS"], -20, ["TENK_NEGATIVE_RECENT"]),
    ("NEGATIVE", ["NEW_GOING_CONCERN"], -20, ["TENK_NEGATIVE_RECENT", "TENK_NEW_GOING_CONCERN"]),
    ("POSITIVE", ["REMOVED_GOING_CONCERN"], -20, []),  # a positive change is neither a weight nor a veto
    ("MIXED", ["NEW_GOING_CONCERN"], -20, []),
    ("NEGATIVE", ["NEW_GOING_CONCERN"], -120, []),  # outside the 90-day window
    ("NEGATIVE", ["NEW_GOING_CONCERN"], +5, []),  # public after the scoring moment: point in time
])
def test_a_negative_10k_change_is_a_veto_never_a_weight(engine, direction, types, at_days, vetoes):
    from app.scoring.engine import score_event
    owner = {"is_ten_pct": True, "is_director": False, "is_ceo": False, "is_cfo": False, "value": 2e6}
    cik = "0000000" + str(abs(at_days)).zfill(3)
    with engine.begin() as c:
        tid = _event(c, et="TENK_CHANGE", d=(NOW + timedelta(days=at_days)).date(), key=f"tk{direction}{types}{at_days}",
                     payload={"direction": direction, "change_types": types}, cik=cik)
        c.execute(text("UPDATE events SET available_at = :a WHERE id = :i"), {"a": NOW + timedelta(days=at_days), "i": tid})
        eid = _event(c, payload=owner, key=f"b{direction}{types}{at_days}", cik=cik)
        ev = dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": eid}).mappings().one())
        s = score_event(c, ev, NOW)
    assert s["vetoes"] == vetoes
    assert s["score_pp"] == pytest.approx(5.02, abs=0.01)  # the 10-K never adds or subtracts a weight
    tk = next(x for x in s["components"] if x["component"] == "TENK_NEGATIVE_RECENT")
    assert tk["effect_pp"] == 0.0 and (tk["evidence_event"] == tid) is bool(vetoes)


# ------------------------------------------------------------------ kept apart from the gate, research-only

def _live_setup(engine, monkeypatch):
    monkeypatch.setenv("PAPER_RESEARCH_MODE", "1")
    today = date.today()
    with engine.begin() as c:
        _universe(c)
        zb = _bars(c, "ZBIO", today - timedelta(days=90), 60, 10.0, 0.01)
        spy = _bars(c, "SPY", today - timedelta(days=90), 60, 400.0, 0.5)
        eid = _event(c, d=today - timedelta(days=1),
                     payload={"is_ten_pct": True, "is_director": False, "is_ceo": False, "is_cfo": False, "value": 2e6})
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
    assert s["event_id"] == eid and s["research_only"] and s["vetoes"] == [] and s["score_pp"] == pytest.approx(5.02, abs=0.01)
    assert s["model_version"] == "score.v2-evidence" and s["evidence_version"] == "scoring_evidence_v3_night_2026-09-30"
    assert scan_new_events(engine, days=5)["checked"] == 0  # already proposed: nothing re-scored twice


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_scoring_failure_never_stops_a_proposal(engine, monkeypatch):
    from app.paper.proposals import scan_new_events
    from app.scoring import engine as se
    _live_setup(engine, monkeypatch)
    real = se.record
    monkeypatch.setattr(se, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = scan_new_events(engine, days=5)
    assert out["created"] == 1 and out["score_errors"] == 1
    monkeypatch.setattr(se, "record", real)
    out = scan_new_events(engine, days=5)  # the event is no longer rescanned (a proposal exists) ...
    assert out["checked"] == 0 and out["scores_retried"] == 1  # ... but its missing score is retried
    from app.db.models import proposed_trades, signal_scores
    with engine.connect() as c:
        p = c.execute(select(proposed_trades)).mappings().all()
        s = c.execute(select(signal_scores)).mappings().one()
    assert len(p) == 1 and s["scored_at"] == p[0]["proposal_created_at"] and s["vetoes"] == []  # original decision
    assert scan_new_events(engine, days=5)["scores_retried"] == 0


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
    assert "SEVERE_FINANCING_RISK" in s["vetoes"] and s["score_pp"] == pytest.approx(5.02, abs=0.01)  # score is not a veto


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_scoring_veto_is_stored_with_the_gate_vetoes_and_leaves_the_gate_alone(engine, monkeypatch):
    from app.db.models import proposed_trades, signal_scores
    from app.paper.proposals import scan_new_events
    _live_setup(engine, monkeypatch)
    with engine.begin() as c:
        tid = _event(c, et="TENK_CHANGE", d=date.today() - timedelta(days=10), key="tkneg",
                     payload={"direction": "NEGATIVE", "change_types": ["NEW_GOING_CONCERN"]})
        c.execute(text("UPDATE events SET available_at = now() - interval '10 days', is_backfill = true WHERE id = :i"),
                  {"i": tid})
    out = scan_new_events(engine, days=5)
    with engine.connect() as c:
        s = c.execute(select(signal_scores)).mappings().one()
        n = c.execute(select(proposed_trades)).mappings().all()
    assert out["created"] == 1 and len(n) == 1  # the gate is unchanged: a scoring veto is research-only
    assert s["vetoes"] == ["TENK_NEGATIVE_RECENT", "TENK_NEW_GOING_CONCERN"]


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


def test_family_audit_separates_tested_from_unmeasured():
    from app.scoring.engine import family_status
    rows = {r["family"]: r for r in family_status()}
    for fam in ("13D", "HOUSE_BUY", "MULTI_SIGNAL"):
        assert rows[fam]["status"] == "NOT_SIGNIFICANT" and "tested" in rows[fam]["why"], fam
    for fam in ("NEWS", "GOV_CONTRACT", "DISCOVERY", "ACTIVIST_CROWDING"):
        assert rows[fam]["status"] == "NOT_MEASURED", fam
    assert "veto" in rows["10K_CHANGE"]["why"] and rows["TENK_NEGATIVE_RECENT (veto)"]["status"] == "VETO"
    assert rows["US_13F_ACCUMULATION"]["status"] == "NOT_SIGNIFICANT" and "tested" in rows["US_13F_ACCUMULATION"]["why"]
    assert rows["US_FDA_EVENT"]["status"] == "NOT_MEASURED" and "coverage" in rows["US_FDA_EVENT"]["why"]


@db
@pytest.mark.usefixtures("clean")
def test_one_failing_retry_does_not_block_the_others(engine, monkeypatch):
    from app.db.models import proposed_trades, signal_scores
    from app.scoring import engine as se
    with engine.begin() as c:
        ids = [_event(c, key=f"r{i}", payload={"is_ceo": True, "value": 5e4}) for i in range(2)]
        for i, eid in enumerate(ids):
            c.execute(proposed_trades.insert().values(
                code=f"PE00000{i}", ticker="ZBIO", cik="0001953926", event_id=eid, signal_family="INSIDER_BUY",
                event_type="INSIDER_OPEN_MARKET_BUY", proposal_created_at=NOW + timedelta(minutes=i), reference_date=date(2024, 3, 1),
                entry_reference_price=10.0, suggested_position_pct=0.03, max_position_pct=0.05, stop_reference=8.5, thesis="t",
                risks=[], reasons=[], data_quality="OK", gate={}, approval_status="APPROVED", expires_at=NOW + timedelta(days=3),
                research_only=True))
    real = se.record

    def flaky(engine_, event_id, vetoes=None, now=None, context=True):
        if event_id == ids[0]:
            raise RuntimeError("boom")
        return real(engine_, event_id, vetoes, now, context)
    monkeypatch.setattr(se, "record", flaky)
    assert se.retry_missing(engine, ["INSIDER_OPEN_MARKET_BUY"], NOW - timedelta(days=1)) == 1  # the second still recovers
    with engine.connect() as c:
        assert [r[0] for r in c.execute(select(signal_scores.c.event_id))] == [ids[1]]


@db
@pytest.mark.usefixtures("clean")
def test_a_retry_keeps_the_veto_of_a_change_superseded_after_the_decision(engine):
    """A NEGATIVE 10-K change valid at the decision but superseded before the retry: today's supersession state cannot
    say what held at the decision, so the retry keeps the veto (the conservative side)."""
    from app.db.models import signal_scores
    from app.scoring.engine import record
    with engine.begin() as c:
        tid = _event(c, et="TENK_CHANGE", d=(NOW - timedelta(days=10)).date(), key="tk", payload={"direction": "NEGATIVE"})
        c.execute(text("UPDATE events SET available_at = :a WHERE id = :i"), {"a": NOW - timedelta(days=10), "i": tid})
        eid = _event(c, key="buy", payload={"is_ceo": True, "value": 5e4})
        c.execute(text("UPDATE events SET superseded = true WHERE id = :i"), {"i": tid})  # after the decision
    record(engine, eid, [], now=NOW, context=False)
    with engine.connect() as c:
        comps = c.execute(select(signal_scores.c.components)).scalar()
    tk = next(x for x in comps if x["component"] == "TENK_NEGATIVE_RECENT")
    assert tk["evidence_event"] == tid and "retry" in tk["why"]  # errs on the side of the veto
    with engine.connect() as c:
        assert c.execute(select(signal_scores.c.vetoes)).scalar() == ["TENK_NEGATIVE_RECENT"]


@db
@pytest.mark.usefixtures("clean")
def test_an_event_not_public_yet_is_still_recorded(engine):
    from app.db.models import signal_scores
    from app.scoring.engine import record
    with engine.begin() as c:
        eid = _event(c, key="future", payload={"is_ten_pct": True})
        c.execute(text("UPDATE events SET available_at = :a WHERE id = :i"), {"a": NOW + timedelta(hours=1), "i": eid})
    assert record(engine, eid, ["DUPLICATE"], now=NOW)["cell"] == "NOT_PUBLIC_YET"
    with engine.connect() as c:
        row = c.execute(select(signal_scores)).mappings().one()
    assert row["score_pp"] == 0.0 and row["vetoes"] == ["DUPLICATE"]
