"""Interactive Brokers adapter (Client Portal Web API shape). Not connected in this build: no credentials are
configured or read here. Paper mode can be wired later as a SECOND execution simulation next to the internal
ledger (app.paper), which stays the analytical source of truth. Live accounts can never place orders."""
from __future__ import annotations

from app.brokers import Broker, NotConnected, OrderPreview, RealMoneyDisabled


class IbkrBroker(Broker):
    name = "ibkr"

    def __init__(self, base_url: str | None = None, account_id: str | None = None, paper: bool = True):
        self.base_url, self.account_id, self.paper = base_url, account_id, paper

    def _require(self):
        if not self.base_url or not self.account_id:
            raise NotConnected("IBKR is not connected (no gateway / paper account configured)")

    def get_account(self) -> dict:
        self._require()
        raise NotConnected("gateway client not implemented until a paper account is connected")

    def get_cash(self) -> float:
        return float(self.get_account().get("cash", 0.0))

    def get_positions(self) -> list[dict]:
        self._require()
        raise NotConnected("gateway client not implemented until a paper account is connected")

    def get_quote(self, symbol: str) -> dict:
        self._require()
        raise NotConnected("gateway client not implemented until a paper account is connected")

    def preview_order(self, symbol, side, quantity, order_type="LMT", limit_price=None) -> OrderPreview:
        warnings = [] if order_type == "LMT" else ["market orders are not used by PUBLIC EDGE"]
        if side not in ("BUY", "SELL") or quantity <= 0:
            raise ValueError("invalid order")
        return OrderPreview(symbol, side, quantity, order_type, limit_price, limit_price, None, warnings)

    def place_order(self, preview: OrderPreview, *, approval_code: str) -> dict:
        if not self.paper:
            raise RealMoneyDisabled("real-money execution is disabled in this build")
        if not approval_code:
            raise PermissionError("every order needs a human approval code")
        self._require()
        raise NotConnected("IBKR paper execution not connected")

    def cancel_order(self, order_id: str) -> dict:
        self._require()
        raise NotConnected("IBKR paper execution not connected")

    def order_status(self, order_id: str) -> dict:
        self._require()
        raise NotConnected("IBKR paper execution not connected")
