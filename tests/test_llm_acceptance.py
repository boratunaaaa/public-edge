from datetime import date

import pytest

from app.corpactions.llm_acceptance import decide


@pytest.mark.parametrize("field", ["exchange_ratio", "cash_per_share"])
def test_ratio_and_cash_auto_accept_only_through_the_gate(field):
    assert decide(field, 0.5, gate_passed=True) == "AUTO_ACCEPTED"
    assert decide(field, 0.5, gate_passed=False) == "REJECTED"
    assert decide(field, None, gate_passed=True) == "EMPTY"


def test_successor_and_date_are_never_auto_accepted():
    assert decide("successor", "Acme", gate_passed=True) == "PENDING_REVIEW"
    assert decide("successor", "Acme", gate_passed=True, primary={"value_cik": "0000000001", "successor_ciks": ["0000000001"]}) == "VALIDATED"
    assert decide("successor", "Acme", gate_passed=True, primary={"value_cik": "0000000002", "successor_ciks": ["0000000001"]}) == "PENDING_REVIEW"
    p = {"anchors": [date(2024, 3, 1)], "stated_in_completion_8k": True}
    assert decide("effective_date", "2024-02-28", gate_passed=True, primary=p) == "VALIDATED"
    assert decide("effective_date", "2022-07-01", gate_passed=True, primary=p) == "PENDING_REVIEW"  # another deal's date
    assert decide("effective_date", "2024-02-28", gate_passed=True, primary={"anchors": [date(2024, 3, 1)]}) == "PENDING_REVIEW"


def test_fund_mergers_are_blocked():
    for f in ("exchange_ratio", "cash_per_share", "successor", "effective_date"):
        assert decide(f, 1.0, gate_passed=True, is_fund=True) == "BLOCKED"
