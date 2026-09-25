"""Integration tests against a real PostgreSQL (TEST_DATABASE_URL). Skipped when unavailable."""
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.db.models import (activist_filings, congress_transactions, event_returns, events, filings, form144_filings,
                           insider_transactions, notifications, source_health)

FX = Path(__file__).parent / "fixtures"
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set"),
              pytest.mark.usefixtures("clean", "no_network_charts")]


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


# ================================================================== V2 integration

def _seed_universe(engine, cik="0001953926", ticker="ZBIO", mcap=400e6, adv=3e6, as_of=None):
    """Snapshot dated on the news day (market cap must be point-in-time; a later snapshot is not used)."""
    from app.db.models import universe_snapshots
    with engine.begin() as c:
        c.execute(universe_snapshots.insert().values(as_of=as_of or date(2026, 3, 31), cik=cik, ticker=ticker, name="Zenas BioPharma, Inc.",
                                                     price=4.0, shares=int(mcap / 4), shares_end=date.today(), market_cap=mcap,
                                                     bucket="EARLY_SMALL", adv_dollar_30d=adv))


def _8k_text(acc="0001234567-26-000001", accepted="20260331163000", body="Zenas BioPharma (NASDAQ: ZBIO) signed a five-year supply agreement "
             "with Pfizer valued at $180 million for commercial production. Production will start in 2027."):
    return (f"<SEC-HEADER>\nACCESSION NUMBER:\t\t{acc}\nCONFORMED SUBMISSION TYPE:\t8-K\nFILED AS OF DATE:\t\t20260331\n"
            f"<ACCEPTANCE-DATETIME>{accepted}\nITEM INFORMATION:\t\tEntry into a Material Definitive Agreement\n</SEC-HEADER>\n"
            f"<DOCUMENT>\n<TYPE>8-K\n<TEXT>\nForm 8-K cover\n</TEXT>\n</DOCUMENT>\n<DOCUMENT>\n<TYPE>EX-99.1\n<TEXT>\n{body}\n</TEXT>\n</DOCUMENT>")


def test_news_8k_cluster_event_and_syndicated_copy(engine):
    from app.db.models import news_items, news_story_clusters
    from app.news.pipeline import RawItem, ingest
    from app.news.sources import process_8k
    _seed_universe(engine)
    with engine.begin() as c:
        ids = process_8k(c, _8k_text(), "1953926", "ZBIO")
    assert ids, "binding $180M supply agreement at a $400M company must be a material event"
    with engine.connect() as c:
        cl = c.execute(select(news_story_clusters)).mappings().first()
        ev = c.execute(select(events).where(events.c.event_type == "MATERIAL_NEWS")).mappings().first()
    assert cl["agreement"] == "commercial_supply_agreement" and cl["binding"] and cl["materiality"] in ("HIGH", "CRITICAL")
    assert cl["value_to_mcap"] == pytest.approx(180e6 / 400e6)
    # public time = SEC acceptance (16:30 ET), exact
    assert ev["public_at"].isoformat().startswith("2026-03-31T20:30") and ev["date_quality"] == "exact"
    # a later syndicated copy (tier 4) joins the same story and cannot move public time later or earlier than the original
    from datetime import datetime as dt, timezone as tz
    with engine.begin() as c:
        ingest(c, RawItem("news_yahoo_watchlist", 4, "copy-1", "https://finance.yahoo.com/x",
                          "Zenas BioPharma signed a five-year supply agreement with Pfizer valued at $180 million",
                          "copy", dt(2026, 3, 31, 21, 5, tzinfo=tz.utc), tickers=["ZBIO"], ciks=["0001953926"]))
    assert count(engine, news_story_clusters) == 1 and count(engine, news_items) == 2
    with engine.connect() as c:
        cl2 = c.execute(select(news_story_clusters)).mappings().first()
    assert cl2["n_items"] == 2 and cl2["public_at"] == cl["public_at"]
    assert count(engine, events, events.c.event_type == "MATERIAL_NEWS") == 1  # 2 copies != 2 signals


def test_mou_press_release_is_not_material(engine):
    from app.news.sources import process_8k
    _seed_universe(engine)
    with engine.begin() as c:
        process_8k(c, _8k_text(body="Zenas BioPharma (NASDAQ: ZBIO) entered into a non-binding memorandum of understanding "
                                    "with Pfizer to explore a strategic partnership."), "1953926", "ZBIO")
    assert count(engine, events, events.c.event_type == "MATERIAL_NEWS") == 0


