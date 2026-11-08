"""Persistent deterministic spot execution simulator, with no network access.

Orders fill immediately at their supplied LIMIT price. Synthetic symbol rules
are fixed fixtures, not current exchange filters. There is no matching engine,
spread, partial filling, queue position, latency, or liquidity simulation.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import nullcontext
from decimal import Decimal, localcontext

from .memory import TradingMemory
from .orders import OrderIntent, OrderNotFound, OrderRejected, OrderSnapshot, decimal_value
from .risk import SymbolRules, notional


def _text(value: Decimal) -> str:
    """Canonical exact text, without Decimal.normalize's context rounding."""
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class SimulatedBroker:
    environment = "simulation"

    def __init__(
        self,
        memory: TradingMemory,
        *,
        account_id: str,
        initial_balances: dict[str, str | Decimal],
        fee: str | Decimal = "0.001",
    ):
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", account_id):
            raise ValueError("Simulation account_id must contain 1-100 letters, digits, underscores, or hyphens")
        if not isinstance(initial_balances, dict):
            raise ValueError("initial_balances must be a dictionary of exact balances")
        balances = {}
        for asset, value in initial_balances.items():
            if not isinstance(asset, str) or not re.fullmatch(r"[A-Za-z0-9]{1,30}", asset):
                raise ValueError("Balance asset must be an asset code")
            asset = asset.upper()
            if asset in balances:
                raise ValueError("Duplicate balance assets after normalization")
            balances[asset] = decimal_value(value, "initial balance", allow_zero=True)
        self.fee = decimal_value(fee, "simulation fee", allow_zero=True)
        if self.fee >= Decimal("0.1"):
            raise ValueError("Simulation fee must be in [0, 0.1)")
        self.memory = memory
        self.account_id = account_id
        self.scope = "simulation:" + account_id
        initial_json = _json({asset: _text(value) for asset, value in balances.items()})
        fee_text = _text(self.fee)
        # Account provisioning can join a caller's larger atomic revision
        # transaction. Ordinary callers still receive standalone atomicity.
        with (nullcontext() if self.memory.connection.in_transaction else self.memory.transaction()):
            self.memory.connection.execute("""
                CREATE TABLE IF NOT EXISTS simulation_wallets (
                    account_id TEXT PRIMARY KEY,
                    initial_json TEXT NOT NULL,
                    balances_json TEXT NOT NULL,
                    fee TEXT NOT NULL
                )
            """)
            self.memory.connection.execute("""
                CREATE TABLE IF NOT EXISTS simulation_orders (
                    client_order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                )
            """)
            row = self.memory.connection.execute(
                "SELECT initial_json, fee FROM simulation_wallets WHERE account_id = ?", (account_id,),
            ).fetchone()
            if row is None:
                self.memory.connection.execute(
                    "INSERT INTO simulation_wallets VALUES (?, ?, ?, ?)",
                    (account_id, initial_json, initial_json, fee_text),
                )
            elif row["initial_json"] != initial_json or row["fee"] != fee_text:
                raise ValueError("Simulation account initial balances or fee changed; use a new account_id")

    def get_rules(self, symbol: str) -> SymbolRules:
        if not isinstance(symbol, str):
            raise OrderRejected("Unsupported synthetic simulation symbol")
        symbol = symbol.upper().replace("/", "").replace("-", "")
        assets = {
            "BTCEUR": ("BTC", "EUR"), "BTCUSDT": ("BTC", "USDT"),
            "ETHEUR": ("ETH", "EUR"), "ETHUSDT": ("ETH", "USDT"),
        }
        if symbol not in assets:
            raise OrderRejected("Unsupported synthetic simulation symbol")
        base, quote = assets[symbol]
        step = "0.00001" if base == "BTC" else "0.0001"
        return SymbolRules(
            symbol=symbol, base_asset=base, quote_asset=quote,
            min_qty=step, max_qty="1000000", step_size=step,
            min_price="0.01", max_price="1000000000", tick_size="0.01",
            min_notional="5", max_notional="0",
        )

    def get_balances(self) -> dict[str, Decimal]:
        row = self.memory.connection.execute(
            "SELECT balances_json FROM simulation_wallets WHERE account_id = ?", (self.account_id,),
        ).fetchone()
        if row is None:
            raise OrderRejected("Simulation wallet is unavailable")
        # Stored amounts result from exact arithmetic and may legitimately
        # exceed the input parser's decimal-place bound after charging fees.
        return {asset: Decimal(value) for asset, value in json.loads(row["balances_json"]).items()}

    @staticmethod
    def _intent_json(intent: OrderIntent) -> str:
        if not isinstance(intent, OrderIntent):
            raise ValueError("An OrderIntent is required")
        data = intent.to_dict()
        data["quantity"] = _text(intent.quantity)
        data["limit_price"] = _text(intent.limit_price)
        return _json(data)

    def _prior_order(self, intent: OrderIntent) -> OrderSnapshot | None:
        expected = self._intent_json(intent)
        row = self.memory.connection.execute(
            "SELECT account_id, intent_json, snapshot_json FROM simulation_orders WHERE client_order_id = ?",
            (intent.client_order_id,),
        ).fetchone()
        if row is None:
            return None
        if row["account_id"] != self.account_id or row["intent_json"] != expected:
            raise OrderRejected("client_order_id belongs to a different simulation account or intent")
        return OrderSnapshot(**json.loads(row["snapshot_json"]))

    def place_order(self, intent: OrderIntent) -> OrderSnapshot:
        encoded_intent = self._intent_json(intent)
        with self.memory.transaction():
            previous = self._prior_order(intent)
            if previous is not None:
                return previous
            rules = self.get_rules(intent.symbol)
            try:
                rules.validate(intent)
            except ValueError as exc:
                raise OrderRejected(str(exc)) from exc
            balances = self.get_balances()
            with localcontext() as context:
                context.prec = 100
                amount = notional(intent)
                fee = amount * self.fee
                base = balances.get(rules.base_asset, Decimal("0"))
                quote = balances.get(rules.quote_asset, Decimal("0"))
                if intent.side == "BUY":
                    required = amount + fee
                    if quote < required:
                        raise OrderRejected(f"Insufficient {rules.quote_asset} balance including simulation fee")
                    balances[rules.quote_asset] = quote - required
                    balances[rules.base_asset] = base + intent.quantity
                else:
                    if base < intent.quantity:
                        raise OrderRejected(f"Insufficient {rules.base_asset} balance for a simulated spot sale")
                    balances[rules.base_asset] = base - intent.quantity
                    balances[rules.quote_asset] = quote + amount - fee
            snapshot = OrderSnapshot(
                **intent.to_dict(), status="FILLED", executed_quantity=intent.quantity,
                cumulative_quote_quantity=Decimal(_text(amount)),
                exchange_order_id="sim-" + intent.client_order_id,
                updated_at_ms=int(time.time() * 1000),
            )
            self.memory.connection.execute(
                "UPDATE simulation_wallets SET balances_json = ? WHERE account_id = ?",
                (_json({asset: _text(value) for asset, value in balances.items()}), self.account_id),
            )
            self.memory.connection.execute(
                "INSERT INTO simulation_orders VALUES (?, ?, ?, ?)",
                (intent.client_order_id, self.account_id, encoded_intent, _json(snapshot.to_dict())),
            )
            return snapshot

    def get_order(self, intent: OrderIntent, *, exchange_order_id: str | None = None) -> OrderSnapshot:
        snapshot = self._prior_order(intent)
        if snapshot is None:
            raise OrderNotFound("Simulation order was not found")
        if exchange_order_id is not None and exchange_order_id != snapshot.exchange_order_id:
            raise OrderNotFound("Simulation exchange order ID does not match the stored order")
        return snapshot

    def cancel_order(self, intent: OrderIntent, *, exchange_order_id: str | None = None) -> OrderSnapshot:
        # This simulator has no resting orders. FILLED is immutable; a cancel
        # request neither refunds funds nor invents a cancellation fill.
        return self.get_order(intent, exchange_order_id=exchange_order_id)
