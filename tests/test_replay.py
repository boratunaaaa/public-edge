"""Historical replay V1: point-in-time visibility, leakage guards, production-pipeline reuse, isolation, determinism."""
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from app.core.clock import ReplayClock, use_clock
from app.marketdata.calendar import is_trading_day
from app.marketdata.provider import Bar, ShareFact
from app.replay.config import ReplayConfig
from app.replay.errors import ReplayIsolationError, ReplayLeakage
from app.replay.source import SourceData

URL = os.environ.get("TEST_DATABASE_URL")
db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
ACME, OTHER = "0000000101", "0000000202"
UTC = timezone.utc


def _series(sym_start: date, end: date, price: float, vol: int, overrides: dict | None = None) -> list[Bar]:
    out, d, i = [], sym_start, 0
    while d <= end:
        if is_trading_day(d):
            p = price * (1 + 0.002 * i)
            v = (overrides or {}).get(d, vol)
            out.append(Bar(d, p, p * 1.01, p * 0.99, p * 1.001, p * 1.001, v))
            i += 1
        d += timedelta(days=1)
    return out


def _event(eid: int, avail: datetime, *, cik=ACME, ticker="ACME", et="INSIDER_OPEN_MARKET_BUY") -> dict:
    return {"id": eid, "event_type": et, "dedupe_key": f"t{eid}", "ticker": ticker, "company_name": "Acme Corp",
            "issuer_cik": cik, "public_date": avail.date(), "public_at": avail, "title": f"buy {eid}",
            "payload": {"value": 250000}, "source_url": "u", "available_at": avail, "date_quality": "exact"}


def source(events=None, acme_overrides=None, **kw) -> SourceData:
    start, end = date(2020, 10, 1), date(2021, 9, 30)
    return SourceData(
        events=events if events is not None else [_event(1, datetime(2021, 2, 5, 21, 30, tzinfo=UTC))],
        bars={"SPY": _series(start, end, 380.0, 50_000_000), "ACME": _series(start, end, 20.0, 200_000, acme_overrides)},
        symbols=[{"cik": ACME, "symbol": "ACME", "first_seen": date(2015, 1, 5), "last_seen": date(2021, 9, 1),
                  "source": "form345_filing"}],
        companies={ACME: "Acme Corp", OTHER: "Other Holdings Inc"},
        share_facts={ACME: [ShareFact("a1", date(2020, 9, 30), date(2020, 11, 2), "10-Q", 10_000_000)]},
        security_master=[{"cik": ACME, "name": "Acme Corp", "sector": None, "delisting_date": kw.get("delisting"),
                          "delisting_reason": "MERGER" if kw.get("delisting") else None}],
        label="unit-fixture")


def cfg(rid="t1", start=date(2021, 2, 1), end=date(2021, 3, 31), **kw) -> ReplayConfig:
    return ReplayConfig(replay_id=rid, start=datetime(start.year, start.month, start.day, tzinfo=UTC),
                        end=datetime(end.year, end.month, end.day, 23, tzinfo=UTC), **kw)


def run(c: ReplayConfig, src: SourceData):
    from app.replay.engine import ReplayRun
    r = ReplayRun(c, src, URL)
    return r, r.run()


def q(schema: str, sql: str, **p):
    eng = create_engine(URL, connect_args={"options": f"-csearch_path={schema}"})
    with eng.connect() as c:
        out = c.execute(text(sql), p).all()
    eng.dispose()
    return out


# ------------------------------------------------------------------ 1-2. event visibility

@db
def test_future_event_invisible_and_visible_exactly_at_available_at():
    from app.replay.store import ReplayStore
    from app.replay.visibility import Guards, Stager
    t = datetime(2021, 2, 5, 21, 30, tzinfo=UTC)
    src = source([_event(1, t), _event(2, t + timedelta(seconds=1))])
    st = ReplayStore(URL, "replay_vis")
    st.create()
    stager = Stager(src, Guards())
    with st.engine.begin() as c:
        assert stager.stage_events(c, t - timedelta(microseconds=1)) == []  # 1 microsecond before: invisible
        assert stager.stage_events(c, t) == [1]  # exactly at available_at: visible; event 2 (one second later) not
        assert c.execute(text("SELECT array_agg(id) FROM events")).scalar() == [1]
        assert stager.stage_events(c, t + timedelta(seconds=1)) == [2]


# ------------------------------------------------------------------ 3. prices

