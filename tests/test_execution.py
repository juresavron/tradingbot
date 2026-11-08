import contextlib
import io
import json
import tempfile
import unittest
from decimal import Decimal
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from tradingbot.cli import main
from tradingbot.execution import ExecutionEngine
from tradingbot.memory import TradingMemory
from tradingbot.order_store import ExecutionStore
from tradingbot.orders import BrokerUnavailable, OrderNotFound, OrderRejected, OrderSnapshot
from tradingbot.risk import RiskLimits
from tradingbot.simulated_broker import SimulatedBroker


def setup(memory, broker=None):
    broker = broker or SimulatedBroker(memory, account_id="demo", initial_balances={"EUR": "300"})
    store = ExecutionStore(memory, account_id="demo", environment=broker.environment,
                           scope=broker.scope, limits=RiskLimits())
    return ExecutionEngine(store, broker), store, broker


def prepare(engine, request="buy-1"):
    return engine.prepare(request_id=request, symbol="BTC/EUR", side="BUY",
                          quantity="0.001", limit_price="60000")


def snapshot(intent, status="NEW", executed="0", quote="0", stamp=1000):
    return OrderSnapshot(**intent.to_dict(), status=status, executed_quantity=executed,
                         cumulative_quote_quantity=quote, exchange_order_id="123", updated_at_ms=stamp)


