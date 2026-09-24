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


# ---------------- reliable notification delivery ----------------

def _mk_event(engine, key="E1", priority=9, ticker="ZBIO", **kw):
    from app.db.store import create_event
    with engine.begin() as c:
        return create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key=key, public_date=date.today(), ticker=ticker,
                            title="t", payload={"is_ceo": True, "value": 1}, source_url="u", priority=priority, **kw)


def _notif(engine, key):
    with engine.connect() as c:
        return c.execute(select(notifications).where(notifications.c.dedupe_key == key)).mappings().first()


def test_dispatch_failure_is_retried_not_lost(engine, monkeypatch):
    from app.notifications import dispatch, telegram
    monkeypatch.setattr(telegram, "configured", lambda: True)
    eid = _mk_event(engine)
    calls = []

    def boom(t):
        calls.append(t)
        raise RuntimeError("telegram 502")
    monkeypatch.setattr(telegram, "send", boom)
    r = dispatch.dispatch(engine)
    assert r["failed"] == 1 and _notif(engine, f"event:{eid}")["status"] == "failed"
    # before next_retry_at: not retried
    assert dispatch.dispatch(engine)["candidates"] == 0
    with engine.begin() as c:
        c.execute(notifications.update().values(next_retry_at=text("now() - interval '1 second'")))
    monkeypatch.setattr(telegram, "send", lambda t: calls.append(t))
    r = dispatch.dispatch(engine)
    n = _notif(engine, f"event:{eid}")
    assert r["sent"] == 1 and n["status"] == "sent" and n["attempts"] == 2 and n["sent_at"] is not None
    assert dispatch.dispatch(engine)["candidates"] == 0  # delivered exactly once
    assert len(calls) == 2


def test_dispatch_recovers_stale_claim_after_crash(engine, monkeypatch):
    from app.notifications import dispatch, telegram
    monkeypatch.setattr(telegram, "configured", lambda: True)
    eid = _mk_event(engine)
    with engine.begin() as c:  # simulate: claimed, then the process died before sending
        c.execute(notifications.insert().values(dedupe_key=f"event:{eid}", event_id=eid, kind="event", status="sending",
                                                attempts=1, claimed_at=text("now() - interval '10 minutes'")))
    sent = []
    monkeypatch.setattr(telegram, "send", lambda t: sent.append(t))
    assert dispatch.dispatch(engine)["sent"] == 1 and len(sent) == 1
    assert _notif(engine, f"event:{eid}")["status"] == "sent"


def test_fresh_claim_is_not_double_sent(engine, monkeypatch):
    from app.notifications import dispatch
    with engine.begin() as c:
        assert dispatch.claim(c, "k", "event") is True
        assert dispatch.claim(c, "k", "event") is False  # another worker holds a fresh claim


def test_send_system_failure_then_retry(engine, monkeypatch):
    from app.notifications import dispatch, telegram

    def boom(t):
        raise RuntimeError("down")
    monkeypatch.setattr(telegram, "send", boom)
    with pytest.raises(RuntimeError):
        dispatch.send_system(engine, "system:x", "hello")
    assert _notif(engine, "system:x")["status"] == "failed"
    with engine.begin() as c:
        c.execute(notifications.update().values(next_retry_at=text("now() - interval '1 second'")))
    got = []
    monkeypatch.setattr(telegram, "send", lambda t: got.append(t))
    assert dispatch.send_system(engine, "system:x", "hello") is True and got == ["hello"]
    assert dispatch.send_system(engine, "system:x", "hello") is False


# ---------------- estimated dates are segregated ----------------

def test_estimated_dates_excluded_from_verified_stats_and_multi_signal(engine):
    from app.backtest.jobs import event_type_stats
    from app.db.store import create_event
    from app.signals.detect import multi_signal_check
    d = date(2025, 5, 1)
    with engine.begin() as c:
        g = create_event(c, event_type="GOV_CONTRACT", dedupe_key="AWARD:est", public_date=d, ticker="ZBIO", title="c",
                         payload={"value_to_mcap": 0.3}, source_url="u", priority=5, is_backfill=True, date_quality="estimated")
        i = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key="B1", public_date=d, ticker="ZBIO", title="b",
                         payload={"is_ceo": True}, source_url="u", priority=5)
        assert multi_signal_check(c, "ZBIO", d) is None  # estimated contract cannot form a multi-signal
        for eid in (g, i):
            c.execute(event_returns.insert().values(event_id=eid, horizon_days=30, entry_date=d, entry_price=1, exit_date=d,
                                                    exit_price=1.1, ret=0.1))
        assert event_type_stats(c, "GOV_CONTRACT", "all", horizons=(30,))[30]["n"] == 0
        assert event_type_stats(c, "INSIDER_OPEN_MARKET_BUY", "all", horizons=(30,))[30]["n"] == 1


