"""Paper trading: run a strategy against LIVE market prices with FAKE money.

This is the mandatory step between backtesting and risking a single real
euro. It answers the question a backtest can't: does the strategy survive
contact with the present, not just the past?

State is persisted to a JSON file whenever something happens (a candle
closes, a trade executes, the kill switch fires), so you can stop the bot
(Ctrl+C) and restart it later without losing the account.

The first closed-candle API snapshot seeds the strategy. Subsequent bars are
retained in the account forever, including across restarts, so a sliding API
window cannot erase a past entry signal. This seed can differ from a research
backtest initialized years earlier. Signal computation and saved history grow
with the experiment; a single API snapshot is limited to 1000 candles. A gap
larger than the fetched window must be repaired explicitly, never skipped.

A kill switch flattens the position and stops the bot if equity drops more
than `max_drawdown_stop` below its peak. Real risk management is not
optional, so the fake version practices it too.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .candles import TIMEFRAME_MS, Candle
from .exchange import MarketData
from .strategy import Strategy


class PaperTrader:
    def __init__(
        self,
        client: MarketData,
        symbol: str,
        timeframe: str,
        strategy: Strategy,
        state_path: str | Path = "paper_state.json",
        starting_cash: float = 100.0,
        fee: float = 0.001,
        slippage: float = 0.0005,
        size: float = 1.0,
        max_drawdown_stop: float = 0.25,
        lookback: int = 300,
        reporter=None,
    ):
        def finite_number(value: float) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

        if not finite_number(starting_cash) or starting_cash <= 0:
            raise ValueError("starting_cash must be finite and > 0")
        for name, value in (("fee", fee), ("slippage", slippage)):
            if not finite_number(value) or not 0 <= value < 1:
                raise ValueError(f"{name} must be finite and in [0, 1)")
        if not finite_number(size) or not 0 < size <= 1:
            raise ValueError("size must be finite and in (0, 1]")
        if not finite_number(max_drawdown_stop) or not 0 < max_drawdown_stop <= 1:
            raise ValueError("max_drawdown_stop must be finite and in (0, 1]")
        if type(lookback) is not int or not 1 <= lookback <= 1000:
            raise ValueError("lookback must be an integer in [1, 1000] (the market API page limit)")
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        self.client = client
        self.symbol = symbol
        self.timeframe = timeframe
        self.strategy = strategy
        self.state_path = Path(state_path)
        self.fee = fee
        self.slippage = slippage
        self.size = size
        self.max_drawdown_stop = max_drawdown_stop
        self.lookback = lookback
        self.reporter = reporter
        self.experiment_config = {
            "version": 1,
            "strategy": self.strategy.describe(),
            "starting_cash": starting_cash,
            "fee": fee,
            "slippage": slippage,
            "size": size,
            "max_drawdown_stop": max_drawdown_stop,
            "lookback": lookback,
        }
        self.state = self._load_state(starting_cash)
        self._signal_history = self._decode_signal_history(self.state["signal_history"])

    # ---------------------------------------------------------------- state

    def _read_saved_state(self) -> dict | None:
        """Read the JSON account; alternate durable backends can override this."""
        if self.state_path.exists():
            with open(self.state_path) as f:
                return json.load(f)
        return None

    def _load_state(self, starting_cash: float) -> dict:
        state = self._read_saved_state()
        if state is not None:
            if state.get("symbol") != self.symbol or state.get("timeframe") != self.timeframe:
                raise ValueError(
                    f"State file {self.state_path} is for "
                    f"{state.get('symbol')}/{state.get('timeframe')}, not "
                    f"{self.symbol}/{self.timeframe}. Use --state to pick another file."
                )
            saved_config = state.get("experiment_config")
            if not isinstance(saved_config, dict) or set(saved_config) != set(self.experiment_config):
                raise ValueError(
                    f"State file {self.state_path} has no complete experiment configuration; "
                    "its original costs and risk settings cannot be verified. "
                    "Use --state to start a new experiment in another file. The existing file is unchanged."
                )
            changed = [
                name for name, value in self.experiment_config.items()
                if saved_config[name] != value
            ]
            if changed:
                raise ValueError(
                    f"State file {self.state_path} belongs to a different experiment "
                    f"({', '.join(changed)} changed). Resume with its original settings, "
                    "or use --state to start a new experiment in another file."
                )
            if "signal_history" not in state:
                if state.get("last_candle_ts", 0) or state.get("trades") or state.get("equity_log"):
                    raise ValueError(
                        f"State file {self.state_path} has processed candles but no signal history. "
                        "Its strategy state cannot be reconstructed. Use --state for a new experiment; "
                        "the existing file is unchanged."
                    )
                state["signal_history"] = []
            history = self._decode_signal_history(state["signal_history"])
            if history and history[-1].ts != state.get("last_candle_ts"):
                raise ValueError("Saved signal history does not match the last processed candle; use a new --state file")
            if not history and (state.get("last_candle_ts", 0) or state.get("trades") or state.get("equity_log")):
                raise ValueError("Processed account has empty signal history; use a new --state file")
            return state
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy.describe(),
            "experiment_config": dict(self.experiment_config),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "starting_cash": starting_cash,
            "cash": starting_cash,
            "qty": 0.0,
            "entry_price": 0.0,
            "peak_equity": starting_cash,
            "last_candle_ts": 0,
            "signal_history": [],
            "trades": [],
            "equity_log": [],
            "killed": False,
        }

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    # ---------------------------------------------------------------- logic

    def _validate_snapshot(self, candles: list[Candle], now_ms: int | None = None) -> list[Candle]:
        """Validate closed, contiguous bars, deduplicating identical timestamps."""
        unique = []
        interval = TIMEFRAME_MS[self.timeframe]
        for candle in candles:
            if type(candle.ts) is not int:
                raise ValueError("Signal candle timestamps must be integer milliseconds")
            prices = (candle.open, candle.high, candle.low, candle.close)
            if any(not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0 for p in prices):
                raise ValueError("Signal candle OHLC prices must be finite and > 0")
            if not isinstance(candle.volume, (int, float)) or not math.isfinite(candle.volume) or candle.volume < 0:
                raise ValueError("Signal candle volume must be finite and >= 0")
            if not candle.low <= candle.open <= candle.high or not candle.low <= candle.close <= candle.high:
                raise ValueError("Signal candle low/high must contain open and close")
            if now_ms is not None and candle.ts + interval > now_ms:
                raise ValueError("Signal history accepts only closed candles")
            if unique and candle.ts == unique[-1].ts:
                if candle != unique[-1]:
                    raise ValueError(f"Conflicting signal candle snapshot at timestamp {candle.ts}")
                continue
            if unique and candle.ts - unique[-1].ts != interval:
                raise ValueError(f"Signal candle history has a gap or is not ordered at the {self.timeframe} interval")
            unique.append(candle)
        return unique

    def _decode_signal_history(self, rows: list) -> list[Candle]:
        try:
            if not isinstance(rows, list):
                raise ValueError("Signal history must be a list")
            candles = [Candle(*row) for row in rows]
        except (TypeError, ValueError) as exc:
            raise ValueError("Saved signal history is invalid; use a new --state file") from exc
        return self._validate_snapshot(candles)

    def _merged_signal_history(self, candles: list[Candle], now_ms: int) -> list[Candle]:
        snapshot = self._validate_snapshot(candles, now_ms)
        if not self._signal_history:
            return snapshot
        by_ts = {c.ts: c for c in self._signal_history}
        first_ts = self._signal_history[0].ts
        added = False
        for candle in snapshot:
            if candle.ts < first_ts:
                raise ValueError("Snapshot predates the initial signal history; cannot change the experiment seed")
            previous = by_ts.get(candle.ts)
            if previous is not None and previous != candle:
                raise ValueError(f"Conflicting signal candle snapshot at timestamp {candle.ts}")
            added = added or previous is None
            by_ts[candle.ts] = candle
        if not added:
            return self._signal_history
        merged = [by_ts[ts] for ts in sorted(by_ts)]
        return self._validate_snapshot(merged)

    def equity(self, price: float) -> float:
        return self.state["cash"] + self.state["qty"] * price

    def _report(self) -> None:
        if self.reporter is not None:
            self.reporter.push(self.state)

    def _decision_target(self, target: int, at_ms: int) -> int:
        """Optional decision context/filter hook; never used for risk exits."""
        return target

    def _execute(self, target: int, price: float, now_ms: int, reason: str) -> None:
        s = self.state
        if target == 1 and s["qty"] == 0.0:
            fill = price * (1 + self.slippage)
            budget = s["cash"] * self.size
            qty = budget / (fill * (1 + self.fee))
            s["cash"] -= budget
            s["qty"] = qty
            s["entry_price"] = fill
            s["entry_cost"] = budget
            s["trades"].append(
                {"ts": now_ms, "side": "BUY", "price": fill, "qty": qty, "reason": reason}
            )
        elif target == 0 and s["qty"] > 0.0:
            fill = price * (1 - self.slippage)
            proceeds = s["qty"] * fill
            proceeds -= proceeds * self.fee
            pnl = proceeds - s.get("entry_cost", 0.0)
            s["cash"] += proceeds
            s["trades"].append(
                {
                    "ts": now_ms,
                    "side": "SELL",
                    "price": fill,
                    "qty": s["qty"],
                    "pnl": pnl,
                    "reason": reason,
                }
            )
            s["qty"] = 0.0
            s["entry_price"] = 0.0

    def step(self, price: float, candles: list[Candle], now_ms: int) -> dict:
        """One evaluation cycle. Pure enough to unit-test with a stub client."""
        s = self.state
        acted = False
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            raise ValueError("Paper price must be finite and > 0")
        if s["killed"]:
            return {"killed": True, "equity": self.equity(price), "price": price, "acted": False}

        # A valid crash price must flatten the account even if historical
        # candle data is stale or inconsistent. Compute the peak locally so
        # ordinary snapshot rejection never changes the in-memory account.
        eq = self.equity(price)
        prev_peak = s["peak_equity"]
        candidate_peak = max(prev_peak, eq)
        drawdown = 1 - eq / candidate_peak if candidate_peak > 0 else 0.0
        if drawdown >= self.max_drawdown_stop:
            s["peak_equity"] = candidate_peak
            self._execute(0, price, now_ms, "kill_switch")
            s["killed"] = True
            s["equity_log"].append([now_ms, self.equity(price), price])
            self._save_state()
            self._report()
            return {"killed": True, "equity": self.equity(price), "price": price, "acted": True}

        # Validate snapshots and signals before changing account state. Rejected
        # data must leave both the in-memory account and saved file unchanged.
        history = self._merged_signal_history(candles, now_ms)
        new_candle = bool(history) and (not self._signal_history or history[-1].ts > self._signal_history[-1].ts)
        target = None
        if new_candle:
            signals = self.strategy.signals(history)
            if len(signals) != len(history) or any(signal not in (0, 1) for signal in signals):
                raise ValueError("Strategy must return one binary signal per candle")
            target = signals[-1]

        s["peak_equity"] = candidate_peak

        # Retain the initial snapshot and every subsequent closed bar. A
        # rolling API window must not reset stateful RSI/breakout decisions.
        # This intentionally costs O(total retained bars) per new candle and
        # grows the state file; never trim history and silently change signals.
        if new_candle:
            self._signal_history = history
            s["signal_history"] = [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in history]
            s["last_candle_ts"] = history[-1].ts
            target = self._decision_target(target, now_ms)
            if target not in (0, 1):
                raise ValueError("Decision target must be binary (0 or 1)")
            current = 1 if s["qty"] > 0.0 else 0
            if target != current:
                self._execute(target, price, now_ms, "signal")
                acted = True

        # Log and persist only when something happened — polling every 30s
        # for weeks must not grow the state file into the megabytes.
        if new_candle or acted:
            s["equity_log"].append([now_ms, self.equity(price), price])
            self._save_state()
            self._report()
        elif s["peak_equity"] > prev_peak:
            # Persist peak improvements (without growing the log) so the kill
            # switch measures drawdown from the true observed peak even after
            # an ungraceful restart.
            self._save_state()
        return {"killed": False, "equity": self.equity(price), "price": price, "acted": acted}

    # ---------------------------------------------------------------- loop

    def run(self, poll_seconds: int = 30) -> None:
        s = self.state
        if s["killed"]:
            raise SystemExit(
                f"This account hit its kill switch (max drawdown {self.max_drawdown_stop:.0%}). "
                "Start fresh with a new --state file, and think about what went wrong first."
            )
        tf_min = TIMEFRAME_MS[self.timeframe] // 60_000
        quote = self.symbol.upper().replace("-", "/").partition("/")[2] or "quote units"
        print(
            f"Paper trading {self.symbol} on {self.timeframe} candles with "
            f"{self.strategy.describe()} — fake {s['starting_cash']:.2f} {quote}, live prices.\n"
            f"Signals are checked when a candle closes (every {tf_min} min); "
            f"prices every {poll_seconds}s. Ctrl+C to stop. State: {self.state_path}\n"
            + ("Web dashboard reporting: ON\n" if self.reporter else ""),
            flush=True,
        )
        failures = 0
        try:
            while True:
                try:
                    candles = self.client.get_candles(self.symbol, self.timeframe, limit=self.lookback)
                    price = self.client.get_price(self.symbol)
                except RuntimeError as e:
                    # A weeks-long experiment must survive wifi drops and API
                    # hiccups: back off and retry instead of dying on one poll.
                    failures += 1
                    wait = min(poll_seconds * 2 ** min(failures, 5), 900)
                    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(
                        f"[{stamp}] network problem (attempt {failures}), retrying in {wait}s: {e}",
                        flush=True,
                    )
                    time.sleep(wait)
                    continue
                failures = 0
                # Requests can cross a candle-close boundary; evaluate closed
                # bars against the current time, not the pre-request time.
                now_ms = int(time.time() * 1000)
                status = self.step(price, candles, now_ms)
                eq = status["equity"]
                ret = eq / s["starting_cash"] - 1
                pos = "LONG" if s["qty"] > 0 else "flat"
                stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                note = "  << traded" if status["acted"] else ""
                print(
                    f"[{stamp}] {self.symbol} {price:,.2f}  {pos:<5} "
                    f"equity {eq:,.2f} {quote} ({ret:+.2%}){note}",
                    flush=True,
                )
                if status["killed"]:
                    print(
                        f"\nKILL SWITCH: equity fell {self.max_drawdown_stop:.0%} from its peak. "
                        "Position closed, bot stopped. This is the system working as designed.",
                        flush=True,
                    )
                    break
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            try:
                eq = self.equity(self.client.get_price(self.symbol))
            except RuntimeError:
                eq = s["equity_log"][-1][1] if s["equity_log"] else s["cash"]
            print(
                f"\nStopped. Equity {eq:,.2f} {quote} "
                f"({eq / s['starting_cash'] - 1:+.2%}) over {len(s['trades'])} trades. "
                f"State saved to {self.state_path} — run the same command to resume.",
                flush=True,
            )
