import tempfile
import unittest
from pathlib import Path

from tradingbot.memory import TradingMemory
from tradingbot.order_store import ExecutionStore
from tradingbot.orders import OrderIntent, OrderSnapshot
from tradingbot.risk import RiskLimits, SymbolRules


def intent(order_id="order-1", side="BUY", quantity="1", price="100"):
    return OrderIntent(order_id, "BTC/EUR", side, quantity, price)


def rules():
    return SymbolRules("BTCEUR", "BTC", "EUR", "0.1", "10", "0.1", "1", "1000", "0.01", "10")


def snapshot(order=None, *, status="NEW", executed="0", quote="0", updated=1000, exchange_id="broker-1"):
    order = order or intent()
    return OrderSnapshot(
        order.client_order_id, order.symbol, order.side, order.quantity, order.limit_price,
        status, executed, quote, exchange_id, updated,
    )


def store(memory, **overrides):
    options = dict(account_id="test", environment="simulation", scope="wallet-1", limits=RiskLimits())
    options.update(overrides)
    return ExecutionStore(memory, **options)


class TestExecutionStore(unittest.TestCase):
    def test_restart_freezes_account_scope_environment_and_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.sqlite3"
            with TradingMemory(path) as memory:
                orders = store(memory)
                with memory.transaction():
                    orders.prepare(intent(), rules(), "request-1", 10)
                    orders.claim_submit("order-1", 11)
            with TradingMemory(path) as memory:
                orders = store(memory, limits=RiskLimits("300.00", "0.0020"))
                self.assertEqual(orders.get("order-1")["status"], "SUBMITTING")
                self.assertEqual(len(orders.events("order-1")), 2)
                for change in (
                    {"scope": "other-wallet"}, {"environment": "binance-spot-testnet"},
                    {"limits": RiskLimits("301")}, {"limits": RiskLimits("300", "0.003")},
                ):
                    with self.subTest(change=change), self.assertRaisesRegex(ValueError, "immutable"):
                        store(memory, **change)
                with self.assertRaisesRegex(ValueError, "scope already belongs"):
                    store(memory, account_id="other")
                store(memory, account_id="testnet", environment="binance-spot-testnet")

    def test_prepare_is_idempotent_and_rejects_conflicting_or_unresolved_orders(self):
        with TradingMemory(":memory:") as memory:
            orders = store(memory)
            with memory.transaction():
                first = orders.prepare(intent(), rules(), "request-1", 10)
                again = orders.prepare(intent(quantity="1.00"), rules(), "request-1", 11)
                self.assertEqual(again, first)
                with self.assertRaisesRegex(ValueError, "different immutable"):
                    orders.prepare(intent(price="101"), rules(), "request-1", 12)
                with self.assertRaisesRegex(ValueError, "already in use"):
                    orders.prepare(intent(), rules(), "different-request", 12)
                with self.assertRaisesRegex(ValueError, "unresolved"):
                    orders.prepare(intent("order-2"), rules(), "request-2", 12)
                orders.set_state("order-1", "UNKNOWN", 12, error="SUBMIT_TIMEOUT")
                with self.assertRaisesRegex(ValueError, "unresolved"):
                    orders.prepare(intent("order-2"), rules(), "request-2", 13)
                orders.set_state("order-1", "REJECTED", 13)
                orders.prepare(intent("order-2"), rules(), "request-2", 14)
            self.assertEqual(len(orders.list_orders()), 2)
            self.assertEqual(len([e for e in orders.events("order-1") if e["status"] == "PREPARED"]), 1)

    def test_submit_and_cancel_claims_happen_only_once_and_cancel_preserves_partial_fills(self):
        with TradingMemory(":memory:") as memory:
            orders = store(memory)
            with memory.transaction():
                orders.prepare(intent(), rules(), "request-1", 10)
                self.assertTrue(orders.claim_submit("order-1", 11))
                self.assertFalse(orders.claim_submit("order-1", 12))
                orders.apply_snapshot(snapshot(), 13)
                self.assertTrue(orders.claim_cancel("order-1", 14))
                self.assertFalse(orders.claim_cancel("order-1", 15))
                partial = orders.apply_snapshot(snapshot(status="PARTIALLY_FILLED", executed="0.4", quote="39.6", updated=1001), 16)
                self.assertEqual(partial["status"], "CANCELING")
                self.assertTrue(partial["cancel_requested"])
                canceled = orders.apply_snapshot(snapshot(status="CANCELED", executed="0.4", quote="39.6", updated=1002), 17)
                self.assertEqual(canceled["status"], "CANCELED")
                self.assertEqual(canceled["snapshot"]["executed_quantity"], "0.4")
                self.assertFalse(orders.claim_cancel("order-1", 18))
                self.assertFalse(orders.claim_submit("order-1", 18))
            events = orders.events("order-1")
            self.assertEqual(sum(e["status"] == "SUBMITTING" for e in events), 1)
            self.assertEqual(sum(e["detail"].get("cancel_requested", False) for e in events), 1)

    def test_snapshot_immutable_fields_and_execution_totals_are_checked(self):
        with TradingMemory(":memory:") as memory:
            orders = store(memory)
            with memory.transaction():
                orders.prepare(intent(), rules(), "request-1", 10)
                orders.apply_snapshot(snapshot(status="PARTIALLY_FILLED", executed="0.4", quote="39.6"), 11)
                for incoming, message in (
                    (snapshot(intent(price="101")), "immutable"),
                    (snapshot(exchange_id="other"), "order ID"),
                    (snapshot(status="PARTIALLY_FILLED", executed="0.3", quote="29.7", updated=1001), "cannot decrease"),
                    (snapshot(status="PARTIALLY_FILLED", executed="0.5", quote="39.5", updated=1001), "cannot decrease"),
                    (snapshot(status="PARTIALLY_FILLED", executed="0.5", quote="50.01", updated=1001), "limit price"),
                ):
                    with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                        orders.apply_snapshot(incoming, 12)
                # Same-millisecond fills are real: monotone increments are valid.
                increased = orders.apply_snapshot(snapshot(status="PARTIALLY_FILLED", executed="0.5", quote="49.5"), 12)
                self.assertEqual(increased["snapshot"]["executed_quantity"], "0.5")

    def test_stale_and_exact_duplicate_snapshots_do_not_change_the_journal(self):
        with TradingMemory(":memory:") as memory:
            orders = store(memory)
            with memory.transaction():
                orders.prepare(intent(), rules(), "request-1", 10)
                current = snapshot(status="PARTIALLY_FILLED", executed="0.5", quote="49.5", updated=1002)
                original = orders.apply_snapshot(current, 11)
                count = len(orders.events("order-1"))
                self.assertEqual(orders.apply_snapshot(current, 12), original)
                self.assertEqual(orders.apply_snapshot(snapshot(updated=1001), 12), original)
                self.assertEqual(orders.apply_snapshot(snapshot(updated=1002), 12), original)
                self.assertEqual(len(orders.events("order-1")), count)
                # A confirmed identical broker snapshot resolves a local timeout.
                orders.set_state("order-1", "UNKNOWN", 13, error="LOOKUP_TIMEOUT")
                restored = orders.apply_snapshot(current, 14)
                self.assertEqual(restored["status"], "PARTIALLY_FILLED")
                self.assertIsNone(restored["last_error"])

    def test_terminal_orders_never_regress_or_change_terminal_result(self):
        with TradingMemory(":memory:") as memory:
            orders = store(memory)
            with memory.transaction():
                orders.prepare(intent(), rules(), "request-1", 10)
                terminal = orders.apply_snapshot(snapshot(status="CANCELED", executed="0.4", quote="39.6"), 11)
                self.assertEqual(orders.set_state("order-1", "UNKNOWN", 12), terminal)
                for incoming in (
                    snapshot(status="PARTIALLY_FILLED", executed="0.4", quote="39.6", updated=1001),
                    snapshot(status="FILLED", executed="1", quote="99", updated=1001),
                    snapshot(status="CANCELED", executed="0.5", quote="49.5", updated=1001),
                    snapshot(status="REJECTED", updated=1000),
                ):
                    with self.subTest(status=incoming.status), self.assertRaisesRegex(ValueError, "terminal"):
                        orders.apply_snapshot(incoming, 12)
                with self.assertRaisesRegex(ValueError, "terminal"):
                    orders.set_state("order-1", "REJECTED", 12)

    def test_sell_limit_protects_cumulative_proceeds_and_account_scope_isolated(self):
        with TradingMemory(":memory:") as memory:
            orders, other = store(memory), store(memory, account_id="other", scope="wallet-2")
            sell = intent(side="SELL")
            with memory.transaction():
                orders.prepare(sell, rules(), "request-1", 10)
                with self.assertRaisesRegex(ValueError, "limit price"):
                    orders.apply_snapshot(snapshot(sell, status="PARTIALLY_FILLED", executed="0.5", quote="49.99"), 11)
                orders.apply_snapshot(snapshot(sell, status="PARTIALLY_FILLED", executed="0.5", quote="50.01"), 11)
                with self.assertRaisesRegex(ValueError, "already in use"):
                    other.prepare(sell, rules(), "request-1", 12)
            self.assertIsNone(other.get("order-1"))
            self.assertIsNone(other.get_by_request("request-1"))
            self.assertEqual(other.events("order-1"), [])

    def test_only_unsent_intents_can_be_locally_canceled(self):
        with TradingMemory(":memory:") as memory:
            orders = store(memory)
            with memory.transaction():
                orders.prepare(intent(), rules(), "request-1", 10)
                canceled = orders.set_state("order-1", "CANCELED", 11)
                self.assertEqual(canceled["status"], "CANCELED")
                second = intent("order-2")
                orders.prepare(second, rules(), "request-2", 12)
                orders.claim_submit("order-2", 13)
                with self.assertRaisesRegex(ValueError, "unsent"):
                    orders.set_state("order-2", "CANCELED", 14)
                orders.apply_snapshot(snapshot(second), 14)
                with self.assertRaisesRegex(ValueError, "unsent"):
                    orders.set_state("order-2", "CANCELED", 15)
                with self.assertRaisesRegex(ValueError, "accepted"):
                    orders.set_state("order-2", "REJECTED", 15)
                self.assertEqual(orders.get("order-2")["status"], "NEW")

    def test_transactions_rollback_intent_and_event_and_are_required_for_mutations(self):
        with TradingMemory(":memory:") as memory:
            orders = store(memory)
            with self.assertRaisesRegex(ValueError, "explicit transaction"):
                orders.prepare(intent(), rules(), "request-1", 10)
            with self.assertRaisesRegex(RuntimeError, "crash"):
                with memory.transaction():
                    orders.prepare(intent(), rules(), "request-1", 10)
                    orders.claim_submit("order-1", 11)
                    raise RuntimeError("crash")
            self.assertIsNone(orders.get("order-1"))
            self.assertEqual(orders.events("order-1"), [])
            with memory.transaction():
                with self.assertRaisesRegex(ValueError, "risk limit"):
                    orders.prepare(intent(quantity="4"), rules(), "request-1", 10)


if __name__ == "__main__":
    unittest.main()
