import unittest

from tradingbot.candles import Candle
from tradingbot.strategy import BuyHold, DonchianBreakout, RsiRevert, SmaCross, make_strategy
from tradingbot.synth import generate


def candle(i, close, high=None, low=None):
    return Candle(
        ts=i * 3_600_000,
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=1.0,
    )


class TestDonchianBreakout(unittest.TestCase):
    def test_prior_highs_exclude_current_candle(self):
        candles = [candle(i, 100, high=101, low=99) for i in range(3)]
        # Including the current high would make this valid close breakout
        # impossible: 102 is below this candle's own high of 110.
        candles.append(candle(3, 102, high=110, low=100))
        self.assertEqual(DonchianBreakout(3, 2).signals(candles), [0, 0, 0, 1])

    def test_entry_hysteresis_exit_and_reentry(self):
        candles = [candle(i, 100, high=101, low=99) for i in range(3)]
        candles += [
            candle(3, 102, high=103, low=101),
            candle(4, 101.5, high=102, low=100.5),
            # Exit uses the prior two lows (101 and 100.5), not this low.
            candle(5, 100.25, high=101, low=99),
            candle(6, 101, high=102, low=100.5),
            candle(7, 104, high=105, low=103),
        ]
        self.assertEqual(DonchianBreakout(3, 2).signals(candles), [0, 0, 0, 1, 1, 0, 0, 1])

    def test_window_expires_old_high_and_breakout_is_strict(self):
        candles = [candle(0, 100, high=200, low=99)]
        candles += [candle(i, 100, high=101, low=99) for i in range(1, 4)]
        candles += [candle(4, 101), candle(5, 102)]
        self.assertEqual(DonchianBreakout(3, 2).signals(candles), [0, 0, 0, 0, 0, 1])

    def test_warmup_and_empty_data(self):
        strategy = DonchianBreakout(3, 2)
        self.assertEqual(strategy.signals([]), [])
        self.assertEqual(strategy.signals([candle(i, 100 + i) for i in range(3)]), [0, 0, 0])

    def test_rejects_invalid_periods(self):
        for kwargs in [
            {"entry_period": 0}, {"entry_period": -1}, {"entry_period": 2.5},
            {"entry_period": True}, {"exit_period": 0}, {"exit_period": -1},
            {"exit_period": 1.5}, {"exit_period": False},
            {"entry_period": 3, "exit_period": 3}, {"entry_period": 2, "exit_period": 3},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DonchianBreakout(**kwargs)

    def test_cli_factory_registration(self):
        strategy = make_strategy("donchian_breakout", {"entry_period": "30", "exit_period": "10"})
        self.assertIsInstance(strategy, DonchianBreakout)
        self.assertEqual(strategy.entry_period, 30)
        self.assertEqual(strategy.exit_period, 10)


class TestCausalSignals(unittest.TestCase):
    def test_future_candles_do_not_change_prior_decisions(self):
        candles = generate(days=30, timeframe="4h", seed=7, end_ms=1_700_000_000_000)
        for strategy in [BuyHold(), SmaCross(3, 8), RsiRevert(5), DonchianBreakout(12, 6)]:
            full = strategy.signals(candles)
            self.assertEqual(len(full), len(candles))
            self.assertTrue(set(full) <= {0, 1})
            for stop in range(1, len(candles) + 1):
                with self.subTest(strategy=strategy.name, stop=stop):
                    self.assertEqual(strategy.signals(candles[:stop]), full[:stop])


if __name__ == "__main__":
    unittest.main()
