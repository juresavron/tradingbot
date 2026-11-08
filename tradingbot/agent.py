"""Durable paper execution with timestamped news context and an audit trail.

This module has no exchange order adapter and never spends real money. News
text is stored as untrusted data; it cannot supply executable instructions,
position sizes, or order parameters. ``observe`` preserves the price-only
baseline. ``require-fresh`` additionally blocks new entries when recent news
is unavailable, without predicting its market impact.
"""

from __future__ import annotations

import copy
import re

from .candles import Candle
from .memory import TradingMemory
from .paper import PaperTrader


class MemoryPaperTrader(PaperTrader):
    """Commit the account, decision, and simulated fills in one transaction.

    The database is the account's sole source of truth. A restarted or second
    process reloads the latest account under BEGIN IMMEDIATE before deciding,
    so the same closed candle cannot create duplicate paper fills. This is
    local paper idempotency, not a claim about exchange order reconciliation.
    """

    def __init__(
        self,
        *,
        memory: TradingMemory,
        account_id: str = "default",
        news_policy: str = "observe",
        news_max_age_ms: int = 86_400_000,
        news_feed_fingerprint: str | None = None,
        **paper_options,
    ):
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", account_id):
            raise ValueError("account_id must contain 1 to 100 letters, digits, underscores, or hyphens")
        if news_policy not in ("observe", "require-fresh"):
            raise ValueError("news_policy must be observe or require-fresh")
        if type(news_max_age_ms) is not int or news_max_age_ms <= 0:
            raise ValueError("news_max_age_ms must be a positive integer")
        if news_feed_fingerprint is not None and (
                not isinstance(news_feed_fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{64}", news_feed_fingerprint)):
            raise ValueError("news_feed_fingerprint must be None or a lowercase SHA-256 digest")
        symbol = paper_options.get("symbol", "")
        normalized = symbol.upper().replace("-", "/")
        if not re.fullmatch(r"[A-Z0-9]+/[A-Z0-9]+", normalized):
            raise ValueError("Memory-backed paper trading requires a symbol such as BTC/EUR")
        if paper_options.get("reporter") is not None:
            raise ValueError("External reporting is not supported inside atomic memory transactions")
        self.memory = memory
        self.account_id = account_id
        self.news_policy = news_policy
        self.news_max_age_ms = news_max_age_ms
        self.news_feed_fingerprint = news_feed_fingerprint
        self.news_asset = normalized.partition("/")[0]
        self._pending_decision: dict | None = None
        paper_options["state_path"] = f"sqlite-account-{account_id}"
        paper_options["symbol"] = normalized
        paper_options["reporter"] = None
        # Constructor creation is also serialized. Existing metadata is fully
        # checked by PaperTrader before any existing account is saved.
        with self.memory.transaction():
            super().__init__(**paper_options)
            self._save_state()

    def _read_saved_state(self) -> dict | None:
        return self.memory.load_account(self.account_id)

    def _load_state(self, starting_cash: float) -> dict:
        self.experiment_config.update({
            "news_policy": self.news_policy,
            "news_max_age_ms": self.news_max_age_ms,
            "news_feed_fingerprint": self.news_feed_fingerprint,
        })
        return super()._load_state(starting_cash)

    def _save_state(self) -> None:
        self.memory.save_account(self.account_id, self.state)

    def _decision_target(self, target: int, at_ms: int) -> int:
        articles = self.memory.recent_news(
            at_ms=at_ms, asset=self.news_asset, max_age_ms=self.news_max_age_ms,
        )
        effective = target
        reason = "strategy_long" if target else "strategy_flat"
        if (self.news_policy == "require-fresh" and not articles
                and target == 1 and self.state["qty"] == 0):
            effective = 0
            reason = "blocked_no_fresh_news"
        self._pending_decision = {
            "proposed_target": target,
            "target": effective,
            "reason": reason,
            "news_ids": [article["id"] for article in articles],
        }
        return effective

    def _summary(self, price: float, proposed_target: int) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy.describe(),
            "news_policy": self.news_policy,
            "proposed_target": proposed_target,
            "price": price,
            "cash": self.state["cash"],
            "qty": self.state["qty"],
            "equity": self.equity(price),
            "peak_equity": self.state["peak_equity"],
            "killed": self.state["killed"],
        }

    def step(self, price: float, candles: list[Candle], now_ms: int) -> dict:
        if type(now_ms) is not int or now_ms <= 0:
            raise ValueError("now_ms must be a positive integer timestamp in milliseconds")
        previous_state = copy.deepcopy(self.state)
        previous_history = list(self._signal_history)
        self._pending_decision = None
        try:
            with self.memory.transaction():
                # Refresh after acquiring the database write lock. Keep the
                # dict identity because PaperTrader.run holds a reference.
                latest = self._load_state(self.experiment_config["starting_cash"])
                self.state.clear()
                self.state.update(latest)
                self._signal_history = self._decode_signal_history(self.state["signal_history"])
                if self.state["equity_log"] and now_ms < self.state["equity_log"][-1][0]:
                    raise ValueError("Paper evaluation time cannot precede the last persisted observation")
                fills_before = len(self.state["trades"])
                was_killed = self.state["killed"]
                result = super().step(price, candles, now_ms)
                if result["killed"] and not was_killed:
                    articles = self.memory.recent_news(
                        at_ms=now_ms, asset=self.news_asset, max_age_ms=self.news_max_age_ms,
                    )
                    self._pending_decision = {
                        "proposed_target": 0, "target": 0, "reason": "kill_switch",
                        "news_ids": [article["id"] for article in articles],
                    }
                if self._pending_decision is not None:
                    decision = self._pending_decision
                    decision_id = self.memory.record_decision(
                        account_id=self.account_id, at_ms=now_ms,
                        candle_ts=self.state["last_candle_ts"],
                        target=decision["target"], reason=decision["reason"],
                        news_ids=decision["news_ids"],
                        state=self._summary(price, decision["proposed_target"]),
                    )
                    for seq in range(fills_before, len(self.state["trades"])):
                        self.memory.record_fill(
                            account_id=self.account_id, decision_id=decision_id,
                            seq=seq, fill=self.state["trades"][seq],
                        )
                return result
        except BaseException:
            self.state.clear()
            self.state.update(previous_state)
            self._signal_history = previous_history
            raise
        finally:
            self._pending_decision = None
