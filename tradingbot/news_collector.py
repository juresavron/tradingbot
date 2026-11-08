"""Optional background news intake, independent of the market-data worker.

The collector reads an existing Apify dataset or a public RSS feed. It never
starts actors or interprets article text as instructions. BTC/ETH keyword
matching is only a relevance filter, not sentiment analysis or a trade signal.
Status describes collection attempts, not whether usable fresh news exists.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time

from .memory import TradingMemory
from .news import _article_url, _fields, fetch_apify_dataset, fetch_rss, ingest_items

_RELEVANT = {
    "BTC": re.compile(r"\b(?:bitcoin|btc)\b", re.IGNORECASE),
    "ETH": re.compile(r"\b(?:ethereum|ether|eth)\b", re.IGNORECASE),
}


class NewsCollector:
    def __init__(
        self,
        db_path,
        *,
        asset_provider,
        rss_url=None,
        apify_dataset=None,
        interval_seconds=900,
        start=True,
    ):
        if not callable(asset_provider):
            raise ValueError("News asset_provider must be callable")
        if (not isinstance(interval_seconds, (int, float)) or isinstance(interval_seconds, bool)
                or not math.isfinite(interval_seconds) or interval_seconds < 30):
            raise ValueError("News collection interval must be finite and at least 30 seconds")
        if type(start) is not bool:
            raise ValueError("News collector start must be a boolean")
        if not isinstance(db_path, (str, os.PathLike)) or not str(db_path):
            raise ValueError("News collector requires a database file path")
        if str(db_path) == ":memory:":
            raise ValueError("Background news collection requires a file-backed database")
        if rss_url is None and apify_dataset is None:
            rss_url = os.environ.get("TRADINGBOT_RSS_URL") or None
            apify_dataset = os.environ.get("APIFY_DATASET_ID") or None
        if rss_url and apify_dataset:
            raise ValueError("Configure only one news source: RSS or an Apify dataset")
        if rss_url is not None and not isinstance(rss_url, str):
            raise ValueError("RSS source must be an HTTP(S) URL")
        if apify_dataset is not None and not isinstance(apify_dataset, str):
            raise ValueError("Apify dataset ID must be text")
        self._rss_url = _article_url(rss_url)[0] if rss_url else None
        if apify_dataset and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", apify_dataset):
            raise ValueError("Apify dataset ID has an invalid format")
        self._apify_dataset = apify_dataset or None
        self._field_map = None
        if self._apify_dataset:
            raw_mapping = os.environ.get("APIFY_NEWS_FIELD_MAP")
            if raw_mapping:
                try:
                    self._field_map = json.loads(raw_mapping)
                    if not isinstance(self._field_map, dict):
                        raise ValueError("mapping must be an object")
                    _fields(self._field_map)
                except (ValueError, TypeError):
                    raise ValueError("APIFY_NEWS_FIELD_MAP must be a valid JSON news field mapping") from None
        self._fields = _fields(self._field_map)
        self._db_path = db_path
        self._asset_provider = asset_provider
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._status_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        source = "apify" if self._apify_dataset else "rss" if self._rss_url else None
        self._status = {
            "configured": source is not None, "source": source,
            "last_sync_ms": None, "last_success_ms": None,
            "error": None, "error_type": None, "syncing": False,
            "fetched": 0, "matched": 0, "filtered": 0,
            "inserted": 0, "duplicates": 0, "rejected": 0, "asset": None,
        }
        self._thread = None
        if start and source is not None:
            self._thread = threading.Thread(target=self._run, name="tradingbot-news", daemon=True)
            self._thread.start()

    def snapshot(self) -> dict:
        """Return only public collection metadata, never URLs or credentials."""
        with self._status_lock:
            return {**self._status, "stopped": self._stop.is_set()}

    def _update(self, **values) -> None:
        with self._status_lock:
            self._status.update(values)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._stop.wait(self._interval)

    def sync_once(self) -> dict:
        """Synchronize once; concurrent calls return current in-flight status.

        Source and storage failures become sanitized status. News timestamps
        use receipt time after fetching, never an actor's claimed scrape time.
        """
        if not self._status["configured"] or self._stop.is_set():
            return self.snapshot()
        if not self._sync_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            if self._stop.is_set():
                return self.snapshot()
            self._update(syncing=True, last_sync_ms=int(time.time() * 1000), error=None, error_type=None)
            asset = self._asset_provider()
            if not isinstance(asset, str) or asset.strip().upper() not in _RELEVANT:
                raise ValueError("unsupported news asset")
            asset = asset.strip().upper()
            self._update(asset=asset)
            if self._apify_dataset:
                items = fetch_apify_dataset(self._apify_dataset, token=os.environ.get("APIFY_TOKEN") or None,
                                            limit=100, offset=0, descending=True, timeout=10)
            else:
                items = fetch_rss(self._rss_url, limit=100, timeout=10)
            observed_at_ms = int(time.time() * 1000)
            if self._stop.is_set():
                self._update(syncing=False)
                return self.snapshot()
            if not isinstance(items, list):
                raise ValueError("news source did not return article records")
            relevant = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = " ".join(value for value in (item.get(self._fields["title"]), item.get(self._fields["body"]))
                                if isinstance(value, str))
                if _RELEVANT[asset].search(text):
                    relevant.append(item)
            # A connection belongs to this sync invocation/thread only. No
            # database object is shared with the market-data worker.
            with TradingMemory(self._db_path) as memory:
                result = ingest_items(memory, relevant, observed_at_ms=observed_at_ms,
                                      assets=[asset], field_map=self._field_map)
            self._update(
                last_success_ms=observed_at_ms, fetched=len(items), matched=len(relevant),
                filtered=len(items) - len(relevant), inserted=result["inserted"],
                duplicates=result["duplicates"], rejected=result["rejected"],
            )
        except Exception as exc:
            # Do not forward URLs, tokens, raw provider errors, or article text
            # to public API state, logs, exception messages, or repr output.
            if isinstance(exc, sqlite3.Error):
                error, kind = "News memory is unavailable.", "storage"
            elif isinstance(exc, (ValueError, TypeError)):
                error, kind = "News configuration or article data was rejected.", "validation"
            else:
                error, kind = "News source could not be synchronized.", "source"
            self._update(error=error, error_type=kind)
        finally:
            self._update(syncing=False)
            self._sync_lock.release()
        return self.snapshot()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Requests have a ten-second timeout. The bounded join keeps web
            # shutdown responsive even if a remote peer does not cooperate.
            thread.join(timeout=15)
