"""Broker interface (V2 #56-#59). Signal engines never import a broker: proposals -> human approval -> router.

REAL-MONEY EXECUTION IS DISABLED IN THIS BUILD. place_order() on a live account raises RealMoneyDisabled
unconditionally; there is no flag to turn it on (a policy change needs a code change + review)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class RealMoneyDisabled(RuntimeError):
    pass


class NotConnected(RuntimeError):
    pass


@dataclass
class OrderPreview:
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    est_price: float | None
    est_commission: float | None
    warnings: list[str]


class Broker(ABC):
    name = "abstract"
    paper: bool = True

    @abstractmethod
    def get_account(self) -> dict: ...
    @abstractmethod
    def get_cash(self) -> float: ...
    @abstractmethod
    def get_positions(self) -> list[dict]: ...
    @abstractmethod
    def get_quote(self, symbol: str) -> dict: ...
    @abstractmethod
    def preview_order(self, symbol: str, side: str, quantity: float, order_type: str = "LMT",
                      limit_price: float | None = None) -> OrderPreview: ...
    @abstractmethod
    def place_order(self, preview: OrderPreview, *, approval_code: str) -> dict: ...
    @abstractmethod
    def cancel_order(self, order_id: str) -> dict: ...
    @abstractmethod
    def order_status(self, order_id: str) -> dict: ...


def route(country: str) -> str:
    """Market -> broker id. BIST has no supported broker adapter: manual execution only."""
    return {"US": "ibkr", "CA": "ibkr"}.get(country, "manual")
