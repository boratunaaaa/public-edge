"""Integration tests against a real PostgreSQL (TEST_DATABASE_URL). Skipped when unavailable."""
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text

from app.db.models import (activist_filings, congress_transactions, event_returns, events, filings, form144_filings,
                           insider_transactions, metadata, notifications, source_health)

FX = Path(__file__).parent / "fixtures"
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL)
    metadata.drop_all(eng)
    metadata.create_all(eng)
    yield eng
    metadata.drop_all(eng)


@pytest.fixture(autouse=True)
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


def form4_submission(acc="0001104659-26-038031", owner_xml_patch=None, accepted="20260331163000", filed="20260331"):
    xml = (FX / "form4_purchase.xml").read_text()
    if owner_xml_patch:
        xml = owner_xml_patch(xml)
    return (f"<SEC-HEADER>\nACCESSION NUMBER:\t\t{acc}\nCONFORMED SUBMISSION TYPE:\t4\nFILED AS OF DATE:\t\t{filed}\n"
            f"<ACCEPTANCE-DATETIME>{accepted}\n</SEC-HEADER>\n<DOCUMENT>\n<TYPE>4\n<FILENAME>x.xml\n<TEXT>\n<XML>\n{xml}\n</XML>\n</TEXT>\n</DOCUMENT>")


def count(engine, table, *where):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def process(engine, txt, cik="1953926"):
    from app.sources.sec.processing import process_text
    with engine.begin() as c:
        return process_text(c, txt, cik)


def test_duplicate_filing_is_processed_once(engine):
    first = process(engine, form4_submission())
    second = process(engine, form4_submission())
    from app.parsers.form4 import parse_form4
    n_tx = len(parse_form4((FX / "form4_purchase.xml").read_text()).transactions)
    assert count(engine, filings) == 1
    assert count(engine, insider_transactions) == n_tx
    assert len(first) == 1 and second == []
    ev = count(engine, events, events.c.event_type == "INSIDER_OPEN_MARKET_BUY")
    assert ev == 1
    with engine.connect() as c:
        e = c.execute(select(events)).mappings().first()
    assert e["ticker"] == "ZBIO" and e["payload"]["is_ceo"] and e["payload"]["value"] == 364600.0 + 656540.0
    assert e["public_date"] == date(2026, 3, 31) and e["transaction_date"] == date(2026, 3, 30)
    assert str(e["public_at"].astimezone().utcoffset()) is not None


def test_cluster_event_once_and_multi_signal(engine):
    process(engine, form4_submission())
    other = form4_submission(acc="0001104659-26-038099", owner_xml_patch=lambda x: x.replace(
        "MOULDER LEON O JR", "OTHER DIRECTOR").replace("<rptOwnerCik>0001228865", "<rptOwnerCik>0009999999").replace(
        "Chief Executive Officer", "Director"))
    ids = process(engine, other)
    assert count(engine, events, events.c.event_type == "CLUSTER_INSIDER_BUY") == 1
    process(engine, other)
    assert count(engine, events, events.c.event_type == "CLUSTER_INSIDER_BUY") == 1
    with engine.connect() as c:
        cl = c.execute(select(events).where(events.c.event_type == "CLUSTER_INSIDER_BUY")).mappings().first()
    assert cl["payload"]["number_of_buyers"] == 2 and cl["payload"]["CEO_present"]
    # CEO buy + insider cluster are the SAME evidence source -> no multi-signal
    assert count(engine, events, events.c.event_type == "MULTI_SIGNAL_EVENT") == 0
    assert ids
    # an independent source (federal contract >2% of mcap) on the same ticker -> multi-signal
    from app.db.store import create_event
    from app.signals.detect import multi_signal_check
    with engine.begin() as c:
        create_event(c, event_type="GOV_CONTRACT", dedupe_key="AWARD:test", public_date=date(2026, 4, 2), ticker="ZBIO",
                     title="contract", payload={"value_to_mcap": 0.08}, source_url="u", priority=3)
        mid = multi_signal_check(c, "ZBIO", date(2026, 4, 2))
        again = multi_signal_check(c, "ZBIO", date(2026, 4, 2))
    assert mid and again is None
    with engine.connect() as c:
        m = c.execute(select(events).where(events.c.id == mid)).mappings().first()
    assert m["payload"]["source_groups"] == ["CONTRACT", "INSIDER"]


