"""Data-integrity integration tests against PostgreSQL (TEST_DATABASE_URL)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from app.db.models import (activist_filings, beneficial_owner_links, corporate_actions, data_cleanup_audit, event_returns, events,
                           insider_filing_owners, insider_transactions, security_master, security_symbols)
from app.db.store import create_event
from tests.test_db import URL, count, form4_submission, process

pytestmark = [pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set"),
              pytest.mark.usefixtures("clean", "no_network_charts")]

TRUST_OWNER = """    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0007777777</rptOwnerCik>
            <rptOwnerName>Tellus BioVentures LLC</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>0</isDirector><isOfficer>0</isOfficer><isTenPercentOwner>0</isTenPercentOwner><isOther>1</isOther>
        </reportingOwnerRelationship>
    </reportingOwner>
"""


def _joint(xml: str) -> str:
    """Add a second reporting owner (a family trust) to the fixture -> a joint filing."""
    i = xml.index("<nonDerivativeTable>") if "<nonDerivativeTable>" in xml else xml.index("</ownershipDocument>")
    j = xml.rfind("</reportingOwner>", 0, i) + len("</reportingOwner>")
    return xml[:j] + "\n" + TRUST_OWNER + xml[j:]


def test_joint_form4_is_one_purchase_never_given_to_the_primary_owner(engine):
    """CEO + his LLC (Tellus) file jointly. Rows whose footnote names both stay JOINT; rows whose footnote
    names only the CEO's trust are attributed to him. Economically it is ONE buyer -> one event."""
    ids = process(engine, form4_submission(owner_xml_patch=_joint))
    assert count(engine, insider_filing_owners) == 2
    buys = count(engine, events, events.c.event_type == "INSIDER_OPEN_MARKET_BUY")
    assert buys == 1 and ids
    with engine.connect() as c:
        rows = c.execute(select(insider_transactions.c.owner_cik, insider_transactions.c.owner_attribution,
                                insider_transactions.c.joint_owner_ciks, insider_transactions.c.is_ceo)
                         .where(insider_transactions.c.category == "OPEN_MARKET_BUY")).all()
        link = c.execute(select(beneficial_owner_links)).mappings().first()
    kinds = {r.owner_attribution for r in rows}
    assert kinds <= {"joint", "footnote"} and "joint" in kinds  # never "single" / never the primary owner by default
    assert all(r.owner_cik is None for r in rows if r.owner_attribution == "joint")
    assert all(r.owner_cik == "0001228865" for r in rows if r.owner_attribution == "footnote")
    assert all(len(r.joint_owner_ciks) == 2 and r.is_ceo for r in rows)
    assert (link["entity_key"], link["beneficial_owner_key"], link["confidence"]) == ("0007777777", "0001228865", "HIGH")


def test_trust_of_the_same_insider_is_not_a_second_cluster_buyer(engine):
    process(engine, form4_submission())  # CEO buys directly
    trust = form4_submission(acc="0001104659-26-038077", owner_xml_patch=lambda x: x.replace(
        "MOULDER LEON O JR", "Leon O Moulder Jr Revocable Trust").replace("<rptOwnerCik>0001228865", "<rptOwnerCik>0008888888").replace(
        "Chief Executive Officer", ""))
    process(engine, trust)  # his revocable trust buys two days later in a separate filing
    with engine.connect() as c:
        links = c.execute(select(beneficial_owner_links.c.entity_key, beneficial_owner_links.c.relationship_type)).all()
    assert ("0008888888", "NAMED_TRUST") in [tuple(x) for x in links]
    assert count(engine, events, events.c.event_type == "CLUSTER_INSIDER_BUY") == 0  # one economic owner, not a cluster


def test_insider_history_is_point_in_time(engine):
    from app.signals.insider_type import owner_history
    with engine.begin() as c:
        for y, filed in ((2023, date(2023, 6, 3)), (2024, date(2024, 6, 3)), (2025, date(2026, 5, 1))):  # 2025 trade filed late
            c.execute(insider_transactions.insert().values(
                accession=f"A{y}", line_key="N0", issuer_cik="0001953926", owner_cik="0000000042", owner_name="X",
                trans_date=date(y, 6, 1), category="OPEN_MARKET_BUY", filed_date=filed, source="live", source_url="u"))
        known = owner_history(c, "0000000042", date(2026, 3, 1), filed_before=date(2026, 3, 2))
        leaked = owner_history(c, "0000000042", date(2026, 3, 1))
    assert (2025, 6) not in known and (2025, 6) in leaked


