"""Paper trading: run a strategy against LIVE market prices with FAKE money.

This is the mandatory step between backtesting and risking a single real
euro. It answers the question a backtest can't: does the strategy survive
contact with the present, not just the past?

State is persisted to a JSON file after every step, so you can stop the bot
(Ctrl+C) and restart it later without losing the account.

A kill switch flattens the position and stops the bot if equity drops more
than `max_drawdown_stop` below its peak. Real risk management is not
optional, so the fake version practices it too.
"""

from __future__ import annotations

import json
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
    ):
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
        self.state = self._load_state(starting_cash)

    # ---------------------------------------------------------------- state

    def _load_state(self, starting_cash: float) -> dict:
        if self.state_path.exists():
            with open(self.state_path) as f:
                state = json.load(f)
            if state.get("symbol") != self.symbol or state.get("timeframe") != self.timeframe:
                raise SystemExit(
                    f"State file {self.state_path} is for "
                    f"{state.get('symbol')}/{state.get('timeframe')}, not "
                    f"{self.symbol}/{self.timeframe}. Use --state to pick another file."
                )
            return state
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy.describe(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "starting_cash": starting_cash,
            "cash": starting_cash,
            "qty": 0.0,
            "entry_price": 0.0,
            "peak_equity": starting_cash,
            "last_candle_ts": 0,
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

    def equity(self, price: float) -> float:
        return self.state["cash"] + self.state["qty"] * price

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

        # Kill switch first: protect the account before anything else.
        eq = self.equity(price)
        s["peak_equity"] = max(s["peak_equity"], eq)
        drawdown = 1 - eq / s["peak_equity"] if s["peak_equity"] > 0 else 0.0
        if drawdown >= self.max_drawdown_stop and not s["killed"]:
            self._execute(0, price, now_ms, "kill_switch")
            s["killed"] = True
            s["equity_log"].append([now_ms, self.equity(price)])
            self._save_state()
            return {"killed": True, "equity": self.equity(price), "price": price, "acted": True}

        # Only re-evaluate the strategy when a NEW candle has closed.
        if candles and candles[-1].ts > s["last_candle_ts"]:
            s["last_candle_ts"] = candles[-1].ts
            signals = self.strategy.signals(candles)
            target = signals[-1]
            current = 1 if s["qty"] > 0.0 else 0
            if target != current:
                self._execute(target, price, now_ms, "signal")
                acted = True

        s["equity_log"].append([now_ms, self.equity(price)])
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
        print(
            f"Paper trading {self.symbol} on {self.timeframe} candles with "
            f"{self.strategy.describe()} — fake ${s['starting_cash']:.2f}, live prices.\n"
            f"Signals are checked when a candle closes (every {tf_min} min); "
            f"prices every {poll_seconds}s. Ctrl+C to stop. State: {self.state_path}\n",
            flush=True,
        )
        try:
            while True:
                now_ms = int(time.time() * 1000)
                candles = self.client.get_candles(self.symbol, self.timeframe, limit=self.lookback)
                price = self.client.get_price(self.symbol)
                status = self.step(price, candles, now_ms)
                eq = status["equity"]
                ret = eq / s["starting_cash"] - 1
                pos = "LONG" if s["qty"] > 0 else "flat"
                stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                note = "  << traded" if status["acted"] else ""
                print(
                    f"[{stamp}] {self.symbol} {price:,.2f}  {pos:<5} "
                    f"equity ${eq:,.2f} ({ret:+.2%}){note}",
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
            eq = self.equity(self.client.get_price(self.symbol))
            print(
                f"\nStopped. Equity ${eq:,.2f} "
                f"({eq / s['starting_cash'] - 1:+.2%}) over {len(s['trades'])} trades. "
                f"State saved to {self.state_path} — run the same command to resume.",
                flush=True,
            )
