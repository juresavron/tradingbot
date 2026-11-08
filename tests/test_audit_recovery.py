"""Regression coverage for the post-fill transaction and reader boundaries."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradingbot.automation import AutomationRuntime
from tradingbot.memory import TradingMemory


class FaultingConnection:
    def __init__(self, connection, stage):
        self.connection, self.stage, self.triggered = connection, stage, False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        needles = {"begin": "BEGIN IMMEDIATE", "cost": "INTO automation_fill_costs",
                   "decision": "INTO automation_decisions", "equity": "INTO automation_equity",
                   "state": "UPDATE automation_revisions SET state_json"}
        if not self.triggered and self.stage in needles and needles[self.stage] in sql:
            self.triggered = True
            raise sqlite3.OperationalError("injected finalization fault")
        return self.connection.execute(sql, *args)

    def commit(self):
        if self.stage in ("commit", "commit_ack") and not self.triggered:
            self.triggered = True
            if self.stage == "commit_ack":
                self.connection.commit()
            raise sqlite3.OperationalError("injected commit fault")
        return self.connection.commit()


class TestAuditRecovery(unittest.TestCase):
    def test_a_failed_close_acknowledgement_survives_restart_without_reselling(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            runtime = AutomationRuntime(path, start_worker=False)
            self.addCleanup(runtime.shutdown)
            runtime.configure({"strategy": "buy_hold", "params": {}, "slippage": .0005})
            runtime.control("start")
            runtime.tick_once()
            runtime.control("flatten")
            finish = runtime._finish_cycle

            def fail(memory, *args, **kwargs):
                original = memory.connection
                memory.connection = FaultingConnection(original, "commit")
                try:
                    return finish(memory, *args, **kwargs)
                finally:
                    memory.connection = original

            with patch.object(runtime, "_finish_cycle", side_effect=fail):
                closed = runtime.tick_once()
            self.assertLess(closed["portfolio"]["position_value"], 5)
            self.assertTrue(closed["status"]["flatten_requested"])
            runtime.shutdown()
            restarted = AutomationRuntime(path, start_worker=False)
            self.addCleanup(restarted.shutdown)
            with patch("tradingbot.automation.SimulatedBroker.place_order", side_effect=AssertionError("No second sell")):
                repaired = restarted.tick_once()
            self.assertFalse(repaired["status"]["flatten_requested"])
            self.assertFalse(repaired["status"]["desired_running"])
            self.assertEqual(repaired["metrics"]["n_filled"], 2)
            self.assertEqual(repaired["portfolio"]["quantity"], closed["portfolio"]["quantity"])
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM automation_fill_costs").fetchone()[0], 2)

    def test_every_finalization_failure_retains_a_repairable_fill(self):
        for stage in ("begin", "cost", "decision", "equity", "state", "commit", "commit_ack"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "runtime.sqlite3"
                runtime = AutomationRuntime(path, start_worker=False)
                self.addCleanup(runtime.shutdown)
                runtime.configure({"strategy": "buy_hold", "params": {}, "slippage": 0.0005})
                runtime.control("start")
                finish = runtime._finish_cycle
                faults = []

                def fail_finalization(memory, *args, **kwargs):
                    original = memory.connection
                    proxy = FaultingConnection(original, stage)
                    faults.append(proxy)
                    memory.connection = proxy
                    try:
                        return finish(memory, *args, **kwargs)
                    finally:
                        memory.connection = original

                with patch.object(runtime, "_finish_cycle", side_effect=fail_finalization):
                    failed = runtime.tick_once()
                self.assertTrue(faults[0].triggered)
                self.assertEqual(failed["metrics"]["n_filled"], 1)
                with TradingMemory(path) as memory:
                    row = runtime._load(memory)
                    if stage != "commit_ack":
                        self.assertIsNotNone(row["state"]["inflight"])
                        self.assertEqual(row["state"]["sequence"], 0)
                    else:
                        self.assertIsNone(row["state"]["inflight"])
                        self.assertEqual(row["state"]["sequence"], 1)
                    first_id = failed["orders"][0]["client_order_id"]
                runtime.shutdown()
                restarted = AutomationRuntime(path, start_worker=False)
                self.addCleanup(restarted.shutdown)
                with patch("tradingbot.automation.SimulatedBroker.place_order",
                           side_effect=AssertionError("A committed fill must never be resent")):
                    recovered = restarted.tick_once()
                self.assertEqual(recovered["metrics"]["n_filled"], 1)
                self.assertEqual(recovered["orders"][0]["client_order_id"], first_id)
                self.assertGreater(recovered["metrics"]["slippage_paid"], 0)
                self.assertEqual(recovered["portfolio"]["cash"], failed["portfolio"]["cash"])
                self.assertEqual(recovered["portfolio"]["quantity"], failed["portfolio"]["quantity"])
                with sqlite3.connect(path) as connection:
                    counts = {name: connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                              for name in ("automation_decisions", "automation_equity", "automation_fill_costs")}
                    # A lost commit acknowledgement already completed the first
                    # cycle, so restart legitimately records the next HOLD too.
                    expected = 2 if stage == "commit_ack" else 1
                    self.assertEqual(counts, {"automation_decisions": expected,
                                             "automation_equity": expected, "automation_fill_costs": 1})

    def test_snapshot_is_one_committed_view_during_a_worker_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AutomationRuntime(Path(directory) / "runtime.sqlite3", start_worker=False)
            self.addCleanup(runtime.shutdown)
            runtime.configure({"strategy": "buy_hold", "params": {}})
            runtime.control("start")
            portfolio = AutomationRuntime._portfolio
            interleaved = False

            def fill_after_wallet_read(row, broker):
                nonlocal interleaved
                result = portfolio(row, broker)
                if not interleaved:
                    interleaved = True
                    runtime.tick_once()
                return result

            with patch.object(AutomationRuntime, "_portfolio", staticmethod(fill_after_wallet_read)):
                before = runtime.snapshot()
            after = runtime.snapshot()
            self.assertEqual(before["portfolio"]["cash"], 300)
            self.assertEqual(before["portfolio"]["quantity"], 0)
            self.assertEqual(before["metrics"]["n_filled"], 0)
            self.assertEqual(before["orders"], [])
            self.assertEqual(before["decisions"], [])
            self.assertEqual(after["metrics"]["n_filled"], 1)
            self.assertGreater(after["portfolio"]["quantity"], 0)

    def test_snapshot_cannot_mix_an_old_wallet_and_a_new_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AutomationRuntime(Path(directory) / "runtime.sqlite3", start_worker=False)
            self.addCleanup(runtime.shutdown)
            portfolio = AutomationRuntime._portfolio
            interleaved = False

            def change_after_wallet_read(row, broker):
                nonlocal interleaved
                result = portfolio(row, broker)
                if not interleaved:
                    interleaved = True
                    runtime.configure({"starting_cash": 400})
                return result

            with patch.object(AutomationRuntime, "_portfolio", staticmethod(change_after_wallet_read)):
                before = runtime.snapshot()
            after = runtime.snapshot()
            self.assertEqual(before["revision"], 1)
            self.assertEqual(before["config"]["starting_cash"], before["portfolio"]["cash"])
            self.assertEqual(after["revision"], 2)
            self.assertEqual(after["config"]["starting_cash"], after["portfolio"]["cash"])


if __name__ == "__main__":
    unittest.main()
