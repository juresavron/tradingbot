import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tradingbot.memory import TradingMemory
from tradingbot.news_collector import NewsCollector

BEFORE = 1_800_000_000_000
AFTER = BEFORE + 5000


def article(title="Bitcoin market report", **changes):
    record = {"title": title, "text": "Public market news.", "publishedAt": BEFORE - 1000,
              "url": "https://publisher.example/" + str(sum(map(ord, title)))}
    record.update(changes)
    return record


class TestNewsCollector(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "memory.sqlite3"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def collector(self, **changes):
        options = {"asset_provider": lambda: "BTC", "start": False}
        options.update(changes)
        collector = NewsCollector(self.db, **options)
        self.addCleanup(collector.shutdown)
        return collector

    def test_unconfigured_collector_is_disabled_without_thread_or_network(self):
        with patch("tradingbot.news_collector.fetch_rss") as rss, patch("tradingbot.news_collector.fetch_apify_dataset") as apify:
            collector = self.collector(start=True)
            self.assertFalse(collector.snapshot()["configured"])
            self.assertIsNone(collector.snapshot()["source"])
            self.assertEqual(collector.sync_once()["inserted"], 0)
            self.assertIsNone(collector._thread)
            rss.assert_not_called()
            apify.assert_not_called()
            self.assertFalse(self.db.exists())

    def test_rss_sync_filters_current_asset_and_observes_after_fetch(self):
        collector = self.collector(rss_url="https://publisher.example/feed")
        items = [article(), article("Ethereum market report"), article("Ethernet networking explained"),
                 article("Macro report", text="BTC exchange activity changed.")]
        with patch("tradingbot.news_collector.time.time", side_effect=[BEFORE / 1000, AFTER / 1000]), \
             patch("tradingbot.news_collector.fetch_rss", return_value=items) as fetch:
            status = collector.sync_once()
        fetch.assert_called_once_with("https://publisher.example/feed", limit=100, timeout=10)
        self.assertEqual(status["last_sync_ms"], BEFORE)
        self.assertEqual(status["last_success_ms"], AFTER)
        self.assertEqual((status["fetched"], status["matched"], status["filtered"], status["inserted"]), (4, 2, 2, 2))
        self.assertFalse(status["syncing"])
        with TradingMemory(self.db) as memory:
            self.assertEqual(memory.recent_news(AFTER - 1, "BTC", 60_000), [])
            news = memory.recent_news(AFTER, "BTC", 60_000)
            self.assertEqual(len(news), 2)
            self.assertTrue(all(row["observed_at_ms"] == AFTER and row["assets"] == ["BTC"] for row in news))
            self.assertEqual(memory.recent_news(AFTER, "ETH", 60_000), [])

    def test_apify_environment_config_custom_fields_and_token_never_enter_public_state(self):
        mapping = {"title": "headline", "body": "content", "url": "link", "published": "date"}
        settings = {"APIFY_DATASET_ID": "private_dataset_SECRET", "APIFY_TOKEN": "SECRET-token",
                    "APIFY_NEWS_FIELD_MAP": json.dumps(mapping)}
        with patch.dict(os.environ, settings), patch("tradingbot.news_collector.fetch_apify_dataset") as fetch:
            fetch.return_value = [{"headline": "Ethereum update", "content": "ETH news",
                                   "link": "https://publisher.example/eth", "date": BEFORE - 1000}]
            collector = self.collector(asset_provider=lambda: "eth")
            with patch("tradingbot.news_collector.time.time", return_value=AFTER / 1000):
                status = collector.sync_once()
            fetch.assert_called_once_with("private_dataset_SECRET", token="SECRET-token", limit=100,
                                          offset=0, descending=True, timeout=10)
        self.assertEqual(status["source"], "apify")
        self.assertEqual(status["inserted"], 1)
        self.assertEqual(status["asset"], "ETH")
        self.assertNotIn("SECRET", json.dumps(status))
        self.assertNotIn("publisher.example", json.dumps(status))

    def test_source_failures_are_sanitized_and_successful_status_survives_error(self):
        collector = self.collector(rss_url="https://publisher.example/feed?token=SECRET")
        with patch("tradingbot.news_collector.fetch_rss", return_value=[article()]), \
             patch("tradingbot.news_collector.time.time", return_value=AFTER / 1000):
            successful = collector.sync_once()
        with patch("tradingbot.news_collector.fetch_rss", side_effect=RuntimeError("SECRET raw server response")):
            failed = collector.sync_once()
        self.assertEqual(failed["last_success_ms"], successful["last_success_ms"])
        self.assertEqual(failed["error_type"], "source")
        self.assertNotIn("SECRET", json.dumps(failed))
        self.assertNotIn("publisher.example", json.dumps(failed))
        self.assertFalse(failed["syncing"])

    def test_concurrent_sync_calls_do_not_duplicate_fetch_or_ingestion(self):
        entered, release = threading.Event(), threading.Event()
        collector = self.collector(rss_url="https://publisher.example/feed")
        def fetch(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return [article()]
        with patch("tradingbot.news_collector.fetch_rss", side_effect=fetch) as read, \
             patch("tradingbot.news_collector.time.time", return_value=AFTER / 1000):
            thread = threading.Thread(target=collector.sync_once)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                second = collector.sync_once()
                self.assertTrue(second["syncing"])
                self.assertEqual(read.call_count, 1)
            finally:
                release.set()
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(read.call_count, 1)
        self.assertEqual(collector.snapshot()["inserted"], 1)

    def test_daemon_shutdown_stops_periodic_work_and_manual_sync(self):
        fetched = threading.Event()
        def fetch(*args, **kwargs):
            fetched.set()
            return []
        with patch("tradingbot.news_collector.fetch_rss", side_effect=fetch) as read:
            collector = self.collector(rss_url="https://publisher.example/feed", start=True)
            self.assertTrue(fetched.wait(timeout=2))
            collector.shutdown()
            self.assertFalse(collector._thread.is_alive())
            self.assertTrue(collector._thread.daemon)
            self.assertTrue(collector.snapshot()["stopped"])
            calls = read.call_count
            self.assertTrue(collector.sync_once()["stopped"])
            self.assertEqual(read.call_count, calls)

    def test_invalid_settings_and_field_map_fail_before_starting_threads(self):
        for changes in [
            {"rss_url": "https://publisher.example/feed", "apify_dataset": "dataset"},
            {"rss_url": "https://user:SECRET@publisher.example/feed"}, {"rss_url": "ftp://publisher.example/feed"},
            {"apify_dataset": "dataset?token=SECRET"}, {"interval_seconds": 0}, {"interval_seconds": 29},
            {"interval_seconds": True}, {"interval_seconds": float("nan")}, {"asset_provider": "BTC"},
            {"start": "true"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.collector(**changes)
        for mapping in ("SECRET invalid JSON", "[]", "null", '{"unknown":"SECRET"}'):
            with patch.dict(os.environ, {"APIFY_NEWS_FIELD_MAP": mapping}):
                with self.subTest(mapping=mapping), self.assertRaises(ValueError) as caught:
                    self.collector(apify_dataset="dataset")
                self.assertNotIn("SECRET", str(caught.exception))

    def test_bad_asset_never_fetches_and_explicit_source_overrides_environment(self):
        with patch.dict(os.environ, {"APIFY_DATASET_ID": "env_dataset"}):
            collector = self.collector(rss_url="https://publisher.example/feed", asset_provider=lambda: "DOGE")
            self.assertEqual(collector.snapshot()["source"], "rss")
            with patch("tradingbot.news_collector.fetch_rss") as fetch:
                status = collector.sync_once()
            self.assertEqual(status["error_type"], "validation")
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
