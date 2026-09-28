"""V2 candidate families: point-in-time event generation, identity gates, validation standard, multiple testing."""
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.provenance import end_of_local_day

UTC = timezone.utc
FX = Path(__file__).parent / "fixtures" / "v2"


def _listed(c, cik, sym, first=date(2020, 1, 1)):
    c.execute(text("INSERT INTO security_master (cik, name, status) VALUES (:c, :n, 'ACTIVE') ON CONFLICT DO NOTHING"),
              {"c": cik, "n": f"CO {sym}"})
    c.execute(text("""INSERT INTO security_symbols (cik, symbol, first_seen, last_seen, n_obs, source)
                      VALUES (:c, :s, :f, '2026-12-31', 9, 'form345_filing')"""), {"c": cik, "s": sym, "f": first})


def test_ftd_spike_needs_history_identity_and_uses_publication_time(engine, clean):
    from app.signals.v2_families import _file_minus, ftd_spikes
    assert _file_minus("cnsfails202608b", 6) == "cnsfails202605b" and _file_minus("cnsfails202601a", 1) == "cnsfails202512b"
    with engine.begin() as c:
        _listed(c, "0000000101", "SPKE")
        files = ["cnsfails202605b", "cnsfails202606a", "cnsfails202606b", "cnsfails202607a", "cnsfails202607b", "cnsfails202608a"]
        for i, f in enumerate(files):
            c.execute(text("""INSERT INTO fails_to_deliver VALUES (:d, 'X12345678', 'SPKE', 10000, 20.0, 'x', :f, :a)"""),
                      {"d": date(2026, 5, 20) + timedelta(days=15 * i), "f": f, "a": datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=15 * i)})
        pub = datetime(2026, 9, 15, 13, 9, tzinfo=UTC)
        c.execute(text("INSERT INTO fails_to_deliver VALUES ('2026-08-20', 'X12345678', 'SPKE', 200000, 20.0, 'x', 'cnsfails202608b', :a)"),
                  {"a": pub})
        c.execute(text("INSERT INTO fails_to_deliver VALUES ('2026-08-20', 'Y12345678', 'NOID', 900000, 20.0, 'x', 'cnsfails202608b', :a)"),
                  {"a": pub})
        ids = ftd_spikes(c, "cnsfails202608b", is_backfill=True)
        evs = c.execute(text("SELECT ticker, available_at, issuer_cik FROM events")).mappings().all()
    assert len(ids) == 1 and evs[0]["ticker"] == "SPKE" and evs[0]["available_at"] == pub  # NOID: no history, no identity


def test_short_pressure_zscore_event_once_per_30_days(engine, clean):
    from app.signals.v2_families import short_pressure
    d0 = date(2026, 5, 1)
    with engine.begin() as c:
        _listed(c, "0000000102", "SHRT")
        days = [d0 + timedelta(days=i) for i in range(120) if (d0 + timedelta(days=i)).weekday() < 5][:70]
        for i, d in enumerate(days):
            ratio = 0.40 + (0.01 if i % 2 else -0.01) if i < 65 else 0.75
            c.execute(text("INSERT INTO short_volume_daily VALUES (:d, 'SHRT', :s, 0, 1000000, :a)"),
                      {"d": d, "s": int(ratio * 1_000_000), "a": end_of_local_day(d)})
        ids = short_pressure(c, days[-1], is_backfill=True)
        again = short_pressure(c, days[-1], is_backfill=True)
        ev = c.execute(text("SELECT available_at, payload FROM events")).mappings().one()
    assert len(ids) == 1 and again == [] and ev["available_at"] == end_of_local_day(days[-1])
    assert ev["payload"]["note"].startswith("FINRA short-sale VOLUME")


