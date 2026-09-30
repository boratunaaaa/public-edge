"""The validated 10-K hard veto is re-checked after proposal creation: at approval (decide), in the expiry sweep, and
at every BUY fill (at that fill's session open); SELL / exit orders are never blocked; a 10-K write and a decision
never interleave (issuer advisory lock)."""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.marketdata.calendar import NY
from tests.test_alpha_validation import _event, offline_prices  # noqa: F401 - fixture
from tests.test_db import URL
from tests.test_paper_liquidity_fix import _row, _series, _store

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
CIK = "0001953926"
CREATED = datetime(2024, 3, 1, 15, tzinfo=timezone.utc)
D0 = date(2024, 3, 4)  # Monday: the BUY's first session
OPEN0 = datetime.combine(D0, time(9, 30), NY)


def _proposal(c, created=CREATED, code="PETENK1"):
    from app.db.models import proposed_trades
    eid = _event(c, key=f"buy{code}", payload={"is_ten_pct": True})
    return c.execute(proposed_trades.insert().values(
        code=code, ticker="ZBIO", cik=CIK, event_id=eid, signal_family="INSIDER_BUY", event_type="INSIDER_OPEN_MARKET_BUY",
        proposal_created_at=created, reference_date=created.date(), entry_reference_price=10.0,
        suggested_position_pct=0.03, max_position_pct=0.05, stop_reference=8.5, thesis="t", risks=[], reasons=[],
        data_quality="OK", gate={}, approval_status="PENDING", expires_at=created + timedelta(days=3), research_only=True,
    ).returning(proposed_trades.c.id)).scalar_one()


def _tenk(c, at, direction="NEGATIVE", types=("NEW_RISK_FACTORS",), key="tk"):
    tid = _event(c, et="TENK_CHANGE", d=at.date(), key=key, payload={"direction": direction, "change_types": list(types)})
    c.execute(text("UPDATE events SET available_at = :a WHERE id = :i"), {"a": at, "i": tid})
    return tid


def _p(c, pid):
    from app.db.models import proposed_trades
    return c.execute(select(proposed_trades).where(proposed_trades.c.id == pid)).mappings().one()


def _buy(c, pid, not_before=D0, side="BUY", position_id=None):
    from app.db.models import paper_orders
    from app.paper.ledger import ensure_accounts
    aid = ensure_accounts(c, book="RESEARCH")["B_90D"]
    return c.execute(paper_orders.insert().values(
        account_id=aid, proposal_id=pid, position_id=position_id, ticker="ZBIO", side=side, requested_at=CREATED,
        not_before=not_before, target_pct=0.03, status="PENDING").returning(paper_orders.c.id)).scalar_one()


# ------------------------------------------------------------------ approval

@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("types, veto", [(("NEW_RISK_FACTORS",), "tenk_negative_recent"),
                                         (("NEW_GOING_CONCERN",), "tenk_negative_recent")])
def test_a_10k_published_after_creation_expires_the_proposal_at_approval(engine, types, veto):
    from app.db.models import paper_orders
    from app.paper.proposals import decide
    with engine.begin() as c:
        pid = _proposal(c)
        _tenk(c, CREATED + timedelta(hours=2), types=types)  # after the gate passed, before the approval
    r = decide(engine, "PETENK1", True, by="user", now=CREATED + timedelta(hours=3))
    assert r["ok"] is False and r["error"] == "TENK_VETO" and "TENK_NEGATIVE_RECENT" in r["vetoes"]
    with engine.connect() as c:
        p = _p(c, pid)
        assert p["approval_status"] == "EXPIRED" and p["decided_by"] == f"system:{veto}"
        assert c.execute(select(paper_orders)).first() is None  # no BUY order is ever queued


@db
@pytest.mark.usefixtures("clean")
def test_a_going_concern_of_any_direction_also_blocks_approval(engine):
    from app.paper.proposals import decide
    with engine.begin() as c:
        pid = _proposal(c)
        _tenk(c, CREATED + timedelta(hours=2), direction="MIXED", types=("NEW_GOING_CONCERN",))
    r = decide(engine, "PETENK1", True, by="system:research_auto", now=CREATED + timedelta(hours=3))
    assert r["error"] == "TENK_VETO" and r["vetoes"] == ["TENK_NEW_GOING_CONCERN"]
    with engine.connect() as c:
        assert _p(c, pid)["decided_by"] == "system:tenk_new_going_concern"


@db
@pytest.mark.usefixtures("clean")
def test_approval_uses_only_10ks_public_strictly_before_it(engine):
    from app.paper.proposals import decide
    at = CREATED + timedelta(hours=3)
    with engine.begin() as c:
        _proposal(c)
        _tenk(c, at)  # public exactly at the decision moment: not before it
    r = decide(engine, "PETENK1", True, by="user", now=at)
    assert r["ok"] and r["status"] == "APPROVED" and len(r["orders"]) >= 1