# ------------------------------------------------------------------ security master identity

def _master(c, cik, status="ACTIVE", **kw):
    c.execute(security_master.insert().values(cik=cik, status=status, **kw))


def test_ticker_rename_prices_by_the_same_cik_current_symbol(engine):
    from app.securitymaster.provider import SecSecurityMaster
    with engine.begin() as c:
        c.execute(security_symbols.insert().values(cik="0001953926", symbol="OLDZ", first_seen=date(2021, 1, 1),
                                                   last_seen=date(2023, 6, 1), n_obs=5, source="form345_filing"))
        sm = SecSecurityMaster(c)
        assert sm.resolve_ticker_at_date("0001953926", date(2022, 5, 1)) == "OLDZ"  # identity then
        assert sm.price_symbol("0001953926", date(2022, 5, 1), "OLDZ") == ("ZBIO", "current_ticker_same_cik", None)
        assert sm.resolve_security_at_date("OLDZ", date(2022, 5, 1)) == "0001953926"


def test_reused_ticker_is_never_used_for_another_company(engine):
    from app.securitymaster.provider import SecSecurityMaster
    with engine.begin() as c:
        # a delisted company used OPRA in 2021; today OPRA belongs to Opera (CIK 1737450)
        c.execute(security_symbols.insert().values(cik="0000000777", symbol="OPRA", first_seen=date(2020, 1, 1),
                                                   last_seen=date(2021, 3, 1), n_obs=4, source="form345_filing"))
        sm = SecSecurityMaster(c)
        sym, basis, missing = sm.price_symbol("0000000777", date(2020, 6, 1), "OPRA")
    assert sym is None and missing == "TICKER_REUSED"


def test_delisted_symbol_nobody_owns_is_a_verified_historical_identity(engine):
    from app.securitymaster.provider import SecSecurityMaster
    with engine.begin() as c:
        c.execute(security_symbols.insert().values(cik="0000000888", symbol="GONE", first_seen=date(2020, 1, 1),
                                                   last_seen=date(2022, 3, 1), n_obs=9, source="form345_filing"))
        c.execute(corporate_actions.insert().values(cik="0000000888", action="BANKRUPTCY", action_date=date(2022, 1, 5),
                                                    accession="b", source="sec_submissions"))
        c.execute(corporate_actions.insert().values(cik="0000000888", action="FORM25_NSE", action_date=date(2022, 3, 20),
                                                    accession="d", source="sec_full_index"))
        sm = SecSecurityMaster(c)
        assert sm.price_symbol("0000000888", date(2021, 6, 1), "GONE") == ("GONE", "historical_symbol_verified", None)
        dl = sm.delisting("0000000888")
        assert dl.reason == "BANKRUPTCY" and sm.get_delisting_date("0000000888") == date(2022, 3, 20)
        assert sm.get_listing_period("0000000888") == (date(2020, 1, 1), date(2022, 3, 20))
        assert [a["action"] for a in sm.get_corporate_actions("0000000888")] == ["BANKRUPTCY", "FORM25_NSE"]


def test_returns_with_wrong_price_identity_are_recomputed(engine):
    from app.backtest.jobs import revalidate_price_identity
    with engine.begin() as c:
        ok = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key="K1", public_date=date(2024, 1, 5), ticker="ZBIO",
                          issuer_cik="0001953926", title="t", payload={}, source_url="u")
        bad = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key="K2", public_date=date(2024, 1, 5), ticker="OLDZ",
                           issuer_cik="0001953926", title="t", payload={}, source_url="u")
        for eid in (ok, bad):
            c.execute(event_returns.insert().values(event_id=eid, horizon_days=90, entry_date=date(2024, 1, 8), entry_price=1,
                                                    exit_date=date(2024, 4, 8), exit_price=1.1, ret=0.1, excess_ret=0.05))
            c.execute(events.update().where(events.c.id == eid).values(returns_status="complete"))
    r = revalidate_price_identity(engine)
    assert r == {"kept": 1, "sent_for_recompute": 1, "batch": 2}
    with engine.connect() as c:
        st = dict(c.execute(select(events.c.id, events.c.returns_status)).all())
        assert st[ok] == "complete" and st[bad] == "pending"
        assert c.execute(select(event_returns.c.event_id)).scalars().all() == [ok]


# ------------------------------------------------------------------ quality gate on real rows

