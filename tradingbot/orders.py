"""Exact order values shared by durable execution and broker adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"})
EXCHANGE_STATUSES = TERMINAL_STATUSES | {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}


class BrokerUnavailable(RuntimeError):
    """Exchange state could not be established; a submit may have succeeded."""


class OrderNotFound(BrokerUnavailable):
    """A lookup did not find the order; this does not authorize resubmission."""


class OrderRejected(RuntimeError):
    """The broker definitively rejected this attempt before acceptance."""


def decimal_value(value, name: str, *, allow_zero=False, max_places=20) -> Decimal:
    if type(max_places) is not int or not 0 <= max_places <= 60:
        raise ValueError("Decimal place bound must be an integer in [0, 60]")
    if isinstance(value, (float, bool)):
        raise ValueError(f"{name} must be an exact decimal string or Decimal, not float/bool")
    if isinstance(value, str) and len(value) > 128:
        raise ValueError(f"{name} exceeds the supported decimal input length")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite decimal") from None
    if not number.is_finite() or (number < 0 if allow_zero else number <= 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    # A zero's exponent can be enormous despite its tiny coefficient. Always
    # canonicalize it before fixed-point formatting or SQLite persistence.
    if number.is_zero():
        return Decimal("0")
    # Bound magnitudes/precision before multiplication, modulo, or JSON output.
    if number.adjusted() > 20 or number.as_tuple().exponent < -max_places:
        raise ValueError(f"{name} exceeds supported decimal precision/range")
    return number


def decimal_text(value: Decimal) -> str:
    return "0" if value.is_zero() else format(value, "f")


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal

    def __post_init__(self):
        if not isinstance(self.client_order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", self.client_order_id):
            raise ValueError("client_order_id must contain 1-36 letters, digits, underscores or hyphens")
        if not isinstance(self.symbol, str):
            raise ValueError("symbol must be a Binance spot symbol")
        symbol = self.symbol.upper().replace("/", "").replace("-", "")
        if not re.fullmatch(r"[A-Z0-9]{2,30}", symbol):
            raise ValueError("symbol must be a Binance spot symbol")
        object.__setattr__(self, "symbol", symbol)
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        object.__setattr__(self, "quantity", decimal_value(self.quantity, "quantity"))
        object.__setattr__(self, "limit_price", decimal_value(self.limit_price, "limit_price"))

    def to_dict(self) -> dict:
        return {"client_order_id": self.client_order_id, "symbol": self.symbol, "side": self.side,
                "quantity": decimal_text(self.quantity), "limit_price": decimal_text(self.limit_price)}


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    status: str
    executed_quantity: Decimal
    cumulative_quote_quantity: Decimal
    exchange_order_id: str
    updated_at_ms: int

    def __post_init__(self):
        intent = OrderIntent(self.client_order_id, self.symbol, self.side, self.quantity, self.limit_price)
        for name in ("symbol", "quantity", "limit_price"):
            object.__setattr__(self, name, getattr(intent, name))
        for name in ("executed_quantity", "cumulative_quote_quantity"):
            places = 40 if name == "cumulative_quote_quantity" else 20
            object.__setattr__(self, name, decimal_value(getattr(self, name), name,
                                                       allow_zero=True, max_places=places))
        if self.status not in EXCHANGE_STATUSES:
            raise ValueError("Unsupported exchange order status")
        if self.executed_quantity > self.quantity:
            raise ValueError("Executed quantity exceeds order quantity")
        if self.status == "FILLED" and self.executed_quantity != self.quantity:
            raise ValueError("FILLED requires the full executed quantity")
        if self.status == "NEW" and self.executed_quantity:
            raise ValueError("NEW cannot have executed quantity")
        if self.status == "PARTIALLY_FILLED" and not 0 < self.executed_quantity < self.quantity:
            raise ValueError("PARTIALLY_FILLED requires a partial executed quantity")
        if bool(self.executed_quantity) != bool(self.cumulative_quote_quantity):
            raise ValueError("Executed base and quote quantities must agree")
        if not isinstance(self.exchange_order_id, str) or not self.exchange_order_id:
            raise ValueError("Exchange order ID is required")
        if type(self.updated_at_ms) is not int or self.updated_at_ms <= 0:
            raise ValueError("Exchange update time must be positive milliseconds")

    def to_dict(self) -> dict:
        return {name: decimal_text(value) if isinstance(value, Decimal) else value
                for name, value in vars(self).items()}