@db
@pytest.mark.usefixtures("clean")
def test_a_rejection_is_still_possible_under_a_veto(engine):
    from app.paper.proposals import decide
    with engine.begin() as c:
        pid = _proposal(c)
        _tenk(c, CREATED + timedelta(hours=1))
    assert decide(engine, "PETENK1", False, by="user", now=CREATED + timedelta(hours=3))["status"] == "REJECTED"
    with engine.connect() as c:
        assert _p(c, pid)["approval_status"] == "REJECTED"


# ------------------------------------------------------------------ expiry sweep

@db
@pytest.mark.usefixtures("clean")
def test_the_expiry_sweep_expires_vetoed_pending_proposals_only(engine):
    from app.paper.proposals import expire
    other = "0000000042"
    with engine.begin() as c:
        vetoed = _proposal(c, code="PETENK1")
        clean = _proposal(c, code="PETENK2")
        c.execute(text("UPDATE proposed_trades SET cik = :o WHERE id = :i"), {"o": other, "i": clean})
        _tenk(c, CREATED + timedelta(hours=1), types=("NEW_GOING_CONCERN",))
    expire(engine, now=CREATED + timedelta(hours=2))
    with engine.connect() as c:
        assert _p(c, vetoed)["approval_status"] == "EXPIRED"
        assert _p(c, vetoed)["decided_by"] == "system:tenk_negative_recent"
        assert _p(c, clean)["approval_status"] == "PENDING"


# ------------------------------------------------------------------ fills

@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_pending_buy_is_cancelled_when_a_10k_became_public_before_its_session_open(engine):
    from app.paper.ledger import fill_orders
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60))
        pid = _proposal(c)
        oid = _buy(c, pid)
        _tenk(c, OPEN0 - timedelta(minutes=1), types=("NEW_GOING_CONCERN",))  # after approval, before the open
        out = fill_orders(c, date(2024, 3, 8))
        o = _row(c, oid)
    assert out == {"filled": 0, "cancelled": 1, "waiting": 0}
    assert o["status"] == "CANCELLED" and o["note"] == "validated 10-K risk: TENK_NEGATIVE_RECENT, TENK_NEW_GOING_CONCERN"


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_10k_public_after_the_session_open_does_not_undo_that_fill(engine):
    """Point in time: the BUY filled at the open; a 10-K published later that day was not known then."""
    from app.paper.ledger import fill_orders
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60))
        oid = _buy(c, _proposal(c))
        _tenk(c, OPEN0 + timedelta(hours=5))
        assert fill_orders(c, date(2024, 3, 8))["filled"] == 1
        assert _row(c, oid)["fill_date"] == D0


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_waiting_buy_is_vetoed_at_the_session_it_would_fill_on(engine):
    """No opening trade on D0; a 10-K is published between D0's and D1's opens: the D1 fill is cancelled."""
    from app.paper.ledger import fill_orders
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60, overrides={D0: (None, 0)}))
        oid = _buy(c, _proposal(c))
        _tenk(c, OPEN0 + timedelta(hours=8))
        out = fill_orders(c, date(2024, 3, 8))
        assert out["cancelled"] == 1 and _row(c, oid)["status"] == "CANCELLED"


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_sell_and_exit_orders_are_never_blocked_by_the_veto(engine, monkeypatch):
    from app.db.models import paper_positions
    from app.paper.ledger import fill_orders
    from tests.test_alpha_validation import _serve
    bars = _series(date(2024, 1, 2), 90)
    _serve(monkeypatch, ZBIO=bars)
    with engine.begin() as c:
        _store(c, "ZBIO", bars)
        pid = _proposal(c)
        _buy(c, pid)
        assert fill_orders(c, date(2024, 3, 8))["filled"] == 1
        pos = c.execute(select(paper_positions)).mappings().one()
        _tenk(c, datetime(2024, 3, 20, 12, tzinfo=timezone.utc), types=("NEW_GOING_CONCERN",))
        sell = _buy(c, pid, not_before=date(2024, 3, 25), side="SELL", position_id=pos["id"])
        c.execute(paper_positions.update().values(status="CLOSING", exit_reason="THESIS"))
        out = fill_orders(c, date(2024, 3, 29))
        s = _row(c, sell)
    assert out["cancelled"] == 0 and s["status"] == "FILLED" and s["fill_date"] == date(2024, 3, 25)


# ------------------------------------------------------------------ race: a 10-K write and a decision never interleave

