"""VBTS LIVE_INCREMENTAL mode: checkpoint / watermark, overlap for late documents, rate-limit handling, restart safety.
A fake KAP (index -> page) built from the real disclosure 1668314 with only the Turkish decision sentence changed."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.sources.tr import vbts as V
from tests.test_tr_vbts import REAL, _tr_master, page

UTC = timezone.utc


def vb(i, code="DMRGD", start="28/09/2026", end="27/10/2026", measure="açığa satışa ve kredili işlemlere konu edilemeyecektir"):
    return page(f"{code}.E payları {start} tarihli işlemlerden (seans başından) {end} tarihli işlemlere (seans sonuna) kadar {measure}.",
                i, related=code)


def other(i):
    return vb(i).replace("Pay Piyasasında Volatilite Bazlı Tedbir Sistemi", "Hak Kullanımı")


class FakeKap:
    """Mutable fake KAP: pages can appear later (late publication), 429 / timeouts can be injected per index."""
    def __init__(self, pages: dict, top: int):
        self.pages, self.top, self.fail, self.calls = dict(pages), top, {}, []

    def fetch(self, i):
        self.calls.append(i)
        f = self.fail.get(i)
        if f == 429:
            raise V.RateLimited("429")
        if f == "timeout":
            raise V.Unavailable("timeout")
        return self.pages.get(i, "")

    def latest(self):
        return self.top


@pytest.fixture
def cfg():
    return V.LiveConfig(min_spacing_s=0, backoff_429_s=120, first_lookback=10, live_max_gap=100, max_pages_per_poll=500,
                        late_window_hours=6, max_parse_retries=2)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)

    def __call__(self):
        return self.t


def _col(engine, kap, cfg, clock=None):
    return V.LiveVbtsCollector(engine, fetch=kap.fetch, latest_index=kap.latest, cfg=cfg, clock=clock or Clock(), sleep=lambda s: None)


def _events(engine):
    with engine.connect() as c:
        return sorted(r[0] for r in c.execute(text("SELECT record_key FROM dataset_records WHERE dataset = 'tr_vbts'")))


def _setup(engine):
    with engine.begin() as c:
        for code in ("DMRGD", "ESKLT", "LATEA"):
            _tr_master(c, code)


def test_first_poll_is_a_small_window_then_no_new_event(engine, clean, cfg):
    _setup(engine)
    kap = FakeKap({i: other(i) for i in range(1, 1001)} | {995: vb(995)}, top=1000)
    col = _col(engine, kap, cfg)
    r1 = col.poll()
    assert r1["outcome"] == "NEW_EVENTS" and r1["scanned"] == 10 and min(kap.calls) == 991  # never history
    kap.calls.clear()
    r2 = col.poll()
    assert r2["outcome"] == "NO_NEW_EVENT" and kap.calls == [] and len(_events(engine)) == 2  # nothing above the watermark


def test_one_new_document_and_duplicate_document(engine, clean, cfg):
    _setup(engine)
    kap = FakeKap({i: other(i) for i in range(1, 1001)}, top=1000)
    col = _col(engine, kap, cfg)
    col.poll()
    kap.pages[1001], kap.pages[1002] = vb(1001, "ESKLT"), other(1002)
    kap.top = 1002
    r = col.poll()
    assert r["outcome"] == "NEW_EVENTS" and r["new_documents"] == 1 and r["events"] == 2 and kap.calls[-2:] == [1001, 1002]
    # the same decision re-published under a new index is a new document; the SAME document re-read is not
    V.collect_range(engine, 1001, 1001, lambda i: kap.pages[i])
    assert len(_events(engine)) == 2


def test_checkpoint_restart_gives_the_same_events_as_an_uninterrupted_run(engine, clean, cfg):
    _setup(engine)
    pages = {i: other(i) for i in range(1, 1051)} | {1003: vb(1003), 1020: vb(1020, "ESKLT"), 1041: vb(1041, "LATEA")}
    kap = FakeKap(pages, top=1000)
    _col(engine, kap, cfg).poll()
    kap.top = 1050
    _col(engine, kap, cfg).poll()
    uninterrupted = _events(engine)
    with engine.begin() as c:
        c.execute(text("DELETE FROM dataset_records")); c.execute(text("DELETE FROM processing_jobs"))
    kap2 = FakeKap(pages, top=1000)
    _col(engine, kap2, cfg).poll()
    kap2.top, kap2.fail[1030] = 1050, "timeout"
    r = _col(engine, kap2, cfg).poll()  # dies mid-way: pages up to 1029 committed with their checkpoint
    assert r["outcome"] == "SOURCE_UNAVAILABLE"
    with engine.connect() as c:
        from app.db.store import job_state
        assert job_state(c, V.LIVE_JOB)["next_index"] == 1030  # watermark stops AT the failing index
    kap2.fail.clear()
    kap2.calls.clear()
    _col(engine, kap2, cfg).poll()  # a new process (restart)
    assert min(kap2.calls) == 1030 and _events(engine) == uninterrupted


def test_429_does_not_advance_watermark_backs_off_and_is_not_no_event(engine, clean, cfg):
    _setup(engine)
    kap = FakeKap({i: other(i) for i in range(1, 1100)}, top=1000)
    clock = Clock()
    col = _col(engine, kap, cfg, clock)
    col.poll()
    kap.top, kap.fail[1005] = 1010, 429
    r = col.poll()
    from app.db.store import job_state
    with engine.connect() as c:
        st = job_state(c, V.LIVE_JOB)
    assert r["outcome"] == "SOURCE_RATE_LIMITED" and st["next_index"] == 1005 and st["metrics"]["http_429"] == 1
    assert st["source_status"] == "SOURCE_RATE_LIMITED" and st["consecutive_failures"] == 1
    kap.calls.clear()
    clock.t += timedelta(seconds=60)
    assert col.poll()["outcome"] == "BACKING_OFF" and kap.calls == []  # no aggressive retry
    clock.t += timedelta(seconds=120)
    kap.fail.clear()
    assert col.poll()["outcome"] == "NO_NEW_EVENT" and min(kap.calls) == 1005
    # a second 429 in a row doubles the wait
    kap.top, kap.fail[1011] = 1012, 429
    col.poll()
    kap.fail[1011] = 429
    clock.t += timedelta(seconds=121)
    col.poll()
    with engine.connect() as c:
        st = job_state(c, V.LIVE_JOB)
    assert datetime.fromisoformat(st["backoff_until"]) - clock.t == timedelta(seconds=240)


def test_overlap_catches_late_document_without_duplicates(engine, clean, cfg):
    _setup(engine)
    kap = FakeKap({i: other(i) for i in range(1, 1001) if i != 998}, top=1000)  # 998 not visible yet
    clock = Clock()
    col = _col(engine, kap, cfg, clock)
    col.poll()
    assert _events(engine) == []
    kap.pages[998] = vb(998, "LATEA")  # appears later, with an older index / publication time
    clock.t += timedelta(minutes=30)
    r = col.poll()
    assert r["reprobed"] == 1 and r["events"] == 2
    clock.t += timedelta(minutes=30)
    kap.calls.clear()
    col.poll()
    assert 998 not in kap.calls and len(_events(engine)) == 2  # found once, never re-counted


def test_missing_index_is_given_up_after_the_late_window(engine, clean, cfg):
    kap = FakeKap({i: other(i) for i in range(1, 1001) if i != 999}, top=1000)
    clock = Clock()
    col = _col(engine, kap, cfg, clock)
    col.poll()
    clock.t += timedelta(hours=7)
    col.poll()
    kap.calls.clear()
    col.poll()
    assert 999 not in kap.calls


def test_parse_error_does_not_lose_the_document(engine, clean, cfg):
    _setup(engine)
    kap = FakeKap({i: other(i) for i in range(1, 1001)} | {997: "<html>half page</html>"}, top=1000)
    clock = Clock()
    col = _col(engine, kap, cfg, clock)
    r = col.poll()
    assert r["outcome"] == "SOURCE_PARSE_ERROR" and _events(engine) == []
    kap.pages[997] = vb(997)  # the page renders correctly on a later read
    clock.t += timedelta(minutes=10)
    r = col.poll()
    assert r["events"] == 2 and len(_events(engine)) == 2


def test_unavailable_source_is_not_no_event_and_schema_change_is_flagged(engine, clean, cfg):
    kap = FakeKap({}, top=1000)
    kap.latest = lambda: (_ for _ in ()).throw(V.Unavailable("connect timeout"))
    col = _col(engine, kap, cfg)
    assert col.poll()["outcome"] == "SOURCE_UNAVAILABLE"
    kap2 = FakeKap({}, top=1000)
    kap2.latest = lambda: (_ for _ in ()).throw(V.FormatChanged("member page changed"))
    assert _col(engine, kap2, cfg).poll()["outcome"] == "SOURCE_SCHEMA_CHANGED"


def test_live_never_scans_a_wide_gap(engine, clean, cfg):
    kap = FakeKap({i: other(i) for i in range(1, 1001)}, top=1000)
    col = _col(engine, kap, cfg)
    col.poll()
    kap.top = 5000
    kap.calls.clear()
    r = col.poll()
    assert r["outcome"] == "SOURCE_GAP" and kap.calls == []


def test_repeated_polling_is_idempotent_and_metrics(engine, clean, cfg):
    _setup(engine)
    kap = FakeKap({i: other(i) for i in range(1, 1001)} | {996: vb(996)}, top=1000)
    col = _col(engine, kap, cfg)
    for _ in range(5):
        col.poll()
    with engine.connect() as c:
        m = V.vbts_metrics(c)
    assert len(_events(engine)) == 2
    assert m["vbts_last_seen_document"] == "KAP:996" and m["vbts_events_today"] in (0, 2)  # metrics are per UTC day
    assert m["vbts_source_status"] in ("NO_NEW_EVENT", "NEW_EVENTS") and m["vbts_next_index"] == 1001


def test_poll_interval_follows_the_istanbul_market_clock():
    ist = V.IST
    cfg = V.LiveConfig(poll_decision_window_s=600, poll_market_s=1800, poll_off_s=7200)
    assert V.poll_interval(datetime(2026, 9, 28, 18, 30, tzinfo=ist), cfg) == 600  # Monday after the close
    assert V.poll_interval(datetime(2026, 9, 28, 11, 0, tzinfo=ist), cfg) == 1800  # in session
    assert V.poll_interval(datetime(2026, 9, 28, 23, 0, tzinfo=ist), cfg) == 7200
    assert V.poll_interval(datetime(2026, 9, 27, 18, 30, tzinfo=ist), cfg) == 7200  # Sunday


def test_historical_mode_is_explicit_and_separate(engine, clean):
    out = V.collect_range(engine, 1, 3, lambda i: other(i))
    assert out["scanned"] == 3
    with engine.connect() as c:
        from app.db.store import job_state
        assert job_state(c, V.LIVE_JOB) == {}  # the historical run never touches the live checkpoint
