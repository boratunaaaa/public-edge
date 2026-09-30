"""Validated 10-K risk (app.core.tenk_policy): a hard veto in the paper gate and the SAME vetoes in the research
score; point in time (strictly before the decision), within 90 days, same issuer only."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from tests.test_alpha_validation import _bars, _event, _serve, _universe, offline_prices  # noqa: F401 - fixture
from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
AT = datetime(2024, 3, 1, 15, tzinfo=timezone.utc)
CIK = "0001953926"
BASIS = "current_ticker_same_cik"


def _tenk(c, at, direction="NEGATIVE", types=("NEW_RISK_FACTORS",), cik=CIK, key=None, superseded=False):
    tid = _event(c, et="TENK_CHANGE", d=at.date(), cik=cik, key=key or f"tk{at.isoformat()}{direction}{types}{cik}",
                 payload={"direction": direction, "change_types": list(types)})
    c.execute(text("UPDATE events SET available_at = :a, superseded = :s WHERE id = :i"), {"a": at, "s": superseded, "i": tid})
    return tid


def _risk(c, **kw):
    from app.core.tenk_policy import tenk_risk
    return tenk_risk(c, CIK, AT, **kw)


# ------------------------------------------------------------------ the policy

@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("offset, vetoed", [
    (timedelta(0), False),  # public AT the decision moment: not before it
    (timedelta(seconds=1), True),
    (timedelta(days=89), True),
    (timedelta(days=90), False),  # the window is the 90 days before, exclusive
    (-timedelta(days=1), False),  # public after the decision: never
])
def test_only_changes_public_strictly_before_the_decision_and_within_90_days_count(engine, offset, vetoed):
    with engine.begin() as c:
        tid = _tenk(c, AT - offset)
        r = _risk(c)
    assert (r.vetoes == ("TENK_NEGATIVE_RECENT",)) is vetoed and (r.event_ids == (tid,)) is vetoed


@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("direction, types, vetoes", [
    ("NEGATIVE", ["NEW_RISK_FACTORS"], ("TENK_NEGATIVE_RECENT",)),
    ("NEGATIVE", ["NEW_GOING_CONCERN"], ("TENK_NEGATIVE_RECENT", "TENK_NEW_GOING_CONCERN")),
    ("MIXED", ["NEW_GOING_CONCERN", "NEW_MAJOR_CUSTOMER"], ("TENK_NEW_GOING_CONCERN",)),  # measured by change type
    ("POSITIVE", ["NEW_GOING_CONCERN", "RISK_DECREASE"], ("TENK_NEW_GOING_CONCERN",)),
    ("POSITIVE", ["REMOVED_GOING_CONCERN"], ()),
    ("MIXED", ["NEW_RISK_FACTORS"], ()),
])
def test_the_two_vetoes_follow_the_measured_definitions(engine, direction, types, vetoes):
    with engine.begin() as c:
        _tenk(c, AT - timedelta(days=5), direction, types)
        assert _risk(c).vetoes == vetoes


@db
@pytest.mark.usefixtures("clean")
def test_other_issuers_and_superseded_changes(engine):
    with engine.begin() as c:
        _tenk(c, AT - timedelta(days=5), cik="0000000077")
        assert _risk(c).vetoes == ()  # another company's 10-K
        _tenk(c, AT - timedelta(days=5), types=("NEW_GOING_CONCERN",), superseded=True)
        assert _risk(c).vetoes == ()  # a live decision uses today's supersession state
        assert _risk(c, include_superseded=True).vetoes == ("TENK_NEGATIVE_RECENT", "TENK_NEW_GOING_CONCERN")  # retry


# ------------------------------------------------------------------ paper gate: hard veto

def _buy(c):
    eid = _event(c, key="buy", payload={"is_ceo": True, "value": 5e4})
    return dict(c.execute(text("SELECT * FROM events WHERE id = :i"), {"i": eid}).mappings().one())


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_the_gate_hard_vetoes_a_validated_10k_risk(engine):
    from app.paper.proposals import evaluate_gate
    with engine.begin() as c:
        _universe(c)
        ev = _buy(c)
        g = evaluate_gate(c, ev, family_ok=(True, "FAMILY_OUTPERFORMING"), price_basis=BASIS, now=AT)
        assert g.ok and "NO_VALIDATED_10K_RISK" in g.passed
        tid = _tenk(c, AT - timedelta(days=30), "MIXED", ("NEW_GOING_CONCERN",))
        g = evaluate_gate(c, ev, family_ok=(True, "FAMILY_OUTPERFORMING"), price_basis=BASIS, now=AT)
        assert not g.ok and g.failed == ["TENK_NEW_GOING_CONCERN"] and g.info["tenk_events"] == [tid]
        _tenk(c, AT - timedelta(days=10))
        g = evaluate_gate(c, ev, family_ok=(True, "FAMILY_BLOCKED"), price_basis=BASIS, now=AT)
        assert set(g.failed) == {"TENK_NEGATIVE_RECENT", "TENK_NEW_GOING_CONCERN"}  # research mode never overrides it
        later = evaluate_gate(c, ev, family_ok=(True, "FAMILY_OUTPERFORMING"), price_basis=BASIS,
                              now=AT - timedelta(days=31))  # the same decision taken before either 10-K was public
        assert later.ok and "NO_VALIDATED_10K_RISK" in later.passed


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_scan_creates_no_proposal_and_stores_the_same_vetoes_in_the_score(engine, monkeypatch):
    from app.db.models import proposed_trades, signal_scores
    from app.paper.proposals import scan_new_events
    from tests.test_scoring import _live_setup
    _live_setup(engine, monkeypatch)
    with engine.begin() as c:
        tid = _event(c, et="TENK_CHANGE", d=(datetime.now(timezone.utc) - timedelta(days=10)).date(), key="tkneg",
                     payload={"direction": "NEGATIVE", "change_types": ["NEW_GOING_CONCERN"]})
        c.execute(text("UPDATE events SET available_at = now() - interval '10 days', is_backfill = true WHERE id = :i"),
                  {"i": tid})
    out = scan_new_events(engine, days=5)
    assert out["created"] == 0 and out["rejected"] == {"TENK_NEGATIVE_RECENT": 1, "TENK_NEW_GOING_CONCERN": 1}
    with engine.connect() as c:
        assert c.execute(select(proposed_trades)).first() is None  # no proposal, no order, nothing to approve
        s = c.execute(select(signal_scores)).mappings().one()
    assert s["vetoes"] == ["TENK_NEGATIVE_RECENT", "TENK_NEW_GOING_CONCERN"]
    assert s["score_pp"] == pytest.approx(5.02, abs=0.01)  # the veto blocks; the evidence weight is unchanged


@db
@pytest.mark.usefixtures("clean", "offline_prices")
@pytest.mark.parametrize("offset, direction, types", [
    (timedelta(days=5), "NEGATIVE", ("NEW_RISK_FACTORS",)),
    (timedelta(days=5), "POSITIVE", ("NEW_GOING_CONCERN",)),
    (timedelta(days=5), "POSITIVE", ("RISK_DECREASE",)),
    (timedelta(0), "NEGATIVE", ("NEW_GOING_CONCERN",)),
    (timedelta(days=95), "NEGATIVE", ("NEW_GOING_CONCERN",)),
])
def test_gate_and_score_agree_on_every_case(engine, offset, direction, types):
    """One policy: the gate's 10-K failures and the score's 10-K vetoes are always the same set."""
    from app.core.tenk_policy import TENK_NEGATIVE_RECENT, TENK_NEW_GOING_CONCERN
    from app.paper.proposals import evaluate_gate
    from app.scoring.engine import score_event
    with engine.begin() as c:
        _universe(c)
        ev = _buy(c)
        _tenk(c, AT - offset, direction, types)
        g = evaluate_gate(c, ev, family_ok=(True, "FAMILY_OUTPERFORMING"), price_basis=BASIS, now=AT)
        s = score_event(c, ev, AT)
    tenk = {TENK_NEGATIVE_RECENT, TENK_NEW_GOING_CONCERN}
    assert set(g.failed) & tenk == set(s["vetoes"])


def test_gate_and_score_call_the_one_policy():
    import inspect

    from app.core import tenk_policy
    from app.paper import proposals
    from app.scoring import engine
    assert engine.tenk_risk is tenk_policy.tenk_risk
    assert "tenk_risk(conn, cik, now)" in inspect.getsource(proposals.evaluate_gate)
    assert "'NEGATIVE'" not in inspect.getsource(engine)  # no second copy of the 10-K rule's SQL in the scorer