def test_13f_events_from_two_real_quarters(engine, clean):
    from app.signals.v2_families import thirteen_f
    from app.sources.us import form13f as F
    for q in ("13f_2026q1_mini.zip", "13f_2026q2_mini.zip"):
        F.store(engine, F.parse_dataset(str(FX / "us" / q)), source=q)
    with engine.begin() as c:
        rows = c.execute(text("""SELECT h.cusip FROM inst_holdings_13f h JOIN inst_filings_13f f USING (accession)
                                 WHERE f.period = '2026-06-30'""")).scalars().all()
        # identity only through a 13D cover CUSIP: map every CUSIP of this manager to a synthetic issuer
        for i, cu in enumerate(sorted(set(rows))):
            c.execute(text("""INSERT INTO activist_filings (accession, issuer_cik, cusip, filed_date, form_type, is_13d, is_amendment, source_url)
                              VALUES (:a, :c, :cu, '2025-01-01', 'SC 13D', true, false, 'u')"""),
                      {"a": f"0000000000-25-{i:06d}", "c": f"{9000000 + i:010d}", "cu": cu})
        acc = c.execute(text("""SELECT accession FROM inst_filings_13f WHERE manager_cik = '0002055400' AND period = '2026-06-30'
                                AND submission_type = '13F-HR'""")).scalar()
        ids = thirteen_f(c, acc, is_backfill=True)
        from app.signals.v2_families import accumulation_events
        ids += accumulation_events(c, date(2026, 6, 30), is_backfill=True)
        evs = c.execute(text("SELECT event_type, available_at, public_date FROM events")).mappings().all()
        filed = c.execute(text("SELECT filed_date FROM inst_filings_13f WHERE accession = :a"), {"a": acc}).scalar()
    for e in evs:
        assert e["available_at"] == end_of_local_day(filed) and e["public_date"] == filed  # never the quarter end
    assert len(ids) == len(evs)
    assert any(e["event_type"] == "INST_13F_NEW_POSITION" for e in evs)
    assert {e["event_type"] for e in evs} <= {"INST_13F_NEW_POSITION", "INST_13F_ACCUMULATION", "INST_13F_EXIT", "INST_13F_DECREASE"}


def test_validation_report_and_bh_ledger(engine, clean):
    from app.backtest import validation_v2 as V
    with engine.begin() as c:
        rep = V.family_report(c, "US_FTD_SPIKE")
        V.family_report(c, "US_SHORT_PRESSURE")
        s = V.ledger_summary(c)
    assert rep["horizons"][90]["verdict"] == "BLOCKED" and s["n"] == 6
    assert V.binom_p(0.5, 100) == pytest.approx(1.0) and V.binom_p(0.6, 400) < 0.001


def test_bh_adjustment_is_monotone(engine, clean):
    from app.backtest import validation_v2 as V
    with engine.begin() as c:
        for i, p in enumerate([0.001, 0.02, 0.04, 0.5]):
            c.execute(text("INSERT INTO hypothesis_log (family, horizon, p_value) VALUES (:f, 90, :p)"), {"f": f"F{i}", "p": p})
        V.adjust_ledger(c)
        adj = c.execute(text("SELECT family, adjusted_p FROM hypothesis_log ORDER BY p_value")).all()
    vals = [a for _, a in adj]
    assert vals == sorted(vals) and vals[0] == pytest.approx(0.004) and vals[2] == pytest.approx(0.0533, abs=1e-3)


def test_every_registered_event_type_is_unique_to_one_family():
    from app.core.signal_registry import FAMILIES
    seen = {}
    for f in FAMILIES:
        for et in f.source_event_types:
            assert et not in seen, (et, seen.get(et), f.signal_id)
            seen[et] = f.signal_id


def test_placebo_difference_uses_same_issuer_earlier_window(engine, clean):
    from app.backtest import placebo
    from app.db.models import event_returns, market_prices
    d0 = date(2024, 1, 1)
    with engine.begin() as c:
        bars = []
        for i in range(800):
            d = d0 + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            spy = 100 * (1.0005 ** i)
            stock = 50 * (1.001 ** i) if d >= date(2025, 8, 1) else 50 * (0.9995 ** i)
            bars += [{"symbol": "SPY", "date": d, "open": spy, "high": spy, "low": spy, "close": spy, "adj_close": spy, "volume": 1e6},
                     {"symbol": "TSTX", "date": d, "open": stock, "high": stock, "low": stock, "close": stock, "adj_close": stock, "volume": 1e6}]
        c.execute(market_prices.insert(), bars)
        c.execute(text("""INSERT INTO events (id, event_type, dedupe_key, ticker, issuer_cik, public_date, title, payload, source_url, price_symbol)
                          VALUES (1, 'X_TEST', 'k', 'TSTX', '0000000777', '2025-09-02', 't', '{}', 'u', 'TSTX')"""))
        c.execute(event_returns.insert().values(event_id=1, horizon_days=90, entry_date=date(2025, 9, 3), entry_price=1, exit_date=date(2025, 12, 1),
                                                exit_price=1.1, ret=0.1, spy_ret=0.04, excess_ret=0.06))
        out = placebo.family_vs_placebo(c, "X_TEST", 90)
    assert out["companies"] == 1 and out["event_rate"] == 1.0 and out["placebo_rate"] == 0.0 and out["difference"] == 1.0



