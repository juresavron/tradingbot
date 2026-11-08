"""Exact spot-order filters and per-order capital checks.

These checks do not reserve balances or cap the sum of multiple open orders.
Execution must serialize unresolved orders and use fresh available balances.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext

from .orders import OrderIntent, decimal_text, decimal_value


def notional(intent: OrderIntent) -> Decimal:
    """Return quantity * limit price without the process context's rounding."""
    if not isinstance(intent, OrderIntent):
        raise ValueError("An OrderIntent is required")
    quantity = decimal_value(intent.quantity, "quantity")
    price = decimal_value(intent.limit_price, "limit_price")
    with localcontext() as context:
        context.prec = 100
        return quantity * price


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal
    min_notional: Decimal
    max_notional: Decimal = Decimal("0")

    def __post_init__(self):
        if not isinstance(self.symbol, str):
            raise ValueError("symbol must be a Binance spot symbol")
        symbol = self.symbol.upper().replace("/", "").replace("-", "")
        if not re.fullmatch(r"[A-Z0-9]{2,30}", symbol):
            raise ValueError("symbol must be a Binance spot symbol")
        object.__setattr__(self, "symbol", symbol)
        for name in ("base_asset", "quote_asset"):
            asset = getattr(self, name)
            if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9]{1,30}", asset):
                raise ValueError(f"{name} must be an asset code")
            object.__setattr__(self, name, asset.upper())
        if self.base_asset == self.quote_asset:
            raise ValueError("base_asset and quote_asset must differ")
        if self.symbol != self.base_asset + self.quote_asset:
            raise ValueError("symbol must equal base_asset + quote_asset")
        for name in (
            "min_qty", "max_qty", "step_size", "min_price", "max_price",
            "tick_size", "min_notional", "max_notional",
        ):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name, allow_zero=True))
        for minimum, maximum in (
            ("min_qty", "max_qty"), ("min_price", "max_price"),
            ("min_notional", "max_notional"),
        ):
            low, high = getattr(self, minimum), getattr(self, maximum)
            if high and low > high:
                raise ValueError(f"{minimum} cannot exceed enabled {maximum}")

    def validate(self, intent: OrderIntent) -> None:
        amount = notional(intent)
        if intent.symbol != self.symbol:
            raise ValueError("Order symbol does not match symbol rules")
        quantity, price = intent.quantity, intent.limit_price
        with localcontext() as context:
            context.prec = 100
            if self.min_qty and quantity < self.min_qty:
                raise ValueError("Order quantity is below min_qty")
            if self.max_qty and quantity > self.max_qty:
                raise ValueError("Order quantity exceeds max_qty")
            if self.step_size and quantity % self.step_size:
                raise ValueError("Order quantity must be an exact multiple of step_size")
            if self.min_price and price < self.min_price:
                raise ValueError("Order price is below min_price")
            if self.max_price and price > self.max_price:
                raise ValueError("Order price exceeds max_price")
            if self.tick_size and price % self.tick_size:
                raise ValueError("Order price must be an exact multiple of tick_size")
            if self.min_notional and amount < self.min_notional:
                raise ValueError("Order notional is below min_notional")
            if self.max_notional and amount > self.max_notional:
                raise ValueError("Order notional exceeds max_notional")

    def to_dict(self) -> dict[str, str]:
        return {
            name: decimal_text(value) if isinstance(value, Decimal) else value
            for name, value in vars(self).items()
        }


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("300")
    fee_buffer: Decimal = Decimal("0.002")

    def __post_init__(self):
        object.__setattr__(self, "max_order_notional", decimal_value(self.max_order_notional, "max_order_notional"))
        buffer = decimal_value(self.fee_buffer, "fee_buffer", allow_zero=True)
        if buffer > Decimal("0.1"):
            raise ValueError("fee_buffer must be in [0, 0.1]")
        object.__setattr__(self, "fee_buffer", buffer)

    def validate(self, intent: OrderIntent, rules: SymbolRules, balances: dict[str, Decimal]) -> None:
        rules.validate(intent)
        if not isinstance(balances, dict):
            raise ValueError("balances must be a dictionary of exact nonnegative decimals")
        # Derived fills and commissions can legitimately carry more decimal
        # places than an exchange order's quantity or price inputs.
        available = {asset: decimal_value(balance, "balance", allow_zero=True, max_places=60)
                     for asset, balance in balances.items()}
        amount = notional(intent)
        if amount > self.max_order_notional:
            raise ValueError("Order notional exceeds max_order_notional risk limit")
        with localcontext() as context:
            context.prec = 100
            if intent.side == "BUY":
                required = amount * (Decimal("1") + self.fee_buffer)
                if available.get(rules.quote_asset, Decimal("0")) < required:
                    raise ValueError(f"Insufficient {rules.quote_asset} balance including fee buffer")
            elif intent.side == "SELL":
                if available.get(rules.base_asset, Decimal("0")) < intent.quantity:
                    raise ValueError(f"Insufficient {rules.base_asset} balance for a spot sale")
            else:
                raise ValueError("Order side must be BUY or SELL")
