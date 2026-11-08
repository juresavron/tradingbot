"""At-most-once submission attempts with durable exchange reconciliation.

HTTP calls run outside SQLite transactions. A committed SUBMITTING marker is
never converted back to PREPARED, even when an order lookup reports not found.
Client IDs can be reused by exchanges after fills; blindly resending is unsafe.
"""

from __future__ import annotations

import hashlib
import time

from .orders import BrokerUnavailable, OrderIntent, OrderNotFound, OrderRejected, OrderSnapshot, TERMINAL_STATUSES


class ExecutionEngine:
    def __init__(self, store, broker):
        if broker.environment != store.environment or broker.scope != store.scope:
            raise ValueError("Broker identity does not match the persisted execution account")
        self.store, self.broker = store, broker

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def _get(self, client_order_id):
        record = self.store.get(client_order_id)
        if record is None:
            raise ValueError("Order does not exist in this execution account")
        return record

    def prepare(self, *, request_id: str, symbol: str, side: str, quantity, limit_price) -> dict:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("request_id must contain 1-200 characters")
        material = f"{self.store.account_id}\0{self.store.scope}\0{request_id}"
        client_id = "tb-" + hashlib.sha256(material.encode()).hexdigest()[:32]
        intent = OrderIntent(client_id, symbol, side, quantity, limit_price)
        existing = self.store.get_by_request(request_id)
        if existing is not None:
            if OrderIntent(**existing["intent"]) != intent:
                raise ValueError("Request ID already belongs to different immutable order parameters")
            return existing
        rules = self.broker.get_rules(intent.symbol)
        self.store.limits.validate(intent, rules, self.broker.get_balances())
        with self.store.memory.transaction():
            return self.store.prepare(intent, rules, request_id, self._now())

    def _unknown(self, client_id: str, reason: str) -> dict:
        with self.store.memory.transaction():
            return self.store.set_state(client_id, "UNKNOWN", self._now(), error=reason)

    def _apply(self, client_id: str, snapshot) -> dict:
        if not isinstance(snapshot, OrderSnapshot) or snapshot.client_order_id != client_id:
            return self._unknown(client_id, "inconsistent_exchange_response")
        try:
            with self.store.memory.transaction():
                return self.store.apply_snapshot(snapshot, self._now())
        except ValueError:
            return self._unknown(client_id, "inconsistent_exchange_response")

    def submit(self, client_order_id: str, *, authorize=None) -> dict:
        """Submit an unsent intent after optional local authorization.

        ``authorize`` receives the immutable OrderIntent after broker
        preflight, inside the short transaction that claims submission. It
        may read local state but must perform no network or external I/O.
        Denial cancels only a still-PREPARED intent;
        submitted or uncertain orders retain reconciliation-only semantics.
        An authorization exception propagates and rolls back the transaction.
        """
        record = self._get(client_order_id)
        if record["status"] != "PREPARED":
            return self.reconcile(client_order_id)
        intent = OrderIntent(**record["intent"])
        # Recheck filters and free balances immediately before claiming the
        # submit. A second worker may have claimed it during these GET calls.
        try:
            rules = self.broker.get_rules(intent.symbol)
            if rules.to_dict() != record["rules"]:
                raise ValueError("Symbol rules changed after preparation")
            self.store.limits.validate(intent, rules, self.broker.get_balances())
        except (ValueError, OrderRejected):
            with self.store.memory.transaction():
                current = self._get(client_order_id)
                if current["status"] == "PREPARED":
                    return self.store.set_state(client_order_id, "REJECTED", self._now(), error="preflight_failed")
            return self.reconcile(client_order_id)
        with self.store.memory.transaction():
            current = self._get(client_order_id)
            if current["status"] != "PREPARED":
                claimed = False
            elif authorize is not None and not authorize(intent):
                return self.store.set_state(client_order_id, "CANCELED", self._now(),
                                            error="submission_not_authorized")
            else:
                claimed = self.store.claim_submit(client_order_id, self._now())
        if not claimed:
            return self.reconcile(client_order_id)
        try:
            snapshot = self.broker.place_order(intent)
        except OrderRejected:
            with self.store.memory.transaction():
                current = self._get(client_order_id)
                # An independently acknowledged exchange order takes priority
                # over a contradictory rejection response.
                status = "UNKNOWN" if current["snapshot"] is not None else "REJECTED"
                return self.store.set_state(client_order_id, status, self._now(), error="broker_rejected")
        except (BrokerUnavailable, ValueError):
            return self._unknown(client_order_id, "submission_outcome_unknown")
        return self._apply(client_order_id, snapshot)

    def reconcile(self, client_order_id: str) -> dict:
        record = self._get(client_order_id)
        if record["status"] == "PREPARED" or record["status"] in TERMINAL_STATUSES:
            return record
        try:
            options = ({"exchange_order_id": record["snapshot"]["exchange_order_id"]}
                       if record["snapshot"] is not None else {})
            snapshot = self.broker.get_order(OrderIntent(**record["intent"]), **options)
        except OrderNotFound:
            return self._unknown(client_order_id, "lookup_not_found_requires_review")
        except (BrokerUnavailable, ValueError):
            return self._unknown(client_order_id, "reconciliation_unavailable")
        return self._apply(client_order_id, snapshot)

    def cancel(self, client_order_id: str) -> dict:
        record = self._get(client_order_id)
        if record["status"] in TERMINAL_STATUSES:
            return record
        if record["status"] == "PREPARED":
            with self.store.memory.transaction():
                # Never locally cancel an intent another worker has submitted.
                if self._get(client_order_id)["status"] == "PREPARED":
                    return self.store.set_state(client_order_id, "CANCELED", self._now())
            return self.reconcile(client_order_id)
        if record["status"] not in ("NEW", "PARTIALLY_FILLED") or record["cancel_requested"]:
            record = self.reconcile(client_order_id)
        if record["status"] not in ("NEW", "PARTIALLY_FILLED") or record["cancel_requested"]:
            return record
        with self.store.memory.transaction():
            claimed = self.store.claim_cancel(client_order_id, self._now())
        if not claimed:
            return self.reconcile(client_order_id)
        try:
            options = ({"exchange_order_id": record["snapshot"]["exchange_order_id"]}
                       if record["snapshot"] is not None else {})
            snapshot = self.broker.cancel_order(OrderIntent(**record["intent"]), **options)
        except (BrokerUnavailable, OrderRejected, ValueError):
            return self._unknown(client_order_id, "cancellation_outcome_unknown")
        return self._apply(client_order_id, snapshot)