def test_placebo_excludes_controls_overlapping_unsampled_events(engine, clean):
    """A second event without returns (not in the sampling query) near the control date must block the control."""
    from app.backtest import placebo
    from app.db.models import event_returns, market_prices
    d0 = date(2024, 1, 1)
    with engine.begin() as c:
        bars = []
        for i in range(800):
            d = d0 + timedelta(days=i)
            if d.weekday() < 5:
                bars += [{"symbol": s, "date": d, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "adj_close": 10.0, "volume": 1e6}
                         for s in ("SPY", "TSTY")]
        c.execute(market_prices.insert(), bars)
        c.execute(text("""INSERT INTO events (id, event_type, dedupe_key, ticker, issuer_cik, public_date, title, payload, source_url, price_symbol)
                          VALUES (1, 'X_TEST', 'k1', 'TSTY', '0000000778', '2025-09-02', 't', '{}', 'u', 'TSTY'),
                                 (2, 'X_TEST', 'k2', 'TSTY', '0000000778', '2025-03-10', 't', '{}', 'u', 'TSTY')"""))
        c.execute(event_returns.insert().values(event_id=1, horizon_days=90, entry_date=date(2025, 9, 3), entry_price=1, exit_date=date(2025, 12, 1),
                                                exit_price=1.1, ret=0.1, spy_ret=0.0, excess_ret=0.1))
        out = placebo.family_vs_placebo(c, "X_TEST", 90)
    assert out["companies"] == 0 and out["skipped"] == 1  # control 2025-03-04 is 6 days from the unpriced event 2


def test_v2_historical_runner_is_exclusive(engine, clean):
    from app.jobs import v2_steps
    ran = []
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": v2_steps.LOCK_ID})
        assert v2_steps.run_historical(engine, lambda e, n, f: ran.append(n)) == {"skipped": "LOCKED"}
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": v2_steps.LOCK_ID})
        other.commit()
    v2_steps.run_historical(engine, lambda e, n, f: ran.append(n))
    assert ran == ["v2_13f", "v2_short_data", "v2_health_gov", "v2_ca_grants"]


def test_needs_review_events_are_excluded_from_quality_stats(engine, clean):
    from app.backtest import quality as Q
    with engine.begin() as c:
        c.execute(text("""INSERT INTO events (event_type, dedupe_key, ticker, issuer_cik, public_date, title, payload, source_url, needs_review)
                          VALUES ('X_T', 'a', 'AAA', '0000000001', '2020-01-02', 't', '{}', 'u', false),
                                 ('X_T', 'b', 'BBB', '0000000002', '2020-01-02', 't', '{}', 'u', true)"""))
        rows = Q.load_rows(c, "X_T", "TRUE", {}, 30)
    assert [r.issuer for r in rows] == ["0000000001"]


def test_matched_placebo_matches_on_prior_return_and_blocks_any_family(engine, clean):
    from app.backtest import research_v2 as R
    from app.db.models import event_returns, market_prices
    d0 = date(2019, 1, 1)
    with engine.begin() as c:
        bars = []
        for i in range(2400):
            d = d0 + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            spy = 100.0 * (1.0002 ** i)
            stock = 50.0 * (1 + 0.3 * math.sin(i / 60.0))  # cycles: many dates share a prior-60d return
            bars += [{"symbol": "SPY", "date": d, "open": spy, "high": spy, "low": spy, "close": spy, "adj_close": spy, "volume": 1e6},
                     {"symbol": "CYC", "date": d, "open": stock, "high": stock, "low": stock, "close": stock, "adj_close": stock, "volume": 1e6}]
        c.execute(market_prices.insert(), bars)
        c.execute(text("""INSERT INTO events (id, event_type, dedupe_key, ticker, issuer_cik, public_date, title, payload, source_url, price_symbol)
                          VALUES (900001, 'X_T', 'a', 'CYC', '0000000010', '2023-03-01', 't', '{}', 'u', 'CYC')"""))
        c.execute(event_returns.insert().values(event_id=900001, horizon_days=90, entry_date=date(2023, 3, 2), entry_price=1, exit_date=date(2023, 6, 1),
                                                exit_price=1, ret=0.0, spy_ret=0.0, excess_ret=0.01))
        fam = R.matched_placebo(c, "X_T", lookback=60)
        # a different-family event on every week blocks all controls under contamination='any'
        for k in range(0, 2300, 7):
            c.execute(text("""INSERT INTO events (event_type, dedupe_key, issuer_cik, public_date, title, payload, source_url)
                              VALUES ('OTHER', :k, '0000000010', :d, 't', '{}', 'u')"""), {"k": f"o{k}", "d": d0 + timedelta(days=k)})
        anyc = R.matched_placebo(c, "X_T", lookback=60, contamination="any")
    assert fam["companies"] == 1 and anyc["companies"] == 0


