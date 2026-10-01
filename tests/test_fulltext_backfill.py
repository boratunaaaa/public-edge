"""EDGAR full-text store (app.jobs.fulltext_backfill + app.pit.fulltext): exact under EFTS's unstable page order,
idempotent / resumable, failure isolation, truncation, disk guard, coverage of a date range."""
import gzip
import json
import random
from datetime import date, timedelta

import pytest

from app.jobs import fulltext_backfill as fb
from app.pit.fulltext import load_hits, scan_status

D = date


def _corpus(n_per_day: dict[date, int], key_phrase: str) -> list[dict]:
    docs = []
    for d, n in sorted(n_per_day.items()):
        for i in range(n):
            docs.append({"phrase": key_phrase, "adsh": f"{d:%Y%m%d}-{i:04d}", "fn": "d.htm", "date": d,
                         "ciks": [f"{(i % 7) + 1:010d}"], "form": "8-K"})
    return docs


@pytest.fixture
def efts(monkeypatch):
    """Fake EFTS: hits of the phrase in [startdt, enddt]; every call returns them in a NEW random order (like the real
    relevance-with-ties order), sliced by `from`; `fail` = phrases that raise."""
    state = {"docs": [], "fail": set(), "calls": 0, "rng": random.Random(7), "phantom": 0}

    def page(phrase, forms, start, end, frm):
        state["calls"] += 1
        if phrase in state["fail"]:
            raise RuntimeError("efts 500")
        hits = [d for d in state["docs"] if d["phrase"] == phrase and start <= d["date"] <= end and d["form"] in forms]
        state["rng"].shuffle(hits)
        sl = hits[frm:frm + fb.PAGE]
        return {"hits": {"total": {"value": len(hits) + state["phantom"], "relation": "eq"},
                         "hits": [{"_id": f"{h['adsh']}:{h['fn']}", "_source": {"ciks": h["ciks"], "form": h["form"],
                                                                                "file_date": h["date"].isoformat()}}
                                  for h in sl]}}
    monkeypatch.setattr(fb, "_page", page)
    monkeypatch.setattr(fb, "queries", lambda: {"A": ("alpha", ["8-K"]), "B": ("beta", ["8-K"])})
    monkeypatch.setattr(fb, "free_gb", lambda path="/": 50.0)
    return state


def test_a_period_is_collected_exactly_although_every_page_comes_in_a_new_order(efts, tmp_path):
    days = {D(2023, 1, 1) + timedelta(days=i): 3 for i in range(120)}  # 360 documents, > 3 pages
    efts["docs"] = _corpus(days, "alpha")
    out = fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A"], apply=True)
    assert out["done"] == 1 and out["failed"] == 0 and out["documents"] == 360
    man = json.loads((tmp_path / "manifest/A/2023-01-01_2023-12-31.json").read_text())
    assert man["status"] == "done" and man["total"] == man["docs"] == 360 and man["leaves"] > 1
    with gzip.open(tmp_path / "hits/A/2023-01-01_2023-12-31.jsonl.gz", "rt") as fh:
        assert len({json.loads(x)["adsh"] for x in fh}) == 360


def test_single_page_paging_would_have_lost_documents(efts):
    """The reason for the split: paging one shuffled query does not return every document."""
    efts["docs"] = _corpus({D(2023, 1, 1) + timedelta(days=i): 3 for i in range(120)}, "alpha")
    seen = {d["adsh"] for frm in range(0, 360, fb.PAGE)
            for d in fb._docs(fb._page("alpha", ["8-K"], D(2023, 1, 1), D(2023, 12, 31), frm))}
    assert len(seen) < 360


def test_a_busy_single_day_is_paged_twice_and_must_add_up(efts, tmp_path):
    efts["docs"] = _corpus({D(2023, 3, 1): 150}, "alpha")
    r = fb.fetch_period("alpha", ["8-K"], D(2023, 3, 1), D(2023, 3, 1))
    assert r["status"] == "done" and len(r["docs"]) == 150


def test_a_single_day_beyond_the_window_is_truncated_not_done(efts, tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "EFTS_WINDOW", 120)
    efts["docs"] = _corpus({D(2023, 3, 1): 130}, "alpha")
    fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A"], apply=True)
    assert json.loads((tmp_path / "manifest/A/2023-01-01_2023-12-31.json").read_text())["status"] == "truncated"
    assert scan_status(tmp_path, ["A"], D(2023, 1, 1), D(2023, 12, 31))["done"] is False


def test_rerun_is_idempotent_and_a_failing_query_is_isolated_then_given_up(efts, tmp_path):
    efts["docs"] = _corpus({D(2023, 1, 5): 2}, "alpha") + _corpus({D(2023, 1, 6): 2}, "beta")
    efts["fail"] = {"beta"}
    out = fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A", "B"], apply=True)
    assert (out["done"], out["failed"]) == (1, 1)  # A stored although B failed
    efts["calls"] = 0
    again = fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A", "B"], apply=True)
    assert again["skipped_done"] == 1 and again["failed"] == 1 and efts["calls"] == 1  # only B was asked again
    fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["B"], apply=True)
    assert fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["B"], apply=True)["gave_up"] == 1  # MAX_ATTEMPTS
    efts["fail"] = set()
    assert scan_status(tmp_path, ["A"], D(2023, 1, 1), D(2023, 12, 31))["done"] is True
    st = scan_status(tmp_path, ["A", "B"], D(2023, 1, 1), D(2023, 12, 31))
    assert st["done"] is False and st["keys"]["B"].startswith("failed")


