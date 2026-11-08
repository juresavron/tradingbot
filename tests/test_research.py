import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tradingbot.candles import Candle, save_csv
from tradingbot.cli import main
from tradingbot.research import Candidate, evaluate

HOUR = 3_600_000


def rising(n=1000):
    return [Candle(i * HOUR, 100 + i, 100 + i, 100 + i, 100 + i, 1) for i in range(n)]


class TestResearch(unittest.TestCase):
    def test_holdout_never_changes_selected_candidate_or_training_metrics(self):
        bars = rising()
        candidates = (Candidate("sma_cross", {"fast": "2", "slow": "8"}), Candidate("rsi_revert"))
        first = evaluate(bars, candidates=candidates, timeframe="1h")
        split = first["split"]["train_bars"]
        changed = bars[:split] + [Candle(i * HOUR, 900 - (i - split), 900 - (i - split),
                                       900 - (i - split), 900 - (i - split), 1)
                                  for i in range(split, len(bars))]
        second = evaluate(changed, candidates=candidates, timeframe="1h")
        self.assertEqual(first["selected"], second["selected"])
        self.assertEqual(first["training"], second["training"])
        self.assertNotEqual(first["holdout"]["final_equity"], second["holdout"]["final_equity"])

    def test_warmup_uses_training_history_but_returns_start_in_holdout(self):
        report = evaluate(rising(), timeframe="1h", fee=0, slippage=0,
                          candidates=(Candidate("sma_cross", {"fast": "2", "slow": "8"}),))
        # Warm SMA enters at second holdout open (801), not after 8 new bars.
        self.assertAlmostEqual(report["holdout"]["final_equity"], 300 * 1099 / 801)
        self.assertEqual(report["holdout"]["bars"], 300)
        self.assertEqual(report["holdout"]["n_trades"], 1)
        self.assertAlmostEqual(report["holdout"]["edge_vs_bh_pct"], 0)

    def test_double_cost_stress_and_failed_checks_are_reported(self):
        report = evaluate(rising(), timeframe="1h", candidates=(Candidate("buy_hold"),))
        self.assertLess(report["double_cost_holdout"]["total_return_pct"],
                        report["holdout"]["total_return_pct"])
        self.assertFalse(report["checks"]["enough_holdout_trades"])
        self.assertFalse(report["checks"]["at_least_90_days_in_holdout"])
        self.assertFalse(report["all_checks_passed"])

    def test_boundary_gap_and_wrong_timeframe_rejected(self):
        bars = rising()
        for data, tf in [(bars[:700] + bars[701:], "1h"), (bars, "4h")]:
            with self.subTest(timeframe=tf), self.assertRaises(ValueError):
                evaluate(data, timeframe=tf)

    def test_invalid_research_settings_and_no_training_trades(self):
        for kwargs in [{"train_fraction": 0}, {"train_fraction": float("nan")},
                       {"max_drawdown": -1}, {"min_trades": 0}, {"candidates": ()},
                       {"fee": 0.5}, {"slippage": -0.1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                evaluate(rising(), timeframe="1h", **kwargs)
        flat = [Candle(i * HOUR, 100, 100, 100, 100, 0) for i in range(1000)]
        with self.assertRaisesRegex(ValueError, "No candidate traded"):
            evaluate(flat, timeframe="1h")
        with self.assertRaisesRegex(ValueError, "at least 3 candles"):
            evaluate(rising(5), timeframe="1h")

    def test_cli_writes_reproducible_report_and_preserves_existing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, output = Path(tmp) / "synthetic.csv", Path(tmp) / "research.json"
            save_csv(rising(), data)
            argv = ["research", "--data", str(data), "--timeframe", "1h", "--out", str(output)]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(argv), 0)
                original = output.read_bytes()
                self.assertEqual(main(argv), 1)
            self.assertEqual(output.read_bytes(), original)
            report = json.loads(original)
            self.assertEqual(len(report["source"]["sha256"]), 64)
            self.assertIn("SYNTHETIC DATA", stdout.getvalue())
            self.assertIn("cannot create report", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