def test_news_backtest_entry_after_public_time(engine, monkeypatch):
    from app.backtest import jobs
    from app.marketdata import provider as prov
    from app.news.sources import process_8k
    from tests.test_backtest import series

    class P(prov.MarketDataProvider):
        def get_historical_prices(self, symbol, start, end):
            return [b for b in series(date(2025, 12, 1), 400, base=100 if symbol != "SPY" else 500) if start <= b.date <= end]
    monkeypatch.setattr(prov, "get_provider", lambda: P())
    _seed_universe(engine)
    with engine.begin() as c:
        process_8k(c, _8k_text(accepted="20260331090000"), "1953926", "ZBIO")  # 09:00 ET pre-open
    jobs.compute_pending(engine)
    with engine.connect() as c:
        rows = c.execute(select(event_returns).join(events, events.c.id == event_returns.c.event_id)
                         .where(events.c.event_type == "MATERIAL_NEWS")).mappings().all()
    assert rows and all(r["entry_date"] == date(2026, 3, 31) for r in rows)  # same-day OPEN after a 09:00 release
    with engine.begin() as c:
        process_8k(c, _8k_text(acc="0001234567-26-000002", accepted="20260331110000",
                               body="Zenas BioPharma (NASDAQ: ZBIO) was awarded a $90 million contract by the U.S. Army."), "1953926", "ZBIO")
    jobs.compute_pending(engine)
    with engine.connect() as c:
        late = c.execute(select(event_returns.c.entry_date).join(events, events.c.id == event_returns.c.event_id)
                         .where(events.c.dedupe_key.like("NEWS:%"), events.c.public_at > text("'2026-03-31 14:30+00'"))).all()
    assert late and all(r[0] == date(2026, 4, 1) for r in late)  # 11:00 ET release -> next session open


def test_risk_scan_going_concern_and_reverse_split(engine, monkeypatch):
    from app.db.models import discovery_risks
    from app.discovery import risks as R

    def fake(phrase, forms, start, end, max_hits=2000):
        if "going concern" in phrase and "our ability" in phrase:
            return [{"adsh": "0000000000-26-000001", "cik": "0001953926", "form": "10-Q", "file_date": "2026-08-10", "filename": "q.htm"}]
        if phrase == "reverse stock split":
            return [{"adsh": "0000000000-26-000002", "cik": "0001953926", "form": "8-K", "file_date": "2026-07-01", "filename": "r.htm"}]
        return []
    monkeypatch.setattr(R, "efts_search", fake)
    R.scan_text_risks(engine, {"0001953926"})
    with engine.connect() as c:
        got = {r["risk"]: r["severity"] for r in c.execute(select(discovery_risks)).mappings().all()}
    assert got == {"GOING_CONCERN": "HIGH", "REVERSE_SPLIT": "MEDIUM"}


