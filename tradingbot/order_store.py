"""Transactional order intents and aggregate broker state.

Persist a submit/cancel claim before performing network I/O. An unresolved
order retains its account's reservation, including when broker state is
unknown. This journal stores aggregate execution totals, not individual
exchange trades, commissions, or an independently reconciled cash ledger.
"""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from decimal import Decimal, localcontext

from .memory import TradingMemory
from .orders import OrderIntent, OrderSnapshot, TERMINAL_STATUSES, decimal_text
from .risk import RiskLimits, SymbolRules, notional


class ExecutionStore:
    def __init__(self, memory: TradingMemory, *, account_id: str,
                 environment: str, scope: str, limits: RiskLimits):
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", account_id):
            raise ValueError("Execution account ID must contain 1-100 letters, digits, underscores or hyphens")
        if environment not in ("simulation", "binance-spot-testnet"):
            raise ValueError("Execution environment must be simulation or binance-spot-testnet")
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 200:
            raise ValueError("Execution scope must be a nonempty string of at most 200 characters")
        if not isinstance(limits, RiskLimits):
            raise ValueError("Execution limits must be RiskLimits")
        self.memory = memory
        self.account_id = account_id
        self.environment = environment
        self.scope = scope
        self.limits = limits
        limits_json = self._json({
            name: self._decimal_key(value) for name, value in vars(limits).items()
        })
        # Provisioning may participate in a larger atomic account/revision
        # transaction without weakening TradingMemory's nested-write guard.
        with (nullcontext() if self.memory.connection.in_transaction else self.memory.transaction()):
            # Do not use executescript: it implicitly commits an open SQLite
            # transaction, breaking initialization's all-or-nothing contract.
            for statement in (
                """CREATE TABLE IF NOT EXISTS execution_accounts (
                    account_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    limits_json TEXT NOT NULL,
                    UNIQUE(environment, scope)
                )""",
                """CREATE TABLE IF NOT EXISTS execution_orders (
                    client_order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    last_error TEXT,
                    UNIQUE(account_id, request_id)
                )""",
                """CREATE INDEX IF NOT EXISTS execution_orders_account
                   ON execution_orders(account_id, created_at_ms)""",
                """CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                )""",
            ):
                self.memory.connection.execute(statement)
            row = self.memory.connection.execute(
                "SELECT * FROM execution_accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is not None:
                if (row["environment"], row["scope"], row["limits_json"]) != (
                    environment, scope, limits_json,
                ):
                    raise ValueError("Execution account environment, scope, and risk limits are immutable")
            else:
                bound = self.memory.connection.execute(
                    "SELECT account_id FROM execution_accounts WHERE environment = ? AND scope = ?",
                    (environment, scope),
                ).fetchone()
                if bound is not None:
                    raise ValueError("This broker scope already belongs to another local execution account")
                self.memory.connection.execute(
                    "INSERT INTO execution_accounts VALUES (?, ?, ?, ?)",
                    (account_id, environment, scope, limits_json),
                )

    @staticmethod
    def _json(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _decimal_key(value: Decimal) -> str:
        text = decimal_text(value)
        return text.rstrip("0").rstrip(".") if "." in text else text

    @staticmethod
    def _at(at_ms: int) -> int:
        if type(at_ms) is not int or at_ms <= 0:
            raise ValueError("Order observation time must be positive integer milliseconds")
        return at_ms

    @staticmethod
    def _request(request_id: str) -> str:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("request_id must be a nonempty string of at most 200 characters")
        return request_id

    def _write(self) -> None:
        self.memory._require_transaction()

    def _decode(self, row) -> dict | None:
        if row is None:
            return None
        order = dict(row)
        for key in ("intent", "rules", "snapshot"):
            raw = order.pop(key + "_json")
            order[key] = None if raw is None else json.loads(raw)
        order["cancel_requested"] = bool(order["cancel_requested"])
        return order

    def get(self, client_order_id: str) -> dict | None:
        row = self.memory.connection.execute(
            "SELECT * FROM execution_orders WHERE account_id = ? AND client_order_id = ?",
            (self.account_id, client_order_id),
        ).fetchone()
        return self._decode(row)

    def get_by_request(self, request_id: str) -> dict | None:
        self._request(request_id)
        row = self.memory.connection.execute(
            "SELECT * FROM execution_orders WHERE account_id = ? AND request_id = ?",
            (self.account_id, request_id),
        ).fetchone()
        return self._decode(row)

    def list_orders(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Order limit must be an integer in [1, 1000]")
        return [self._decode(row) for row in self.memory.connection.execute(
            """SELECT * FROM execution_orders WHERE account_id = ?
               ORDER BY created_at_ms DESC, client_order_id ASC LIMIT ?""",
            (self.account_id, limit),
        )]

    def _order(self, client_order_id: str, at_ms: int) -> dict:
        self._at(at_ms)
        order = self.get(client_order_id)
        if order is None:
            raise ValueError("Order is not present in this execution account")
        if at_ms < order["updated_at_ms"]:
            raise ValueError("Local order observation time cannot move backwards")
        return order

    def _event(self, client_order_id: str, at_ms: int, status: str, detail: dict) -> None:
        self.memory.connection.execute(
            "INSERT INTO execution_events(client_order_id, at_ms, status, detail_json) VALUES (?, ?, ?, ?)",
            (client_order_id, at_ms, status, self._json(detail)),
        )

    def prepare(self, intent: OrderIntent, rules: SymbolRules,
                request_id: str, at_ms: int) -> dict:
        self._write()
        self._at(at_ms)
        self._request(request_id)
        if not isinstance(intent, OrderIntent) or not isinstance(rules, SymbolRules):
            raise ValueError("An OrderIntent and SymbolRules are required")
        prior = self.get_by_request(request_id)
        if prior is not None:
            if OrderIntent(**prior["intent"]) != intent:
                raise ValueError("request_id already identifies a different immutable order intent")
            return prior
        if self.memory.connection.execute(
            "SELECT 1 FROM execution_orders WHERE client_order_id = ?", (intent.client_order_id,)
        ).fetchone() is not None:
            raise ValueError("client_order_id is already in use")
        terminal = tuple(sorted(TERMINAL_STATUSES))
        placeholders = ",".join("?" for _ in terminal)
        if self.memory.connection.execute(
            f"SELECT 1 FROM execution_orders WHERE account_id = ? AND status NOT IN ({placeholders}) LIMIT 1",
            (self.account_id, *terminal),
        ).fetchone() is not None:
            raise ValueError("An unresolved order already reserves this execution account")
        rules.validate(intent)
        if notional(intent) > self.limits.max_order_notional:
            raise ValueError("Order notional exceeds max_order_notional risk limit")
        self.memory.connection.execute(
            """INSERT INTO execution_orders(
                client_order_id, account_id, request_id, intent_json, rules_json,
                status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, 'PREPARED', ?, ?)""",
            (intent.client_order_id, self.account_id, request_id,
             self._json(intent.to_dict()), self._json(rules.to_dict()), at_ms, at_ms),
        )
        self._event(intent.client_order_id, at_ms, "PREPARED", {"request_id": request_id})
        return self.get(intent.client_order_id)

    def claim_submit(self, client_order_id: str, at_ms: int) -> bool:
        self._write()
        order = self._order(client_order_id, at_ms)
        if order["status"] != "PREPARED":
            return False
        cursor = self.memory.connection.execute(
            """UPDATE execution_orders SET status = 'SUBMITTING', updated_at_ms = ?, last_error = NULL
               WHERE account_id = ? AND client_order_id = ? AND status = 'PREPARED'""",
            (at_ms, self.account_id, client_order_id),
        )
        if cursor.rowcount:
            self._event(client_order_id, at_ms, "SUBMITTING", {})
        return cursor.rowcount == 1

    def claim_cancel(self, client_order_id: str, at_ms: int) -> bool:
        self._write()
        order = self._order(client_order_id, at_ms)
        if order["status"] not in ("NEW", "PARTIALLY_FILLED") or order["cancel_requested"]:
            return False
        cursor = self.memory.connection.execute(
            """UPDATE execution_orders SET status = 'CANCELING', cancel_requested = 1,
                       updated_at_ms = ?, last_error = NULL
               WHERE account_id = ? AND client_order_id = ?
                     AND status IN ('NEW', 'PARTIALLY_FILLED') AND cancel_requested = 0""",
            (at_ms, self.account_id, client_order_id),
        )
        if cursor.rowcount:
            self._event(client_order_id, at_ms, "CANCELING", {"cancel_requested": True})
        return cursor.rowcount == 1

    def set_state(self, client_order_id: str, status: str,
                  at_ms: int, error: str | None = None) -> dict:
        self._write()
        if status not in ("UNKNOWN", "REJECTED", "CANCELED"):
            raise ValueError("Local order state must be UNKNOWN, REJECTED, or CANCELED")
        if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", error)):
            raise ValueError("Order errors must be generic codes without response content")
        order = self._order(client_order_id, at_ms)
        if order["status"] in TERMINAL_STATUSES:
            if status in TERMINAL_STATUSES and status != order["status"]:
                raise ValueError("A terminal order cannot change its terminal state")
            return order
        if status == "REJECTED" and order["snapshot"] is not None:
            raise ValueError("An accepted broker order cannot be locally rejected")
        if status == "CANCELED" and order["status"] != "PREPARED":
            raise ValueError("Only an unsent PREPARED order can be locally canceled")
        if order["status"] == status and order["last_error"] == error:
            return order
        self.memory.connection.execute(
            "UPDATE execution_orders SET status = ?, updated_at_ms = ?, last_error = ? WHERE client_order_id = ?",
            (status, at_ms, error, client_order_id),
        )
        self._event(client_order_id, at_ms, status, {} if error is None else {"error": error})
        return self.get(client_order_id)

    def apply_snapshot(self, snapshot: OrderSnapshot, at_ms: int) -> dict:
        self._write()
        if not isinstance(snapshot, OrderSnapshot):
            raise ValueError("An OrderSnapshot is required")
        order = self._order(snapshot.client_order_id, at_ms)
        intent = OrderIntent(**order["intent"])
        received_intent = OrderIntent(snapshot.client_order_id, snapshot.symbol, snapshot.side,
                                      snapshot.quantity, snapshot.limit_price)
        if received_intent != intent:
            raise ValueError("Broker snapshot does not match the immutable order intent")
        with localcontext() as context:
            context.prec = 100
            bound = snapshot.executed_quantity * intent.limit_price
            if (intent.side == "BUY" and snapshot.cumulative_quote_quantity > bound
                    or intent.side == "SELL" and snapshot.cumulative_quote_quantity < bound):
                raise ValueError("Broker cumulative quote violates the limit price")
        if snapshot.status == "REJECTED" and snapshot.executed_quantity:
            raise ValueError("A rejected order cannot have executions")
        previous = OrderSnapshot(**order["snapshot"]) if order["snapshot"] is not None else None
        if previous is not None:
            if snapshot.exchange_order_id != previous.exchange_order_id:
                raise ValueError("Broker exchange order ID cannot change")
            if snapshot.updated_at_ms < previous.updated_at_ms:
                return order
            if (order["status"] in TERMINAL_STATUSES and snapshot.status in TERMINAL_STATUSES
                    and snapshot.status != order["status"]):
                raise ValueError("A terminal order cannot change terminal state")
            if (snapshot.executed_quantity < previous.executed_quantity
                    or snapshot.cumulative_quote_quantity < previous.cumulative_quote_quantity):
                if snapshot.updated_at_ms == previous.updated_at_ms:
                    return order
                raise ValueError("Broker cumulative execution totals cannot decrease")
        if order["status"] in TERMINAL_STATUSES:
            if snapshot.status != order["status"]:
                raise ValueError("A terminal order cannot regress or change terminal state")
            if previous is not None and (
                snapshot.executed_quantity != previous.executed_quantity
                or snapshot.cumulative_quote_quantity != previous.cumulative_quote_quantity
            ):
                raise ValueError("A terminal order cannot change execution totals")
        effective_status = snapshot.status
        if order["cancel_requested"] and snapshot.status not in TERMINAL_STATUSES:
            effective_status = "CANCELING"
        if (previous is not None and snapshot.to_dict() == previous.to_dict()
                and order["status"] == effective_status and order["last_error"] is None):
            return order
        self.memory.connection.execute(
            """UPDATE execution_orders SET status = ?, snapshot_json = ?, updated_at_ms = ?, last_error = NULL
               WHERE client_order_id = ?""",
            (effective_status, self._json(snapshot.to_dict()), at_ms, snapshot.client_order_id),
        )
        self._event(snapshot.client_order_id, at_ms, effective_status, {"snapshot": snapshot.to_dict()})
        return self.get(snapshot.client_order_id)

    def events(self, client_order_id: str) -> list[dict]:
        if self.get(client_order_id) is None:
            return []
        result = []
        for row in self.memory.connection.execute(
            "SELECT * FROM execution_events WHERE client_order_id = ? ORDER BY id ASC", (client_order_id,)
        ):
            event = dict(row)
            event["detail"] = json.loads(event.pop("detail_json"))
            result.append(event)
        return result
