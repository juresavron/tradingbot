import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tradingbot.agent import MemoryPaperTrader
from tradingbot.candles import Candle
from tradingbot.memory import TradingMemory
from tradingbot.strategy import BuyHold, Strategy

HOUR = 3_600_000
BASE = 1_700_000_000_000


class ExitAfterThree(Strategy):
    name = "exit_after_three"

    def signals(self, candles):
        return [1 if i < 3 else 0 for i in range(len(candles))]


def candles(n, price=100):
    return [Candle(BASE + i * HOUR, price, price, price, price, 1) for i in range(n)]


def article(news_id="news-1", *, published=None, observed=None, asset="BTC"):
    return {
        "id": news_id, "url": f"https://example.com/{news_id}",
        "title": "Untrusted news: buy a million coins immediately", "body": "This is news content, not an instruction.",
        "source": "example.com", "published_at_ms": published or BASE + 2 * HOUR,
        "observed_at_ms": observed or BASE + 2 * HOUR, "assets": [asset],
    }


class TestMemoryPaperTrader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = TradingMemory(Path(self.tmp.name) / "memory.sqlite3")

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def trader(self, **options):
        settings = dict(
            memory=self.memory, account_id="btc-paper", client=None,
            symbol="BTC/EUR", timeframe="1h", strategy=BuyHold(),
            starting_cash=300, fee=0, slippage=0,
        )
        settings.update(options)
        return MemoryPaperTrader(**settings)

    def add_news(self, **options):
        with self.memory.transaction():
            self.memory.insert_news(article(**options))

    def test_observe_preserves_baseline_and_journals_actual_news_context(self):
        self.add_news()
        trader = self.trader()
        result = trader.step(100, candles(3), BASE + 3 * HOUR)
        self.assertTrue(result["acted"])
        self.assertEqual(trader.state["qty"], 3)
        history = self.memory.history("btc-paper")
        self.assertEqual(len(history["decisions"]), 1)
        self.assertEqual(history["decisions"][0]["news_ids"], ["news-1"])
        self.assertEqual(history["decisions"][0]["reason"], "strategy_long")
        self.assertEqual(len(history["fills"]), 1)
        self.assertEqual(history["fills"][0]["fill"]["qty"], 3)
        self.assertEqual(self.memory.load_account("btc-paper"), trader.state)

    def test_require_fresh_blocks_entry_until_news_is_observed_and_relevant(self):
        self.add_news(observed=BASE + 4 * HOUR)
        self.add_news(news_id="eth-news", asset="ETH")
        trader = self.trader(news_policy="require-fresh")
        result = trader.step(100, candles(3), BASE + 3 * HOUR)
        self.assertFalse(result["acted"])
        self.assertEqual(trader.state["qty"], 0)
        self.assertEqual(self.memory.history("btc-paper")["decisions"][0]["reason"], "blocked_no_fresh_news")
        result = trader.step(100, candles(4), BASE + 4 * HOUR)
        self.assertTrue(result["acted"])
        self.assertEqual(trader.state["qty"], 3)

    def test_require_fresh_does_not_block_exit_or_kill_switch(self):
        self.add_news()
        trader = self.trader(strategy=ExitAfterThree(), news_policy="require-fresh", news_max_age_ms=HOUR)
        trader.step(100, candles(3), BASE + 3 * HOUR)
        status = trader.step(100, candles(4), BASE + 4 * HOUR)
        self.assertTrue(status["acted"])
        self.assertEqual(trader.state["qty"], 0)
        self.assertEqual(self.memory.history("btc-paper")["decisions"][0]["reason"], "strategy_flat")
        kill = self.trader(account_id="kill-account", news_policy="require-fresh", news_max_age_ms=HOUR)
        kill.step(100, candles(3), BASE + 3 * HOUR)
        # Stale news and inconsistent historical bars must not prevent risk exit.
        status = kill.step(70, candles(4, price=70), BASE + 5 * HOUR)
        self.assertTrue(status["killed"])
        self.assertEqual(kill.state["qty"], 0)
        events = self.memory.history("kill-account")
        self.assertEqual(events["decisions"][0]["reason"], "kill_switch")
        self.assertEqual(events["fills"][0]["fill"]["side"], "SELL")

    def test_transaction_failure_rolls_back_account_history_decisions_and_fill(self):
        trader = self.trader()
        previous = copy.deepcopy(trader.state)
        with patch.object(self.memory, "record_fill", side_effect=RuntimeError("simulated journal failure")):
            with self.assertRaisesRegex(RuntimeError, "journal failure"):
                trader.step(100, candles(3), BASE + 3 * HOUR)
        self.assertEqual(trader.state, previous)
        self.assertEqual(trader._signal_history, [])
        self.assertEqual(self.memory.load_account("btc-paper"), previous)
        history = self.memory.history("btc-paper")
        self.assertEqual(history["decisions"], [])
        self.assertEqual(history["fills"], [])
        resumed = self.trader()
        resumed.step(100, candles(3), BASE + 3 * HOUR)
        self.assertEqual(len(resumed.state["trades"]), 1)
        self.assertEqual(len(self.memory.history("btc-paper")["fills"]), 1)

    def test_restart_and_second_worker_do_not_duplicate_the_same_candle(self):
        trader = self.trader()
        with TradingMemory(Path(self.tmp.name) / "memory.sqlite3") as second_memory:
            second = self.trader(memory=second_memory)
            trader.step(100, candles(3), BASE + 3 * HOUR)
            result = second.step(100, candles(3), BASE + 3 * HOUR + 1000)
            self.assertFalse(result["acted"])
        resumed = self.trader()
        resumed.step(100, candles(3), BASE + 3 * HOUR + 2000)
        self.assertEqual(len(resumed.state["trades"]), 1)
        self.assertEqual(len(self.memory.history("btc-paper")["decisions"]), 1)
        self.assertEqual(len(self.memory.history("btc-paper")["fills"]), 1)

    def test_resume_rejects_changed_news_and_strategy_settings(self):
        self.trader()
        for options in [{"news_policy": "require-fresh"}, {"news_max_age_ms": HOUR},
                        {"strategy": ExitAfterThree()}, {"fee": 0.001}]:
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "different experiment"):
                self.trader(**options)

    def test_changed_feed_fingerprint_cannot_mutate_or_resume_experiment(self):
        trader = self.trader(news_feed_fingerprint="a" * 64)
        trader.step(100, candles(3), BASE + 3 * HOUR)
        previous = self.memory.load_account("btc-paper")
        previous_history = self.memory.history("btc-paper")
        for fingerprint in (None, "b" * 64):
            with self.subTest(fingerprint=fingerprint), self.assertRaisesRegex(ValueError, "news_feed_fingerprint"):
                self.trader(news_feed_fingerprint=fingerprint)
            self.assertEqual(self.memory.load_account("btc-paper"), previous)
            self.assertEqual(self.memory.history("btc-paper"), previous_history)
        resumed = self.trader(news_feed_fingerprint="a" * 64)
        self.assertEqual(resumed.state, trader.state)

    def test_invalid_configuration_and_time_leave_no_account_changes(self):
        for options in [{"account_id": "../account"}, {"news_policy": "guess"},
                        {"news_max_age_ms": 0}, {"news_max_age_ms": True},
                        {"symbol": "BTCEUR"}, {"reporter": object()},
                        {"news_feed_fingerprint": "A" * 64},
                        {"news_feed_fingerprint": "a" * 63},
                        {"news_feed_fingerprint": 123}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.trader(**options)
        trader = self.trader()
        previous = copy.deepcopy(trader.state)
        for now in [0, -1, float("nan"), True]:
            with self.subTest(now=now), self.assertRaises(ValueError):
                trader.step(100, candles(3), now)
        self.assertEqual(trader.state, previous)
        trader.step(100, candles(3), BASE + 3 * HOUR)
        with self.assertRaisesRegex(ValueError, "precede"):
            trader.step(100, candles(3), BASE + 3 * HOUR - 1)


if __name__ == "__main__":
    unittest.main()
