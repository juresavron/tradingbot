import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tradingbot.paper import PaperTrader
from tradingbot.strategy import Strategy
from tradingbot.webreport import EQUITY_TAIL, WebReporter

HOUR = 3_600_000

ENV_KEYS = [
    "TRADINGBOT_WEB_URL",
    "TRADINGBOT_WEB_ANON_KEY",
    "TRADINGBOT_WEB_RUN_ID",
    "TRADINGBOT_WEB_RUN_KEY",
]


class RecordingReporter(WebReporter):
    def __init__(self):
        super().__init__("https://example.supabase.co", "anon", "run-id", "run-key")
        self.payloads = []

    def _post(self, payload):
        self.payloads.append(payload)


class FailingReporter(WebReporter):
    def __init__(self):
        super().__init__("https://example.supabase.co", "anon", "run-id", "run-key")

    def _post(self, payload):
        raise OSError("network down")


def sample_state(n_equity=3):
    return {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "strategy": "sma_cross(fast=20, slow=50)",
        "starting_cash": 100.0,
        "killed": False,
        "equity_log": [[i * HOUR, 100.0 + i, 50_000.0 + i] for i in range(n_equity)],
        "trades": [
            {"ts": HOUR, "side": "BUY", "price": 50_000.0, "qty": 0.002, "reason": "signal"},
            {"ts": 2 * HOUR, "side": "SELL", "price": 50_500.0, "qty": 0.002, "pnl": 1.0, "reason": "signal"},
        ],
    }


class TestFromEnv(unittest.TestCase):
    def test_missing_env_disables_reporting(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ENV_KEYS:
                os.environ.pop(key, None)
            self.assertIsNone(WebReporter.from_env())

    def test_all_env_enables_reporting(self):
        env = {k: f"value-{k}" for k in ENV_KEYS}
        env["TRADINGBOT_WEB_URL"] = "https://example.supabase.co/"
        with mock.patch.dict(os.environ, env):
            reporter = WebReporter.from_env()
        self.assertIsNotNone(reporter)
        self.assertEqual(reporter.endpoint, "https://example.supabase.co/rest/v1/rpc/ingest")


class TestPayload(unittest.TestCase):
    def test_payload_shape(self):
        reporter = RecordingReporter()
        payload = reporter.build_payload(sample_state())
        self.assertEqual(payload["p_run_id"], "run-id")
        self.assertEqual(payload["p_key"], "run-key")
        self.assertEqual(payload["p_equity"][0], [0, 100.0, 50_000.0])
        self.assertEqual(payload["p_trades"][0]["seq"], 0)
        self.assertEqual(payload["p_trades"][1]["seq"], 1)
        self.assertEqual(payload["p_trades"][1]["pnl"], 1.0)
        self.assertEqual(payload["p_meta"]["symbol"], "BTC/USDT")

    def test_old_two_element_equity_entries_are_accepted(self):
        state = sample_state()
        state["equity_log"] = [[0, 100.0], [HOUR, 101.0]]
        payload = RecordingReporter().build_payload(state)
        self.assertEqual(payload["p_equity"], [[0, 100.0, None], [HOUR, 101.0, None]])

    def test_equity_is_tail_limited(self):
        payload = RecordingReporter().build_payload(sample_state(n_equity=EQUITY_TAIL + 50))
        self.assertEqual(len(payload["p_equity"]), EQUITY_TAIL)

    def test_push_failure_is_swallowed(self):
        reporter = FailingReporter()
        self.assertFalse(reporter.push(sample_state()))
        self.assertEqual(reporter.consecutive_failures, 1)

    def test_push_success_resets_failures(self):
        reporter = RecordingReporter()
        reporter.consecutive_failures = 5
        self.assertTrue(reporter.push(sample_state()))
        self.assertEqual(reporter.consecutive_failures, 0)


class AlwaysLong(Strategy):
    name = "always_long"

    def signals(self, candles):
        return [1] * len(candles)


class TestPaperIntegration(unittest.TestCase):
    def make_trader(self, tmp, reporter):
        return PaperTrader(
            client=None,
            symbol="BTC/USDT",
            timeframe="1h",
            strategy=AlwaysLong(),
            state_path=Path(tmp) / "state.json",
            starting_cash=100.0,
            fee=0.0,
            slippage=0.0,
            reporter=reporter,
        )

    def candles(self, n, price=100.0):
        from tradingbot.candles import Candle

        return [Candle(ts=i * HOUR, open=price, high=price, low=price, close=price, volume=1.0) for i in range(n)]

    def test_trade_triggers_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            reporter = RecordingReporter()
            trader = self.make_trader(tmp, reporter)
            trader.step(price=100.0, candles=self.candles(5), now_ms=99 * HOUR)
            self.assertEqual(len(reporter.payloads), 1)
            self.assertEqual(reporter.payloads[0]["p_trades"][0]["side"], "BUY")

    def test_idle_poll_does_not_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            reporter = RecordingReporter()
            trader = self.make_trader(tmp, reporter)
            trader.step(price=100.0, candles=self.candles(5), now_ms=99 * HOUR)
            trader.step(price=100.0, candles=self.candles(5), now_ms=99 * HOUR + 30_000)
            self.assertEqual(len(reporter.payloads), 1)

    def test_reporter_failure_does_not_break_trading(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader = self.make_trader(tmp, FailingReporter())
            status = trader.step(price=100.0, candles=self.candles(5), now_ms=99 * HOUR)
            self.assertTrue(status["acted"])
            self.assertGreater(trader.state["qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
