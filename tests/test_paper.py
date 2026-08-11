import json
import tempfile
import unittest
from pathlib import Path

from tradingbot.candles import Candle
from tradingbot.paper import PaperTrader
from tradingbot.strategy import Strategy

HOUR = 3_600_000


class AlwaysLong(Strategy):
    name = "always_long"

    def signals(self, candles):
        return [1] * len(candles)


class AlwaysFlat(Strategy):
    name = "always_flat"

    def signals(self, candles):
        return [0] * len(candles)


def candles_upto(n: int, price: float = 100.0) -> list[Candle]:
    return [Candle(ts=i * HOUR, open=price, high=price, low=price, close=price, volume=1.0) for i in range(n)]


def make_trader(tmpdir: str, strategy: Strategy, **kwargs) -> PaperTrader:
    return PaperTrader(
        client=None,  # tests drive step() directly; the network client is never used
        symbol="BTC/USDT",
        timeframe="1h",
        strategy=strategy,
        state_path=Path(tmpdir) / "state.json",
        starting_cash=100.0,
        **kwargs,
    )


class TestPaperTrader(unittest.TestCase):
    def test_buys_on_new_candle_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader = make_trader(tmp, AlwaysLong(), fee=0.0, slippage=0.0)
            status = trader.step(price=100.0, candles=candles_upto(5), now_ms=99 * HOUR)
            self.assertTrue(status["acted"])
            self.assertGreater(trader.state["qty"], 0.0)
            self.assertAlmostEqual(status["equity"], 100.0)

    def test_no_retrade_on_same_candle(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader = make_trader(tmp, AlwaysLong(), fee=0.0, slippage=0.0)
            trader.step(price=100.0, candles=candles_upto(5), now_ms=99 * HOUR)
            trades_before = len(trader.state["trades"])
            status = trader.step(price=101.0, candles=candles_upto(5), now_ms=100 * HOUR)
            self.assertFalse(status["acted"])
            self.assertEqual(len(trader.state["trades"]), trades_before)

    def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader = make_trader(tmp, AlwaysLong(), fee=0.0, slippage=0.0)
            trader.step(price=100.0, candles=candles_upto(5), now_ms=99 * HOUR)
            qty = trader.state["qty"]

            reloaded = make_trader(tmp, AlwaysLong(), fee=0.0, slippage=0.0)
            self.assertEqual(reloaded.state["qty"], qty)
            self.assertEqual(len(reloaded.state["trades"]), 1)

    def test_kill_switch_flattens_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader = make_trader(tmp, AlwaysLong(), fee=0.0, slippage=0.0, max_drawdown_stop=0.25)
            trader.step(price=100.0, candles=candles_upto(5), now_ms=99 * HOUR)
            # Price collapses 30% below entry -> drawdown past the 25% limit.
            status = trader.step(price=70.0, candles=candles_upto(6, price=70.0), now_ms=105 * HOUR)
            self.assertTrue(status["killed"])
            self.assertEqual(trader.state["qty"], 0.0)
            self.assertTrue(trader.state["killed"])

    def test_costs_are_charged(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader = make_trader(tmp, AlwaysLong(), fee=0.001, slippage=0.0005)
            status = trader.step(price=100.0, candles=candles_upto(5), now_ms=99 * HOUR)
            self.assertLess(status["equity"], 100.0)

    def test_state_file_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            trader = make_trader(tmp, AlwaysFlat())
            trader.step(price=100.0, candles=candles_upto(5), now_ms=99 * HOUR)
            with open(Path(tmp) / "state.json") as f:
                state = json.load(f)
            self.assertEqual(state["symbol"], "BTC/USDT")
            self.assertEqual(state["qty"], 0.0)


if __name__ == "__main__":
    unittest.main()