def test_13d_idempotent_and_classified(engine):
    txt = (FX / "sc13d_full.txt").read_text(encoding="latin-1")
    a = process(engine, txt, "1737450")
    b = process(engine, txt, "1737450")
    assert count(engine, activist_filings) == 1 and len(a) >= 1 and b == []
    with engine.connect() as c:
        row = c.execute(select(activist_filings)).mappings().first()
        ev = c.execute(select(events).where(events.c.event_type == "ACTIVIST_13D")).mappings().first()
    assert row["ticker"] == "OPRA" and row["is_13d"] and row["is_amendment"] and row["percent"] == 67.9
    assert ev["public_at"] is not None and ev["payload"]["item4_class"]


def test_form144_stored_not_major(engine):
    process(engine, (FX / "f144_full.txt").read_text(encoding="latin-1"), "1204818")
    assert count(engine, form144_filings) == 1
    assert count(engine, events) == 0  # $715k proposal is information only, below "major" thresholds


def test_backtest_uses_filing_date_not_transaction_date(engine, monkeypatch):
    from app.backtest import jobs
    from app.marketdata import provider as prov
    from tests.test_backtest import series

    class FakeProvider(prov.MarketDataProvider):  # test-only deterministic prices
        def get_historical_prices(self, symbol, start, end):
            return [b for b in series(date(2025, 12, 1), 400, base=100 if symbol != "SPY" else 500) if start <= b.date <= end]

    monkeypatch.setattr(prov, "get_provider", lambda: FakeProvider())
    process(engine, form4_submission())
    r = jobs.compute_pending(engine)
    assert r["processed"] == 1
    with engine.connect() as c:
        rows = c.execute(select(event_returns)).mappings().all()
        e = c.execute(select(events)).mappings().first()
    assert rows
    for row in rows:
        # accepted 16:30 ET on 2026-03-31 -> entry must be the 2026-04-01 open, never the 03-30 transaction date
        assert row["entry_date"] == date(2026, 4, 1)
        assert row["entry_date"] > e["transaction_date"]
    assert e["returns_status"] in ("partial", "complete")


def test_congress_cluster(engine):
    from app.sources.congress.house import congress_cluster_check
    base = dict(chamber="house", asset_name="Tutor Perini (TPC) [ST]", asset_type="ST", ticker="TPC", transaction_type="PURCHASE",
                amount_min=1001, amount_max=15000, amount_range="$1,001 - $15,000", owner="self", source_url="u")
    with engine.begin() as c:
        c.execute(congress_transactions.insert(), [
            dict(base, doc_id="1", line_no=0, politician="A Member", transaction_date=date(2026, 9, 1), filing_date=date(2026, 9, 10)),
            dict(base, doc_id="2", line_no=0, politician="B Member", transaction_date=date(2026, 9, 5), filing_date=date(2026, 9, 20)),
            dict(base, doc_id="3", line_no=0, politician="C Member", transaction_date=date(2026, 9, 6), filing_date=date(2026, 10, 30)),
        ])
        ids = congress_cluster_check(c, "TPC", date(2026, 9, 20))
        again = congress_cluster_check(c, "TPC", date(2026, 9, 20))
    assert len(ids) == 1 and again == []
    with engine.connect() as c:
        ev = c.execute(select(events)).mappings().first()
    # member C's report is not public yet on 2026-09-20 -> must not be counted
    assert ev["payload"]["n_members"] == 2 and ev["public_date"] == date(2026, 9, 20)


def test_notification_dedupe(engine, monkeypatch):
    from app.notifications import dispatch, telegram
    sent = []
    monkeypatch.setattr(telegram, "send", lambda t: sent.append(t))
    assert dispatch.send_system(engine, "system:test", "hello") is True
    assert dispatch.send_system(engine, "system:test", "hello") is False
    assert sent == ["hello"] and count(engine, notifications) == 1


def test_source_health_failure_counter(engine):
    from app.db.store import health_failure, health_success
    with engine.begin() as c:
        assert health_failure(c, "x", "boom") == 1
        assert health_failure(c, "x", "boom") == 2
        assert health_failure(c, "x", "boom") == 3
    with engine.connect() as c:
        assert c.execute(select(source_health.c.status).where(source_health.c.source == "x")).scalar() == "ERROR"
    with engine.begin() as c:
        health_success(c, "x", 5)
    with engine.connect() as c:
        row = c.execute(select(source_health).where(source_health.c.source == "x")).mappings().first()
    assert row["consecutive_failures"] == 0 and row["status"] == "WORKING"