def test_13f_accumulation_fires_at_the_third_new_holder_filing(engine, clean):
    from app.signals.v2_families import accumulation_events
    q1, q2 = date(2026, 3, 31), date(2026, 6, 30)
    with engine.begin() as c:
        c.execute(text("""INSERT INTO activist_filings (accession, issuer_cik, cusip, filed_date, form_type, is_13d, is_amendment, source_url)
                          VALUES ('0000000000-25-999999', '0000000555', '123456789', '2025-01-01', 'SC 13D', true, false, 'u')"""))
        for m in range(4):
            mc = f"{m + 1:010d}"
            for per, acc, filed in ((q1, f"A{m}-1", date(2026, 5, 10)), (q2, f"A{m}-2", date(2026, 7, 20 + m))):
                c.execute(text("""INSERT INTO inst_filings_13f (accession, manager_cik, manager_name, period, filed_date, available_at,
                                  submission_type, entries, source) VALUES (:a, :m, :n, :p, :f, :av, '13F-HR', 20, 't')"""),
                          {"a": acc, "m": mc, "n": f"M{m}", "p": per, "f": filed, "av": datetime(filed.year, filed.month, filed.day, 23, tzinfo=UTC)})
                c.execute(text("INSERT INTO inst_holdings_13f (accession, line, cusip, issuer_name, value, shares) VALUES (:a, 1, 'OTHER0001', 'X', 1, 1)"),
                          {"a": acc})
            c.execute(text("INSERT INTO inst_holdings_13f (accession, line, cusip, issuer_name, value, shares) VALUES (:a, 2, '123456789', 'TGT', 1, 1)"),
                      {"a": f"A{m}-2"})
        ids = accumulation_events(c, q2, is_backfill=True)
        ev = c.execute(text("SELECT public_date, payload->>'trigger_manager' FROM events WHERE id = :i"), {"i": ids[0]}).one()
    assert len(ids) == 1 and ev[0] == date(2026, 7, 22) and ev[1] == "M2"  # third manager's filing, not the fourth


def test_13f_new_data_resets_completion_and_interrupted_run_resumes(engine, clean, monkeypatch):
    from app.db.store import job_state, save_job_state
    from app.jobs import v2_steps
    import app.sources.us.form13f as F
    with engine.begin() as c:
        save_job_state(c, "v2_13f_events", {"complete": True, "done": []})
        save_job_state(c, "form13f_datasets", {"done": ["u_old"]})
    monkeypatch.setattr(v2_steps, "disk_ok", lambda: True)
    monkeypatch.setattr(F, "dataset_urls", lambda: ["u_new", "u_old"])

    def ingest(engine_, u, **k):
        with engine_.begin() as c:
            st = job_state(c, "form13f_datasets")
            save_job_state(c, "form13f_datasets", {"done": sorted(set(st["done"]) | {u})})
        return {"url": u}
    monkeypatch.setattr(F, "ingest_dataset", ingest)

    def boom(*a, **k):
        raise RuntimeError("interrupted")
    monkeypatch.setattr(v2_steps, "after_13f", boom)
    with pytest.raises(RuntimeError):
        v2_steps.backfill_13f(engine)
    with engine.connect() as c:
        assert job_state(c, "v2_13f_events")["complete"] is False  # persisted BEFORE the new data set was loaded
    calls = []
    monkeypatch.setattr(v2_steps, "after_13f", lambda e, **k: calls.append(1) or 0)
    v2_steps.backfill_13f(engine)  # every URL is ALREADY_INGESTED now, but events are still pending -> recomputed
    assert calls == [1]
    with engine.connect() as c:
        assert job_state(c, "v2_13f_events")["complete"] is True