class TestExecutionEngine(unittest.TestCase):
    def test_local_submission_denial_runs_after_preflight_and_never_posts(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            preflight = []
            original_balances = broker.get_balances

            def balances():
                self.assertFalse(memory.connection.in_transaction)
                preflight.append(True)
                return original_balances()

            def deny(intent):
                self.assertEqual(len(preflight), 1)
                self.assertTrue(memory.connection.in_transaction)
                self.assertEqual(intent.client_order_id, row["client_order_id"])
                with self.assertRaises(FrozenInstanceError):
                    intent.side = "SELL"
                return False

            with mock.patch.object(broker, "get_balances", side_effect=balances), \
                    mock.patch.object(broker, "place_order", side_effect=AssertionError("Denied orders cannot post")):
                result = engine.submit(row["client_order_id"], authorize=deny)
            self.assertEqual(result["status"], "CANCELED")
            self.assertEqual(result["last_error"], "submission_not_authorized")
            self.assertEqual(broker.get_balances()["EUR"], Decimal("300"))
            self.assertEqual([event["status"] for event in store.events(row["client_order_id"])],
                             ["PREPARED", "CANCELED"])

    def test_local_submission_permission_claims_before_posting_outside_transaction(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            original_place = broker.place_order
            observed = []

            def allow(intent):
                self.assertTrue(memory.connection.in_transaction)
                observed.append(intent)
                return True

            def place(intent):
                self.assertFalse(memory.connection.in_transaction)
                self.assertEqual(store.get(intent.client_order_id)["status"], "SUBMITTING")
                return original_place(intent)

            with mock.patch.object(broker, "place_order", side_effect=place):
                result = engine.submit(row["client_order_id"], authorize=allow)
            self.assertEqual(result["status"], "FILLED")
            self.assertEqual(len(observed), 1)

    def test_already_claimed_during_preflight_ignores_guard_and_never_cancels_or_resends(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            original_balances = broker.get_balances

            def competing_claim():
                balances = original_balances()
                with memory.transaction():
                    store.claim_submit(row["client_order_id"], engine._now())
                return balances

            guard = mock.Mock(side_effect=AssertionError("A claimed order cannot be reauthorized"))
            with mock.patch.object(broker, "get_balances", side_effect=competing_claim), \
                    mock.patch.object(broker, "place_order", side_effect=AssertionError("A claimed order cannot be resent")):
                result = engine.submit(row["client_order_id"], authorize=guard)
            self.assertEqual(result["status"], "UNKNOWN")
            guard.assert_not_called()
            self.assertNotIn("CANCELED", [event["status"] for event in store.events(row["client_order_id"])])
            with mock.patch.object(broker, "place_order", side_effect=AssertionError("Unknown orders cannot be resent")):
                self.assertEqual(engine.submit(row["client_order_id"], authorize=guard)["status"], "UNKNOWN")
            guard.assert_not_called()

    def test_local_submission_guard_exception_leaves_intent_unclaimed(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)

            def broken_guard(intent):
                self.assertTrue(memory.connection.in_transaction)
                raise RuntimeError("local authorization could not be established")

            with mock.patch.object(broker, "place_order", side_effect=AssertionError("Failed guards cannot post")), \
                    mock.patch.object(store, "claim_submit", wraps=store.claim_submit) as claim:
                with self.assertRaisesRegex(RuntimeError, "authorization could not be established"):
                    engine.submit(row["client_order_id"], authorize=broken_guard)
                claim.assert_not_called()
            self.assertFalse(memory.connection.in_transaction)
            self.assertEqual(store.get(row["client_order_id"])["status"], "PREPARED")
            self.assertEqual(len(store.events(row["client_order_id"])), 1)
            self.assertEqual(broker.get_balances()["EUR"], Decimal("300"))

    def test_simulation_persists_intent_fills_once_and_reuses_request_without_network(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            self.assertEqual(row["status"], "PREPARED")
            self.assertEqual(broker.get_balances()["EUR"], Decimal("300"))
            result = engine.submit(row["client_order_id"])
            self.assertEqual(result["status"], "FILLED")
            self.assertEqual(broker.get_balances()["EUR"], Decimal("239.940000"))
            self.assertEqual(broker.get_balances()["BTC"], Decimal("0.001"))
            with mock.patch.object(broker, "get_rules", side_effect=AssertionError("No broker request expected")):
                self.assertEqual(prepare(engine), result)
                self.assertEqual(engine.submit(row["client_order_id"]), result)
            self.assertEqual([event["status"] for event in store.events(row["client_order_id"])],
                             ["PREPARED", "SUBMITTING", "FILLED"])

    def test_conflicting_request_and_aggregate_unresolved_commitment_are_rejected(self):
        with TradingMemory(":memory:") as memory:
            engine, _, _ = setup(memory)
            row = prepare(engine)
            with self.assertRaisesRegex(ValueError, "immutable"):
                engine.prepare(request_id="buy-1", symbol="BTC/EUR", side="BUY",
                               quantity="0.002", limit_price="60000")
            with self.assertRaisesRegex(ValueError, "unresolved"):
                prepare(engine, "buy-2")
            self.assertEqual(engine.cancel(row["client_order_id"])["status"], "CANCELED")
            self.assertEqual(prepare(engine, "buy-2")["status"], "PREPARED")

    def test_accepted_order_with_lost_response_reconciles_after_restart_without_resend(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.sqlite3"
            with TradingMemory(path) as memory:
                engine, _, broker = setup(memory)
                row = prepare(engine)
                original = broker.place_order

                def lose_response(intent):
                    self.assertFalse(memory.connection.in_transaction)
                    original(intent)
                    raise BrokerUnavailable("response lost")

                with mock.patch.object(broker, "place_order", side_effect=lose_response) as post:
                    result = engine.submit(row["client_order_id"])
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertEqual(post.call_count, 1)
            with TradingMemory(path) as memory:
                engine, _, broker = setup(memory)
                with mock.patch.object(broker, "place_order", side_effect=AssertionError("Must never resend")):
                    result = engine.submit(row["client_order_id"])
                self.assertEqual(result["status"], "FILLED")
                self.assertEqual(broker.get_balances()["BTC"], Decimal("0.001"))

    def test_crash_after_claim_and_repeated_not_found_never_resubmits_or_releases_commitment(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            with memory.transaction():
                store.claim_submit(row["client_order_id"], engine._now())
            with mock.patch.object(broker, "place_order", side_effect=AssertionError("Must not resend")):
                for _ in range(2):
                    result = engine.submit(row["client_order_id"])
                    self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(broker.get_balances()["EUR"], Decimal("300"))
            with self.assertRaisesRegex(ValueError, "unresolved"):
                prepare(engine, "buy-2")

    def test_failed_post_journal_update_recovers_remote_fill(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            with mock.patch.object(store, "apply_snapshot", side_effect=RuntimeError("disk failure")):
                with self.assertRaisesRegex(RuntimeError, "disk failure"):
                    engine.submit(row["client_order_id"])
            self.assertEqual(store.get(row["client_order_id"])["status"], "SUBMITTING")
            self.assertEqual(engine.reconcile(row["client_order_id"])["status"], "FILLED")
            self.assertEqual(broker.get_balances()["BTC"], Decimal("0.001"))

    def test_other_worker_claims_during_preflight_only_one_worker_posts(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            original = broker.get_balances

            def competing_claim():
                balances = original()
                with memory.transaction():
                    store.claim_submit(row["client_order_id"], engine._now())
                return balances

            with mock.patch.object(broker, "get_balances", side_effect=competing_claim), \
                    mock.patch.object(broker, "place_order", side_effect=AssertionError("Lost claim must not post")):
                self.assertEqual(engine.submit(row["client_order_id"])["status"], "UNKNOWN")

    def test_stale_preflight_balance_rejects_only_unsent_intent(self):
        with TradingMemory(":memory:") as memory:
            engine, _, broker = setup(memory)
            row = prepare(engine)
            with mock.patch.object(broker, "get_balances", return_value={"EUR": Decimal("1")}), \
                    mock.patch.object(broker, "place_order", side_effect=AssertionError("Should not submit")):
                self.assertEqual(engine.submit(row["client_order_id"])["status"], "REJECTED")

    def test_ambiguous_and_definitive_rejections_are_distinguished(self):
        for error, expected in [(OrderRejected("invalid"), "REJECTED"), (BrokerUnavailable("timeout"), "UNKNOWN")]:
            with self.subTest(expected=expected), TradingMemory(":memory:") as memory:
                engine, _, broker = setup(memory)
                row = prepare(engine)
                with mock.patch.object(broker, "place_order", side_effect=error):
                    self.assertEqual(engine.submit(row["client_order_id"])["status"], expected)

    def test_partial_fill_then_uncertain_cancel_reconciles_without_second_delete(self):
        with TradingMemory(":memory:") as memory:
            engine, store, broker = setup(memory)
            row = prepare(engine)
            from tradingbot.orders import OrderIntent
            intent = OrderIntent(**row["intent"])
            partial = snapshot(intent, "PARTIALLY_FILLED", "0.0004", "24")
            canceled = snapshot(intent, "CANCELED", "0.0004", "24", stamp=2000)
            with mock.patch.object(broker, "place_order", return_value=partial):
                self.assertEqual(engine.submit(intent.client_order_id)["status"], "PARTIALLY_FILLED")
            with mock.patch.object(broker, "cancel_order", side_effect=BrokerUnavailable("lost")) as delete:
                self.assertEqual(engine.cancel(intent.client_order_id)["status"], "UNKNOWN")
                with mock.patch.object(broker, "get_order", return_value=canceled):
                    result = engine.cancel(intent.client_order_id)
            self.assertEqual(delete.call_count, 1)
            self.assertEqual(result["status"], "CANCELED")
            self.assertEqual(result["snapshot"]["executed_quantity"], "0.0004")
            self.assertTrue(result["cancel_requested"])
            self.assertEqual(engine.cancel(intent.client_order_id), result)

    def test_malformed_broker_response_remains_unknown(self):
        with TradingMemory(":memory:") as memory:
            engine, _, broker = setup(memory)
            row = prepare(engine)
            with mock.patch.object(broker, "place_order", return_value=None):
                self.assertEqual(engine.submit(row["client_order_id"])["status"], "UNKNOWN")

    def test_high_precision_simulated_fee_balance_remains_usable(self):
        with TradingMemory(":memory:") as memory:
            broker = SimulatedBroker(memory, account_id="demo", initial_balances={"EUR": "300"},
                                     fee="0.00000000000000000001")
            engine, _, _ = setup(memory, broker)
            first = engine.prepare(request_id="tiny-fee", symbol="BTC/EUR", side="BUY",
                                   quantity="0.00101", limit_price="60000.01")
            self.assertEqual(engine.submit(first["client_order_id"])["status"], "FILLED")
            self.assertLess(broker.get_balances()["EUR"].as_tuple().exponent, -20)
            self.assertEqual(prepare(engine, "second-buy")["status"], "PREPARED")


class TestExecutionCLI(unittest.TestCase):
    def run_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_cli_prepares_submits_reconciles_and_inspects_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = ["execution", "--db", str(Path(tmp) / "exec.sqlite3")]
            code, out, _ = self.run_cli(root + ["prepare", "--request", "demo", "--side", "BUY",
                                              "--quantity", "0.001", "--limit-price", "60000"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["environment"], "simulation")
            for command in ("submit", "reconcile", "cancel"):
                code, out, _ = self.run_cli(root + [command, "--request", "demo"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["order"]["status"], "FILLED")
            code, out, _ = self.run_cli(root + ["balances"])
            self.assertEqual(code, 0)
            self.assertEqual(Decimal(json.loads(out)["balances"]["EUR"]), Decimal("239.94"))
            code, out, _ = self.run_cli(root + ["list", "--request", "demo"])
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)["events"]), 3)

    def test_missing_keys_and_invalid_order_fail_cleanly_without_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = ["execution", "--db", str(Path(tmp) / "exec.sqlite3")]
            with mock.patch.dict("os.environ", {}, clear=True):
                code, _, err = self.run_cli(root + ["--mode", "testnet", "balances"])
            self.assertEqual(code, 2)
            self.assertIn("BINANCE_TESTNET_API_SECRET", err)
            self.assertNotIn("Traceback", err)
            code, _, err = self.run_cli(root + ["prepare", "--request", "bad", "--side", "BUY",
                                              "--quantity", "NaN", "--limit-price", "60000"])
            self.assertEqual(code, 2)
            self.assertIn("finite", err)


if __name__ == "__main__":
    unittest.main()
