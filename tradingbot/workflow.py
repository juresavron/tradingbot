"""Command-line news intake, durable memory, and paper execution workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from .agent import MemoryPaperTrader
from .exchange import MarketData
from .memory import TradingMemory
from .news import fetch_apify_dataset, fetch_rss, ingest_items
from .strategy import STRATEGIES, make_strategy

DEFAULT_DB = "data/bot_memory.sqlite3"


def _mapping(pairs: list[str] | None) -> dict[str, str]:
    mapping = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or key not in {"title", "url", "body", "source", "published"} or not value:
            raise ValueError("--field-map needs title/url/body/source/published=actorField")
        mapping[key] = value
    return mapping


def _select_items(items: list, args: argparse.Namespace, mapping: dict) -> tuple[list, int]:
    if not args.keyword:
        return items, 0
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in args.keyword) + r")\b", re.I)
    fields = [mapping.get("title", "title"), mapping.get("body", "text")]
    selected = [item for item in items if isinstance(item, dict) and
                pattern.search(" ".join(str(item.get(field, "")) for field in fields))]
    return selected, len(items) - len(selected)


def sync_news(memory: TradingMemory, args: argparse.Namespace) -> dict:
    mapping = _mapping(args.field_map)
    if args.apify_dataset:
        # Never accept a token on argv or put it into an URL/log.
        token = os.environ.get(args.token_env) or None
        items = fetch_apify_dataset(args.apify_dataset, token=token,
                                    limit=args.limit, offset=args.offset)
    elif args.rss_url:
        if mapping:
            raise ValueError("RSS already has normalized fields; remove --field-map")
        items = fetch_rss(args.rss_url, limit=args.limit)
    else:
        raise ValueError("Choose --apify-dataset or --rss-url")
    selected, filtered = _select_items(items, args, mapping)
    # Availability begins after the fetch completes, never at publication time
    # or an actor-provided timestamp which the bot did not observe itself.
    result = ingest_items(memory, selected, observed_at_ms=int(time.time() * 1000),
                          assets=args.asset, field_map=mapping)
    result["filtered"] = filtered
    return result


def _news_result(result: dict) -> str:
    return (f"News: {result['inserted']} inserted, {result['duplicates']} duplicates, "
            f"{result['rejected']} rejected, {result.get('filtered', 0)} filtered")


def cmd_news(args: argparse.Namespace) -> int:
    try:
        with TradingMemory(args.db) as memory:
            if args.news_command == "import":
                with open(args.file) as f:
                    items = json.load(f)
                if not isinstance(items, list):
                    raise ValueError("News file must contain a JSON array of article objects")
                mapping = _mapping(args.field_map)
                selected, filtered = _select_items(items, args, mapping)
                result = ingest_items(memory, selected, observed_at_ms=int(time.time() * 1000),
                                      assets=args.asset, field_map=mapping)
                result["filtered"] = filtered
            else:
                result = sync_news(memory, args)
        print(_news_result(result))
        for error in result.get("errors", []):
            print(f"Rejected news: {error}", file=sys.stderr)
        return 1 if result["rejected"] else 0
    except (OSError, sqlite3.Error) as e:
        print(f"error: news intake failed ({type(e).__name__})", file=sys.stderr)
        return 1


class _NewsMarketData(MarketData):
    """Refresh a bounded batch; news failures do not suppress market polling.

    Fetches are synchronous and can delay this poll by the news timeout.
    """

    def __init__(self, memory: TradingMemory, args: argparse.Namespace):
        super().__init__()
        self.memory, self.args = memory, args
        self.next_sync = 0.0

    def get_candles(self, *args, **kwargs):
        if (self.args.apify_dataset or self.args.rss_url) and time.monotonic() >= self.next_sync:
            self.next_sync = time.monotonic() + self.args.news_refresh
            try:
                print(_news_result(sync_news(self.memory, self.args)), flush=True)
            except (RuntimeError, ValueError, OSError, sqlite3.Error) as e:
                # Do not expose response content, tokens, or an authenticated URL.
                print(f"News refresh failed ({type(e).__name__}); current freshness policy still applies.", flush=True)
        return super().get_candles(*args, **kwargs)


def cmd_agent_paper(args: argparse.Namespace) -> int:
    from .cli import parse_params

    if args.poll < 1 or args.news_refresh < 1:
        raise ValueError("--poll and --news-refresh must be positive seconds")
    if not math.isfinite(args.news_max_age_hours) or args.news_max_age_hours <= 0:
        raise ValueError("--news-max-age-hours must be finite and > 0")
    _mapping(args.field_map)
    if not args.asset:
        args.asset = [args.symbol.upper().replace("-", "/").partition("/")[0]]
    if not 1 <= args.limit <= 1000 or args.offset < 0:
        raise ValueError("--limit must be in [1, 1000] and --offset must be nonnegative")
    if args.rss_url and args.field_map:
        raise ValueError("--field-map is only needed for Apify data; RSS already has normalized fields")
    feed_config = {
        "apify_dataset": args.apify_dataset, "rss_url": args.rss_url,
        "assets": sorted(set(asset.upper() for asset in args.asset)),
        "keywords": sorted(set(keyword.lower() for keyword in args.keyword or [])),
        "field_map": _mapping(args.field_map), "limit": args.limit,
        "offset": args.offset, "refresh_seconds": args.news_refresh,
    }
    fingerprint = hashlib.sha256(json.dumps(feed_config, sort_keys=True).encode()).hexdigest()
    try:
        with TradingMemory(args.db) as memory:
            client = _NewsMarketData(memory, args)
            trader = MemoryPaperTrader(
                memory=memory, account_id=args.account,
                client=client, symbol=args.symbol, timeframe=args.timeframe,
                strategy=make_strategy(args.strategy, parse_params(args.params)),
                starting_cash=args.cash, fee=args.fee, slippage=args.slippage,
                size=args.size, max_drawdown_stop=args.max_drawdown,
                news_policy=args.news_policy,
                news_max_age_ms=int(args.news_max_age_hours * 3_600_000),
                news_feed_fingerprint=fingerprint,
            )
            print(f"Paper execution only. Memory: {args.db}, account: {args.account}; news policy: {args.news_policy}")
            if not args.apify_dataset and not args.rss_url:
                print("No recurring news source configured; using already imported news from memory.")
            trader.run(poll_seconds=args.poll)
        return 0
    except (OSError, sqlite3.Error) as e:
        print(f"error: paper memory unavailable ({type(e).__name__})", file=sys.stderr)
        return 1


def cmd_memory(args: argparse.Namespace) -> int:
    # Inspect an existing database; a typo must not create a new empty memory.
    if not Path(args.db).is_file():
        raise ValueError("Memory database does not exist; import news or start agent-paper first")
    try:
        with TradingMemory(args.db) as memory:
            result = memory.history(args.account, limit=args.limit)
            news_ids = sorted({article_id for decision in result["decisions"] for article_id in decision["news_ids"]})
            result["news"] = memory.get_news(news_ids[:1000])
            account = memory.load_account(args.account)
            result["account"] = None if account is None else {
                key: account.get(key) for key in (
                    "symbol", "timeframe", "strategy", "starting_cash", "cash", "qty",
                    "peak_equity", "last_candle_ts", "killed", "experiment_config")}
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (OSError, sqlite3.Error) as e:
        print(f"error: cannot read memory ({type(e).__name__})", file=sys.stderr)
        return 1


def _source_args(parser, *, required=False):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--apify-dataset", help="existing Apify dataset ID; never launches an actor")
    group.add_argument("--rss-url", help="public publisher RSS/Atom URL")
    parser.add_argument("--token-env", default="APIFY_TOKEN", help="environment variable name; never pass the token itself")
    parser.add_argument("--limit", type=int, default=100, help="maximum latest items per refresh")
    parser.add_argument("--offset", type=int, default=0, help="Apify latest-first batch offset for manual backfill")


def _article_args(parser, *, required=False):
    parser.add_argument("--asset", action="append", default=None, required=required,
                        help="ticker to tag this feed with; repeat for more assets (agent default: pair's base asset)")
    parser.add_argument("--keyword", action="append", help="keep only matching title/text; repeat for alternatives")
    parser.add_argument("--field-map", action="append", help="logical field=actor field; e.g. published=datePublished")


def add_workflow_commands(sub) -> None:
    p = sub.add_parser("news", help="ingest news into durable memory (no actor launches)")
    p.add_argument("--db", default=DEFAULT_DB)
    commands = p.add_subparsers(dest="news_command", required=True)
    imp = commands.add_parser("import", help="import an existing JSON article export")
    imp.add_argument("--file", required=True)
    _article_args(imp, required=True)
    sync = commands.add_parser("sync", help="read an existing Apify dataset or publisher feed")
    _source_args(sync, required=True)
    _article_args(sync, required=True)
    p.set_defaults(func=cmd_news)

    p = sub.add_parser("agent-paper", help="news + persistent memory + atomic paper execution")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--account", default="binance-btceur-paper-v1")
    p.add_argument("--symbol", default="BTC/EUR")
    p.add_argument("--timeframe", choices=["1m", "5m", "15m", "1h", "4h", "1d"], default="4h")
    p.add_argument("--strategy", choices=sorted(STRATEGIES), default="donchian_breakout")
    p.add_argument("--params")
    p.add_argument("--cash", type=float, default=300.0, help="fake capital in the pair's quote currency")
    p.add_argument("--fee", type=float, default=0.001)
    p.add_argument("--slippage", type=float, default=0.0005)
    p.add_argument("--size", type=float, default=1.0)
    p.add_argument("--max-drawdown", type=float, default=0.25)
    p.add_argument("--poll", type=int, default=30)
    p.add_argument("--news-policy", choices=["observe", "require-fresh"], default="require-fresh")
    p.add_argument("--news-max-age-hours", type=float, default=24.0)
    p.add_argument("--news-refresh", type=int, default=900, help="seconds between news reads; does not start actors")
    _source_args(p)
    _article_args(p)
    p.set_defaults(func=cmd_agent_paper)

    p = sub.add_parser("memory", help="inspect recent decisions, referenced news IDs and paper fills")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--account", default="binance-btceur-paper-v1")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory)
