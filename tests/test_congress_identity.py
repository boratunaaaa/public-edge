"""Congress identity repair: issuer at the trade date, merged-record protection, clusters by issuer, audited repair."""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text

from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
OLD, NEW = "0000000701", "0000000702"  # two issuers that used the symbol "RUSE" at different times


def _sym(c, cik, symbol, first, last, source="form345_filing"):
    c.execute(text("""INSERT INTO security_symbols (cik, symbol, first_seen, last_seen, n_obs, source)
                      VALUES (:c, :s, :f, :l, 5, :src)"""), {"c": cik, "s": symbol, "f": first, "l": last, "src": source})


def _master(c, cik, name):
    c.execute(text("INSERT INTO security_master (cik, name) VALUES (:c, :n) ON CONFLICT (cik) DO UPDATE SET name = :n"),
              {"c": cik, "n": name})


def _world(c):
    """RUSE: Oldco 2019-2021, Newco (today's owner, in the current ticker map) from 2023. AMBI: two issuers at once."""
    from app.entities.registry import resolver, sync_registry
    sync_registry(c, [{"cik": NEW, "name": "Newco Robotics Inc", "ticker": "RUSE", "exchange": "NYSE"},
                      {"cik": "0000000705", "name": "Mapped Only Holdings", "ticker": "MAPO", "exchange": "NYSE"}])
    resolver.load(c, force=True)
    _master(c, OLD, "Oldco Pharmaceuticals Corp")
    _master(c, NEW, "Newco Robotics Inc")
    _master(c, "0000000703", "Alpha Widgets Inc")
    _master(c, "0000000704", "Beta Widgets Inc")
    _master(c, "0000000705", "Mapped Only Holdings")
    _sym(c, OLD, "RUSE", date(2019, 1, 2), date(2021, 6, 30))
    _sym(c, NEW, "RUSE", date(2023, 3, 1), date(2026, 9, 1))
    _sym(c, "0000000703", "AMBI", date(2020, 1, 2), date(2022, 1, 2))
    _sym(c, "0000000704", "AMBI", date(2020, 6, 1), date(2022, 6, 1))
    _sym(c, "0000000706", "LATE", date(2024, 1, 2), date(2026, 9, 1))  # observed only from 2024
    _master(c, "0000000706", "Late Observed Corp")


# ------------------------------------------------------------------ pure helpers

@pytest.mark.parametrize("asset, merged", [
    ("Deere & Company (DE) [ST]", False),
    ("Albemarle Corporation (AlB) [ST] gfedc FIlINg STATuS: New Deere & Company (DE) [ST]", True),  # two lines run together
    ("Church & Dwight Company, Inc. gfedcb (CHD) [ST] FIlINg STATuS: New Merck & Company (MRK)", True),
    ("berkshire Hathaway Inc. New gfedc (bRK.b) [ST]", False),
])
def test_merged_record_protection(asset, merged):
    from app.sources.congress.identity import merged_record
    assert merged_record(asset) is merged


@pytest.mark.parametrize("asset, ticker, name", [
    ("Bank of America Corporation Common Stock (BAC) [ST]", "BAC", "Bank of America Corporation"),
    ("Notification Amount Cap. Type Date Gains > $200? JPMorgan (JPM) [ST]", "JPM", "JPMorgan"),
    ("Notification Amount Cap. Type Date Gains > ? (UNH) [ST]", "UNH", ""),  # page header only: no name
    ("berkshire Hathaway Inc. New gfedc (bRK.b) [ST]", "BRK.B", "berkshire Hathaway Inc"),
])
def test_the_disclosed_name_is_the_text_right_before_the_ticker(asset, ticker, name):
    from app.sources.congress.identity import clean_asset_name
    assert clean_asset_name(asset, ticker) == name


# ------------------------------------------------------------------ resolution at the trade date