def test_discovery_evaluate_thesis_and_watchlist(engine):
    from app.db.models import company_thesis, discovery_companies, fundamentals_quarterly, watchlist
    from app.discovery.engine import evaluate
    from app.discovery.evidence import extract_from_text
    _seed_universe(engine, as_of=date.today())
    rows = []
    for (y, qn), v in {(2025, 1): 5e6, (2025, 2): 5e6, (2025, 3): 5e6, (2025, 4): 6e6, (2026, 1): 7e6, (2026, 2): 9e6, (2026, 3): 12e6}.items():
        rows.append(dict(cik="0001953926", concept="Revenue", period_end=date(y, 3 * qn, 28), val=v))
    with engine.begin() as c:
        c.execute(fundamentals_quarterly.insert(), rows)
        extract_from_text(c, "0001953926", "Zenas BioPharma is a company that manufactures actuator modules for robots. "
                          "In June 2026, General Motors signed a five-year supply agreement with us for our actuator modules. "
                          "We are the sole-source supplier of these modules and demand exceeds our capacity. "
                          "Tesla is a potential customer and may evaluate our products.",
                          url="https://www.sec.gov/x", accession="a", form="10-K", filed=date(2026, 3, 1))
        u = {"cik": "0001953926", "ticker": "ZBIO", "name": "Zenas BioPharma, Inc.", "market_cap": 400e6, "bucket": "EARLY_SMALL",
             "price": 4.0, "adv_dollar_30d": 3e6}
        r = evaluate(c, u, is_backfill=False)
    assert r["candidate"]
    with engine.connect() as c:
        dc = c.execute(select(discovery_companies)).mappings().first()
        th = c.execute(select(company_thesis)).mappings().first()
        wl = c.execute(select(watchlist)).mappings().first()
    assert [x["counterparty"] for x in dc["customers"]] == ["GM"]  # Tesla "potential customer" is not a customer
    assert dc["stage"] in ("EARLY_COMMERCIAL", "PRODUCTION_RAMP") and "REVENUE_ACCELERATION" in dc["metrics"]["flags"]
    s = th["sections"]
    assert "GM" in s["key_customers"]["text"] and s["key_customers"]["evidence"][0]["source"] == "https://www.sec.gov/x"
    assert s["what_they_do"]["evidence"] and s["latest_material_news"]["text"] is None  # no invented news
    assert wl["category"] in ("DISCOVERY", "HIGH_INTEREST")
    assert count(engine, events, events.c.event_type == "FIRST_AUTOMOTIVE_OEM") == 1


def test_watchlist_manual_override_survives_automation(engine):
    from app.db.models import watchlist
    from app.discovery.watchlist import auto_update, set_category
    with engine.begin() as c:
        set_category(c, "0001953926", "ZBIO", "ARCHIVED", "analyst decision", actor="manual", manual_override=True)
        auto_update(c, "0001953926", "ZBIO", level="HIGH", candidate=True, why=["x"], bucket="EARLY_SMALL")
    with engine.connect() as c:
        assert c.execute(select(watchlist.c.category)).scalar() == "ARCHIVED"


def test_daily_report_quiet_day(engine):
    from app.reports import daily_report
    assert "kayda değer bir gelişme olmadı" in daily_report(engine)


def test_alert_scorecard_counts_only_sent_alerts(engine):
    from datetime import datetime as dt, timedelta as td, timezone as tz
    from app.db.store import create_event
    from app.reports import alert_scorecard
    now = dt.now(tz.utc)
    with engine.begin() as c:
        ids = []
        for i, ex in enumerate((0.05, -0.02, 0.08)):
            eid = create_event(c, event_type="INSIDER_OPEN_MARKET_BUY", dedupe_key=f"S{i}", public_date=date(2026, 5, 1),
                               ticker="ZBIO", title="t", payload={}, source_url="u", priority=6)
            c.execute(event_returns.insert().values(event_id=eid, horizon_days=30, entry_date=date(2026, 5, 2), entry_price=1,
                                                    exit_date=date(2026, 6, 1), exit_price=1, ret=ex, excess_ret=ex))
            ids.append(eid)
        for eid in ids[:2]:  # the third alert was never sent -> must not count
            c.execute(notifications.insert().values(dedupe_key=f"event:{eid}", event_id=eid, kind="event", status="sent",
                                                    attempts=1, sent_at=now - td(days=60)))
        lines = alert_scorecard(c, now)
    assert "gönderilen 2 bildirim: 1 tanesinde" in lines[0] and "1 tanesinde geçemedi" in lines[0]
    assert "0 tanesinin sonucu ölçülemedi" in lines[0]


def test_evaluate_all_end_to_end(engine):
    """Runs the real universe selection + evaluation loop (regression: result-level .distinct() crashed it)."""
    from app.db.models import discovery_companies, fundamentals_quarterly
    from app.discovery.engine import evaluate_all
    from app.discovery.evidence import extract_from_text
    _seed_universe(engine, as_of=date.today())
    with engine.begin() as c:
        c.execute(fundamentals_quarterly.insert(), [dict(cik="0001953926", concept="Revenue", period_end=date.today() - timedelta(days=30),
                                                         val=5e6)])
        extract_from_text(c, "0001953926", "General Motors signed a five-year supply agreement with us for our modules.",
                          url="u", accession="a", form="10-K", filed=date.today())
    stats = evaluate_all(engine, is_backfill=True)
    assert stats["errors"] == 0 and stats["evaluated"] == 1
    assert count(engine, discovery_companies) == 1
