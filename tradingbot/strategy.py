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


STRATEGIES: dict[str, type[Strategy]] = {
    SmaCross.name: SmaCross,
    RsiRevert.name: RsiRevert,
    BuyHold.name: BuyHold,
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