def test_signal_code_cannot_see_or_request_future_prices():
    from app.replay.visibility import Guards, ReplayPriceProvider, Visibility
    src = source()
    vis = Visibility(phase="SIGNAL", signal_cap=date(2021, 2, 4))
    prov = ReplayPriceProvider(src, vis, Guards())
    with use_clock(ReplayClock(datetime(2021, 2, 5, 14, 0, tzinfo=UTC))):  # Friday 09:00 ET: Friday's bar does not exist
        bars = prov.get_historical_prices("ACME", date(2021, 1, 1), date(2021, 2, 5))
        assert bars[-1].date == date(2021, 2, 4)
        with pytest.raises(ReplayLeakage) as e:
            prov.get_historical_prices("ACME", date(2021, 1, 1), date(2021, 2, 8))  # "tomorrow"
        assert e.value.kind == "FUTURE_PRICE_REQUEST"
        vis.phase, vis.exec_cap = "EXECUTION", date(2021, 2, 5)  # the fill engine sees the session it fills on
        assert prov.get_historical_prices("ACME", date(2021, 2, 1), date(2021, 2, 5))[-1].date == date(2021, 2, 5)


# ------------------------------------------------------------------ 4. timing through the real pipeline

@db
def test_after_close_publication_trades_at_the_next_session_open_and_premarket_the_same_day():
    evs = [_event(1, datetime(2021, 2, 5, 21, 30, tzinfo=UTC)),  # Friday 16:30 ET: after the close
           _event(2, datetime(2021, 2, 10, 13, 0, tzinfo=UTC), cik=OTHER, ticker="OTHR")]  # Wed 08:00 ET: pre-market
    src = source(evs)
    src.bars["OTHR"] = _series(date(2020, 10, 1), date(2021, 9, 30), 20.0, 200_000)
    src.symbols.append({"cik": OTHER, "symbol": "OTHR", "first_seen": date(2016, 1, 4), "last_seen": date(2021, 9, 1),
                        "source": "form345_filing"})
    src.share_facts[OTHER] = [ShareFact("o1", date(2020, 9, 30), date(2020, 11, 2), "10-Q", 10_000_000)]
    r, res = run(cfg("t_timing", end=date(2021, 2, 19)), src)
    assert res.status == "COMPLETED", res.leakage
    rows = q("replay_t_timing", """SELECT p.ticker, p.proposal_created_at, min(o.fill_date) FROM proposed_trades p
                                  JOIN paper_orders o ON o.proposal_id = p.id WHERE o.side = 'BUY' GROUP BY 1, 2 ORDER BY 1""")
    by = {t: (created, fill) for t, created, fill in rows}
    assert by["ACME"][1] == date(2021, 2, 8)  # Monday open, never Friday's
    assert by["ACME"][0] >= datetime(2021, 2, 5, 21, 30, tzinfo=UTC)
    assert by["OTHR"][1] == date(2021, 2, 10)  # public before the open -> that day's open


# ------------------------------------------------------------------ 5. isolation from production

@db
def test_replay_never_writes_the_production_ledger(engine, clean):
    """`engine`/`clean` = the test database's public schema, standing in for production state."""
    _, res = run(cfg("t_iso", end=date(2021, 2, 19)), source())
    assert res.status == "COMPLETED"
    with engine.connect() as c:
        for t in ("paper_orders", "paper_positions", "proposed_trades", "paper_accounts", "paper_trades", "events"):
            assert c.execute(text(f"SELECT count(*) FROM public.{t}")).scalar() == 0, t
    assert q("replay_t_iso", "SELECT count(*) FROM paper_orders")[0][0] > 0


def test_isolation_guard_refuses_the_production_database(monkeypatch):
    from app.replay.store import ReplayStore
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@dbhost:5432/public_edge")
    with pytest.raises(ReplayIsolationError):
        ReplayStore("postgresql+psycopg://other:x@dbhost:5432/public_edge", "replay_x")  # same host + database
    with pytest.raises(ReplayIsolationError):
        ReplayStore("postgresql+psycopg://u:p@elsewhere:5432/replay_db", "public")  # not a replay schema


# ------------------------------------------------------------------ 6. determinism

@db
def test_same_config_and_snapshot_give_the_same_result():
    _, a = run(cfg("t_det_a"), source())
    _, b = run(cfg("t_det_b"), source())
    assert a.status == b.status == "COMPLETED"
    assert a.provenance["config_hash"] == b.provenance["config_hash"]
    assert a.provenance["data_snapshot"] == b.provenance["data_snapshot"]
    assert a.metrics == b.metrics and a.metrics["fills"] > 0
    assert q("replay_t_det_a", "SELECT code FROM proposed_trades ORDER BY id") == \
        q("replay_t_det_b", "SELECT code FROM proposed_trades ORDER BY id")


