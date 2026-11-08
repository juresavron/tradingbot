import tempfile
import unittest
from pathlib import Path

from tradingbot.memory import TradingMemory


def article(article_id="btc", published=100, observed=110, assets=None):
    return {
        "id": article_id,
        "url": "https://example.com/" + article_id,
        "title": "Bitcoin news",
        "body": "A market observation, not a trading instruction.",
        "source": "example.com",
        "published_at_ms": published,
        "observed_at_ms": observed,
        "assets": ["BTC"] if assets is None else assets,
    }


class TestTradingMemory(unittest.TestCase):
    def test_restart_preserves_account_decisions_fills_and_news(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "subdir" / "memory.sqlite3"
            with TradingMemory(path) as memory:
                with memory.transaction():
                    memory.insert_news(article())
                    memory.save_account("paper", {"cash": 300, "qty": 0})
                    decision = memory.record_decision("paper", 120, 100, 0, "hold", ["btc"], {"cash": 300})
                    memory.record_fill("paper", decision, 0, {"side": "BUY", "qty": 1})
            with TradingMemory(path) as memory:
                self.assertEqual(memory.load_account("paper"), {"cash": 300, "qty": 0})
                self.assertEqual(memory.recent_news(120, "BTC", 100)[0]["id"], "btc")
                history = memory.history("paper")
                self.assertEqual(history["decisions"][0]["news_ids"], ["btc"])
                self.assertEqual(history["decisions"][0]["state"], {"cash": 300})
                self.assertEqual(history["fills"][0]["fill"]["side"], "BUY")
                self.assertIsNone(memory.load_account("missing"))

    def test_account_decision_fills_and_news_rollback_together(self):
        with TradingMemory(":memory:") as memory:
            with memory.transaction():
                memory.save_account("paper", {"cash": 300})
            with self.assertRaisesRegex(RuntimeError, "crash"):
                with memory.transaction():
                    memory.save_account("paper", {"cash": 0})
                    memory.insert_news(article())
                    decision = memory.record_decision("paper", 120, 100, 1, "enter", ["btc"], {})
                    memory.record_fill("paper", decision, 0, {"side": "BUY"})
                    raise RuntimeError("crash")
            self.assertEqual(memory.load_account("paper"), {"cash": 300})
            self.assertEqual(memory.recent_news(120, "BTC", 100), [])
            self.assertEqual(memory.history("paper"), {"decisions": [], "fills": []})

    def test_news_is_immutable_and_never_available_before_observation(self):
        with TradingMemory(":memory:") as memory:
            with memory.transaction():
                self.assertTrue(memory.insert_news(article(published=100, observed=150)))
                amended = article(published=90, observed=110)
                amended["title"] = "Changed later"
                self.assertFalse(memory.insert_news(amended))
                memory.insert_news(article("future-publication", published=200, observed=120))
                memory.insert_news(article("unrelated", assets=["ETH"]))
                memory.insert_news(article("stale-publication", published=1, observed=150))
            self.assertEqual(memory.recent_news(140, "BTC", 100), [])
            news = memory.recent_news(150, "btc", 100)
            self.assertEqual([item["id"] for item in news], ["btc"])
            self.assertEqual(news[0]["title"], "Bitcoin news")
            self.assertEqual(news[0]["observed_at_ms"], 150)
            self.assertEqual(memory.recent_news(500, "BTC", 100), [])

    def test_fill_retry_is_idempotent_and_conflicts_rollback(self):
        with TradingMemory(":memory:") as memory:
            with memory.transaction():
                decision = memory.record_decision("paper", 120, 100, 1, "enter", [], {})
                memory.record_fill("paper", decision, 0, {"side": "BUY", "qty": 1})
            with memory.transaction():
                memory.record_fill("paper", decision, 0, {"qty": 1, "side": "BUY"})
            self.assertEqual(len(memory.history("paper")["fills"]), 1)
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                with memory.transaction():
                    memory.save_account("paper", {"cash": 0})
                    memory.record_fill("paper", decision, 0, {"side": "BUY", "qty": 2})
            self.assertIsNone(memory.load_account("paper"))
            with memory.transaction():
                memory.record_fill("other-paper", decision, 0, {"side": "BUY", "qty": 2})
            self.assertEqual(len(memory.history("other-paper")["fills"]), 1)

    def test_writes_require_transaction_and_nested_transactions_fail(self):
        with TradingMemory(":memory:") as memory:
            with self.assertRaisesRegex(ValueError, "explicit transaction"):
                memory.save_account("paper", {})
            with self.assertRaisesRegex(ValueError, "explicit transaction"):
                memory.insert_news(article())
            with memory.transaction():
                memory.save_account("paper", {"cash": 300})
                with self.assertRaisesRegex(ValueError, "Nested"):
                    with memory.transaction():
                        pass
            self.assertEqual(memory.load_account("paper"), {"cash": 300})

    def test_history_separates_accounts_and_limits_recent_rows(self):
        with TradingMemory(":memory:") as memory:
            with memory.transaction():
                for number in range(3):
                    decision = memory.record_decision("paper", number, number, number % 2, "signal", [], {})
                    memory.record_fill("paper", decision, number, {"n": number})
                memory.record_decision("other", 9, 9, 0, "unrelated", [], {})
            history = memory.history("paper", limit=2)
            self.assertEqual([row["at_ms"] for row in history["decisions"]], [2, 1])
            self.assertEqual([row["fill"]["n"] for row in history["fills"]], [2, 1])

    def test_news_lookup_preserves_requested_order_decodes_assets_and_is_bounded(self):
        with TradingMemory(":memory:") as memory:
            with memory.transaction():
                memory.insert_news(article("first", assets=["BTC", "eth"]))
                memory.insert_news(article("second", published=101, observed=111))
            news = memory.get_news(["second", "missing", "first", "second"])
            self.assertEqual([item["id"] for item in news], ["second", "first"])
            self.assertEqual(news[1]["assets"], ["BTC", "ETH"])
            self.assertNotIn("assets_json", news[0])
            self.assertEqual(news[1]["published_at_ms"], 100)
            self.assertEqual(news[1]["observed_at_ms"], 110)
            self.assertEqual(memory.get_news([]), [])
            self.assertEqual(memory.get_news(["first') OR 1=1 --"]), [])
            ids = [f"missing-{i}" for i in range(999)] + ["first"]
            self.assertEqual([item["id"] for item in memory.get_news(ids)], ["first"])
            for invalid in (["missing"] * 1001, "first", [None], [""]):
                with self.subTest(invalid_type=type(invalid).__name__), self.assertRaises(ValueError):
                    memory.get_news(invalid)

    def test_recent_news_summaries_limit_relevant_rows_after_time_and_asset_filtering(self):
        with TradingMemory(":memory:") as memory:
            with memory.transaction():
                memory.insert_news(article("btc-older", published=110, observed=120))
                memory.insert_news(article("eth-newer", published=150, observed=160, assets=["ETH"]))
                memory.insert_news(article("btc-newer", published=140, observed=150))
                memory.insert_news(article("btc-unobserved", published=155, observed=200))
                memory.insert_news(article("btc-stale", published=10, observed=20))
            summaries = memory.recent_news(170, "BTC", 100, limit=1, include_body=False)
            self.assertEqual([item["id"] for item in summaries], ["btc-newer"])
            self.assertNotIn("body", summaries[0])
            self.assertEqual(summaries[0]["assets"], ["BTC"])
            self.assertEqual(summaries[0]["observed_at_ms"], 150)
            full = memory.recent_news(170, "BTC", 100)
            self.assertEqual([item["id"] for item in full], ["btc-newer", "btc-older"])
            self.assertEqual(full[0]["body"], article()["body"])
            for invalid in (0, 1001, True, 1.5):
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    memory.recent_news(170, "BTC", 100, limit=invalid)
            with self.assertRaises(ValueError):
                memory.recent_news(170, "BTC", 100, include_body="false")


if __name__ == "__main__":
    unittest.main()
