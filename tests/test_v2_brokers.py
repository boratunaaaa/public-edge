import pytest

from app.brokers import NotConnected, RealMoneyDisabled, route
from app.brokers.ibkr import IbkrBroker


def test_real_money_is_disabled_even_when_connected():
    b = IbkrBroker(base_url="https://localhost:5000", account_id="U123", paper=False)
    p = b.preview_order("AAPL", "BUY", 1, "LMT", 100.0)
    with pytest.raises(RealMoneyDisabled):
        b.place_order(p, approval_code="ABC123")


def test_paper_needs_approval_and_connection():
    b = IbkrBroker()
    p = b.preview_order("AAPL", "BUY", 1, "LMT", 100.0)
    with pytest.raises(PermissionError):
        b.place_order(p, approval_code="")
    with pytest.raises(NotConnected):
        b.place_order(p, approval_code="ABC123")


def test_router_and_no_broker_import_in_signal_engines():
    import pathlib
    assert route("US") == "ibkr" and route("CA") == "ibkr" and route("TR") == "manual"
    root = pathlib.Path(__file__).parent.parent / "app"
    for sub in ("signals", "discovery", "news", "sources", "backtest"):
        for f in (root / sub).rglob("*.py"):
            assert "app.brokers" not in f.read_text(), f
