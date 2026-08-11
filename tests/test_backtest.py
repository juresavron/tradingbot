import unittest

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

    def test_buy_hold_strategy_matches_benchmark(self):
        candles = generate(days=90, timeframe="1h", seed=3, end_ms=1_700_000_000_000)
        result = run_backtest(candles, BuyHold(), timeframe="1h")
        m = result.metrics
        self.assertAlmostEqual(m["total_return_pct"], m["bh_return_pct"], delta=0.3)


if __name__ == "__main__":
    unittest.main()
