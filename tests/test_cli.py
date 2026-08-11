import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tradingbot.cli import main, parse_params


class TestCliErrors(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stderr.getvalue()

    def test_missing_data_file_is_a_clean_error(self):
        code, err = self.run_cli(["backtest", "--data", "nonexistent.csv", "--strategy", "sma_cross"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_truncated_csv_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("ts,open,high,low,close,volume\n1,2,3\n")
            code, err = self.run_cli(["backtest", "--data", str(path), "--strategy", "sma_cross"])
            self.assertEqual(code, 1)
            self.assertIn("error:", err)
            self.assertNotIn("Traceback", err)

    def test_invalid_strategy_params_are_a_clean_error(self):
        code, err = self.run_cli(
            ["backtest", "--data", "nonexistent.csv", "--strategy", "sma_cross", "--params", "fast=99,slow=10"]
        )
        self.assertEqual(code, 2)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_param_name_is_a_clean_error(self):
        code, err = self.run_cli(
            ["backtest", "--data", "nonexistent.csv", "--strategy", "sma_cross", "--params", "speed=9"]
        )
        self.assertEqual(code, 2)
        self.assertIn("no parameter", err)


class TestParseParams(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(parse_params("fast=10,slow=30"), {"fast": "10", "slow": "30"})

    def test_none_is_empty(self):
        self.assertEqual(parse_params(None), {})

    def test_bad_format_exits(self):
        with self.assertRaises(SystemExit):
            parse_params("fast10")


if __name__ == "__main__":
    unittest.main()
