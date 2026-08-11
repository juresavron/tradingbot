"""Public market data client (Binance spot REST API).

Uses only public endpoints -- no account, no API keys, no money involved.
Tries data-api.binance.vision first (a public market-data mirror that works
in some regions where api.binance.com is geo-blocked), then falls back.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .candles import TIMEFRAME_MS, Candle

BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]

USER_AGENT = "tradingbot/0.1 (educational; github.com/juresavron/tradingbot)"


def normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").upper()


class MarketData:
    """Minimal REST client for public candle and price data."""

    def __init__(self, base_urls: list[str] | None = None, timeout: float = 15.0):
        self.base_urls = list(base_urls or BASE_URLS)
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> object:
        query = urllib.parse.urlencode(params)
        last_error: Exception | None = None
        for base in self.base_urls:
            url = f"{base}{path}?{query}"
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                # Remember the base that worked so later calls skip dead hosts.
                if base != self.base_urls[0]:
                    self.base_urls.remove(base)
                    self.base_urls.insert(0, base)
                return data
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
                last_error = e
        raise RuntimeError(
            f"Could not reach any market data host ({', '.join(self.base_urls)}). "
            f"Last error: {last_error}. If the exchange is geo-blocked where you are, "
            "try a different network, or generate offline test data with: "
            "python3 -m tradingbot synth --days 365"
        )

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        start_ms: int | None = None,
        closed_only: bool = True,
    ) -> list[Candle]:
        params: dict = {
            "symbol": normalize_symbol(symbol),
            "interval": timeframe,
            "limit": min(limit, 1000),
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        raw = self._get("/api/v3/klines", params)
        now_ms = int(time.time() * 1000)
        candles = []
        for row in raw:
            close_time = int(row[6])
            # The last kline the API returns is usually the still-forming candle.
            # Trading on it would peek at an unfinished bar, so drop it.
            if closed_only and close_time >= now_ms:
                continue
            candles.append(
                Candle(
                    ts=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        return candles

    def get_price(self, symbol: str) -> float:
        data = self._get("/api/v3/ticker/price", {"symbol": normalize_symbol(symbol)})
        return float(data["price"])


def fetch_history(
    symbol: str,
    timeframe: str,
    days: int,
    client: MarketData | None = None,
    quiet: bool = False,
) -> list[Candle]:
    """Fetch `days` of history, paginating past the API's 1000-bar page limit."""
    client = client or MarketData()
    tf_ms = TIMEFRAME_MS[timeframe]
    end_ms = int(time.time() * 1000)
    cursor = end_ms - days * 24 * 60 * 60 * 1000
    candles: list[Candle] = []
    while cursor < end_ms:
        page = client.get_candles(symbol, timeframe, limit=1000, start_ms=cursor)
        if not page:
            break
        candles.extend(page)
        if not quiet:
            print(f"  fetched {len(candles)} bars...", flush=True)
        cursor = page[-1].ts + tf_ms
        if len(page) < 1000:
            break
        time.sleep(0.25)  # stay well under the public rate limit
    # De-duplicate on timestamp in case pages overlap.
    seen: set[int] = set()
    unique = []
    for c in candles:
        if c.ts not in seen:
            seen.add(c.ts)
            unique.append(c)
    unique.sort(key=lambda c: c.ts)
    return unique