# ---------------- house backfill failure queue ----------------

def test_house_year_not_done_while_documents_failed(engine, monkeypatch):
    from app import http as apphttp
    from app.db.store import job_state
    from app.sources.congress import house
    metas = [{"doc_id": d, "name": "Test Member", "state_district": "NY03", "filing_date": date(2023, 3, 1), "year": 2023}
             for d in ("111", "222")]
    monkeypatch.setattr(house, "fetch_index", lambda y: metas if y == 2023 else [])
    pdf = (FX / "house_ptr_20034188.pdf").read_bytes()

    class R:
        content = pdf
    broken = {"222"}

    def get(url, **kw):
        if any(url.endswith(f"/{d}.pdf") for d in broken):
            raise apphttp.HttpError(url, 503, "temporarily unavailable")
        return R()
    monkeypatch.setattr(house.http, "get", get)
    import datetime as dt

    class FakeDate(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2024, 6, 1)
    monkeypatch.setattr(house, "date", FakeDate)
    house.HousePtrSource().run_backfill(engine, years=1)
    with engine.begin() as c:
        st = job_state(c, "backfill_house")
    assert "2023" not in st["done"] and "222" in st["failed"]["2023"]
    assert count(engine, filings, filings.c.source == "house") == 1
    broken.clear()  # source recovers -> restart retries only the failed document
    house.HousePtrSource().run_backfill(engine, years=1)
    with engine.begin() as c:
        st = job_state(c, "backfill_house")
    assert "2023" in st["done"] and not st["failed"]
    assert count(engine, filings, filings.c.source == "house") == 2


# ---------------- transient price failures ----------------

def test_transient_price_failure_is_retried_not_no_data(engine, monkeypatch):
    from app import http as apphttp
    from app.backtest import jobs
    from app.marketdata import provider as prov
    from tests.test_backtest import series
    mode = {"m": "down"}

    class P(prov.MarketDataProvider):
        def get_historical_prices(self, symbol, start, end):
            if symbol == "SPY":
                return [b for b in series(date(2025, 12, 1), 400, base=500) if start <= b.date <= end]
            if mode["m"] == "down":
                raise apphttp.HttpError("x", 503, "unavailable")
            if mode["m"] == "404":
                raise apphttp.HttpError("x", 404, "No data found, symbol may be delisted")
            return [b for b in series(date(2025, 12, 1), 400) if start <= b.date <= end]
    monkeypatch.setattr(prov, "get_provider", lambda: P())
    process(engine, form4_submission())
    r = jobs.compute_pending(engine)
    with engine.connect() as c:
        e = c.execute(select(events)).mappings().first()
    assert r["retry"] == 1 and e["returns_status"] == "retry" and "503" in e["returns_error"]
    with engine.begin() as c:  # time passes, provider recovers
        c.execute(events.update().values(returns_checked_at=text("now() - interval '21 hours'")))
    mode["m"] = "up"
    assert jobs.compute_pending(engine)["processed"] == 1
    with engine.connect() as c:
        assert c.execute(select(events.c.returns_status)).scalar() in ("partial", "complete")


def test_confirmed_missing_symbol_is_no_data(engine, monkeypatch):
    from app import http as apphttp
    from app.backtest import jobs
    from app.marketdata import provider as prov
    from tests.test_backtest import series

    class P(prov.MarketDataProvider):
        def get_historical_prices(self, symbol, start, end):
            if symbol == "SPY":
                return [b for b in series(date(2025, 12, 1), 400, base=500) if start <= b.date <= end]
            raise apphttp.HttpError("x", 404, "No data found, symbol may be delisted")
    monkeypatch.setattr(prov, "get_provider", lambda: P())
    process(engine, form4_submission())
    assert jobs.compute_pending(engine)["no_data"] == 1