@db
@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize("ticker, asset, on, cik, conf, reason", [
    ("RUSE", "Oldco Pharmaceuticals (RUSE) [ST]", date(2020, 5, 1), OLD, "HIGH", "SYMBOL_PERIOD"),  # not today's owner
    ("RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 1), NEW, "HIGH", "SYMBOL_PERIOD"),
    ("RUSE", "Oldco Pharmaceuticals (RUSE) [ST]", date(2024, 5, 1), NEW, "LOW", "REVIEW_REQUIRED:NAME_MISMATCH"),
    ("RUSE", "Something (RUSE) [ST]", date(2022, 6, 1), None, "LOW", "REVIEW_REQUIRED:NO_ISSUER_AT_DATE"),  # reused, gap
    ("AMBI", "Widgets (AMBI) [ST]", date(2021, 3, 1), None, "LOW", "REVIEW_REQUIRED:AMBIGUOUS_SYMBOL:0000000703,0000000704"),
    ("LATE", "Late Observed Corp (LATE) [ST]", date(2021, 3, 1), "0000000706", "MEDIUM", "SINGLE_ISSUER_SYMBOL_NAME_MATCH"),
    ("LATE", "Other Name Ltd (LATE) [ST]", date(2021, 3, 1), "0000000706", "LOW", "REVIEW_REQUIRED:SINGLE_ISSUER_SYMBOL_UNCONFIRMED"),
    ("MAPO", "Mapped Only Holdings (MAPO) [ST]", date(2021, 3, 1), "0000000705", "MEDIUM", "CURRENT_MAP_NAME_MATCH"),
    ("NOPE", "Foreign OTC Co (NOPE) [ST]", date(2021, 3, 1), None, "NONE", "REVIEW_REQUIRED:NO_SEC_ISSUER"),
    (None, "No Ticker Here [ST]", date(2021, 3, 1), None, "NONE", "REVIEW_REQUIRED:NO_TICKER"),
    ("RUSE", "A Corp (RUSE) [ST] Other Corp (OTHR) [ST]", date(2024, 5, 1), None, "LOW", "REVIEW_REQUIRED:PARSER_MERGED_RECORDS"),
])
def test_identity_at_the_trade_date(engine, ticker, asset, on, cik, conf, reason):
    from app.sources.congress.identity import resolve
    with engine.begin() as c:
        _world(c)
        i = resolve(c, ticker, asset, on)
    assert (i.cik, i.confidence, i.reason) == (cik, conf, reason)


@db
@pytest.mark.usefixtures("clean")
def test_names_that_are_only_tickers_are_not_a_contradiction(engine):
    from app.sources.congress.identity import resolve
    with engine.begin() as c:
        _world(c)
        _sym(c, "0000000707", "TKNM", date(2019, 1, 2), date(2026, 1, 2))  # the DB knows no name for it, only symbols
        i = resolve(c, "TKNM", "Totally Named Partners L.P. (TKNM) [ST]", date(2021, 1, 4))
    assert (i.cik, i.confidence) == ("0000000707", "MEDIUM")


# ------------------------------------------------------------------ forward path: PTR events and clusters

def _tx(c, doc, line, pol, ticker, asset, tdate, fdate, ttype="PURCHASE"):
    from app.db.models import congress_transactions
    c.execute(congress_transactions.insert().values(
        chamber="house", doc_id=doc, line_no=line, politician=pol, ticker=ticker, asset_name=asset, asset_type="ST",
        transaction_type=ttype, transaction_date=tdate, filing_date=fdate, amount_min=1001, amount_max=15000,
        amount_range="$1,001 - $15,000", owner="self", source_url="u"))


@db
@pytest.mark.usefixtures("clean")
def test_a_cluster_counts_only_trades_of_the_same_issuer_and_carries_it(engine):
    from app.db.models import events
    from app.sources.congress.house import congress_cluster_check
    with engine.begin() as c:
        _world(c)
        # three members bought "RUSE" within a week - but one of them in a merged / unsafe PTR record
        _tx(c, "1", 0, "A Member", "RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 1), date(2024, 5, 10))
        _tx(c, "2", 0, "B Member", "RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 3), date(2024, 5, 12))
        _tx(c, "3", 0, "C Member", "RUSE", "X Corp (XX) [ST] Newco Robotics (RUSE) [ST]", date(2024, 5, 4), date(2024, 5, 12))
        ids = congress_cluster_check(c, "RUSE", date(2024, 5, 12), cik=NEW)
        ev = c.execute(select(events).where(events.c.id.in_(ids))).mappings().all()
    assert len(ev) == 1 and ev[0]["issuer_cik"] == NEW and ev[0]["payload"]["n_members"] == 2  # C's record excluded


@db
@pytest.mark.usefixtures("clean")
def test_process_ptr_writes_the_trade_date_issuer_and_reviews_unsafe_lines(engine, monkeypatch):
    from app.db.models import events
    from app.parsers.house_ptr import Ptr, PtrLine
    from app.sources.congress import house

    def line(n, asset, ticker):
        return PtrLine(line_no=n, owner="self", asset_name=asset, asset_type="ST", ticker=ticker,
                       transaction_type="PURCHASE", transaction_date=date(2020, 5, 1), notification_date=date(2020, 5, 2),
                       amount_min=1001, amount_max=15000, amount_range="$1,001 - $15,000")
    ptr = Ptr(name="A Member", state_district="CA01", text_ok=True, lines=[
        line(0, "Oldco Pharmaceuticals (RUSE) [ST]", "RUSE"),  # 2020: Oldco, although today's map says Newco
        line(1, "A Corp (AAA) [ST] Oldco (RUSE) [ST]", "AAA"),  # merged record
    ])
    monkeypatch.setattr(house, "pdf_text", lambda b: "text")
    monkeypatch.setattr(house, "parse_ptr_text", lambda t: ptr)
    with engine.begin() as c:
        _world(c)
        house.process_ptr(c, {"doc_id": "D1", "year": 2020, "name": "A Member", "state_district": "CA01",
                              "filing_date": date(2020, 5, 20)}, b"%PDF")
        rows = {r["dedupe_key"]: r for r in c.execute(select(events).where(events.c.event_type == "CONGRESS_BUY")).mappings()}
    ok, bad = rows["CONG:house:D1:0"], rows["CONG:house:D1:1"]
    assert (ok["issuer_cik"], ok["identity_confidence"], ok["needs_review"]) == (OLD, "HIGH", False)
    assert (bad["issuer_cik"], bad["identity_confidence"], bad["needs_review"]) == (None, "LOW", True)
    assert bad["payload"]["identity_review"] == "REVIEW_REQUIRED:PARSER_MERGED_RECORDS"


# ------------------------------------------------------------------ repair job: dry run, apply, undo

def _event(c, key, ticker, cik, tdate):
    from app.db.store import create_event
    return create_event(c, event_type="CONGRESS_BUY", dedupe_key=key, public_date=tdate, transaction_date=tdate,
                        ticker=ticker, issuer_cik=cik, title="t", payload={"politician": "A Member"}, source_url="u",
                        public_at=datetime(tdate.year, tdate.month, tdate.day, 21, tzinfo=timezone.utc))


def _ret(c, eid):
    c.execute(text("""INSERT INTO event_returns (event_id, horizon_days, entry_date, entry_price, exit_date, exit_price, ret)
                      VALUES (:e, 90, '2020-05-04', 10, '2020-08-03', 11, 0.1)"""), {"e": eid})


def _seed(c):
    _world(c)
    ids = {}
    cases = {  # key: (tx ticker, asset, trade date, stored cik)
        "same": ("RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 1), NEW),
        "changed": ("RUSE", "Oldco Pharmaceuticals (RUSE) [ST]", date(2020, 5, 1), NEW),  # today's map was used
        "filled": ("RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 6, 3), None),
        "review": ("AMBI", "Widgets (AMBI) [ST]", date(2021, 3, 1), "0000000703"),
    }
    for n, (key, (tk, asset, d, cik)) in enumerate(cases.items()):
        _tx(c, f"R{n}", 0, "A Member", tk, asset, d, d)
        ids[key] = _event(c, f"CONG:house:R{n}:0", tk, cik, d)
        _ret(c, ids[key])
    return ids


@db
@pytest.mark.usefixtures("clean")
def test_the_dry_run_counts_and_writes_nothing(engine):
    from app.jobs.congress_repair import run
    with engine.begin() as c:
        _seed(c)
    out = run(engine)
    assert out["mode"] == "DRY_RUN"
    assert out["counts"] == {"CONGRESS_BUY:SAME": 1, "CONGRESS_BUY:CHANGED": 1, "CONGRESS_BUY:FILLED": 1,
                             "CONGRESS_BUY:REVIEW": 1}
    assert out["returns_to_remove"] == 3 and out["returns_kept"] == 1
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM event_returns")).scalar() == 4
        assert c.execute(text("SELECT count(*) FROM data_cleanup_audit")).scalar() == 0


@db
@pytest.mark.usefixtures("clean")
def test_apply_repairs_removes_stale_returns_audits_and_undo_restores(engine):
    from app.jobs.congress_repair import run, undo
    with engine.begin() as c:
        ids = _seed(c)
        before = {k: dict(c.execute(text("SELECT issuer_cik, identity_confidence, needs_review FROM events WHERE id = :i"),
                                    {"i": v}).mappings().one()) for k, v in ids.items()}
        full_before = {k: c.execute(text("SELECT to_jsonb(e) - 'updated_at' FROM events e WHERE id = :i"), {"i": v}).scalar()
                       for k, v in ids.items()}
    out = run(engine, do_apply=True)
    with engine.connect() as c:
        ev = {k: dict(c.execute(text("""SELECT issuer_cik, identity_confidence, needs_review, returns_status, missing_reason,
                                            EXISTS (SELECT 1 FROM event_returns r WHERE r.event_id = e.id) has_ret
                                            FROM events e WHERE id = :i"""), {"i": v}).mappings().one()) for k, v in ids.items()}
        audit = c.execute(text("SELECT count(*) FROM data_cleanup_audit WHERE run_id = :r"), {"r": out["run_id"]}).scalar()
    assert ev["same"]["issuer_cik"] == NEW and ev["same"]["has_ret"] and ev["same"]["identity_confidence"] == "HIGH"
    assert ev["changed"]["issuer_cik"] == OLD and not ev["changed"]["has_ret"] and ev["changed"]["returns_status"] == "pending"
    assert ev["filled"]["issuer_cik"] == NEW and not ev["filled"]["has_ret"]
    assert ev["review"]["needs_review"] and ev["review"]["issuer_cik"] is None and not ev["review"]["has_ret"]
    assert ev["review"]["missing_reason"] == "IDENTITY_REVIEW"
    assert audit == 4 and out["applied"]["returns_removed"] == 3
    with engine.begin() as c:  # the return worker recomputed a return for the repaired identity before the undo
        _ret(c, ids["changed"])
        assert undo(c, out["run_id"]) == 4
        after = {k: dict(c.execute(text("SELECT issuer_cik, identity_confidence, needs_review FROM events WHERE id = :i"),
                                   {"i": v}).mappings().one()) for k, v in ids.items()}
        rows_after = {k: c.execute(text("SELECT to_jsonb(e) - 'updated_at' FROM events e WHERE id = :i"), {"i": v}).scalar()
                      for k, v in ids.items()}
        rets_after = c.execute(text("SELECT event_id, horizon_days, ret FROM event_returns ORDER BY 1")).all()
    assert after == before
    assert rows_after == full_before  # every column restored exactly
    assert [tuple(r) for r in rets_after] == [(ids[k], 90, 0.1) for k in ("same", "changed", "filled", "review")]


@db
@pytest.mark.usefixtures("clean")
def test_a_cluster_event_takes_its_members_issuer_or_goes_to_review(engine):
    from app.db.store import create_event
    from app.jobs.congress_repair import run
    with engine.begin() as c:
        _world(c)
        _tx(c, "C1", 0, "A Member", "RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 1), date(2024, 5, 10))
        _tx(c, "C2", 0, "B Member", "RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 3), date(2024, 5, 12))
        _tx(c, "C3", 0, "C Member", "AMBI", "Widgets (AMBI) [ST]", date(2021, 3, 1), date(2021, 3, 5))
        _tx(c, "C4", 0, "D Member", "AMBI", "Widgets (AMBI) [ST]", date(2021, 3, 2), date(2021, 3, 6))
        for k, tk, d in (("C1", "RUSE", date(2024, 5, 1)), ("C2", "RUSE", date(2024, 5, 3)),
                         ("C3", "AMBI", date(2021, 3, 1)), ("C4", "AMBI", date(2021, 3, 2))):
            _event(c, f"CONG:house:{k}:0", tk, None, d)

        def cluster(key, tk, members, d):
            return create_event(c, event_type="CONGRESS_CLUSTER_BUY", dedupe_key=key, public_date=d, transaction_date=d,
                                ticker=tk, title="t", source_url="u",
                                payload={"trades": [{"politician": p, "date": str(t)} for p, t in members]})
        good = cluster("CL1", "RUSE", [("A Member", date(2024, 5, 1)), ("B Member", date(2024, 5, 3))], date(2024, 5, 12))
        bad = cluster("CL2", "AMBI", [("C Member", date(2021, 3, 1)), ("D Member", date(2021, 3, 2))], date(2021, 3, 6))
    run(engine, do_apply=True)
    with engine.connect() as c:
        g = c.execute(text("SELECT issuer_cik, needs_review FROM events WHERE id = :i"), {"i": good}).one()
        b = c.execute(text("SELECT issuer_cik, needs_review FROM events WHERE id = :i"), {"i": bad}).one()
    assert tuple(g) == (NEW, False) and tuple(b) == (None, True)


@db
@pytest.mark.usefixtures("clean")
def test_a_safe_identity_lifts_an_earlier_identity_review_and_fixes_the_name(engine):
    from app.jobs.congress_repair import run
    with engine.begin() as c:
        _world(c)
        _tx(c, "S1", 0, "A Member", "RUSE", "Oldco Pharmaceuticals (RUSE) [ST]", date(2020, 5, 1), date(2020, 5, 1))
        eid = _event(c, "CONG:house:S1:0", "RUSE", NEW, date(2020, 5, 1))
        c.execute(text("""UPDATE events SET needs_review = true, missing_reason = 'IDENTITY_REVIEW', company_name = 'Newco Robotics Inc',
                          payload = payload || '{"identity_review": "old"}'::jsonb WHERE id = :i"""), {"i": eid})
        other = _event(c, "CONG:house:S1:9", "RUSE", NEW, date(2024, 5, 1))  # reviewed for another reason: kept
        _tx(c, "S1", 9, "A Member", "RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 1), date(2024, 5, 1))
        c.execute(text("UPDATE events SET needs_review = true, missing_reason = 'DATA_QUALITY' WHERE id = :i"), {"i": other})
    run(engine, do_apply=True)
    with engine.connect() as c:
        r = c.execute(text("SELECT issuer_cik, needs_review, missing_reason, company_name, payload ? 'identity_review' ir "
                           "FROM events WHERE id = :i"), {"i": eid}).mappings().one()
        o = c.execute(text("SELECT needs_review, missing_reason FROM events WHERE id = :i"), {"i": other}).one()
    assert (r["issuer_cik"], r["needs_review"], r["missing_reason"], r["ir"]) == (OLD, False, None, False)
    assert r["company_name"] == "Oldco Pharmaceuticals Corp"
    assert tuple(o) == (True, "DATA_QUALITY")


@db
@pytest.mark.usefixtures("clean")
def test_an_ambiguous_cluster_member_line_sends_the_cluster_to_review(engine):
    from app.db.store import create_event
    from app.jobs.congress_repair import run
    with engine.begin() as c:
        _world(c)
        _tx(c, "M1", 0, "A Member", "RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 1), date(2024, 5, 10))
        _tx(c, "M2", 0, "B Member", "RUSE", "Newco Robotics (RUSE) [ST]", date(2024, 5, 3), date(2024, 5, 12))
        _tx(c, "M3", 0, "B Member", "RUSE", "X Corp (XX) [ST] Newco (RUSE) [ST]", date(2024, 5, 3), date(2024, 5, 20))  # same member/date
        for k in ("M1", "M2", "M3"):
            _event(c, f"CONG:house:{k}:0", "RUSE", None, date(2024, 5, 1))
        cl = create_event(c, event_type="CONGRESS_CLUSTER_BUY", dedupe_key="CLM", public_date=date(2024, 5, 12),
                          transaction_date=date(2024, 5, 3), ticker="RUSE", title="t", source_url="u",
                          payload={"trades": [{"politician": "A Member", "date": "2024-05-01"},
                                              {"politician": "B Member", "date": "2024-05-03"}]})
        new = create_event(c, event_type="CONGRESS_CLUSTER_BUY", dedupe_key="CLN", public_date=date(2024, 5, 12),
                           transaction_date=date(2024, 5, 3), ticker="RUSE", title="t", source_url="u",
                           payload={"trades": [{"politician": "A Member", "date": "2024-05-01", "doc_id": "M1", "line_no": 0},
                                               {"politician": "B Member", "date": "2024-05-03", "doc_id": "M2", "line_no": 0}]})
    run(engine, do_apply=True)
    with engine.connect() as c:
        assert tuple(c.execute(text("SELECT issuer_cik, needs_review FROM events WHERE id = :i"), {"i": cl}).one()) == (None, True)
        assert tuple(c.execute(text("SELECT issuer_cik, needs_review FROM events WHERE id = :i"), {"i": new}).one()) == (NEW, False)
