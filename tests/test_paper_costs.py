import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tradingbot.automation import AutomationRuntime


class TestForwardPaperCosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "paper.sqlite3"
        self.runtime = AutomationRuntime(self.path, start_worker=False)

    def tearDown(self):
        self.runtime.shutdown()
        self.tmp.cleanup()

    def test_adverse_buy_and_sell_prices_fees_and_durable_cost_journal(self):
        self.runtime.configure({"strategy": "buy_hold", "params": {}, "fee": 0.002, "slippage": 0.01})
        self.runtime.control("start")
        bought = self.runtime.tick_once()
        buy = bought["orders"][0]
        self.assertEqual(Decimal(buy["intent"]["limit_price"]), Decimal("101"))
        quantity = Decimal(buy["snapshot"]["executed_quantity"])
        quote = Decimal(buy["snapshot"]["cumulative_quote_quantity"])
        self.assertAlmostEqual(bought["portfolio"]["cash"], float(Decimal("300") - quote * Decimal("1.002")))
        self.assertAlmostEqual(bought["metrics"]["slippage_paid"], float(quantity))
        self.assertAlmostEqual(bought["metrics"]["fees_paid"], float(quote * Decimal("0.002")))
        self.runtime.control("flatten")
        sold = self.runtime.tick_once()
        sell = next(o for o in sold["orders"] if o["intent"]["side"] == "SELL")
        self.assertEqual(Decimal(sell["intent"]["limit_price"]), Decimal("99.99"))
        with sqlite3.connect(self.path) as connection:
            costs = connection.execute("SELECT market_price,fill_price,quantity,fee_quote,slippage_quote FROM automation_fill_costs").fetchall()
        self.assertEqual(len(costs), 2)
        self.assertTrue(all(Decimal(row[3]) > 0 and Decimal(row[4]) > 0 for row in costs))
        self.assertAlmostEqual(sold["metrics"]["fees_paid"], float(sum(Decimal(c[3]) for c in costs)))
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        resumed = self.runtime.snapshot()
        self.assertEqual(resumed["metrics"], sold["metrics"])

    def test_high_configured_fee_is_reserved_before_full_cash_entry(self):
        self.runtime.configure({"strategy": "buy_hold", "params": {}, "fee": 0.05,
                                "position_fraction": 1, "max_order_notional": 300})
        self.runtime.control("start")
        result = self.runtime.tick_once()
        self.assertIsNone(result["status"]["error"])
        self.assertEqual(result["orders"][0]["status"], "FILLED")
        self.assertGreaterEqual(result["portfolio"]["cash"], 0)

    def test_feed_errors_and_recovery_remain_in_history_after_restart(self):
        with patch.object(self.runtime, "_market", side_effect=RuntimeError("secret provider URL")):
            failed = self.runtime.tick_once()
        self.assertEqual(failed["events"][0]["detail"], {"code": "market_or_execution_unavailable"})
        self.assertNotIn("secret provider", json.dumps(failed))
        recovered = self.runtime.tick_once()
        self.assertIsNone(recovered["status"]["error"])
        self.assertEqual([e["kind"] for e in recovered["events"]], ["recovered", "error"])
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        self.assertEqual(self.runtime.snapshot()["events"], recovered["events"])

    def test_existing_simulation_migrates_without_changing_wallet_or_costs(self):
        before = self.runtime.snapshot()
        self.runtime.shutdown()
        with sqlite3.connect(self.path) as connection:
            config = dict(before["config"])
            del config["fee"], config["slippage"]
            connection.execute("UPDATE automation_revisions SET config_json=? WHERE revision=?",
                               (json.dumps(config), before["revision"]))
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        after = self.runtime.snapshot()
        self.assertEqual(after["account_id"], before["account_id"])
        self.assertEqual(after["portfolio"], before["portfolio"])
        self.assertEqual(after["config"]["fee"], 0.001)
        self.assertEqual(after["config"]["slippage"], 0)

    def test_invalid_costs_do_not_create_revisions(self):
        before = self.runtime.snapshot()["revision"]
        for key in ("fee", "slippage"):
            for value in (-0.1, 0.1, float("nan"), float("inf"), True):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.runtime.configure({key: value})
                self.assertEqual(self.runtime.snapshot()["revision"], before)
