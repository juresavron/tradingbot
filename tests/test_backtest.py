import unittest
from dataclasses import replace

from tradingbot.backtest import max_drawdown, run_backtest
from tradingbot.candles import Candle
from tradingbot.strategy import BuyHold, SmaCross, Strategy
from tradingbot.synth import generate

HOUR = 3_600_000


def flat_candles(n: int, price: float = 100.0) -> list[Candle]:
    return [Candle(ts=i * HOUR, open=price, high=price, low=price, close=price, volume=1.0) for i in range(n)]


class SignalAt(Strategy):
    """Test helper: go long at a fixed candle index, exit at another."""

    name = "signal_at"

    def __init__(self, enter: int, exit: int | None = None):
        self.enter = enter
        self.exit = exit

    def signals(self, candles):
        out = []
        for i in range(len(candles)):
            on = i >= self.enter and (self.exit is None or i < self.exit)
            out.append(1 if on else 0)
        return out


class FixedSignals(Strategy):
    def __init__(self, values):
        self.values = values

    def signals(self, candles):
        return self.values


class TestInputValidation(unittest.TestCase):
    def test_rejects_invalid_account_and_cost_parameters(self):
        cases = {
            "starting_cash": [0.0, -100.0, float("nan"), float("inf")],
            "fee": [-0.01, 1.0, float("nan"), float("inf")],
            "slippage": [-0.01, 1.0, float("nan"), float("inf")],
            "size": [0.0, -0.1, 1.1, float("nan"), float("inf")],
            "stop_pct": [0.0, -0.1, 1.0, float("nan"), float("inf")],
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(parameter=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        run_backtest(flat_candles(10), BuyHold(), **{name: value})

    def test_rejects_nonfinite_or_nonpositive_prices(self):
        for field in ("open", "high", "low", "close"):
            for value in (0.0, -1.0, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    candles = flat_candles(10)
                    candles[5] = replace(candles[5], **{field: value})
                    with self.assertRaisesRegex(ValueError, "OHLC"):
                        run_backtest(candles, BuyHold())

    def test_rejects_invalid_candle_ranges_and_volumes(self):
        for changes, message in [
            ({"open": 101.0}, "low/high"),
            ({"close": 99.0}, "low/high"),
            ({"low": 101.0}, "low/high"),
            ({"high": 99.0}, "low/high"),
            ({"volume": -1.0}, "volume"),
            ({"volume": float("nan")}, "volume"),
            ({"volume": float("inf")}, "volume"),
        ]:
            with self.subTest(changes=changes):
                candles = flat_candles(10)
                candles[5] = replace(candles[5], **changes)
                with self.assertRaisesRegex(ValueError, message):
                    run_backtest(candles, BuyHold())

    def test_rejects_duplicates_gaps_unsorted_and_nonfinite_timestamps(self):
        for timestamp in (4 * HOUR, 6 * HOUR, 3 * HOUR, float("nan"), float("inf")):
            with self.subTest(timestamp=timestamp):
                candles = flat_candles(10)
                candles[5] = replace(candles[5], ts=timestamp)
                with self.assertRaisesRegex(ValueError, "timestamp"):
                    run_backtest(candles, BuyHold())

    def test_rejects_unknown_or_mismatched_timeframe(self):
        with self.assertRaisesRegex(ValueError, "Unsupported timeframe"):
            run_backtest(flat_candles(10), BuyHold(), timeframe="2h")
        with self.assertRaisesRegex(ValueError, "4h interval"):
            run_backtest(flat_candles(10), BuyHold(), timeframe="4h")

    def test_accepts_matching_timeframe_and_zero_volume(self):
        candles = [replace(c, ts=i * 4 * HOUR, volume=0.0) for i, c in enumerate(flat_candles(10))]
        result = run_backtest(candles, BuyHold(), timeframe="4h", fee=0.0, slippage=0.0)
        self.assertAlmostEqual(result.metrics["final_equity"], 100.0)

    def test_rejects_missing_extra_and_nonbinary_signals(self):
        for signals in ([0] * 9, [0] * 11, [0] * 9 + [2], [0] * 9 + [-1], [0] * 9 + [float("nan")]):
            with self.subTest(signals=signals):
                with self.assertRaisesRegex(ValueError, "one binary signal"):
                    run_backtest(flat_candles(10), FixedSignals(signals))


class TestNoLookahead(unittest.TestCase):
    def test_entry_fills_at_next_open_not_signal_close(self):
        # Candle 5 closes at 100, candle 6 opens (gaps) at 150. A signal at
        # candle 5's close must pay 150, not 100 — you can't buy the past.
        candles = flat_candles(10)
        candles[6] = Candle(ts=6 * HOUR, open=150.0, high=150.0, low=150.0, close=150.0, volume=1.0)
        for i in range(7, 10):
            candles[i] = Candle(ts=i * HOUR, open=150.0, high=150.0, low=150.0, close=150.0, volume=1.0)
        result = run_backtest(candles, SignalAt(enter=5), fee=0.0, slippage=0.0)
        self.assertEqual(len(result.trades), 1)
        self.assertAlmostEqual(result.trades[0].entry_price, 150.0)


class TestCosts(unittest.TestCase):
    def test_round_trip_on_flat_prices_loses_exactly_the_costs(self):
        fee, slip = 0.001, 0.0005
        candles = flat_candles(20)
        result = run_backtest(
            candles, SignalAt(enter=2, exit=10), starting_cash=100.0, fee=fee, slippage=slip
        )
        # Buy at 100*(1+slip) paying fee, sell at 100*(1-slip) paying fee.
        expected = 100.0 / (1 + slip) / (1 + fee) * (1 - slip) * (1 - fee)
        self.assertAlmostEqual(result.metrics["final_equity"], expected, places=6)
        self.assertEqual(result.metrics["n_trades"], 1)

    def test_zero_cost_round_trip_preserves_cash(self):
        candles = flat_candles(20)
        result = run_backtest(candles, SignalAt(enter=2, exit=10), fee=0.0, slippage=0.0)
        self.assertAlmostEqual(result.metrics["final_equity"], 100.0, places=9)


class TestStopLoss(unittest.TestCase):
    def test_stop_triggers_at_stop_price(self):
        candles = flat_candles(20)
        # Price crashes to 80 inside candle 8; a 5% stop must exit at 95.
        candles[8] = Candle(ts=8 * HOUR, open=100.0, high=100.0, low=80.0, close=80.0, volume=1.0)
        for i in range(9, 20):
            candles[i] = Candle(ts=i * HOUR, open=80.0, high=80.0, low=80.0, close=80.0, volume=1.0)
        result = run_backtest(
            candles, SignalAt(enter=2), fee=0.0, slippage=0.0, stop_pct=0.05
        )
        stops = [t for t in result.trades if t.reason == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(stops[0].exit_price, 95.0)

    def test_gap_below_stop_fills_at_open(self):
        candles = flat_candles(20)
        # Candle 8 OPENS at 70, already below the 95 stop: fill is 70, not 95.
        for i in range(8, 20):
            candles[i] = Candle(ts=i * HOUR, open=70.0, high=70.0, low=70.0, close=70.0, volume=1.0)
        result = run_backtest(
            candles, SignalAt(enter=2), fee=0.0, slippage=0.0, stop_pct=0.05
        )
        stops = [t for t in result.trades if t.reason == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(stops[0].exit_price, 70.0)


class TestMaxDrawdown(unittest.TestCase):
    def test_known_curve(self):
        # Peak 120 -> trough 90 is a 25% drawdown.
        self.assertAlmostEqual(max_drawdown([100, 120, 90, 110, 95]), 0.25)

    def test_monotonic_rise_has_zero_drawdown(self):
        self.assertEqual(max_drawdown([1, 2, 3, 4]), 0.0)

    def test_benchmark_drawdown_includes_final_liquidation_costs(self):
        result = run_backtest(flat_candles(10), BuyHold(), fee=0.01, slippage=0.005)
        m = result.metrics
        self.assertAlmostEqual(m["max_drawdown_pct"], -m["total_return_pct"])
        self.assertAlmostEqual(m["bh_max_drawdown_pct"], m["max_drawdown_pct"])


class TestAnnualization(unittest.TestCase):
    def test_short_history_annualization_overflow_is_a_clear_error(self):
        candles = []
        for i in range(100):
            price = 100 * 1.005 ** i
            candles.append(Candle(i * 60_000, price, price, price, price, 1))
        with self.assertRaisesRegex(ValueError, "CAGR cannot be represented.*longer history"):
            run_backtest(candles, BuyHold(), timeframe="1m", fee=0.0, slippage=0.0)

    def test_nonfinite_cagr_percentage_is_rejected(self):
        # The exponentiation itself fits in a float, but multiplying by 100
        # to express the CAGR as a percentage would silently produce infinity.
        last_price = 100 * 1e307 ** (2 / 365)
        prices = [100.0, 100.0, last_price]
        candles = [Candle(i * 24 * HOUR, price, price, price, price, 1) for i, price in enumerate(prices)]
        with self.assertRaisesRegex(ValueError, "CAGR cannot be represented.*longer history"):
            run_backtest(candles, BuyHold(), timeframe="1d", fee=0.0, slippage=0.0)


class TestEndToEnd(unittest.TestCase):
    def test_sma_cross_on_synthetic_data_runs_clean(self):
        candles = generate(days=120, timeframe="1h", seed=7, end_ms=1_700_000_000_000)
        result = run_backtest(candles, SmaCross(fast=20, slow=50), timeframe="1h")
        m = result.metrics
        self.assertGreater(m["final_equity"], 0.0)
        self.assertGreater(m["n_trades"], 0)
        self.assertEqual(len(result.equity), len(candles))
        # All PnL must be realized: cash accounting has to reconcile.
        pnl_sum = sum(t.pnl for t in result.trades)
        self.assertAlmostEqual(m["final_equity"], m["starting_cash"] + pnl_sum, places=6)

    def test_buy_hold_strategy_matches_benchmark_exactly(self):
        # The benchmark uses the same execution rule as strategies (enter at
        # the second candle's open), so buy_hold must equal it to float noise.
        candles = generate(days=90, timeframe="1h", seed=3, end_ms=1_700_000_000_000)
        result = run_backtest(candles, BuyHold(), timeframe="1h")
        m = result.metrics
        self.assertAlmostEqual(m["total_return_pct"], m["bh_return_pct"], places=6)
        self.assertAlmostEqual(m["edge_vs_bh_pct"], 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
