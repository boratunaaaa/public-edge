"""BIST VBTS collector V1: real KAP disclosure 1668314 (tests/fixtures/global/tr) + edge-case pages derived from it
(structure unchanged, only the Turkish decision sentence replaced with BIST's standard wording)."""
import gzip
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.sources.tr import vbts as V

IST = ZoneInfo("Europe/Istanbul")
REAL = gzip.open(Path(__file__).parent / "fixtures" / "global" / "tr" / "1668314.html.gz").read().decode("utf-8", "replace")
ORIG = "DMRGD.E payları 28/09/2026 tarihli işlemlerden (seans başından) 27/10/2026 tarihli işlemlere (seans sonuna) kadar açığa satışa ve kredili işlemlere konu edilemeyecektir."


def page(sentence: str, index: int, published: str = "2026.09.25 18:21:09", related: str = "DMRGD") -> str:
    p = REAL.replace(ORIG, sentence).replace('1668314', str(index)).replace('2026.09.25 18:21:09', published)
    return p.replace('"relatedStocks\\":\\"DMRGD\\"', f'"relatedStocks\\":\\"{related}\\"')


def at(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=IST)


def _tr_master(c, code="DMRGD", valid_from=date(2025, 1, 1)):
    from app.markets import master_db
    sid = f"TR:XIST:{code}"
    master_db.upsert_security(c, security_id=sid, country="TR", exchange="XIST", issuer_id=f"OID-{code}", issuer_name=code)
    master_db.add_symbol_period(c, sid, code, valid_from, None, "test", exchange="XIST")
    return sid


def test_real_disclosure_publication_vs_effective_dates():
    d = V.parse_page(REAL, 1668314)
    evs = V.extract_events(d)
    assert [(e.ticker_at_event, e.measure_type) for e in evs] == [("DMRGD", "SHORT_SELL_BAN"), ("DMRGD", "MARGIN_TRADING_BAN")]
    e = evs[0]
    assert e.available_at == datetime(2026, 9, 25, 15, 21, 9, tzinfo=timezone.utc)  # publication, exact
    assert (e.effective_from, e.effective_to) == (date(2026, 9, 28), date(2026, 10, 27))  # never the knowledge time
    assert e.source_document_id == "KAP:1668314" and e.parser_version == "vbts-v1" and len(e.raw_payload_hash) == 64


def test_non_vbts_disclosure_is_ignored_and_bist_50_note_is_not_a_measure():
    other = REAL.replace("Pay Piyasasında Volatilite Bazlı Tedbir Sistemi", "Hak Kullanımı")
    assert V.parse_page(other, 1) is None
    d = V.parse_page(REAL, 1668314)
    assert all(e.ticker_at_event == "DMRGD" for e in V.extract_events(d))


def test_multiple_securities_and_measures_in_one_sentence():
    s = ("ARMGD.E, KARSN.E ve DGNMO.E paylarında 11/09/2026 tarihli işlemlerden (seans başından) 09/10/2026 tarihli işlemlere "
         "(seans sonuna) kadar brüt takas uygulanacak, açığa satış yapılamayacaktır.")
    evs = V.extract_events(V.parse_page(page(s, 900001), 900001))
    assert {(e.ticker_at_event, e.measure_type) for e in evs} == {
        (c, m) for c in ("ARMGD", "KARSN", "DGNMO") for m in ("SHORT_SELL_BAN", "GROSS_SETTLEMENT")}
    assert len({e.source_document_id for e in evs}) == 1  # document link kept across normalized events


def test_unknown_measure_wording_is_other_not_guessed():
    s = "XYZAB.E paylarında 01/10/2026 tarihli işlemlerden 15/10/2026 tarihli işlemlere kadar ek tedbir uygulanacaktır."
    evs = V.extract_events(V.parse_page(page(s, 900002), 900002))
    assert [e.measure_type for e in evs] == ["OTHER_VBTS"]


def test_malformed_page_and_block_page():
    with pytest.raises(V.FormatChanged):
        V.parse_page("<html><body>maintenance</body></html>", 5)
    with pytest.raises(V.SourceBlocked):
        V.parse_page("<html>Request Rejected 150207-PPE13</html>", 6)
    from app.core.source_state import classify_exception
    assert classify_exception(V.FormatChanged("x"))[0] == "SCHEMA_CHANGED"
    assert classify_exception(V.SourceBlocked("x"))[0] == "BLOCKED"


def test_same_document_is_idempotent(engine, clean):
    with engine.begin() as c:
        _tr_master(c)
    fetch = {1668314: REAL}.get
    a = V.collect_range(engine, 1668314, 1668314, lambda i: fetch(i, ""))
    b = V.collect_range(engine, 1668314, 1668314, lambda i: fetch(i, ""))
    with engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM dataset_records WHERE dataset = 'tr_vbts'")).scalar()
    assert (a["stored"], b["stored"], b["duplicates"], n) == (2, 0, 2, 2)


def test_unresolved_code_is_review_required_never_bound(engine, clean):
    with engine.begin() as c:
        _tr_master(c, valid_from=date(2026, 9, 28))  # the code's known period starts AFTER the decision
    out = V.collect_range(engine, 1668314, 1668314, lambda i: REAL)
    with engine.connect() as c:
        rows = c.execute(text("SELECT security_id, needs_review, payload FROM dataset_records")).all()
    assert out["review_required"] == 2
    assert all(r[0] is None and r[1] and r[2]["data_quality_state"] == "REVIEW_REQUIRED" for r in rows)
    assert all(r[2]["candidate_security_id"] == "TR:XIST:DMRGD" for r in rows)  # recorded for the reviewer only