def test_quality_counts_unpriced_events_instead_of_dropping_them(engine):
    from app.backtest import quality as Q
    with engine.begin() as c:
        for k in range(40):
            cik = f"{k + 100:010d}"
            eid = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key=f"Q{k}", public_date=date(2023, 1, 5), ticker=f"T{k}",
                               issuer_cik=cik, title="t", payload={}, source_url="u")
            if k < 30:
                c.execute(event_returns.insert().values(event_id=eid, horizon_days=90, entry_date=date(2023, 1, 6), entry_price=1,
                                                        exit_date=date(2023, 4, 6), exit_price=1.1, ret=0.1, excess_ret=0.02 if k % 2 else -0.02))
                c.execute(events.update().where(events.c.id == eid).values(price_symbol=f"T{k}", price_symbol_basis="current_ticker_same_cik"))
            else:
                c.execute(events.update().where(events.c.id == eid).values(returns_status="no_data", missing_reason="NO_PRICE_DATA"))
        sup = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key="QS", public_date=date(2023, 1, 5), ticker="T0",
                           issuer_cik="0000000999", title="t", payload={}, source_url="u")
        c.execute(events.update().where(events.c.id == sup).values(superseded=True))
        st = Q.compute(c, "INSIDER_OPEN_MARKET_BUY", "all", "TRUE", {}, 90, use_cache=False, persist=True)
    assert st["unique_events"] == 40 and st["unresolved_episodes"] == 10  # superseded row excluded, unpriced rows kept
    assert st["status"] == "BLOCKED" and st["company_equal_rate"] is not None
    from app.backtest.probability import success_rate
    with engine.begin() as c:
        rate = success_rate(c, "INSIDER_OPEN_MARKET_BUY", {}, persist=True)
    assert rate.probability is None and rate.status == "BLOCKED" and "10 geçmiş örneğin fiyat verisi eksik" in rate.blocked_reason


def test_bankrupt_issuer_counts_as_loss_in_the_gate(engine):
    from app.backtest import quality as Q
    with engine.begin() as c:
        _master(c, "0000000888", status="DELISTED", delisting_reason="BANKRUPTCY", delisting_date=date(2023, 3, 1))
        eid = create_event(c, event_type="CLUSTER_INSIDER_BUY", dedupe_key="B1", public_date=date(2023, 1, 5), ticker="GONE",
                           issuer_cik="0000000888", title="t", payload={}, source_url="u")
        c.execute(events.update().where(events.c.id == eid).values(returns_status="no_data", missing_reason="DELISTED_BANKRUPTCY"))
        rows = Q.load_rows(c, "CLUSTER_INSIDER_BUY", "TRUE", {}, 90)
    assert [(r.outcome, r.reason) for r in rows] == [("loss", "DELISTED_PERFORMANCE_RULE")]


# ------------------------------------------------------------------ legacy cleanup (dry run first, never deletes)

def _13d(c, acc, filed, pct, cls, amend, investor="0000005555"):
    c.execute(activist_filings.insert().values(accession=acc, form_type="SC 13D/A" if amend else "SC 13D", is_13d=True, is_amendment=amend,
                                               issuer_cik="0001737450", ticker="OPRA", investor_cik=investor, investor_name="Fund",
                                               percent=pct, filed_date=filed, item4_class=cls, source_url="u"))
    return create_event(c, event_type="ACTIVIST_13D", dedupe_key=f"13D:{acc}", public_date=filed, ticker="OPRA", issuer_cik="0001737450",
                        title="t", payload={"accession": acc, "is_amendment": amend}, source_url="u")


def test_13d_cleanup_dry_run_then_apply(engine):
    from app.integrity.cleanup import run_cleanup
    with engine.begin() as c:
        orig = _13d(c, "a1", date(2024, 1, 2), 6.0, "BOARD_SEAT", False)
        boiler = _13d(c, "a2", date(2024, 2, 2), 6.3, "BOARD_SEAT", True)
        moved = _13d(c, "a3", date(2024, 3, 2), 8.1, "BOARD_SEAT", True)
        purpose = _13d(c, "a4", date(2024, 4, 2), 8.2, "PROXY_FIGHT", True)
    dry = run_cleanup(engine, dry_run=True, steps=("13d",))
    assert dry["13d"]["superseded"] == 1 and count(engine, events, events.c.superseded.is_(True)) == 0
    assert count(engine, data_cleanup_audit, data_cleanup_audit.c.dry_run.is_(True)) == 1
    run_cleanup(engine, dry_run=False, steps=("13d",))
    with engine.connect() as c:
        st = {r.id: (r.superseded, r.canonical_id) for r in c.execute(select(events.c.id, events.c.superseded, events.c.canonical_id))}
    assert st[boiler] == (True, orig) and st[orig][0] is False and st[moved][0] is False and st[purpose][0] is False
    assert count(engine, events) == 4  # nothing deleted
    again = run_cleanup(engine, dry_run=False, steps=("13d",))
    assert again["13d"]["superseded"] == 0  # idempotent


