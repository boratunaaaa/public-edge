"""PIT backfill jobs: dry run writes nothing, idempotent re-runs, resumable, per-company failure isolation, disk guard."""
import pytest
from sqlalchemy import text

from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
A, B, C = "0000000101", "0000000102", "0000000103"


def _cf(cash):
    return {"facts": {"us-gaap": {"CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
        {"val": cash, "end": "2023-03-31", "filed": "2023-05-10", "accn": "0000000101-23-000001", "form": "10-Q"}]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            {"val": -60, "start": "2022-01-01", "end": "2022-12-31", "filed": "2023-02-28", "accn": "0000000101-23-000000",
             "form": "10-K"}]}}}}}


class _Resp:
    def __init__(self, d):
        import json
        self._d, self.content = d, json.dumps(d).encode()

    def json(self):
        return self._d


@pytest.fixture
def sec(monkeypatch):
    """Offline SEC: companyfacts for A and B, B's submissions with one older page, C fails, others 404."""
    from app import http
    calls = []

    def get(url, **kw):
        calls.append(url)
        if "companyfacts/CIK" + A in url:
            return _Resp(_cf(120))
        if "companyfacts/CIK" + B in url:
            return _Resp(_cf(90))
        if C in url:
            raise http.HttpError(url, 500, "boom")
        if "submissions/CIK" + B + ".json" in url:
            return _Resp({"filings": {"recent": {"accessionNumber": ["b-2"], "filingDate": ["2024-05-01"], "form": ["8-K"],
                                                 "acceptanceDateTime": [""], "items": ["3.02"]},
                                      "files": [{"name": "CIK" + B + "-submissions-001.json", "filingCount": 2}]}})
        if "submissions-001.json" in url:
            return _Resp({"accessionNumber": ["b-2", "b-1"], "filingDate": ["2024-05-01", "2015-06-01"],
                          "form": ["8-K", "S-3"], "acceptanceDateTime": ["2024-05-01T20:00:00.000Z", ""], "items": ["", ""]})
        raise http.HttpError(url, 404, "nf")
    monkeypatch.setattr(http, "get", get)
    return calls


@db
@pytest.mark.usefixtures("clean")
def test_dry_run_writes_nothing_and_estimates(engine, sec):
    from app.jobs.pit_backfill import run
    out = run(engine, "xbrl", ciks=[A, B], apply=False)
    assert out["mode"] == "DRY_RUN" and out["done"] == 2 and out["rows"] == 4 and "estimate" in out
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM pit_facts")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pit_backfill_progress")).scalar() == 0


@db
@pytest.mark.usefixtures("clean")
def test_apply_is_idempotent_resumable_and_isolates_a_failing_company(engine, sec):
    from app.jobs.pit_backfill import MAX_ATTEMPTS, run
    out = run(engine, "xbrl", ciks=[A, C, B], apply=True)
    assert (out["done"], out["failed"]) == (2, 1)  # C failed, B still processed after it
    with engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM pit_facts")).scalar()
        prog = dict(c.execute(text("SELECT cik, status FROM pit_backfill_progress WHERE job = 'xbrl'")).all())
    assert n == 4 and prog == {A: "done", B: "done", C: "failed"}
    sec.clear()
    again = run(engine, "xbrl", ciks=[A, C, B], apply=True)  # resumable: only C is retried
    assert again["queued"] == 1 and all(C in u for u in sec)
    for _ in range(MAX_ATTEMPTS):
        run(engine, "xbrl", ciks=[A, C, B], apply=True)
    last = run(engine, "xbrl", ciks=[A, C, B], apply=True)
    assert last["queued"] == 0 and last["gave_up"] == 1  # given up after MAX_ATTEMPTS - and reported
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM pit_facts")).scalar() == 4  # nothing duplicated
        assert c.execute(text("SELECT count(DISTINCT run_id) FROM pit_facts")).scalar() == 1  # provenance kept


@db
@pytest.mark.usefixtures("clean")
def test_submissions_merge_the_older_page_and_fill_missing_acceptance(engine, sec):
    from app.jobs.pit_backfill import run
    out = run(engine, "submissions", ciks=[B], apply=True)
    assert out["done"] == 1 and out["pages"] == 1
    with engine.connect() as c:
        rows = {r[0]: r[1:] for r in c.execute(text(
            "SELECT accession, form, acceptance_at IS NOT NULL, source_block FROM sec_filings ORDER BY 1"))}
    assert set(rows) == {"b-1", "b-2"}
    assert rows["b-2"][1] is True  # the recent block had no time; the older page's time filled it
    assert rows["b-1"][0] == "S-3" and rows["b-1"][2].endswith("-001.json")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM corporate_actions")).scalar() == 0  # production actions untouched


@db
@pytest.mark.usefixtures("clean")
def test_two_values_for_one_fact_are_counted(engine, sec, monkeypatch):
    from app import http
    from app.jobs.pit_backfill import run
    cf = _cf(120)
    usd = cf["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"]["units"]["USD"]
    usd.append({**usd[0], "val": 99})
    monkeypatch.setattr(http, "get", lambda url, **kw: _Resp(cf))
    assert run(engine, "xbrl", ciks=[A], apply=False)["value_conflicts"] == 1


def test_apply_refuses_a_process_local_sec_budget(monkeypatch):
    import sys
    from app.jobs import pit_backfill
    monkeypatch.delenv("PE_SHARED_RATELIMIT", raising=False)
    monkeypatch.setattr(sys, "argv", ["pit_backfill", "xbrl", "--limit", "1", "--apply"])
    with pytest.raises(SystemExit) as e:
        pit_backfill.main()
    assert e.value.code == 2


@db
@pytest.mark.usefixtures("clean")
def test_the_disk_guard_stops_cleanly(engine, sec, monkeypatch):
    from app.jobs import pit_backfill
    monkeypatch.setattr(pit_backfill, "free_gb", lambda path="/": 7.5)
    out = pit_backfill.run(engine, "xbrl", ciks=[A, B], apply=True)
    assert out["done"] == 0 and "free disk" in out["stopped"]


@db
def test_migration_0016_round_trip():
    import os
    import subprocess

    from sqlalchemy import create_engine
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    name = "pe_mig_0016"
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        c.execute(text(f"CREATE DATABASE {name}"))
    url = URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        seen = []
        for args in (["upgrade", "head"], ["downgrade", "0015"], ["upgrade", "head"]):
            p = subprocess.run(["alembic", *args], cwd=root, env=env, capture_output=True, text=True, timeout=300)
            assert p.returncode == 0, p.stderr[-2000:]
            eng = create_engine(url)
            with eng.connect() as c:
                seen.append((c.execute(text("SELECT version_num FROM alembic_version")).scalar(),
                             c.execute(text("SELECT to_regclass('public.pit_facts') IS NOT NULL AND "
                                            "to_regclass('public.sec_filings') IS NOT NULL")).scalar()))
            eng.dispose()
        assert seen == [("0016", True), ("0015", False), ("0016", True)]
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
