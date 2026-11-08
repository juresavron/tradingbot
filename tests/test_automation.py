import json
import math
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tradingbot.automation import AutomationRuntime
from tradingbot.candles import Candle, TIMEFRAME_MS
from tradingbot.execution import ExecutionEngine
from tradingbot.memory import TradingMemory
from tradingbot.orders import BrokerUnavailable
from tradingbot.simulated_broker import SimulatedBroker


class TestAutomationRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "automation.sqlite3"
        self.runtime = AutomationRuntime(self.path, start_worker=False)

    def tearDown(self):
        self.runtime.shutdown()
        self.tmp.cleanup()

    def configure_fast(self, **changes):
        config = {"strategy": "sma_cross", "params": {"fast": 2, "slow": 4}}
        config.update(changes)
        return self.runtime.configure(config)

    def until(self, predicate, *, ticks=125):
        state = self.runtime.snapshot()
        for _ in range(ticks):
            if predicate(state):
                return state
            state = self.runtime.tick_once()
        self.fail("Automatic execution did not reach the expected portfolio state")

    def test_default_is_paused_simulation_with_finite_snapshot(self):
        state = self.runtime.snapshot()
        self.assertEqual(state["config"]["source"], "demo")
        self.assertEqual(state["config"]["symbol"], "BTC/EUR")
        self.assertFalse(state["status"]["running"])
        self.assertEqual(state["capabilities"]["execution"], "simulation")
        self.assertFalse(state["capabilities"]["llm"])
        self.assertEqual(state["portfolio"]["cash"], 300)
        self.assertEqual(state["portfolio"]["quantity"], 0)
        self.assertEqual(state["metrics"]["n_orders"], 0)
        json.dumps(state, allow_nan=False)
        for _ in range(3):
            state = self.runtime.tick_once()
        self.assertEqual(state["metrics"]["n_orders"], 0)

    def test_strategy_automatically_buys_then_sells_via_durable_execution(self):
        self.configure_fast()
        self.runtime.control("start")
        entered = self.until(lambda s: s["portfolio"]["quantity"] > 0)
        self.assertLessEqual(entered["portfolio"]["position_value"], 75.01)
        self.assertTrue(any(o["intent"]["side"] == "BUY" and o["status"] == "FILLED" for o in entered["orders"]))
        exited = self.until(lambda s: s["portfolio"]["position_value"] < 5 and any(
            o["intent"]["side"] == "SELL" and o["status"] == "FILLED" for o in s["orders"]))
        self.assertLess(exited["portfolio"]["position_value"], 5)
        self.assertGreaterEqual(exited["metrics"]["n_filled"], 2)
        self.assertGreaterEqual(len(exited["decisions"]), 2)
        self.assertTrue(exited["equity"])
        self.assertTrue(all(math.isfinite(p["equity"]) for p in exited["equity"]))
        json.dumps(exited, allow_nan=False)

    def test_pause_prevents_entries_but_allows_existing_position_to_exit(self):
        self.configure_fast()
        self.runtime.control("start")
        self.runtime.control("pause")
        for _ in range(4):
            paused = self.runtime.tick_once()
        self.assertFalse(paused["status"]["running"])
        self.assertEqual(paused["metrics"]["n_orders"], 0)
        self.runtime.control("start")
        self.until(lambda s: s["portfolio"]["quantity"] > 0)
        self.runtime.control("pause")
        exited = self.until(lambda s: s["portfolio"]["position_value"] < 5)
        self.assertFalse(exited["status"]["running"])
        buys = [o for o in exited["orders"] if o["intent"]["side"] == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertTrue(any(o["intent"]["side"] == "SELL" for o in exited["orders"]))

    def test_manual_flatten_is_durable_and_does_not_reenable_entries(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        self.until(lambda s: s["portfolio"]["quantity"] > 0)
        self.runtime.control("flatten")
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        flattened = self.until(lambda s: s["portfolio"]["position_value"] < 5, ticks=5)
        self.assertFalse(flattened["status"]["running"])
        self.assertEqual(flattened["metrics"]["n_filled"], 2)
        for _ in range(3):
            state = self.runtime.tick_once()
        self.assertLess(state["portfolio"]["position_value"], 5)
        self.assertEqual(state["metrics"]["n_orders"], 2)

    def test_pause_during_blocked_market_is_durable_and_stale_cycle_cannot_buy(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        entered, release = threading.Event(), threading.Event()
        market = self.runtime._market

        def blocked_market(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test market was not released")
            return market(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.runtime, "_market", side_effect=blocked_market):
            cycle = pool.submit(self.runtime.tick_once)
            try:
                self.assertTrue(entered.wait(2))
                paused = pool.submit(self.runtime.control, "pause").result(timeout=2)
                self.assertFalse(cycle.done())
                self.assertFalse(paused["status"]["desired_running"])
                with sqlite3.connect(self.path) as conn:
                    self.assertEqual(conn.execute("SELECT desired_running FROM automation_controls WHERE revision=?",
                                                  (paused["revision"],)).fetchone()[0], 0)
            finally:
                release.set()
            finished = cycle.result(timeout=2)
        self.assertFalse(finished["status"]["desired_running"])
        self.assertEqual(finished["metrics"]["n_filled"], 0)
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        self.assertFalse(self.runtime.snapshot()["status"]["desired_running"])
        self.assertEqual(self.runtime.tick_once()["metrics"]["n_filled"], 0)

    def test_flatten_during_blocked_market_returns_then_closes_on_usable_price(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        self.until(lambda s: s["portfolio"]["quantity"] > 0)
        entered, release = threading.Event(), threading.Event()
        market = self.runtime._market

        def blocked_market(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test market was not released")
            return market(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.runtime, "_market", side_effect=blocked_market):
            cycle = pool.submit(self.runtime.tick_once)
            try:
                self.assertTrue(entered.wait(2))
                pending = pool.submit(self.runtime.control, "flatten").result(timeout=2)
                self.assertFalse(cycle.done())
                self.assertFalse(pending["status"]["desired_running"])
                self.assertTrue(pending["status"]["flatten_requested"])
                self.assertGreater(pending["portfolio"]["position_value"], 5)
                with self.assertRaisesRegex(ValueError, "Finish closing"):
                    self.runtime.control("start")
            finally:
                release.set()
            closed = cycle.result(timeout=2)
        self.assertFalse(closed["status"]["desired_running"])
        self.assertFalse(closed["status"]["flatten_requested"])
        self.assertLess(closed["portfolio"]["position_value"], 5)
        self.assertEqual(closed["metrics"]["n_filled"], 2)

    def test_pause_during_broker_preflight_cancels_unsent_buy_even_if_resumed(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        entered, release = threading.Event(), threading.Event()
        submit = ExecutionEngine.submit

        def blocked_preflight(engine, client_order_id, **kwargs):
            balances = engine.broker.get_balances

            def blocked_balances():
                self.assertFalse(engine.store.memory.connection.in_transaction)
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test preflight was not released")
                return balances()

            with patch.object(engine.broker, "get_balances", side_effect=blocked_balances):
                return submit(engine, client_order_id, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool, \
                patch("tradingbot.automation.ExecutionEngine.submit", new=blocked_preflight), \
                patch("tradingbot.automation.SimulatedBroker.place_order", side_effect=AssertionError("Fenced buy cannot post")):
            cycle = pool.submit(self.runtime.tick_once)
            try:
                self.assertTrue(entered.wait(2))
                paused = pool.submit(self.runtime.control, "pause").result(timeout=2)
                resumed = pool.submit(self.runtime.control, "start").result(timeout=2)
                self.assertFalse(cycle.done())
                self.assertGreater(resumed["status"]["control_generation"], paused["status"]["control_generation"])
            finally:
                release.set()
            canceled = cycle.result(timeout=2)
        self.assertEqual(canceled["orders"][0]["status"], "CANCELED")
        self.assertEqual(canceled["orders"][0]["last_error"], "submission_not_authorized")
        self.assertTrue(canceled["status"]["desired_running"])
        self.assertEqual(canceled["portfolio"]["quantity"], 0)
        # A new decision can use the new permission; the old plan cannot.
        self.assertEqual(self.runtime.tick_once()["metrics"]["n_filled"], 1)

    def test_flatten_after_submit_claim_preserves_uncertain_order_and_reconciles_once(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        entered, release = threading.Event(), threading.Event()
        place = SimulatedBroker.place_order

        def blocked_submission(broker, intent):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test submission was not released")
            place(broker, intent)
            raise BrokerUnavailable("response lost after the already claimed fill")

        with ThreadPoolExecutor(max_workers=2) as pool, \
                patch("tradingbot.automation.SimulatedBroker.place_order", new=blocked_submission):
            cycle = pool.submit(self.runtime.tick_once)
            try:
                self.assertTrue(entered.wait(2))
                pending = pool.submit(self.runtime.control, "flatten").result(timeout=2)
                self.assertFalse(cycle.done())
                self.assertEqual(pending["orders"][0]["status"], "SUBMITTING")
                self.assertTrue(pending["status"]["flatten_requested"])
                self.assertFalse(pending["status"]["desired_running"])
            finally:
                release.set()
            uncertain = cycle.result(timeout=2)
        self.assertEqual(uncertain["orders"][0]["status"], "UNKNOWN")
        self.assertGreater(uncertain["portfolio"]["quantity"], 0)
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        with patch("tradingbot.automation.SimulatedBroker.place_order", side_effect=AssertionError("Must reconcile, not resend")):
            reconciled = self.runtime.tick_once()
        self.assertEqual(reconciled["metrics"]["n_filled"], 1)
        self.assertTrue(reconciled["status"]["flatten_requested"])
        closed = self.until(lambda s: s["portfolio"]["position_value"] < 5, ticks=5)
        self.assertEqual(closed["metrics"]["n_filled"], 2)
        self.assertFalse(closed["status"]["desired_running"])

    def test_new_flatten_survives_completion_of_previous_request(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        self.until(lambda s: s["portfolio"]["quantity"] > 0)
        older = self.runtime.control("flatten")
        entered, release = threading.Event(), threading.Event()
        place = SimulatedBroker.place_order

        def blocked_sell(broker, intent):
            self.assertEqual(intent.side, "SELL")
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test sell was not released")
            return place(broker, intent)

        with ThreadPoolExecutor(max_workers=2) as pool, patch("tradingbot.automation.SimulatedBroker.place_order", new=blocked_sell):
            cycle = pool.submit(self.runtime.tick_once)
            try:
                self.assertTrue(entered.wait(2))
                newer = pool.submit(self.runtime.control, "flatten").result(timeout=2)
                self.assertGreater(newer["status"]["control_generation"], older["status"]["control_generation"])
            finally:
                release.set()
            finished = cycle.result(timeout=2)
        self.assertLess(finished["portfolio"]["position_value"], 5)
        self.assertTrue(finished["status"]["flatten_requested"])
        self.assertFalse(self.runtime.tick_once()["status"]["flatten_requested"])

    def test_existing_state_is_upgraded_once_without_reenabling_or_losing_flatten(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        self.until(lambda s: s["portfolio"]["quantity"] > 0)
        self.runtime.control("flatten")
        self.runtime.shutdown()
        # Emulate a database from before the separate control table existed.
        with sqlite3.connect(self.path) as conn:
            record = conn.execute("SELECT revision,state_json FROM automation_revisions ORDER BY revision DESC LIMIT 1").fetchone()
            state = json.loads(record[1])
            # Older code allowed Start during a pending flatten. Upgrade
            # must give the close priority instead of resuming entries.
            state.update(desired_running=True, flatten=True)
            conn.execute("UPDATE automation_revisions SET state_json=? WHERE revision=?", (json.dumps(state), record[0]))
            conn.execute("DROP TABLE automation_controls")
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        upgraded = self.runtime.snapshot()
        self.assertFalse(upgraded["status"]["desired_running"])
        self.assertTrue(upgraded["status"]["flatten_requested"])
        self.runtime.control("pause")
        generation = self.runtime.snapshot()["status"]["control_generation"]
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        self.assertEqual(self.runtime.snapshot()["status"]["control_generation"], generation)
        self.assertTrue(self.runtime.snapshot()["status"]["flatten_requested"])

    def test_drawdown_trigger_sells_and_halts_instead_of_reentering(self):
        self.configure_fast(strategy="buy_hold", params={}, max_drawdown=0.01)
        self.runtime.control("start")
        self.until(lambda s: s["portfolio"]["quantity"] > 0)
        halted = self.until(lambda s: s["status"]["state"] == "halted" and s["portfolio"]["position_value"] < 5)
        self.assertFalse(halted["status"]["running"])
        self.assertLess(halted["portfolio"]["position_value"], 5)
        self.assertTrue(any(o["intent"]["side"] == "SELL" and o["status"] == "FILLED" for o in halted["orders"]))
        order_count = halted["metrics"]["n_orders"]
        for _ in range(3):
            state = self.runtime.tick_once()
        self.assertEqual(state["metrics"]["n_orders"], order_count)

    def test_restart_preserves_running_setting_balance_and_execution_ids(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        before = self.until(lambda s: s["portfolio"]["quantity"] > 0)
        before_ids = {o["client_order_id"] for o in before["orders"]}
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        restored = self.runtime.snapshot()
        self.assertTrue(restored["status"]["running"])
        self.assertEqual(restored["portfolio"]["cash"], before["portfolio"]["cash"])
        self.assertEqual(restored["portfolio"]["quantity"], before["portfolio"]["quantity"])
        advanced = self.runtime.tick_once()
        self.assertEqual({o["client_order_id"] for o in advanced["orders"]}, before_ids)
        self.assertEqual(advanced["metrics"]["n_filled"], 1)

    def test_lost_submission_response_is_reconciled_after_restart_without_resubmission(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        place_order = SimulatedBroker.place_order

        def fill_then_lose_response(broker, order):
            place_order(broker, order)
            raise BrokerUnavailable("simulated lost submit response")

        with patch("tradingbot.automation.SimulatedBroker.place_order", new=fill_then_lose_response):
            unknown = self.runtime.tick_once()
        self.assertEqual(len(unknown["orders"]), 1)
        self.assertEqual(unknown["orders"][0]["status"], "UNKNOWN")
        self.assertGreater(unknown["portfolio"]["quantity"], 0)
        unknown_quantity = unknown["portfolio"]["quantity"]
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        with patch("tradingbot.automation.SimulatedBroker.place_order", side_effect=AssertionError("must not resubmit")):
            reconciled = self.runtime.tick_once()
        self.assertEqual(reconciled["metrics"]["n_orders"], 1)
        self.assertEqual(reconciled["metrics"]["n_filled"], 1)
        self.assertEqual(reconciled["portfolio"]["quantity"], unknown_quantity)

    def test_pause_cancels_a_prepared_unsent_buy_after_interrupted_cycle(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        with patch("tradingbot.automation.ExecutionEngine.submit", side_effect=BrokerUnavailable("simulated interrupted worker")):
            prepared = self.runtime.tick_once()
        self.assertEqual(prepared["orders"][0]["status"], "PREPARED")
        self.assertEqual(prepared["portfolio"]["quantity"], 0)
        self.runtime.control("pause")
        canceled = self.runtime.tick_once()
        self.assertEqual(canceled["orders"][0]["status"], "CANCELED")
        self.assertEqual(canceled["metrics"]["n_filled"], 0)
        self.assertEqual(canceled["portfolio"]["quantity"], 0)

    def test_restart_cancels_old_unsent_buy_without_a_quote_and_requires_fresh_cycle(self):
        self.configure_fast(source="binance", strategy="buy_hold", params={})
        self.runtime.control("start")
        now_ms = int(time.time() * 1000)
        with patch("tradingbot.automation.MarketData.get_candles", return_value=self.market_bars(now_ms)), \
                patch("tradingbot.automation.MarketData.get_price", return_value=101), \
                patch("tradingbot.automation.ExecutionEngine.submit", side_effect=BrokerUnavailable("interrupted before submission")):
            prepared = self.runtime.tick_once(now_ms=now_ms)
        self.assertEqual(prepared["orders"][0]["status"], "PREPARED")
        client_id = prepared["orders"][0]["client_order_id"]
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        later = now_ms + 5 * TIMEFRAME_MS["4h"]
        with patch("tradingbot.automation.SimulatedBroker.place_order", side_effect=AssertionError("old plan must not be submitted")), \
                patch("tradingbot.automation.MarketData.get_price", side_effect=RuntimeError("public feed offline")) as get_price:
            recovered = self.runtime.tick_once(now_ms=later)
            get_price.assert_not_called()
            unavailable = self.runtime.tick_once(now_ms=later + 1000)
            get_price.assert_called_once()
        self.assertEqual(recovered["orders"][0]["client_order_id"], client_id)
        self.assertEqual(recovered["orders"][0]["status"], "CANCELED")
        self.assertEqual(unavailable["metrics"]["n_orders"], 1)
        self.assertEqual(unavailable["metrics"]["n_filled"], 0)
        self.assertEqual(unavailable["portfolio"]["quantity"], 0)
        self.assertTrue(unavailable["status"]["error"])

    def test_restart_does_not_submit_old_news_approved_buy_after_news_expires(self):
        self.configure_fast(source="binance", strategy="buy_hold", params={}, news_policy="require-fresh")
        self.runtime.control("start")
        now_ms = int(time.time() * 1000)
        bars = self.market_bars(now_ms)
        article = {"id": "initially-fresh", "url": "https://example.com/news", "title": "A market report",
                   "body": "An observation, not an order instruction.", "source": "example.com",
                   "published_at_ms": now_ms - 1000, "observed_at_ms": now_ms - 1000, "assets": ["BTC"]}
        with patch("tradingbot.automation.MarketData.get_candles", return_value=bars), \
                patch("tradingbot.automation.MarketData.get_price", return_value=101), \
                patch("tradingbot.automation.TradingMemory.recent_news", return_value=[article]), \
                patch("tradingbot.automation.ExecutionEngine.submit", side_effect=BrokerUnavailable("interrupted before submission")):
            prepared = self.runtime.tick_once(now_ms=now_ms)
        self.assertEqual(prepared["orders"][0]["status"], "PREPARED")
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        later = now_ms + 25 * 60 * 60 * 1000
        interval = TIMEFRAME_MS["4h"]
        later_end = later // interval * interval
        extended = list(bars)
        while extended[-1].ts + interval < later_end:
            extended.append(Candle(extended[-1].ts + interval, 101, 101, 101, 101, 1))
        with patch("tradingbot.automation.MarketData.get_candles", return_value=extended[-300:]), \
                patch("tradingbot.automation.MarketData.get_price", return_value=101), \
                patch("tradingbot.automation.TradingMemory.recent_news", return_value=[]), \
                patch("tradingbot.automation.SimulatedBroker.place_order", side_effect=AssertionError("expired approval must not cause an order")):
            recovered = self.runtime.tick_once(now_ms=later)
            gated = self.runtime.tick_once(now_ms=later + 1000)
        self.assertEqual(recovered["orders"][0]["status"], "CANCELED")
        self.assertEqual(gated["metrics"]["n_orders"], 1)
        self.assertEqual(gated["metrics"]["n_filled"], 0)
        self.assertEqual(gated["portfolio"]["quantity"], 0)
        self.assertEqual(gated["status"]["last_action"], "BLOCKED")
        self.assertEqual(gated["status"]["last_reason"], "blocked_no_fresh_news")

    def test_configuration_requires_paused_flat_state_and_preserves_old_simulation(self):
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        with self.assertRaises(ValueError):
            self.runtime.configure({"starting_cash": 400})
        self.until(lambda s: s["portfolio"]["quantity"] > 0)
        self.runtime.control("pause")
        with self.assertRaises(ValueError):
            self.runtime.configure({"starting_cash": 400})
        self.runtime.control("flatten")
        self.until(lambda s: s["portfolio"]["position_value"] < 5, ticks=5)
        with sqlite3.connect(self.path) as conn:
            before_orders = conn.execute("SELECT COUNT(*) FROM execution_orders").fetchone()[0]
        configured = self.runtime.configure({"starting_cash": 400})
        self.assertEqual(configured["portfolio"]["cash"], 400)
        self.assertEqual(configured["metrics"]["n_orders"], 0)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_orders").fetchone()[0], before_orders)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM simulation_wallets").fetchone()[0], 2)

    def test_failed_revision_creation_rolls_back_wallet_and_execution_account(self):
        before = self.runtime.snapshot()
        with sqlite3.connect(self.path) as conn:
            wallet_before = conn.execute("SELECT * FROM simulation_wallets WHERE account_id=?",
                                         (before["account_id"],)).fetchone()
            account_before = conn.execute("SELECT * FROM execution_accounts WHERE account_id=?",
                                          (before["account_id"],)).fetchone()
            conn.execute("""CREATE TRIGGER reject_new_automation_revision
                BEFORE INSERT ON automation_revisions WHEN NEW.revision > 1
                BEGIN SELECT RAISE(ABORT, 'injected revision write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.configure({"starting_cash": 350})
        after = self.runtime.snapshot()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["config"], before["config"])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM simulation_wallets WHERE account_id=?",
                                          (before["account_id"],)).fetchone(), wallet_before)
            self.assertEqual(conn.execute("SELECT * FROM execution_accounts WHERE account_id=?",
                                          (before["account_id"],)).fetchone(), account_before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM simulation_wallets").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_accounts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM automation_revisions").fetchone()[0], 1)
            conn.execute("DROP TRIGGER reject_new_automation_revision")
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        replacement = self.runtime.configure({"starting_cash": 400})
        self.assertEqual(replacement["revision"], 2)
        self.assertEqual(replacement["portfolio"]["cash"], 400)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM simulation_wallets WHERE account_id=?",
                                          (before["account_id"],)).fetchone(), wallet_before)

    def test_unknown_configuration_fields_and_invalid_limits_cannot_mutate_settings(self):
        before = self.runtime.snapshot()["config"]
        for changes in ({"api_key": "must-not-be-accepted"}, {"source": "production"},
                        {"starting_cash": -1}, {"position_fraction": 1.5},
                        {"max_drawdown": 0}, {"max_order_notional": float("nan")},
                        {"strategy": "sma_cross", "params": {"fast": "oops"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.runtime.configure(changes)
            self.assertEqual(self.runtime.snapshot()["config"], before)
        with self.assertRaises(ValueError):
            self.runtime.control("trade-real-money")

    def test_require_fresh_news_blocks_entry_instead_of_inventing_a_signal(self):
        self.configure_fast(news_policy="require-fresh")
        self.runtime.control("start")
        for _ in range(6):
            state = self.runtime.tick_once()
        self.assertEqual(state["metrics"]["n_orders"], 0)
        self.assertEqual(state["portfolio"]["quantity"], 0)
        self.assertEqual(state["status"]["last_action"], "BLOCKED")

    def test_single_owner_prevents_a_second_automatic_worker_for_same_database(self):
        with self.assertRaises((ValueError, RuntimeError)):
            AutomationRuntime(self.path, start_worker=False)

    def test_worker_executes_without_browser_polling(self):
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, poll_seconds=0.02, start_worker=True)
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        deadline = time.monotonic() + 3
        filled = 0
        while time.monotonic() < deadline and not filled:
            # Read the execution journal directly, not snapshot/API. This
            # demonstrates the server worker itself drives order execution.
            with sqlite3.connect(self.path) as conn:
                filled = conn.execute("SELECT COUNT(*) FROM execution_orders WHERE status = 'FILLED'").fetchone()[0]
            if not filled:
                time.sleep(0.02)
        self.assertGreater(filled, 0)

    def test_worker_retries_after_outer_iteration_failure_without_leaking_error_content(self):
        self.runtime.shutdown()
        self.runtime = AutomationRuntime(self.path, poll_seconds=0.02, start_worker=False)
        self.configure_fast(strategy="buy_hold", params={})
        self.runtime.control("start")
        tick_once = self.runtime.tick_once
        iterations = []

        def fail_once_then_run(*args, **kwargs):
            iterations.append(True)
            if len(iterations) == 1:
                raise RuntimeError("https://secret.invalid/private?token=hidden")
            return tick_once(*args, **kwargs)

        with patch.object(self.runtime, "tick_once", side_effect=fail_once_then_run):
            self.runtime.start_worker()
            deadline = time.monotonic() + 3
            filled = 0
            while time.monotonic() < deadline and not filled:
                with sqlite3.connect(self.path) as conn:
                    filled = conn.execute("SELECT COUNT(*) FROM execution_orders WHERE status='FILLED'").fetchone()[0]
                if not filled:
                    time.sleep(0.02)
            self.assertGreater(filled, 0)
            self.assertGreaterEqual(len(iterations), 2)
            self.assertTrue(self.runtime.worker_enabled)
            self.assertTrue(self.runtime.worker_alive)
            state = self.runtime.snapshot()
        self.assertTrue(state["status"]["worker_alive"])
        serialized = json.dumps(state, allow_nan=False)
        self.assertNotIn("secret.invalid", serialized)
        self.assertNotIn("hidden", serialized)

    def market_bars(self, now_ms):
        interval = TIMEFRAME_MS["4h"]
        end = now_ms // interval * interval
        prices = [100] * 299 + [101]
        return [Candle(end - (300 - i) * interval, p, p, p, p, 1) for i, p in enumerate(prices)]

    def bullish_bars_through(self, last_open_ms):
        interval = TIMEFRAME_MS["4h"]
        prices = [100] * 299 + [101]
        return [Candle(last_open_ms - (299 - i) * interval, p, p, p, p, 1)
                for i, p in enumerate(prices)]

    def test_fresh_quote_cannot_authorize_entry_from_six_hour_old_closed_bar(self):
        self.configure_fast(source="binance")
        receipt = int(datetime(2026, 11, 8, 6, tzinfo=timezone.utc).timestamp() * 1000)
        interval = TIMEFRAME_MS["4h"]
        # The bullish bar closes at midnight, six hours before this receipt.
        # A fresh ticker must not make that older strategy signal current.
        bars = self.bullish_bars_through(receipt - 6 * 60 * 60 * 1000 - interval)
        with patch("tradingbot.automation.MarketData.get_candles", return_value=bars), \
                patch("tradingbot.automation.MarketData.get_price", return_value=150):
            self.runtime.control("start")
            state = self.runtime.tick_once(now_ms=receipt)
        self.assertEqual(state["status"]["error"], "market_data_stale")
        self.assertEqual(state["metrics"]["n_orders"], 0)
        self.assertEqual(state["portfolio"]["quantity"], 0)

    def test_previous_closed_bar_has_only_sixty_seconds_of_publication_grace(self):
        boundary = int(datetime(2026, 11, 8, 4, tzinfo=timezone.utc).timestamp() * 1000)
        interval = TIMEFRAME_MS["4h"]
        # At 04:00 the expected latest closed bar opens at 00:00. The previous
        # one opens at 20:00 and may briefly remain the exchange's last result.
        bars = self.bullish_bars_through(boundary - 2 * interval)
        for delay_ms, accepted in ((30_000, True), (61_000, False)):
            with self.subTest(delay_ms=delay_ms):
                runtime = AutomationRuntime(Path(self.tmp.name) / f"grace-{delay_ms}.sqlite3", start_worker=False)
                try:
                    runtime.configure({"source": "binance", "strategy": "sma_cross", "params": {"fast": 2, "slow": 4}})
                    runtime.control("start")
                    with patch("tradingbot.automation.MarketData.get_candles", return_value=bars), \
                            patch("tradingbot.automation.MarketData.get_price", return_value=150):
                        state = runtime.tick_once(now_ms=boundary + delay_ms)
                    if accepted:
                        self.assertIsNone(state["status"]["error"])
                        self.assertEqual(state["metrics"]["n_filled"], 1)
                    else:
                        self.assertEqual(state["status"]["error"], "market_data_stale")
                        self.assertEqual(state["metrics"]["n_orders"], 0)
                finally:
                    runtime.shutdown()

    def test_publication_grace_never_accepts_a_second_missing_closed_bar(self):
        self.configure_fast(source="binance")
        boundary = int(datetime(2026, 11, 8, 4, tzinfo=timezone.utc).timestamp() * 1000)
        interval = TIMEFRAME_MS["4h"]
        bars = self.bullish_bars_through(boundary - 3 * interval)
        with patch("tradingbot.automation.MarketData.get_candles", return_value=bars), \
                patch("tradingbot.automation.MarketData.get_price", return_value=150):
            self.runtime.control("start")
            state = self.runtime.tick_once(now_ms=boundary + 30_000)
        self.assertEqual(state["status"]["error"], "market_data_stale")

    def test_publication_grace_does_not_span_a_whole_short_candle_interval(self):
        self.configure_fast(source="binance", timeframe="1m")
        interval = TIMEFRAME_MS["1m"]
        boundary = 1_783_008_000_000 // interval * interval
        prices = [100] * 299 + [101]
        bars = [Candle(boundary - (301 - i) * interval, p, p, p, p, 1)
                for i, p in enumerate(prices)]
        self.runtime.control("start")
        with patch("tradingbot.automation.MarketData.get_candles", return_value=bars), \
                patch("tradingbot.automation.MarketData.get_price", return_value=150):
            state = self.runtime.tick_once(now_ms=boundary + 10_000)
        self.assertEqual(state["metrics"]["n_orders"], 0)
        self.assertEqual(state["status"]["error"], "market_data_stale")
        self.assertEqual(state["metrics"]["n_orders"], 0)
        self.assertEqual(state["portfolio"]["quantity"], 0)

    def test_public_market_data_is_used_but_orders_remain_simulated(self):
        self.configure_fast(source="binance")
        now_ms = int(time.time() * 1000)
        with patch("tradingbot.automation.MarketData.get_candles", return_value=self.market_bars(now_ms)), \
                patch("tradingbot.automation.MarketData.get_price", return_value=101):
            self.runtime.control("start")
            state = self.runtime.tick_once(now_ms=now_ms)
        self.assertEqual(state["status"]["source"], "binance")
        self.assertEqual(state["status"]["mode"], "simulation")
        self.assertEqual(state["metrics"]["n_filled"], 1)
        self.assertGreater(state["portfolio"]["quantity"], 0)

    def test_snapshot_and_decision_news_context_are_bounded_without_article_bodies(self):
        self.configure_fast(source="binance", news_policy="require-fresh")
        now_ms = int(time.time() * 1000)
        with TradingMemory(self.path) as memory, memory.transaction():
            for number in range(80):
                memory.insert_news({
                    "id": f"news-{number:03d}", "url": f"https://example.com/{number}",
                    "title": "A timestamped market report", "body": "Long untrusted article body " * 100,
                    "source": "example.com", "published_at_ms": now_ms - 1000,
                    "observed_at_ms": now_ms - 500, "assets": ["BTC"],
                })
        self.runtime.control("start")
        with patch("tradingbot.automation.MarketData.get_candles", return_value=self.market_bars(now_ms)), \
                patch("tradingbot.automation.MarketData.get_price", return_value=101):
            state = self.runtime.tick_once(now_ms=now_ms)
        self.assertEqual(state["metrics"]["n_filled"], 1)
        self.assertEqual(len(state["news"]), 50)
        self.assertTrue(all("body" not in article for article in state["news"]))
        self.assertEqual(len(state["decisions"][0]["news_ids"]), 50)

    def test_repeated_price_polls_on_same_closed_bar_do_not_duplicate_decisions_or_orders(self):
        self.configure_fast(source="binance")
        now_ms = int(time.time() * 1000)
        with patch("tradingbot.automation.MarketData.get_candles", return_value=self.market_bars(now_ms)), \
                patch("tradingbot.automation.MarketData.get_price", return_value=101):
            self.runtime.control("start")
            first = self.runtime.tick_once(now_ms=now_ms)
            repeated = self.runtime.tick_once(now_ms=now_ms + 1000)
            repeated = self.runtime.tick_once(now_ms=now_ms + 2000)
        self.assertEqual(first["metrics"]["n_orders"], 1)
        self.assertEqual(repeated["metrics"]["n_orders"], 1)
        self.assertEqual(len(repeated["decisions"]), len(first["decisions"]))
        self.assertEqual(repeated["portfolio"]["quantity"], first["portfolio"]["quantity"])

    def test_capped_exit_dust_does_not_block_a_later_new_entry_or_erase_quantity(self):
        self.configure_fast(source="binance", max_drawdown=1)
        self.runtime.control("start")
        interval = TIMEFRAME_MS["4h"]
        origin = int(time.time() * 1000) // interval * interval - 400 * interval
        prices = [100] * 299 + [101, 110, 108, 106, 104, 106, 110, 112]
        bars = [Candle(origin + i * interval, p, p, p, p, 1) for i, p in enumerate(prices)]

        def tick(end):
            with patch("tradingbot.automation.MarketData.get_candles", return_value=bars[:end]), \
                    patch("tradingbot.automation.MarketData.get_price", return_value=prices[end - 1]):
                return self.runtime.tick_once(now_ms=bars[end - 1].ts + interval)

        for end in range(300, 305):
            dust = tick(end)
        self.assertEqual(dust["metrics"]["n_filled"], 2)
        self.assertGreater(dust["portfolio"]["quantity"], 0)
        self.assertLess(dust["portfolio"]["position_value"], 5)
        dust_quantity = dust["portfolio"]["quantity"]
        self.runtime.control("pause")
        tick(305)
        paused = tick(306)
        self.assertEqual(paused["metrics"]["n_filled"], 2)
        self.assertEqual(paused["portfolio"]["quantity"], dust_quantity)
        self.runtime.control("start")
        reentered = tick(307)
        buys = [o for o in reentered["orders"] if o["intent"]["side"] == "BUY"]
        self.assertEqual(len(buys), 2)
        latest_buy = max(buys, key=lambda o: float(o["intent"]["limit_price"]))
        self.assertAlmostEqual(reentered["portfolio"]["quantity"],
                               dust_quantity + float(latest_buy["snapshot"]["executed_quantity"]))
        for order in buys:
            self.assertLessEqual(float(order["intent"]["quantity"]) * float(order["intent"]["limit_price"]), 75)

    def test_valid_crash_ticker_triggers_risk_sale_despite_stale_candle_history(self):
        self.configure_fast(source="binance", strategy="buy_hold", params={})
        now_ms = int(time.time() * 1000)
        bars = self.market_bars(now_ms)
        with patch("tradingbot.automation.MarketData.get_candles", return_value=bars), \
                patch("tradingbot.automation.MarketData.get_price", return_value=101):
            self.runtime.control("start")
            entered = self.runtime.tick_once(now_ms=now_ms)
        self.assertGreater(entered["portfolio"]["quantity"], 0)
        interval = TIMEFRAME_MS["4h"]
        stale = [Candle(c.ts - 4 * interval, c.open, c.high, c.low, c.close, c.volume) for c in bars]
        with patch("tradingbot.automation.MarketData.get_candles", return_value=stale), \
                patch("tradingbot.automation.MarketData.get_price", return_value=50):
            halted = self.runtime.tick_once(now_ms=now_ms + 1000)
        self.assertEqual(halted["status"]["state"], "halted")
        self.assertFalse(halted["status"]["running"])
        self.assertEqual(halted["portfolio"]["quantity"], 0)
        self.assertEqual(halted["metrics"]["n_filled"], 2)
        self.assertTrue(any(o["intent"]["side"] == "SELL" and o["status"] == "FILLED" for o in halted["orders"]))

    def test_bad_or_stale_market_data_cannot_create_an_order(self):
        self.configure_fast(source="binance")
        now_ms = int(time.time() * 1000)
        bars = self.market_bars(now_ms)
        interval = TIMEFRAME_MS["4h"]
        stale = [Candle(c.ts - 4 * interval, c.open, c.high, c.low, c.close, c.volume) for c in bars]
        future = bars + [Candle(now_ms // interval * interval, 102, 102, 102, 102, 1)]
        cases = [(stale, 101), (future, 101), (bars, float("nan"))]
        self.runtime.control("start")
        for candles, price in cases:
            with self.subTest(price=price, last_timestamp=candles[-1].ts):
                self.runtime.control("start")
                with patch("tradingbot.automation.MarketData.get_candles", return_value=candles), \
                        patch("tradingbot.automation.MarketData.get_price", return_value=price):
                    state = self.runtime.tick_once(now_ms=now_ms)
                self.assertTrue(state["status"]["error"])
                self.assertEqual(state["metrics"]["n_orders"], 0)
                json.dumps(state, allow_nan=False)
        self.runtime.control("start")
        with patch("tradingbot.automation.MarketData.get_candles", side_effect=RuntimeError("https://secret.invalid?token=hidden")):
            state = self.runtime.tick_once(now_ms=now_ms)
        self.assertTrue(state["status"]["error"])
        self.assertNotIn("secret.invalid", state["status"]["error"])
        self.assertNotIn("hidden", state["status"]["error"])
        self.assertTrue(state["status"]["desired_running"])
        self.assertFalse(state["status"]["running"])
        self.assertFalse(self.runtime.control("pause")["status"]["desired_running"])


if __name__ == "__main__":
    unittest.main()
