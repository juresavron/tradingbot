import unittest

from tradingbot.indicators import rsi, sma


class TestSma(unittest.TestCase):
    def test_known_values(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(sma(values, 3), [None, None, 2.0, 3.0, 4.0])

    def test_period_one_is_identity(self):
        values = [3.0, 1.0, 4.0]
        self.assertEqual(sma(values, 1), values)

    def test_warmup_is_none(self):
        out = sma([1.0] * 10, 5)
        self.assertTrue(all(v is None for v in out[:4]))
        self.assertTrue(all(v == 1.0 for v in out[4:]))


class TestRsi(unittest.TestCase):
    def test_warmup_is_none(self):
        values = [float(i) for i in range(20)]
        out = rsi(values, 14)
        self.assertTrue(all(v is None for v in out[:14]))
        self.assertTrue(all(v is not None for v in out[14:]))

    def test_all_gains_is_100(self):
        values = [float(i) for i in range(20)]
        out = rsi(values, 14)
        self.assertAlmostEqual(out[-1], 100.0)

    def test_all_losses_is_0(self):
        values = [float(i) for i in range(20, 0, -1)]
        out = rsi(values, 14)
        self.assertAlmostEqual(out[-1], 0.0)

    def test_balanced_moves_near_50(self):
        # Alternating +1/-1 has equal average gain and loss -> RSI ~ 50.
        values = [100.0 + (i % 2) for i in range(40)]
        out = rsi(values, 14)
        self.assertAlmostEqual(out[-1], 50.0, delta=5.0)


if __name__ == "__main__":
    unittest.main()
