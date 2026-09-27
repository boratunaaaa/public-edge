"""TR / CA / AU canonical events inside the SHARED core (real fixtures -> events table -> quality, multi-signal,
dispatch, news clustering, risk/discovery features, backtest calendar). US behaviour must not change."""
import gzip
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.markets.calendars import calendar_for
from tests.test_db import URL

FX = Path(__file__).parent / "fixtures" / "global"
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def real_events():
    from app.sources.au import asx
    from app.sources.ca import newswire as nw
    from app.sources.tr import kap
    tr = [e for p in (FX / "tr").glob("*.html.gz")
          for e in kap.to_canonical(kap.parse_page(gzip.decompress(p.read_bytes()).decode("utf-8", "ignore"), p.name.split(".")[0]))]
    au = asx.dedupe([e for e in (asx.to_canonical(a) for a in asx.parse_list_page(
        gzip.decompress((FX / "au/prevBusDayAnns.html.gz").read_bytes()).decode())) if e])
    ca = [e for p in (1, 2, 3) for r in nw.parse_sitemap(gzip.decompress((FX / f"ca/newswire_ca_sitemap_p{p}.xml.gz").read_bytes()).decode())
          for e in nw.to_canonical(r)]
    return tr, ca, au


def store(conn, events):
    from app.db.store import create_event
    ids = []
    for e in events:
        cal = calendar_for(e.exchange)
        ids.append(create_event(conn, **e.core_kwargs(cal.local_date(e.published_at))))
    return ids


def test_real_samples_reach_the_minimum_event_counts():
    tr, ca, au = real_events()
    assert len(au) >= 100 and len(tr) >= 20 and len(ca) >= 20
    for evs in (tr, ca, au):
        assert all(e.source_url and e.published_at.tzinfo and e.raw for e in evs)


@db
@pytest.mark.usefixtures("clean")
def test_canonical_events_are_stored_by_the_shared_core(engine):
    from app.db.models import events
    tr, ca, au = real_events()
    with engine.begin() as c:
        ids = store(c, tr + ca + au)
        again = store(c, tr[:5])  # idempotent: same dedupe keys
        rows = c.execute(select(events.c.event_type, events.c.issuer_cik, events.c.payload).where(events.c.id.in_(ids))).mappings().all()
    assert all(i is not None for i in ids) and all(i is None for i in again)
    assert {r["event_type"][:3] for r in rows} == {"TR_", "CA_", "AU_"} and all(r["issuer_cik"] is None for r in rows)
    assert all(r["payload"]["raw"] and r["payload"]["country"] in ("TR", "CA", "AU") for r in rows)


@db
@pytest.mark.usefixtures("clean")
def test_quality_layer_reads_country_event_types(engine):
    from app.backtest import quality as Q
    tr, _, _ = real_events()
    with engine.begin() as c:
        store(c, tr)
        rows = Q.load_rows(c, "TR_SHARE_BUYBACK", "TRUE", {}, 30, as_of=date(2027, 1, 1))
    assert rows and all(r.outcome == "unresolved" for r in rows)  # no prices yet: counted, never silently dropped


@db
@pytest.mark.usefixtures("clean")
def test_us_multi_signal_is_not_affected_by_a_same_ticker_foreign_listing(engine):
    """BHP trades on the ASX and (as an ADR) in New York: AU events must never feed the US multi-signal."""
    from app.db.store import create_event
    from app.signals.detect import multi_signal_check
    from app.sources.au import asx
    a = asx.Announcement("BHP", datetime(2026, 9, 24, 1, tzinfo=timezone.utc), True, "Becoming a substantial holder from MQG", "03149999")
    b = asx.Announcement("BHP", datetime(2026, 9, 24, 2, tzinfo=timezone.utc), True, "Trading Halt", "03149998")
    with engine.begin() as c:
        store(c, [asx.to_canonical(a), asx.to_canonical(b)])
        create_event(c, event_type="ACTIVIST_13D", dedupe_key="us13d", public_date=date(2026, 9, 24), ticker="BHP", issuer_cik="0000811809",
                     title="t", payload={"item4_class": "BOARD_SEAT"}, source_url="u")
        assert multi_signal_check(c, "BHP", date(2026, 9, 25)) is None  # one US source group only


@db
@pytest.mark.usefixtures("clean")
def test_global_events_are_never_alerted(engine, monkeypatch):
    from app.notifications import dispatch
    tr, ca, au = real_events()
    with engine.begin() as c:
        store(c, tr + ca + au)
        c.execute(text("UPDATE events SET public_date = current_date"))
    assert dispatch._candidates(engine) == []  # priority 1 < ALERT_MIN_PRIORITY: stored, not sent


def test_news_clustering_merges_the_same_turkish_story():
    from app.news.cluster import lead_signature, signature_similarity
    a = lead_signature("ALFAS pay geri alım işlemleri", "Şirketimiz tarafından 25 Eylül 2026 tarihinde 33,38 TL fiyattan toplam 100.000 lot ALFAS pay senedi geri alınmıştır.")
    b = lead_signature("ALFAS pay geri alımı", "Şirketimiz tarafından 25 Eylül 2026 tarihinde 33,38 TL fiyattan toplam 100.000 lot ALFAS pay senedi geri alınmıştır.")
    c = lead_signature("MOGAN pay alım bildirimi", "Güriş Holding MOGAN payları ile ilgili olarak 9,09 TL ortalama fiyattan hisse alışı gerçekleştirmiştir.")
    assert signature_similarity(a, b) > signature_similarity(a, c)


def test_risk_and_discovery_features_accept_real_tr_events():
    from app.markets import discovery_vocab, tr_risk
    tr, _, _ = real_events()
    vbts = [e for e in tr if e.family == "TRADING_RESTRICTION" and e.fields.get("measure") == "VBTS"]
    f = tr_risk.extract(vbts[0].ticker, date(2026, 9, 29), tr)
    assert f.flags.get("vbts_active") and f.flags.get("credit_ban")
    r = discovery_vocab.discovery_features("TR", tr)
    assert r["max_level_from_keywords"] in ("NONE", "LOW", "MEDIUM")


def test_backtest_entry_after_an_evening_kap_disclosure_uses_istanbul_calendar():
    from app.backtest.engine import entry_bar
    from app.marketdata.provider import Bar
    cal = calendar_for("XIST")
    bars = [Bar(date(2026, 9, 25) + timedelta(days=i), 1, 1, 1, 1, 1, 1) for i in range(6)]
    b = entry_bar(bars, date(2026, 9, 25), datetime(2026, 9, 25, 15, 6, 26, tzinfo=timezone.utc), cal)  # 18:06 Istanbul
    assert b.date == date(2026, 9, 28)
    # 08:00 UTC = 11:00 Istanbul (BIST session running -> next session) but 04:00 New York (before the NYSE open)
    mid = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
    assert entry_bar(bars, date(2026, 9, 25), mid, cal).date == date(2026, 9, 28)
    assert entry_bar(bars, date(2026, 9, 25), mid).date == date(2026, 9, 25)  # default calendar = NYSE, unchanged
