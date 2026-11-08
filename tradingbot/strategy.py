"""Trading strategies.

A strategy looks at candles and outputs one target position per candle:
    1 = be long (hold the coin), 0 = be flat (hold cash).

Signals are aligned with candle indexes: signals[i] is the decision made at
the CLOSE of candle i. The backtester and paper trader both execute that
decision at the earliest realistic moment afterwards (next candle's open /
current live price) -- never inside candle i itself.

These are long/flat spot strategies only: no shorting, no leverage. That is
deliberate. Leverage is how small accounts die.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections import deque

from .candles import Candle
from .indicators import rsi, sma


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def signals(self, candles: list[Candle]) -> list[int]:
        """Return one target position (0 or 1) per candle."""

    def describe(self) -> str:
        params = ", ".join(f"{k}={v}" for k, v in vars(self).items())
        return f"{self.name}({params})"


class SmaCross(Strategy):
    """Trend following: long while the fast moving average is above the slow one.

    The classic first strategy. Wins in long trends, bleeds in sideways chop.
    """

    name = "sma_cross"

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        self.fast = int(fast)
        self.slow = int(slow)

    def signals(self, candles: list[Candle]) -> list[int]:
        closes = [c.close for c in candles]
        fast_ma = sma(closes, self.fast)
        slow_ma = sma(closes, self.slow)
        out = []
        for f, s in zip(fast_ma, slow_ma):
            out.append(1 if f is not None and s is not None and f > s else 0)
        return out


class RsiRevert(Strategy):
    """Mean reversion: buy when RSI says 'oversold', sell once it recovers.

    Bets that sharp dips bounce back. Works in ranging markets, gets run over
    when the market keeps falling ('catching a falling knife').
    """

    name = "rsi_revert"

    def __init__(self, period: int = 14, buy_below: float = 30.0, exit_above: float = 55.0):
        if not buy_below < exit_above:
            raise ValueError("buy_below must be < exit_above")
        self.period = int(period)
        self.buy_below = float(buy_below)
        self.exit_above = float(exit_above)

    def signals(self, candles: list[Candle]) -> list[int]:
        closes = [c.close for c in candles]
        values = rsi(closes, self.period)
        out = []
        position = 0
        for v in values:
            if v is None:
                out.append(0)
                continue
            if position == 0 and v < self.buy_below:
                position = 1
            elif position == 1 and v > self.exit_above:
                position = 0
            out.append(position)
        return out


class BuyHold(Strategy):
    """Buy on the first candle and never sell. The benchmark to beat."""

    name = "buy_hold"

    def signals(self, candles: list[Candle]) -> list[int]:
        return [1] * len(candles)


class DonchianBreakout(Strategy):
    """Trend candidate: enter above prior highs, exit below prior lows.

    Separate entry and exit windows keep the position through smaller price
    reversals. Periods count candles: the defaults span 20 and 10 days on 4h
    data. These are research defaults, not evidence of a profitable strategy.
    """

    name = "donchian_breakout"

    def __init__(self, entry_period: int = 120, exit_period: int = 60):
        for name, value in (("entry_period", entry_period), ("exit_period", exit_period)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if exit_period >= entry_period:
            raise ValueError("exit_period must be < entry_period")
        self.entry_period = entry_period
        self.exit_period = exit_period

    def signals(self, candles: list[Candle]) -> list[int]:
        # Deques hold extrema of the previous windows only. Add this candle
        # after deciding, so its own high/low cannot set its breakout level.
        highs: deque[int] = deque()
        lows: deque[int] = deque()
        position = 0
        out = []
        for i, candle in enumerate(candles):
            while highs and highs[0] < i - self.entry_period:
                highs.popleft()
            while lows and lows[0] < i - self.exit_period:
                lows.popleft()

            if i >= self.entry_period:
                if position == 0 and candle.close > candles[highs[0]].high:
                    position = 1
                elif position == 1 and candle.close < candles[lows[0]].low:
                    position = 0
            out.append(position)

            while highs and candles[highs[-1]].high <= candle.high:
                highs.pop()
            highs.append(i)
            while lows and candles[lows[-1]].low >= candle.low:
                lows.pop()
            lows.append(i)
        return out


STRATEGIES: dict[str, type[Strategy]] = {
    SmaCross.name: SmaCross,
    RsiRevert.name: RsiRevert,
    BuyHold.name: BuyHold,
    DonchianBreakout.name: DonchianBreakout,
}


def make_strategy(name: str, params: dict[str, str] | None = None) -> Strategy:
    """Build a strategy by name, coercing CLI string params to the right types."""
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(STRATEGIES)}")
    cls = STRATEGIES[name]
    signature = inspect.signature(cls.__init__)
    kwargs = {}
    for key, value in (params or {}).items():
        if key not in signature.parameters:
            valid = [p for p in signature.parameters if p != "self"]
            raise ValueError(f"Strategy '{name}' has no parameter '{key}'. Valid: {valid}")
        default = signature.parameters[key].default
        kwargs[key] = type(default)(value)
    return cls(**kwargs)
