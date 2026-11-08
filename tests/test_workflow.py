import contextlib
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tradingbot.candles import Candle
from tradingbot.cli import main
from tradingbot.memory import TradingMemory


def article(title="DEMO Bitcoin news fixture"):
    return {"title": title, "url": "https://example.org/demo-bitcoin",
            "text": "Synthetic test content, not market news.",
            "publishedAt": datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()}


class TestWorkflow(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_news_json_import_and_duplicate_observation_are_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, db = Path(tmp) / "news.json", Path(tmp) / "memory.sqlite3"
            path.write_text(json.dumps([article()]))
            argv = ["news", "--db", str(db), "import", "--file", str(path), "--asset", "BTC"]
            self.assertEqual(self.run_cli(argv)[0], 0)
            code, out, _ = self.run_cli(argv)
            self.assertEqual(code, 0)
            self.assertIn("0 inserted, 1 duplicates", out)
            with TradingMemory(db) as memory:
                self.assertEqual(len(memory.recent_news(int(time.time() * 1000), "BTC", 86_400_000)), 1)

    def test_news_rss_sync_filters_unrelated_articles(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            with mock.patch("tradingbot.workflow.fetch_rss", return_value=[article(), article("Unrelated story")]):
                code, out, _ = self.run_cli([
                    "news", "--db", str(db), "sync", "--rss-url", "https://example.org/rss",
                    "--asset", "BTC", "--keyword", "bitcoin",
                ])
            self.assertEqual(code, 0)
            self.assertIn("1 inserted", out)
            self.assertIn("1 filtered", out)

    def test_apify_sync_uses_environment_binding_without_printing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            token = "example-test-token"
            with mock.patch.dict("os.environ", {"APIFY_TOKEN": token}), \
                    mock.patch("tradingbot.workflow.fetch_apify_dataset", return_value=[article()]) as fetch:
                code, out, err = self.run_cli([
                    "news", "--db", str(db), "sync", "--apify-dataset", "dataset-id", "--asset", "BTC",
                ])
            self.assertEqual(code, 0)
            fetch.assert_called_once_with("dataset-id", token=token, limit=100, offset=0)
            self.assertNotIn(token, out + err)

    def test_actual_cli_paper_run_atomically_records_news_decision_and_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            interval = 4 * 3_600_000
            end = int(time.time() * 1000) // interval * interval
            prices = [100] * 120 + [110] * 10
            bars = [Candle(end - (130 - i) * interval, p, p, p, p, 1) for i, p in enumerate(prices)]
            with mock.patch("tradingbot.workflow.fetch_rss", return_value=[article()]), \
                    mock.patch("tradingbot.exchange.MarketData.get_candles", side_effect=[bars, bars, KeyboardInterrupt]), \
                    mock.patch("tradingbot.exchange.MarketData.get_price", return_value=110), \
                    mock.patch("tradingbot.paper.time.sleep", return_value=None):
                code, out, _ = self.run_cli([
                    "agent-paper", "--db", str(db), "--account", "demo",
                    "--rss-url", "https://example.org/rss", "--keyword", "bitcoin", "--poll", "1",
                ])
            self.assertEqual(code, 0)
            self.assertIn("300.00 EUR", out)
            with TradingMemory(db) as memory:
                state = memory.load_account("demo")
                history = memory.history("demo")
                self.assertGreater(state["qty"], 0)
                self.assertEqual(len(history["decisions"]), 1)
                self.assertEqual(len(history["fills"]), 1)
                self.assertEqual(len(history["decisions"][0]["news_ids"]), 1)
            code, out, _ = self.run_cli(["memory", "--db", str(db), "--account", "demo"])
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)["fills"]), 1)

    def test_news_failure_blocks_entry_and_does_not_expose_remote_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.sqlite3"
            interval = 4 * 3_600_000
            end = int(time.time() * 1000) // interval * interval
            bars = [Candle(end - (3 - i) * interval, 100, 100, 100, 100, 1) for i in range(3)]
            with mock.patch("tradingbot.workflow.fetch_rss", side_effect=RuntimeError("private remote detail")), \
                    mock.patch("tradingbot.exchange.MarketData.get_candles", side_effect=[bars, KeyboardInterrupt]), \
                    mock.patch("tradingbot.exchange.MarketData.get_price", return_value=100), \
                    mock.patch("tradingbot.paper.time.sleep", return_value=None):
                code, out, _ = self.run_cli([
                    "agent-paper", "--db", str(db), "--account", "demo", "--strategy", "buy_hold",
                    "--rss-url", "https://example.org/rss", "--poll", "1",
                ])
            self.assertEqual(code, 0)
            self.assertNotIn("private remote detail", out)
            with TradingMemory(db) as memory:
                self.assertEqual(memory.load_account("demo")["qty"], 0)
                self.assertEqual(memory.history("demo")["decisions"][0]["reason"], "blocked_no_fresh_news")
                self.assertEqual(memory.history("demo")["fills"], [])

    def test_bad_arguments_fail_cleanly_and_memory_inspection_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite3"
            code, _, err = self.run_cli(["memory", "--db", str(missing)])
            self.assertEqual(code, 2)
            self.assertIn("does not exist", err)
            self.assertFalse(missing.exists())
            for args in [["--poll", "0"], ["--news-refresh", "0"], ["--news-max-age-hours", "nan"],
                         ["--limit", "0"], ["--field-map", "invalid=value"]]:
                with self.subTest(args=args):
                    code, _, err = self.run_cli(["agent-paper", "--db", str(missing)] + args)
                    self.assertEqual(code, 2)
                    self.assertIn("error:", err)
                    self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