@db
@pytest.mark.usefixtures("clean")
def test_an_approval_waits_for_an_uncommitted_10k_write_and_then_sees_it(engine):
    from app.paper.proposals import decide
    with engine.begin() as c:
        pid = _proposal(c)
    writer = engine.connect()
    tx = writer.begin()
    try:
        _tenk(writer, CREATED + timedelta(hours=1), key="tkrace")  # takes the issuer's exclusive lock, not committed
        with engine.connect() as probe:
            from app.core.tenk_policy import tenk_risk
            with pytest.raises(OperationalError):  # a decision on this issuer must wait for the writer
                with probe.begin():
                    probe.execute(text("SET LOCAL lock_timeout = '300ms'"))  # never leaks into the pool
                    tenk_risk(probe, CIK, CREATED + timedelta(hours=3), lock=True)
    finally:
        tx.commit()
        writer.close()
    r = decide(engine, "PETENK1", True, by="user", now=CREATED + timedelta(hours=3))
    assert r["error"] == "TENK_VETO"  # once committed, the waiting decision sees it
    with engine.connect() as c:
        assert _p(c, pid)["approval_status"] == "EXPIRED"


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_10k_write_waits_for_an_uncommitted_fill(engine):
    """The fill holds the issuer's shared lock until it commits: a 10-K write cannot slip in between the check and the
    fill; it is serialized after it (and the next stage re-checks)."""
    from app.paper.ledger import fill_orders
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60))
        oid = _buy(c, _proposal(c))
    filler = engine.connect()
    tx = filler.begin()
    try:
        assert fill_orders(filler, date(2024, 3, 8))["filled"] == 1  # not committed yet
        with engine.connect() as w:
            with pytest.raises(OperationalError):
                with w.begin():
                    w.execute(text("SET LOCAL lock_timeout = '300ms'"))
                    _tenk(w, OPEN0 - timedelta(days=1), key="tklate")
    finally:
        tx.commit()
        filler.close()
    with engine.connect() as c:
        assert _row(c, oid)["status"] == "FILLED"


@db
@pytest.mark.usefixtures("clean")
def test_a_reclassifier_supersession_waits_for_an_uncommitted_decision(engine):
    """A supersession changes the 10-K risk too: while an approval holds the issuer's shared lock, the (multi-issuer)
    reclassifier must wait, and only then withdraw the old event."""
    import threading
    import time as _t

    from app.core.tenk_policy import tenk_risk
    from app.db.models import events, tenk_filings
    from app.tenk.engine import supersede_old_classifier
    with engine.begin() as c:
        for i, cik in enumerate(("0000000009", CIK)):  # two issuers in one reclassification transaction
            acc = f"000000000{i}-24-000001"
            c.execute(tenk_filings.insert().values(cik=cik, accession=acc, form="10-K", filing_date=date(2024, 3, 1),
                                                   source_url="u", profile={}, status="ok"))
            _event(c, et="TENK_CHANGE", key=f"tenk:{acc}", cik=cik, payload={"direction": "NEGATIVE"})
    holder = engine.connect()
    tx = holder.begin()
    done = threading.Event()
    try:
        assert tenk_risk(holder, CIK, CREATED, lock=True).vetoes  # a decision in flight sees the veto
        errors: list[BaseException] = []

        def reclassify():
            try:
                supersede_old_classifier(engine, dry_run=False)
                done.set()
            except BaseException as e:  # noqa: BLE001 - surfaced below
                errors.append(e)
        t = threading.Thread(target=reclassify)
        t.start()
        _t.sleep(1.0)
        assert not done.is_set()  # blocked on the issuer lock, nothing superseded yet
        with engine.connect() as c:
            assert not any(c.execute(select(events.c.superseded).where(events.c.event_type == "TENK_CHANGE")).scalars())
    finally:
        tx.commit()
        holder.close()
    t.join(10)
    assert not errors and done.is_set(), errors
    with engine.connect() as c:
        assert all(c.execute(select(events.c.superseded).where(events.c.event_type == "TENK_CHANGE")).scalars())


def test_multi_issuer_locks_are_always_taken_in_cik_order(monkeypatch):
    from app.core import tenk_policy
    seen = []
    monkeypatch.setattr(tenk_policy, "lock_issuer", lambda conn, cik, exclusive: seen.append(cik))
    tenk_policy.lock_issuers(None, ["0000000300", None, "0000000100", "0000000300", "0000000200"], exclusive=True)
    assert seen == ["0000000100", "0000000200", "0000000300"]


@db
@pytest.mark.usefixtures("clean", "offline_prices")
def test_a_fill_run_locks_every_buy_issuer_up_front(engine):
    """Before filling anything, the run holds the shared 10-K lock of every pending BUY's issuer: a writer on any of
    them waits - including an issuer whose order is processed last."""
    from app.paper.ledger import fill_orders
    from app.core.tenk_policy import lock_issuer
    with engine.begin() as c:
        _store(c, "ZBIO", _series(date(2024, 1, 2), 60))
        _buy(c, _proposal(c))
    runner = engine.connect()
    tx = runner.begin()
    try:
        fill_orders(runner, date(2024, 3, 8))
        with engine.connect() as w:
            with pytest.raises(OperationalError):
                with w.begin():
                    w.execute(text("SET LOCAL lock_timeout = '300ms'"))
                    lock_issuer(w, CIK, exclusive=True)
    finally:
        tx.rollback()
        runner.close()
