"""Candle (OHLCV bar) data model and CSV storage."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class Candle:
    ts: int  # open time, milliseconds since epoch, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


def bars_per_year(timeframe: str) -> float:
    return MS_PER_YEAR / TIMEFRAME_MS[timeframe]


def save_csv(candles: list[Candle], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow([c.ts, c.open, c.high, c.low, c.close, c.volume])
    return path


def load_csv(path: str | Path) -> list[Candle]:
    candles: list[Candle] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            candles.append(
                Candle(
                    ts=int(row["ts"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    candles.sort(key=lambda c: c.ts)
    return candles