def test_multi_signal_cleanup_keeps_one_per_episode(engine):
    from app.integrity.cleanup import run_cleanup
    with engine.begin() as c:
        ids = [create_event(c, event_type="MULTI_SIGNAL_EVENT", dedupe_key=f"M{i}", public_date=d, ticker="TSM", title="t",
                            payload={"source_groups": ["ACTIVIST", "INSIDER"]}, source_url="u")
               for i, d in enumerate((date(2025, 1, 1), date(2025, 1, 8), date(2025, 1, 20), date(2025, 5, 1)))]
    out = run_cleanup(engine, dry_run=False, steps=("multi",))
    assert out["multi"]["superseded"] == 2
    with engine.connect() as c:
        live = c.execute(select(events.c.id).where(events.c.superseded.is_(False))).scalars().all()
    assert sorted(live) == [ids[0], ids[3]]


def test_multi_signal_loses_independence_when_its_13d_component_is_superseded(engine):
    from app.integrity.cleanup import run_cleanup
    with engine.begin() as c:
        orig = _13d(c, "b1", date(2024, 1, 2), 6.0, "BOARD_SEAT", False)
        boiler = _13d(c, "b2", date(2024, 2, 2), 6.2, "BOARD_SEAT", True)  # boilerplate amendment
        cl = create_event(c, event_type="CLUSTER_INSIDER_BUY", dedupe_key="CL1", public_date=date(2024, 2, 5), ticker="OPRA",
                          issuer_cik="0001737450", title="t", payload={"number_of_buyers": 2}, source_url="u")
        m = create_event(c, event_type="MULTI_SIGNAL_EVENT", dedupe_key="MS1", public_date=date(2024, 2, 5), ticker="OPRA", title="t",
                         payload={"source_groups": ["ACTIVIST", "INSIDER"], "component_event_ids": [boiler, cl]}, source_url="u")
    dry = run_cleanup(engine, dry_run=True, steps=("13d", "multi"))
    assert dry["multi"]["component_superseded"] == 1 and count(engine, events, events.c.superseded.is_(True)) == 0
    run_cleanup(engine, dry_run=False, steps=("13d", "multi"))
    with engine.connect() as c:
        st = dict(c.execute(select(events.c.id, events.c.superseded_reason)).all())
    assert st[boiler] == "13D_IMMATERIAL_AMENDMENT" and st[m] == "MULTI_SIGNAL_COMPONENT_SUPERSEDED" and st[orig] is None


def test_multi_owner_cleanup_reattributes_legacy_primary_owner_event(engine):
    from app.integrity.cleanup import run_cleanup
    process(engine, form4_submission())  # stored with the single-owner fixture
    with engine.begin() as c:
        # legacy state: the joint filing's first-listed owner (an LLC) was stored as THE buyer ...
        c.execute(events.update().values(dedupe_key=events.c.dedupe_key.op("||")("-legacy")))
        c.execute(text("UPDATE events SET dedupe_key = replace(dedupe_key, '0001228865-legacy', '0007777777')"))
        # ... and the rescan found the rows are jointly reported by the LLC and the CEO
        c.execute(insider_transactions.update().values(owner_cik=None, owner_name="Tellus BioVentures LLC; MOULDER LEON O JR (ortak bildirim)",
                                                       owner_attribution="joint", joint_owner_ciks=["0007777777", "0001228865"]))
    dry = run_cleanup(engine, dry_run=True, steps=("owners",))
    assert dry["owners"]["superseded"] == 1 and count(engine, events, events.c.superseded.is_(True)) == 0
    out = run_cleanup(engine, dry_run=False, steps=("owners",))
    assert out["owners"]["superseded"] == 1 and out["owners"]["created"] == 1
    with engine.connect() as c:
        rows = c.execute(select(events.c.dedupe_key, events.c.superseded, events.c.canonical_id, events.c.id)
                         .where(events.c.event_type == "INSIDER_OPEN_MARKET_BUY").order_by(events.c.id)).all()
    old, new = rows
    assert old.superseded and old.canonical_id == new.id and new.dedupe_key.endswith(":0001228865") and not new.superseded


