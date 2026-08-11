import unittest

from tradingbot.synth import generate


class TestSynth(unittest.TestCase):
    def test_deterministic_for_same_seed(self):
        a = generate(days=10, seed=1, end_ms=1_700_000_000_000)
        b = generate(days=10, seed=1, end_ms=1_700_000_000_000)
        self.assertEqual(a, b)

    def test_different_seeds_differ(self):
        a = generate(days=10, seed=1, end_ms=1_700_000_000_000)
        b = generate(days=10, seed=2, end_ms=1_700_000_000_000)
        self.assertNotEqual(a, b)

    def test_candles_are_well_formed(self):
        for c in generate(days=30, seed=5, end_ms=1_700_000_000_000):
            self.assertGreaterEqual(c.high, max(c.open, c.close))
            self.assertLessEqual(c.low, min(c.open, c.close))
            self.assertGreater(c.low, 0.0)
            self.assertGreaterEqual(c.volume, 0.0)

    def test_bar_count_and_spacing(self):
        candles = generate(days=10, timeframe="1h", seed=1, end_ms=1_700_000_000_000)
        self.assertEqual(len(candles), 240)
        deltas = {b.ts - a.ts for a, b in zip(candles, candles[1:])}
        self.assertEqual(deltas, {3_600_000})


if __name__ == "__main__":
    unittest.main()