def test_13f_filing_without_previous_quarter_is_reevaluated_after_it_arrives(engine, clean):
    from app.db.store import job_state
    from app.jobs import v2_steps
    q1, q2 = date(2026, 3, 31), date(2026, 6, 30)
    with engine.begin() as c:
        c.execute(text("""INSERT INTO activist_filings (accession, issuer_cik, cusip, filed_date, form_type, is_13d, is_amendment, source_url)
                          VALUES ('0000000000-25-888888', '0000000666', 'NEWCUSIP1', '2025-01-01', 'SC 13D', true, false, 'u')"""))
        c.execute(text("""INSERT INTO inst_filings_13f (accession, manager_cik, manager_name, period, filed_date, available_at, submission_type,
                          entries, source) VALUES ('B-2', '0000000077', 'M', :p, '2026-08-01', '2026-08-02T03:59:59Z', '13F-HR', 12, 't')"""), {"p": q2})
        for i, cu in enumerate(["OLDCUSIP1", "NEWCUSIP1"]):
            c.execute(text("INSERT INTO inst_holdings_13f (accession, line, cusip, issuer_name, value, shares) VALUES ('B-2', :l, :c, 'X', :v, 100)"),
                      {"l": i, "c": cu, "v": 50.0})
    from app.db.store import save_job_state
    with engine.begin() as c:
        save_job_state(c, "form13f_datasets", {"done": ["https://example/t"]})  # source 't' completely ingested
    assert v2_steps.after_13f(engine, is_backfill=True) == 0
    with engine.connect() as c:
        assert "B-2" not in job_state(c, "v2_13f_events").get("done", [])  # previous quarter missing: not final
    with engine.begin() as c:  # the failed previous quarter is loaded on a retry
        c.execute(text("""INSERT INTO inst_filings_13f (accession, manager_cik, manager_name, period, filed_date, available_at, submission_type,
                          entries, source) VALUES ('B-1', '0000000077', 'M', :p, '2026-05-01', '2026-05-02T03:59:59Z', '13F-HR', 12, 't')"""), {"p": q1})
        c.execute(text("INSERT INTO inst_holdings_13f (accession, line, cusip, issuer_name, value, shares) VALUES ('B-1', 0, 'OLDCUSIP1', 'X', 50, 100)"))
    v2_steps.after_13f(engine, is_backfill=True)
    with engine.connect() as c:
        ev = c.execute(text("SELECT event_type FROM events WHERE issuer_cik = '0000000666'")).scalars().all()
        assert "B-2" in job_state(c, "v2_13f_events")["done"]
    assert ev == ["INST_13F_NEW_POSITION"]



def _events_signature(engine):
    with engine.connect() as c:
        return sorted(tuple(r) for r in c.execute(text(
            "SELECT event_type, dedupe_key, public_date, available_at FROM events WHERE event_type LIKE 'INST_13F%'")))


def _run_13f(engine, monkeypatch, fail_q2_after_batches=None):
    import shutil
    from app.jobs import v2_steps
    import app.sources.us.form13f as F
    monkeypatch.setattr(F.http, "download", lambda url, path, **k: shutil.copy(str(FX / "us" / url.rsplit("/", 1)[-1]), path))
    real_parse = F.parse_dataset
    monkeypatch.setattr(F, "parse_dataset", lambda path, **k: real_parse(path, **{**k, "flush_every": 5}))
    with engine.begin() as c:  # CUSIP identity for every held security (13D-cover style mapping)
        for q in ("13f_2026q1_mini.zip", "13f_2026q2_mini.zip"):
            for i, h in enumerate(real_parse(str(FX / "us" / q))["holdings"]):
                c.execute(text("""INSERT INTO activist_filings (accession, issuer_cik, cusip, filed_date, form_type, is_13d, is_amendment, source_url)
                                  VALUES (:a, :c, :cu, '2025-01-01', 'SC 13D', true, false, 'u') ON CONFLICT DO NOTHING"""),
                          {"a": f"X-{h['cusip']}", "c": f"{abs(hash(h['cusip'])) % 10**9:010d}", "cu": h["cusip"]})
    F.ingest_dataset(engine, "https://example/13f_2026q1_mini.zip")
    if fail_q2_after_batches is not None:
        real_upsert, calls = F.upsert_rows, []

        def flaky(conn, table, rows, keys, extra):
            if table == "inst_holdings_13f":
                calls.append(1)
                if len(calls) > fail_q2_after_batches:
                    raise RuntimeError("connection reset while loading holdings")
            return real_upsert(conn, table, rows, keys, extra)
        monkeypatch.setattr(F, "upsert_rows", flaky)
        with pytest.raises(RuntimeError):
            F.ingest_dataset(engine, "https://example/13f_2026q2_mini.zip")
        v2_steps.after_13f(engine, is_backfill=True)  # what backfill_13f does after a failed data set
        monkeypatch.setattr(F, "upsert_rows", real_upsert)
    F.ingest_dataset(engine, "https://example/13f_2026q2_mini.zip")
    v2_steps.after_13f(engine, is_backfill=True)
    return _events_signature(engine)


def test_13f_interrupted_ingestion_retry_gives_the_same_events(engine, clean, monkeypatch):
    uninterrupted = _run_13f(engine, monkeypatch)
    with engine.begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(t.name for t in __import__("app.db.models", fromlist=["metadata"]).metadata.sorted_tables)
                       + " RESTART IDENTITY CASCADE"))
    interrupted = _run_13f(engine, monkeypatch, fail_q2_after_batches=1)
    assert uninterrupted and interrupted == uninterrupted
