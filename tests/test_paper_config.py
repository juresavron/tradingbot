import json
import tempfile
import unittest
from pathlib import Path

from tradingbot.candles import Candle
from tradingbot.paper import PaperTrader
from tradingbot.strategy import BuyHold, DonchianBreakout, RsiRevert, SmaCross


class TestPaperExperimentConfig(unittest.TestCase):
    def make_trader(self, path, **overrides):
        settings = {
            "client": None,
            "symbol": "BTC/EUR",
            "timeframe": "4h",
            "strategy": SmaCross(),
            "state_path": path,
            "starting_cash": 300.0,
        }
        settings.update(overrides)
        return PaperTrader(**settings)

    def persist_account(self, path):
        trader = self.make_trader(path)
        candles = [Candle(i * 14_400_000, 100, 100, 100, 100, 1) for i in range(3)]
        trader.step(100, candles, 43_200_000)
        return trader

    def test_matching_settings_resume_preserves_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.persist_account(path)
            saved_bytes = path.read_bytes()
            resumed = self.make_trader(path)
            self.assertEqual(resumed.state, trader.state)
            self.assertEqual(path.read_bytes(), saved_bytes)
            config = json.loads(saved_bytes)["experiment_config"]
            self.assertEqual(config["starting_cash"], 300)
            self.assertEqual(config["fee"], 0.001)
            self.assertEqual(config["strategy"], SmaCross().describe())

    def test_changed_settings_rejected_without_overwriting_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.persist_account(path)
            saved_bytes = path.read_bytes()
            changes = [
                {"strategy": BuyHold()},
                {"strategy": SmaCross(fast=10, slow=50)},
                {"starting_cash": 301},
                {"fee": 0.002},
                {"slippage": 0.001},
                {"size": 0.5},
                {"max_drawdown_stop": 0.1},
                {"lookback": 500},
                {"timeframe": "1h"},
                {"symbol": "ETH/EUR"},
            ]
            for change in changes:
                with self.subTest(change=change):
                    with self.assertRaisesRegex(ValueError, "--state"):
                        self.make_trader(path, **change)
                    self.assertEqual(path.read_bytes(), saved_bytes)

    def test_legacy_or_incomplete_config_is_not_guessed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.persist_account(path)
            state = json.loads(path.read_text())
            for config in (None, {"fee": 0.001}):
                with self.subTest(config=config):
                    if config is None:
                        state.pop("experiment_config", None)
                    else:
                        state["experiment_config"] = config
                    path.write_text(json.dumps(state))
                    saved_bytes = path.read_bytes()
                    with self.assertRaisesRegex(ValueError, "cannot be verified"):
                        self.make_trader(path)
                    self.assertEqual(path.read_bytes(), saved_bytes)

    def test_invalid_configuration_fails_before_creating_state(self):
        invalid = [
            {"starting_cash": 0}, {"starting_cash": -1},
            {"starting_cash": float("nan")}, {"starting_cash": float("inf")},
            {"fee": -0.01}, {"fee": 1}, {"fee": float("nan")},
            {"slippage": -0.01}, {"slippage": 1}, {"slippage": float("inf")},
            {"size": 0}, {"size": 1.1}, {"size": float("nan")},
            {"max_drawdown_stop": 0}, {"max_drawdown_stop": 1.1},
            {"max_drawdown_stop": float("nan")},
            {"lookback": 0}, {"lookback": 1001}, {"lookback": 3.5},
            {"lookback": True}, {"timeframe": "2h"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            for settings in invalid:
                with self.subTest(settings=settings):
                    with self.assertRaises(ValueError):
                        self.make_trader(path, **settings)
                    self.assertFalse(path.exists())


class TestRetainedSignalHistory(unittest.TestCase):
    INTERVAL = 14_400_000

    def candles(self, prices):
        return [Candle(i * self.INTERVAL, p, p, p, p, 1) for i, p in enumerate(prices)]

    def trader(self, path, strategy):
        return PaperTrader(
            client=None, symbol="BTC/EUR", timeframe="4h", strategy=strategy,
            state_path=path, starting_cash=300, fee=0, slippage=0,
        )

    def test_sliding_snapshot_preserves_longstanding_rsi_and_breakout_entries(self):
        cases = [
            (RsiRevert, list(range(120, 99, -1)) + [100] * 580),
            (DonchianBreakout, [100] * 120 + [110] * 481),
        ]
        for strategy_cls, prices in cases:
            with self.subTest(strategy=strategy_cls.__name__), tempfile.TemporaryDirectory() as tmp:
                candles = self.candles(prices)
                path = Path(tmp) / "state.json"
                trader = self.trader(path, strategy_cls())
                for end in (300, 450, 600):
                    history = candles[:end]
                    window = history[-300:]
                    trader.step(prices[end - 1], window, end * self.INTERVAL)
                    self.assertEqual(trader.strategy.signals(history)[-1], 1)
                    self.assertGreater(trader.state["qty"], 0)
                    self.assertEqual(len(trader.state["signal_history"]), end)
                # A fresh evaluation of only the sliding window loses the entry.
                self.assertEqual(strategy_cls().signals(candles[300:600])[-1], 0)
                self.assertEqual(len(trader.state["trades"]), 1)
                # Restart retains the same seed and the final entry from before
                # the current API window, rather than adopting a new flat stance.
                resumed = self.trader(path, strategy_cls())
                resumed.step(prices[600], candles[301:601], 601 * self.INTERVAL)
                self.assertEqual(len(resumed.state["signal_history"]), 601)
                self.assertGreater(resumed.state["qty"], 0)
                self.assertEqual(len(resumed.state["trades"]), 1)

    def test_idle_polls_do_not_grow_history_or_rewrite_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.trader(path, BuyHold())
            candles = self.candles([100] * 300)
            trader.step(100, candles, 300 * self.INTERVAL)
            before = path.read_bytes()
            for _ in range(3):
                trader.step(100, candles, 300 * self.INTERVAL + 1000)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(trader.state["signal_history"]), 300)

    def test_conflicts_gaps_and_unclosed_bars_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.trader(path, BuyHold())
            candles = self.candles([100] * 10)
            trader.step(100, candles[:5], 5 * self.INTERVAL)
            before = path.read_bytes()
            account_before = json.loads(before)
            conflicting = Candle(4 * self.INTERVAL, 101, 101, 101, 101, 1)
            checks = [
                ([conflicting, candles[5]], 6 * self.INTERVAL, "Conflicting"),
                (candles[7:9], 9 * self.INTERVAL, "gap"),
                (candles[3:6], 5 * self.INTERVAL, "closed"),
            ]
            for snapshot, now_ms, message in checks:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        trader.step(101, snapshot, now_ms)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(trader.state, account_before)
                    self.assertEqual(len(trader.state["signal_history"]), 5)

    def test_invalid_prices_preserve_in_memory_and_saved_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.trader(path, BuyHold())
            candles = self.candles([100] * 5)
            trader.step(100, candles, 5 * self.INTERVAL)
            before = path.read_bytes()
            account_before = json.loads(before)
            for price in (0, -1, float("nan"), float("inf")):
                with self.subTest(price=price):
                    with self.assertRaisesRegex(ValueError, "price must be finite"):
                        trader.step(price, candles, 5 * self.INTERVAL)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(trader.state, account_before)

    def test_kill_switch_flattens_despite_bad_history_and_never_reopens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.trader(path, BuyHold())
            trader.step(100, self.candles([100] * 5), 5 * self.INTERVAL)
            # This snapshot both conflicts with closed history and has a gap.
            # Neither problem can defer a kill triggered by a valid ticker.
            bad = [Candle(4 * self.INTERVAL, 70, 70, 70, 70, 1),
                   Candle(8 * self.INTERVAL, 70, 70, 70, 70, 1)]
            result = trader.step(70, bad, 9 * self.INTERVAL)
            self.assertTrue(result["killed"])
            self.assertEqual(trader.state["qty"], 0)
            self.assertEqual(trader.state["trades"][-1]["reason"], "kill_switch")
            before = path.read_bytes()
            resumed = self.trader(path, BuyHold())
            result = resumed.step(100, self.candles([100] * 10), 10 * self.INTERVAL)
            self.assertTrue(result["killed"])
            self.assertFalse(result["acted"])
            self.assertEqual(resumed.state["qty"], 0)
            self.assertEqual(path.read_bytes(), before)

    def test_identical_overlap_is_deduplicated_and_new_bars_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.trader(path, BuyHold())
            candles = self.candles([100] * 6)
            trader.step(100, candles[:5], 5 * self.INTERVAL)
            trader.step(100, candles[3:] + [candles[-1]], 6 * self.INTERVAL)
            self.assertEqual(len(trader.state["signal_history"]), 6)
            resumed = self.trader(path, BuyHold())
            self.assertEqual(resumed.state["signal_history"], trader.state["signal_history"])

    def test_processed_account_missing_history_is_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.trader(path, BuyHold())
            trader.step(100, self.candles([100] * 5), 5 * self.INTERVAL)
            state = json.loads(path.read_text())
            for history in (None, []):
                with self.subTest(history=history):
                    if history is None:
                        state.pop("signal_history", None)
                    else:
                        state["signal_history"] = history
                    path.write_text(json.dumps(state))
                    before = path.read_bytes()
                    with self.assertRaisesRegex(ValueError, "--state"):
                        self.trader(path, BuyHold())
                    self.assertEqual(path.read_bytes(), before)

    def test_unprocessed_account_missing_history_can_seed_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            trader = self.trader(path, BuyHold())
            state = trader.state.copy()
            state.pop("signal_history")
            path.write_text(json.dumps(state))
            resumed = self.trader(path, BuyHold())
            resumed.step(100, self.candles([100] * 5), 5 * self.INTERVAL)
            self.assertEqual(len(resumed.state["signal_history"]), 5)


if __name__ == "__main__":
    unittest.main()