# ------------------------------------------------------------------ 7. symbol reuse

@db
def test_symbol_reuse_resolves_the_historical_issuer():
    from app.replay.store import ReplayStore
    from app.replay.visibility import Guards, Stager, identity_check
    src = source()
    src.symbols += [{"cik": OTHER, "symbol": "ZZZ", "first_seen": date(2012, 1, 3), "last_seen": date(2016, 6, 1),
                     "source": "form345_filing"},
                    {"cik": ACME, "symbol": "ZZZ", "first_seen": date(2020, 1, 6), "last_seen": date(2021, 9, 1),
                     "source": "form345_filing"}]
    st = ReplayStore(URL, "replay_reuse")
    st.create()
    stager = Stager(src, Guards())
    with st.engine.begin() as c:
        stager.stage_reference(c, date(2015, 3, 2))
        assert identity_check(c, {"ticker": "ZZZ", "issuer_cik": None}, date(2015, 3, 2)) is None  # OTHER owned it
        assert identity_check(c, {"ticker": "ZZZ", "issuer_cik": ACME}, date(2015, 3, 2)) == \
            "TICKER_OWNED_BY_OTHER_ISSUER_AT_EVENT_DATE"
        reg = dict(c.execute(text("SELECT t.symbol, c.cik FROM tickers t JOIN companies c ON c.id = t.company_id")).all())
        assert reg["ZZZ"] == OTHER  # 2015's registry, not today's
        stager.stage_reference(c, date(2021, 3, 1))
        assert identity_check(c, {"ticker": "ZZZ", "issuer_cik": ACME}, date(2021, 3, 1)) is None
        assert identity_check(c, {"ticker": "ZZZ", "issuer_cik": OTHER}, date(2021, 3, 1)) == \
            "TICKER_OWNED_BY_OTHER_ISSUER_AT_EVENT_DATE"


# ------------------------------------------------------------------ 8. regulatory / label knowledge

@db
def test_future_delisting_and_labels_do_not_leak_backward():
    from app.replay.store import ReplayStore
    from app.replay.visibility import Guards, Stager, check_state
    src = source(delisting=date(2021, 5, 3))
    st = ReplayStore(URL, "replay_labels")
    st.create()
    g = Guards()
    stager = Stager(src, g)
    with st.engine.begin() as c:
        stager.stage_reference(c, date(2021, 3, 1))
        assert c.execute(text("SELECT delisting_date, status FROM security_master")).one() == (None, "UNKNOWN")
        stager.stage_reference(c, date(2021, 5, 4))
        assert c.execute(text("SELECT delisting_date FROM security_master")).scalar() == date(2021, 5, 3)
    with st.engine.begin() as c:  # a later label smuggled in -> the run fails loudly
        c.execute(text("""INSERT INTO discovery_risks (cik, risk, severity, detected_at)
                          VALUES (:c, 'GOING_CONCERN', 'HIGH', '2021-06-01')"""), {"c": ACME})
    with st.engine.connect() as c, pytest.raises(ReplayLeakage) as e:
        check_state(c, g, datetime(2021, 5, 4, 22, tzinfo=UTC), date(2021, 5, 4), date(2021, 5, 4))
    assert e.value.kind == "FUTURE_REGULATORY_LABEL"


def test_snapshot_refuses_later_labels_on_events():
    with pytest.raises(ValueError):
        SourceData(events=[{**_event(1, datetime(2021, 2, 5, 21, tzinfo=UTC)), "needs_review": True}])
    with pytest.raises(ValueError):
        SourceData(events=[{**_event(1, datetime(2021, 2, 5, 21, tzinfo=UTC)), "returns_status": "complete"}])


# ------------------------------------------------------------------ 9-10. execution model (production fills)

@db
def test_no_volume_no_fill():
    """The production model never assumes a fill on a zero-volume session. FINDING (production ledger, reused as is):
    fill_orders always prices the FIRST bar on/after not_before, so after a zero-volume session the order stays
    PENDING forever instead of filling at the next session - the replay reproduces it (docs/HISTORICAL_REPLAY.md)."""
    src = source(acme_overrides={date(2021, 2, 8): 0})  # Monday: zero volume
    _, res = run(cfg("t_novol", end=date(2021, 2, 19)), src)
    assert res.status == "COMPLETED"
    rows = q("replay_t_novol", "SELECT status, fill_date FROM paper_orders WHERE side = 'BUY'")
    assert rows and all(fd != date(2021, 2, 8) for _st, fd in rows)
    assert {st for st, _fd in rows} == {"PENDING"}  # production behaviour: never retried on a later bar


