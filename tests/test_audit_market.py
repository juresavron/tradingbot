"""Causal timestamp, I/O isolation and documented entry-policy regressions."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tradingbot.automation import AutomationRuntime
from tradingbot.candles import Candle, TIMEFRAME_MS
from tradingbot.exchange import MarketData, MarketQuote
from tradingbot.execution import ExecutionEngine
from tradingbot.market_io import BoundedMarketCall
try:
    from tradingbot.server import operational_readiness
except ImportError:
    operational_readiness = None


class TestAuditMarket(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.runtime = AutomationRuntime(self.path, start_worker=False)
        self.now = int(time.time() * 1000)
        self.interval = TIMEFRAME_MS["4h"]

    def tearDown(self):
        self.runtime.shutdown()
        self.temp.cleanup()

    def bars(self, prices, now=None):
        end = (now or self.now) // self.interval * self.interval
        return [Candle(end - (len(prices)-i)*self.interval, p, p, p, p, 1)
                for i, p in enumerate(prices)]

    def test_quote_client_keeps_exchange_event_time_and_rejects_unknown_time(self):
        client = MarketData()
        with patch.object(client, "_get", return_value=[{"p": "105.25", "T": self.now}]) as request:
            quote = client.get_quote("BTC/EUR")
        request.assert_called_once_with("/api/v3/aggTrades", {"symbol": "BTCEUR", "limit": 1})
        self.assertEqual(quote.price, 105.25)
        self.assertEqual(quote.event_ms, self.now)
        for response in ([], [{"p": "105"}], [{"p": "nan", "T": self.now}],
                         [{"p": "105", "T": True}], [{"p": True, "T": self.now}], {"price": "105"}):
            with self.subTest(response=response), patch.object(client, "_get", return_value=response):
                with self.assertRaisesRegex(ValueError, "market_quote_invalid"):
                    client.get_quote("BTC/EUR")

    def test_stale_unknown_skewed_and_late_receipts_never_allow_entry(self):
        cases = [(MarketQuote(100, self.now - 60_001, self.now), "market_quote_stale"),
                 (MarketQuote(100, None, self.now), "market_quote_unknown_time"),
                 (MarketQuote(100, self.now + 2_001, self.now), "market_quote_clock_skew"),
                 (MarketQuote(100, self.now - 4000, self.now - 3001), "market_quote_stale")]
        for index, (quote, code) in enumerate(cases):
            with self.subTest(code=code):
                runtime = AutomationRuntime(Path(self.temp.name)/f"bad-{index}.sqlite3", start_worker=False)
                try:
                    runtime.configure({"source": "binance", "strategy": "buy_hold", "params": {}})
                    runtime.control("start")
                    with patch("tradingbot.automation.MarketData.get_quote", return_value=quote), \
                            patch("tradingbot.automation.MarketData.get_candles") as history:
                        state = runtime.tick_once(now_ms=self.now)
                    self.assertEqual(state["status"]["error"], code)
                    self.assertEqual(state["metrics"]["n_orders"], 0)
                    history.assert_not_called()
                finally:
                    runtime.shutdown()

    @unittest.skipIf(operational_readiness is None, "Optional server dependencies are not installed")
    def test_readiness_distinguishes_receipt_from_exchange_event_age(self):
        from types import SimpleNamespace
        service = SimpleNamespace(worker_enabled=True, worker_alive=True, poll_seconds=5)
        base = {"config": {"source": "binance"}, "orders": [],
                "status": {"heartbeat_ms": self.now, "market_time_ms": self.now,
                           "quote_received_ms": self.now, "price": 100}}
        for event_ms, expected in ((None, None), (self.now - 60_001, False), (self.now, True)):
            with self.subTest(event_ms=event_ms), patch("tradingbot.server.time.time", return_value=self.now/1000):
                base["status"]["price_event_ms"] = event_ms
                result = operational_readiness(service, base)
                self.assertEqual(result["price_event_fresh"], expected)
                self.assertTrue(result["quote_received_fresh"])
                self.assertEqual(result["ready"], expected is True)

    def test_a_decreasing_event_time_is_rejected(self):
        self.runtime.configure({"source": "binance", "strategy": "buy_hold", "params": {}})
        self.runtime.control("start")
        with patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(100, self.now, self.now)), \
                patch("tradingbot.automation.MarketData.get_candles", return_value=self.bars([100]*300)):
            self.assertEqual(self.runtime.tick_once(now_ms=self.now)["metrics"]["n_filled"], 1)
        with patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(99, self.now-1, self.now+1)):
            state = self.runtime.tick_once(now_ms=self.now+1)
        self.assertEqual(state["status"]["error"], "market_quote_out_of_order")
        self.assertEqual(state["metrics"]["n_orders"], 1)

    def test_quote_expiring_during_preflight_is_canceled_without_execution(self):
        self.runtime.configure({"source": "binance", "strategy": "buy_hold", "params": {}})
        self.runtime.control("start")
        clock = [self.now]
        submit = ExecutionEngine.submit

        def delayed(engine, client_order_id, **kwargs):
            clock[0] += 4000
            return submit(engine, client_order_id, **kwargs)

        with patch("tradingbot.automation._now", side_effect=lambda: clock[0]), \
                patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(100, self.now, self.now)), \
                patch("tradingbot.automation.MarketData.get_candles", return_value=self.bars([100]*300)), \
                patch.object(ExecutionEngine, "submit", delayed), \
                patch("tradingbot.automation.SimulatedBroker.place_order", side_effect=AssertionError("Expired entry")):
            state = self.runtime.tick_once()
        self.assertEqual(state["orders"][0]["status"], "CANCELED")
        self.assertEqual(state["status"]["last_reason"], "market_quote_expired_before_submission")
        self.assertEqual(state["portfolio"]["cash"], 300)

    def test_slow_history_is_bounded_and_cannot_delay_next_risk_exit(self):
        self.runtime.configure({"source": "binance", "strategy": "buy_hold", "params": {}})
        self.runtime.control("start")
        with patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(100, self.now, self.now)), \
                patch("tradingbot.automation.MarketData.get_candles", return_value=self.bars([100]*300)):
            self.assertEqual(self.runtime.tick_once(now_ms=self.now)["metrics"]["n_filled"], 1)
        later = self.now + self.interval
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow_history(*args, **kwargs):
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("Test history was not released")
                return self.bars([100]*300, later)
            finally:
                finished.set()

        try:
            with patch("tradingbot.automation.MarketData.get_candles", side_effect=slow_history) as history:
                with patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(100, later, later)):
                    timeout = self.runtime.tick_once(now_ms=later)
                self.assertTrue(entered.is_set())
                self.assertEqual(timeout["status"]["error"], "market_request_timeout")
                self.assertFalse(release.is_set())
                with patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(40, later+1, later+1)):
                    started = time.monotonic()
                    closed = self.runtime.tick_once(now_ms=later+1)
                self.assertLess(time.monotonic()-started, 0.5)
                self.assertEqual(closed["status"]["state"], "halted")
                self.assertEqual(closed["portfolio"]["quantity"], 0)
                self.assertEqual(closed["metrics"]["n_filled"], 2)
                self.assertEqual(history.call_count, 1)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_idle_polls_reuse_closed_history_without_recomputing_signals(self):
        self.runtime.configure({"source": "binance", "strategy": "buy_hold", "params": {}})
        self.runtime.control("start")
        with patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(100, self.now, self.now)), \
                patch("tradingbot.automation.MarketData.get_candles", return_value=self.bars([100]*300)) as history:
            self.runtime.tick_once(now_ms=self.now)
            with patch.object(self.runtime, "_strategy", side_effect=AssertionError("No new bar")):
                repeated = self.runtime.tick_once(now_ms=self.now+1000)
            self.assertEqual(history.call_count, 1)
        self.assertEqual(len(repeated["decisions"]), 1)

    def test_join_current_target_is_explicit_for_startup_and_restart(self):
        windows = [([100]*120, 0), ([100]*120+[110], 1),
                   ([100]*120+[110]+[105]*5, 1), ([100]*120+[110]+[105]*5+[99], 0)]
        for index, (prices, filled) in enumerate(windows):
            with self.subTest(window=index):
                path = Path(self.temp.name)/f"policy-{index}.sqlite3"
                runtime = AutomationRuntime(path, start_worker=False)
                try:
                    runtime.configure({"source": "binance", "params": {"entry_period": 120, "exit_period": 60}})
                    runtime.control("start")
                    with patch("tradingbot.automation.MarketData.get_quote", return_value=MarketQuote(prices[-1], self.now, self.now)), \
                            patch("tradingbot.automation.MarketData.get_candles", return_value=self.bars(prices)):
                        before = runtime.tick_once(now_ms=self.now)
                        runtime.control("pause")
                        runtime.shutdown()
                        runtime = AutomationRuntime(path, start_worker=False)
                        runtime.control("start")
                        after = runtime.tick_once(now_ms=self.now+1)
                    self.assertEqual(before["metrics"]["n_filled"], filled)
                    self.assertEqual(after["metrics"]["n_filled"], filled)
                    self.assertEqual(after["status"]["entry_policy"], "join-current-target")
                finally:
                    runtime.shutdown()


class TestBoundedRequests(unittest.TestCase):
    def test_timed_out_lane_does_not_spawn_requests_and_late_quotes_are_discarded(self):
        lane = BoundedMarketCall()
        release, finished = threading.Event(), threading.Event()
        calls = []

        def blocked():
            calls.append(1)
            try:
                release.wait(2)
                return "late quote"
            finally:
                finished.set()

        try:
            with self.assertRaisesRegex(ValueError, "timeout"):
                lane.run("quote", blocked, .02)
            for _ in range(5):
                with self.assertRaisesRegex(ValueError, "pending"):
                    lane.run("quote", blocked, .02)
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertTrue(lane._job["done"].wait(1))
        self.assertEqual(lane.run("quote", lambda: "new quote", .1), "new quote")


if __name__ == "__main__":
    unittest.main()