def test_state_is_point_in_time_and_expires(engine, clean):
    with engine.begin() as c:
        sid = _tr_master(c)
    V.collect_range(engine, 1668314, 1668314, lambda i: REAL)
    with engine.connect() as c:
        before_pub = V.get_vbts_state(c, sid, at("2026-09-25 18:00"))
        after_pub_before_effect = V.get_vbts_state(c, sid, at("2026-09-26 12:00"))
        in_force = V.get_vbts_state(c, sid, at("2026-09-28 10:00"))
        last_day = V.get_vbts_state(c, sid, at("2026-10-27 17:00"))
        expired = V.get_vbts_state(c, sid, at("2026-10-28 10:00"))
    assert not before_pub["short_sell_ban"] and not after_pub_before_effect["short_sell_ban"]
    assert in_force["short_sell_ban"] and in_force["margin_trading_ban"] and not in_force["gross_settlement"] and in_force["level"] == 1
    assert last_day["short_sell_ban"] and not expired["short_sell_ban"] and expired["active"] == []


def test_overlapping_measures_and_escalation_sequence(engine, clean):
    with engine.begin() as c:
        sid = _tr_master(c, "ESKLT")
    docs = {
        910001: page("ESKLT.E payları 01/09/2026 tarihli işlemlerden (seans başından) 30/09/2026 tarihli işlemlere (seans sonuna) kadar "
                     "açığa satışa ve kredili işlemlere konu edilemeyecektir.", 910001, "2026.08.29 18:30:00", "ESKLT"),
        910002: page("ESKLT.E paylarında 15/09/2026 tarihli işlemlerden (seans başından) 14/10/2026 tarihli işlemlere (seans sonuna) kadar "
                     "brüt takas uygulanacaktır.", 910002, "2026.09.12 18:30:00", "ESKLT"),
        910003: page("ESKLT.E payları 22/09/2026 tarihli işlemlerden (seans başından) 21/10/2026 tarihli işlemlere (seans sonuna) kadar "
                     "tek fiyat yöntemiyle işlem görecektir.", 910003, "2026.09.19 18:30:00", "ESKLT"),
    }
    V.collect_range(engine, 910001, 910003, lambda i: docs.get(i, ""))
    with engine.connect() as c:
        st = V.get_vbts_state(c, sid, at("2026-09-23 11:00"))
        esc = V.vbts_escalation(c, sid, at("2026-10-01 00:00"))
        esc_early = V.vbts_escalation(c, sid, at("2026-09-13 00:00"))
    assert st["short_sell_ban"] and st["gross_settlement"] and st["single_price"] and st["level"] == 3
    assert [(e["level_before"], e["level"], e["escalation"]) for e in esc] == [(0, 1, True), (1, 2, True), (2, 3, True)]
    assert len(esc_early) == 2  # a decision published later is never visible earlier


def test_source_unavailable_is_not_a_quiet_day(engine, clean, monkeypatch):
    monkeypatch.setenv("PE_VBTS_MIN_SPACING_S", "0")
    from app.core.source_state import RunOutcome, classify_exception, record_run
    from app.http import HttpError

    def down(i):
        raise HttpError("https://www.kap.org.tr/tr/Bildirim/1", 429, "Too Many Requests")
    src = V.VbtsSource(fetch=down, latest_index=lambda: 100)
    with pytest.raises(V.RateLimited) as e:
        src.run_live(engine)
    kind, status = classify_exception(e.value)
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    with engine.begin() as c:
        record_run(c, "tr_vbts", RunOutcome(ok=False, failure_kind=kind, response_status=status), now=now)
        down_day = c.execute(text("SELECT status FROM source_daily_status WHERE source = 'tr_vbts'")).scalar()
    with engine.begin() as c:
        c.execute(text("DELETE FROM processing_jobs WHERE name = 'tr_vbts_live'"))
    quiet = V.VbtsSource(fetch=lambda i: page("x", i).replace("Pay Piyasasında Volatilite Bazlı Tedbir Sistemi", "Hak Kullanımı"),
                         latest_index=lambda: 60)
    res = quiet.run_live(engine)
    with engine.begin() as c:
        record_run(c, "tr_vbts_q", RunOutcome(ok=True, items=res.items, events=len(res.events)), now=now)
        quiet_day = c.execute(text("SELECT status FROM source_daily_status WHERE source = 'tr_vbts_q'")).scalar()
    assert (kind, down_day, quiet_day) == ("API_QUOTA", "API_QUOTA", "TRUE_NO_EVENT")


def test_majority_malformed_run_raises_schema_changed(engine, clean):
    with pytest.raises(V.FormatChanged):
        V.collect_range(engine, 1, 40, lambda i: "<html>new layout</html>")
    few = V.collect_range(engine, 1, 30, lambda i: "<html>x</html>" if i == 3 else page("x", i).replace(
        "Pay Piyasasında Volatilite Bazlı Tedbir Sistemi", "Hak Kullanımı"))
    assert few["unparseable"] == 1 and few["documents"] == 0