def test_a_failed_page_never_leaves_a_partial_shard(efts, tmp_path, monkeypatch):
    efts["docs"] = _corpus({D(2023, 1, 1) + timedelta(days=i): 3 for i in range(60)}, "alpha")
    real, n = fb._page, {"k": 0}

    def flaky(*a):
        n["k"] += 1
        if n["k"] == 5:
            raise RuntimeError("timeout")
        return real(*a)
    monkeypatch.setattr(fb, "_page", flaky)
    out = fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A"], apply=True)
    assert out["failed"] == 1 and not (tmp_path / "hits/A/2023-01-01_2023-12-31.jsonl.gz").exists()


def test_dry_run_writes_nothing_and_the_disk_guard_stops(efts, tmp_path, monkeypatch):
    efts["docs"] = _corpus({D(2023, 1, 5): 2}, "alpha")
    assert fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A"])["documents"] == 2
    assert not list(tmp_path.rglob("*.json"))
    monkeypatch.setattr(fb, "free_gb", lambda path="/": 7.9)
    out = fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A"], apply=True)
    assert "free disk" in out["stopped"] and not list(tmp_path.rglob("*.json"))


def test_apply_refuses_a_process_local_sec_budget(monkeypatch, tmp_path):
    import sys
    monkeypatch.delenv("PE_SHARED_RATELIMIT", raising=False)
    monkeypatch.setattr(sys, "argv", ["x", "--out", str(tmp_path), "--apply"])
    with pytest.raises(SystemExit) as e:
        fb.main()
    assert e.value.code == 2


def test_coverage_needs_every_day_of_the_range_and_hits_filter_by_issuer_and_date(efts, tmp_path):
    efts["docs"] = _corpus({D(2022, 6, 1): 2, D(2023, 6, 1): 3}, "alpha")
    fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A"], apply=True)
    assert scan_status(tmp_path, ["A"], D(2023, 2, 1), D(2023, 11, 30))["done"] is True
    gap = scan_status(tmp_path, ["A"], D(2022, 12, 1), D(2023, 6, 30))
    assert gap["done"] is False and gap["keys"]["A"].startswith("gap")
    fb.run(tmp_path, D(2022, 1, 1), D(2022, 12, 31), keys=["A"], apply=True)
    assert scan_status(tmp_path, ["A"], D(2022, 12, 1), D(2023, 6, 30))["done"] is True
    hits = load_hits(tmp_path, ["A"], ["0000000001", "0000000002"], D(2023, 1, 1), D(2023, 12, 31))
    assert [h["adsh"] for h in hits["0000000001"]] == ["20230601-0000"] and len(hits["0000000002"]) == 1
    assert all(h["risk_key"] == "A" and h["file_date"] == "2023-06-01" for hs in hits.values() for h in hs)


def test_the_queries_are_the_production_phrases_plus_the_foreign_annual_lane():
    from app.discovery.risks import TEXT_RISKS
    q = fb.queries()
    assert {k: (p, f) for k, (p, f) in q.items() if k in TEXT_RISKS} == {k: (v[0], v[1]) for k, v in TEXT_RISKS.items()}
    assert {k for k in q if k not in TEXT_RISKS} == {"FOREIGN_GC_0", "FOREIGN_GC_1", "FOREIGN_GC_2"}
    assert all(q[k][1] == ["20-F", "40-F"] for k in q if k.startswith("FOREIGN_GC"))


def test_a_busy_day_is_split_by_form_before_paging(efts, monkeypatch):
    monkeypatch.setattr(fb, "DAY_PASSES", 1)  # one pass per form must be enough once the day is split
    docs = _corpus({D(2003, 3, 31): 90}, "late")
    for i, d in enumerate(docs):
        d["form"] = "NT 10-K" if i % 2 else "NT 10-Q"
    efts["docs"] = docs + [dict(x, adsh=x["adsh"] + "b") for x in docs[:60]]  # 150 on the day: 75 per form
    r = fb.fetch_period("late", ["NT 10-K", "NT 10-Q"], D(2003, 3, 31), D(2003, 3, 31))
    assert r["status"] == "done" and len(r["docs"]) == r["total"] == 150 and r["leaves"] == 2


def test_a_server_error_is_retried_at_the_page_before_the_period_fails(efts, monkeypatch):
    from app import http
    efts["docs"] = _corpus({D(2023, 1, 5): 2}, "alpha")
    real, n = fb._page, {"k": 0}

    def flaky(*a):
        n["k"] += 1
        if n["k"] <= 2:
            raise http.HttpError("efts", 500, "retryable")
        return real(*a)
    monkeypatch.setattr(fb, "_page", flaky)
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)
    assert len(fb.fetch_period("alpha", ["8-K"], D(2023, 1, 1), D(2023, 12, 31))["docs"]) == 2
    n["k"] = -10  # a stubborn error beyond PAGE_RETRIES still fails the period (never a silent gap)
    with pytest.raises(http.HttpError):
        fb.fetch_period("alpha", ["8-K"], D(2023, 1, 1), D(2023, 12, 31))


def test_a_total_the_pages_never_deliver_fails_the_period(efts, tmp_path):
    """EFTS reports one document more than any page returns (an index hiccup): the year is failed, never stored short."""
    efts["docs"], efts["phantom"] = _corpus({D(2023, 1, 5): 3}, "alpha"), 1
    out = fb.run(tmp_path, D(2023, 1, 1), D(2023, 12, 31), keys=["A"], apply=True)
    assert out["failed"] == 1 and not (tmp_path / "hits/A/2023-01-01_2023-12-31.jsonl.gz").exists()