@db
def test_volume_participation_cap_is_respected():
    src = source(acme_overrides={date(2021, 2, 8): 100})  # tiny volume on the fill day
    _, res = run(cfg("t_cap", end=date(2021, 2, 12)), src)
    assert res.status == "COMPLETED"
    rows = q("replay_t_cap", "SELECT shares, participation, note FROM paper_orders WHERE side = 'BUY' AND fill_date = '2021-02-08'")
    assert rows and all(p <= 0.05 + 1e-9 and "participation cap" in (n or "") for _s, p, n in rows)


# ------------------------------------------------------------------ 11. clock

def test_replay_clock_is_deterministic_and_forward_only():
    c = ReplayClock(datetime(2021, 2, 1, 14, tzinfo=UTC))
    c.set(datetime(2021, 2, 1, 14, 30, tzinfo=UTC))
    with pytest.raises(ValueError):
        c.set(datetime(2021, 2, 1, 14, tzinfo=UTC))
    with pytest.raises(ValueError):
        ReplayClock(datetime(2021, 2, 1))
    from app.core import clock
    before = clock.now()
    with use_clock(c):
        assert clock.now() == datetime(2021, 2, 1, 14, 30, tzinfo=UTC) and clock.is_replay()
    assert not clock.is_replay() and clock.now() >= before  # production clock restored


@db
def test_run_steps_follow_the_session_schedule():
    r, res = run(cfg("t_sched", start=date(2021, 2, 1), end=date(2021, 2, 5)), source())
    assert res.status == "COMPLETED" and res.steps == 5 * 3
    stamps = r.clock.history[1:] + [r.clock.now()]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)


# ------------------------------------------------------------------ 12. two runs

@db
def test_two_replay_runs_cannot_contaminate_each_other():
    _, a = run(cfg("t_two_a", end=date(2021, 2, 19)), source())
    _, b = run(cfg("t_two_b", end=date(2021, 2, 19)), source(events=[]))
    assert a.status == b.status == "COMPLETED"
    assert q("replay_t_two_a", "SELECT count(*) FROM paper_orders")[0][0] > 0
    assert q("replay_t_two_b", "SELECT count(*) FROM paper_orders")[0][0] == 0
    assert q("replay_t_two_b", "SELECT count(*) FROM events")[0][0] == 0
    meta = dict(q("replay_meta", "SELECT replay_id, schema_name FROM replay_runs WHERE replay_id LIKE 't_two_%'"))
    assert meta == {"t_two_a": "replay_t_two_a", "t_two_b": "replay_t_two_b"}


# ------------------------------------------------------------------ guards through a run / campaign roles

@db
def test_a_leak_fails_the_run_instead_of_continuing(monkeypatch):
    from app.replay import visibility
    real = visibility.Stager.stage_events

    def leaky(self, conn, now, event_types=None):  # a bug that stages tomorrow's event
        return real(self, conn, now + timedelta(days=1), event_types)
    monkeypatch.setattr(visibility.Stager, "stage_events", leaky)
    _, res = run(cfg("t_leak", end=date(2021, 2, 19)), source())
    assert res.status == "FAILED_LEAKAGE" and res.leakage[0]["kind"] == "FUTURE_EVENT_VISIBLE"


def test_campaign_roles_purge_gap_and_sealed_holdout():
    from app.replay.campaign import ReplayCampaign, Split, windows
    split = Split(datetime(2021, 1, 1, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC), purge_gap_days=30)
    starts = [datetime(2020, 3, 2, tzinfo=UTC), datetime(2020, 10, 1, tzinfo=UTC), datetime(2021, 2, 1, tzinfo=UTC),
              datetime(2022, 2, 1, tzinfo=UTC)]
    cs = windows(starts, split=split)
    roles = {c.replay_id: c.period_role for c in cs}
    assert roles["c_20200302_3m"] == "development" and roles["c_20210201_6m"] == "validation"
    assert roles["c_20220201_3m"] == "holdout"
    assert "c_20201001_6m" not in roles  # straddles the development / validation boundary: never scheduled
    with pytest.raises(NotImplementedError):
        ReplayCampaign("x", cs, "postgresql+psycopg://a:b@h/replay", concurrency=2)
    with pytest.raises(ValueError):
        ReplayCampaign("x", cs, "postgresql+psycopg://a:b@h/replay", concurrency=8)