# ------------------------------------------------------------------ point-in-time news universe

def test_news_backfill_uses_listed_at_the_time_universe(engine, monkeypatch):
    from app.news import backfill as nb
    with engine.begin() as c:
        # listed in 2021-2022 under its own symbol, delisted since: MUST be in the historical sample
        c.execute(security_symbols.insert().values(cik="0000000888", symbol="GONE", first_seen=date(2021, 1, 1),
                                                   last_seen=date(2022, 12, 1), n_obs=9, source="form345_filing"))
    day = date.today() - timedelta(days=3)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    from app.securitymaster.provider import SecSecurityMaster
    with engine.begin() as c:
        sm = SecSecurityMaster(c)
        assert sm.resolve_ticker_at_date("0000000888", date(2022, 6, 1)) == "GONE"
        assert sm.resolve_ticker_at_date("0000000888", day) is None  # not listed anymore on a recent date
    monkeypatch.setattr(nb, "daily_8k", lambda d: [("0000000888", "0000000888-26-000001")] if d == day else [])
    fetched = []
    monkeypatch.setattr(nb.edgar, "fetch_full_text", lambda cik, acc: fetched.append(acc) or "")
    out = nb.backfill_news_8k(engine, days=5)
    assert fetched == [] and out["skipped_unlisted"] >= 1  # not listed on that date -> never pulled in by today's list


def test_government_subsidiary_mapping_is_research_only(engine, monkeypatch):
    from app.sources.usaspending import awards as A
    monkeypatch.setattr(A, "parent_name", lambda gid: "Tutor Perini Corp")
    monkeypatch.setattr(A, "get_market_cap", lambda conn, t, d: 1e9)
    award = {"generated_internal_id": "CONT_1", "Award Amount": 6e8, "Base Obligation Date": str(date.today()),
             "Recipient Name": "Tutor Perini Building Subsidiary LLC", "Awarding Agency": "Department of Defense", "Award ID": "X1"}
    with engine.begin() as c:
        ids = A.process_award(c, award, is_backfill=False, first_seen=date.today())
        ev = c.execute(select(events).where(events.c.id == ids[0])).mappings().first()
        row = c.execute(text("SELECT mapping_confidence, mapping_source FROM government_awards")).first()
    assert row[0] == "MEDIUM" and "parent" in row[1]
    assert ev["payload"]["mapping_confidence"] == "MEDIUM" and ev["priority"] <= 3  # never a high-priority alert


def test_oge_first_run_is_history_and_new_documents_alert(engine, monkeypatch):
    from app.sources.trump import oge
    docs = [{"type": "<a href='https://x/1.pdf'>278 Transaction</a>", "name": "Trump, Donald J", "docDate": "2026-08-22T04:21:08"}]

    class R:
        def __init__(self, payload=None, content=b"%PDF-1"):
            self._p, self.content = payload, content

        def json(self):
            return self._p

    monkeypatch.setattr(oge.http, "get", lambda url, **kw: R({"data": docs}) if url == oge.INDEX_URL else R(content=url.encode()))
    monkeypatch.setattr(oge, "pdf_text", lambda b: ("1 APPLE INC (ZBIO) COMMON lourchaso 7/7/2026 Yes $1 000 001 -$5 000 000", 1))
    first = oge.TrumpOgeSource().run_live(engine)
    docs.append({"type": "<a href='https://x/2.pdf'>278 Transaction</a>", "name": "Trump, Donald J", "docDate": "2026-09-22T04:21:08"})
    monkeypatch.setattr(oge, "pdf_text", lambda b: ("2 SOMETHING ELSE sale 8/1/2026 No $15 001 -$50 000", 1))
    second = oge.TrumpOgeSource().run_live(engine)
    with engine.connect() as c:
        evs = c.execute(select(events.c.is_backfill, events.c.priority, events.c.payload).order_by(events.c.id)).mappings().all()
        tick = c.execute(text("SELECT ticker FROM oge_transactions WHERE line_no = 1")).scalar()
    assert first.items == 1 and second.items == 1
    assert evs[0]["is_backfill"] and not evs[1]["is_backfill"] and evs[1]["priority"] >= 5
    assert evs[1]["payload"]["sales"] == 1 and tick == "ZBIO"  # explicit "(TICKER)" only
