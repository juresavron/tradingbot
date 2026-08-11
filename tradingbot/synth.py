"""Synthetic (fake) price data generator.

Useful for testing the machinery offline and for unit tests. This is NOT real
market data and backtests on it say nothing about real profitability -- files
it produces are always named synthetic_* so they can't be mistaken for the
real thing.
"""

from __future__ import annotations

import math
import random
import time

from .candles import TIMEFRAME_MS, Candle

SECONDS_PER_YEAR = 365 * 24 * 60 * 60


def generate(
    days: int,
    timeframe: str = "1h",
    seed: int = 42,
    start_price: float = 50_000.0,
    drift: float = 0.10,
    vol: float = 0.60,
    end_ms: int | None = None,
) -> list[Candle]:
    """Geometric Brownian motion candles: `drift` and `vol` are annualized."""
    tf_ms = TIMEFRAME_MS[timeframe]
    n = days * 24 * 60 * 60 * 1000 // tf_ms
    if end_ms is None:
        end_ms = int(time.time() * 1000) // tf_ms * tf_ms
    start_ms = end_ms - n * tf_ms

    rng = random.Random(seed)
    dt = (tf_ms / 1000) / SECONDS_PER_YEAR
    sigma_bar = vol * math.sqrt(dt)

    candles: list[Candle] = []
    price = start_price
    for i in range(n):
        log_ret = (drift - 0.5 * vol * vol) * dt + sigma_bar * rng.gauss(0, 1)
        close = price * math.exp(log_ret)
        wick_hi = abs(rng.gauss(0, sigma_bar / 2))
        wick_lo = abs(rng.gauss(0, sigma_bar / 2))
        high = max(price, close) * (1 + wick_hi)
        low = min(price, close) * (1 - wick_lo)
        volume = rng.lognormvariate(3, 1)
        candles.append(
            Candle(
                ts=start_ms + i * tf_ms,
                open=round(price, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(close, 2),
                volume=round(volume, 4),
            )
        )
        price = close
    return candles
