"""Memory guards for the historical backfill (1 GB container): huge EDGAR submissions are never decoded whole,
and a filing that keeps failing is not re-fetched every cycle."""
import httpx
import pytest

from tests.test_db import URL

db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def test_full_text_reads_only_the_head_of_a_huge_submission(monkeypatch):
    """A SC 13D with 60-80 MB of embedded exhibits (CIK 1961847, 2024) OOM-killed the backfill every cycle."""
    from app.sources.sec import edgar
    seen = {}

    def fake_get(url, headers=None, **kw):
        seen["range"] = (headers or {}).get("Range")
        body = b"<SEC-DOCUMENT><TYPE>SC 13D\n" + b"x" * (edgar.MAX_FULL_TEXT_BYTES + 5_000_000)  # server ignoring Range
        return httpx.Response(200, content=body, request=httpx.Request("GET", url))
    monkeypatch.setattr(edgar.http, "get", fake_get)
    txt = edgar.fetch_full_text("1961847", "0001493152-24-035926")
    assert seen["range"] == f"bytes=0-{edgar.MAX_FULL_TEXT_BYTES - 1}"
    assert len(txt) == edgar.MAX_FULL_TEXT_BYTES and txt.startswith("<SEC-DOCUMENT><TYPE>SC 13D")


@pytest.mark.parametrize("raw,expected", [("123456 78 9", "123456789"), ("123456\n789", "123456789"),
                                          ("12345678\t9", "123456789"), ("1234567890123", None), ("", None), (None, None)])
def test_cusip_is_nine_characters_or_nothing(raw, expected):
    """A CUSIP spanning a line break became > 9 characters: the insert failed and the filing was re-fetched forever."""
    from app.parsers.schedule13d import _cusip
    assert _cusip(raw) == expected


def test_legacy_13d_cusip_across_a_line_break_fits_the_column():
    from app.parsers.schedule13d import parse_schedule13_legacy
    txt = "SCHEDULE 13D\n(Name of Issuer)\n 12345\nA10 9\n(CUSIP Number)\nPercent of class represented by amount in row (11): 7.5%"
    s = parse_schedule13_legacy(txt, "SC 13D")
    assert s.cusip is None or len(s.cusip) == 9


@db
@pytest.mark.usefixtures("clean")
def test_13d_backfill_gives_up_on_a_filing_that_keeps_failing(engine, monkeypatch):
    from app.db.store import job_state
    from app.sources.sec import backfill as B
    calls = []
    monkeypatch.setattr(B, "list_13d_filings", lambda y, q: [("SC 13D", "1961847", "0001493152-24-035926")] if (y, q) == (2024, 3) else [])
    monkeypatch.setattr(B, "disk_ok", lambda: True)

    def boom(cik, acc):
        calls.append(acc)
        raise RuntimeError("value too long for type character varying(9)")
    monkeypatch.setattr(B.edgar, "fetch_full_text", boom)
    for _ in range(B.MAX_13D_FAILS + 2):
        with engine.begin() as c:  # keep the quarter open for the next run, as a failing quarter would stay
            c.execute(__import__("sqlalchemy").text("UPDATE processing_jobs SET state = state - 'done' WHERE name = 'backfill_13d'"))
        B.backfill_13d(engine, years=3)
    with engine.connect() as c:
        st = job_state(c, "backfill_13d")
    assert len(calls) == B.MAX_13D_FAILS  # then skipped, not re-fetched every cycle
    assert st["gave_up"] == ["0001493152-24-035926"]
