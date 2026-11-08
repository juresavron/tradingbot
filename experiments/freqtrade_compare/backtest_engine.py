"""Exercise the installed Freqtrade backtester with explicit offline fixtures.

The real Binance adapter supplies precision/accounting behavior, but its market
metadata is a declared fixture and all HTTP requests are forbidden. This is not
a Binance connectivity, liquidity, or profitability test.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tradingbot.automation import AutomationRuntime
from tradingbot.candles import TIMEFRAME_MS, load_csv


def load_config(run_dir: Path):
    from freqtrade.configuration import Configuration
    from freqtrade.enums import RunMode

    # Explicit args keep all state/artifacts separate from the actual bot.
    config = Configuration({
        "config": [str(HERE / "config.paper.json")],
        "strategy_path": str(HERE / "strategies"),
        "user_data_dir": str(run_dir),
        "dry_run": True,
        "db_url": f"sqlite:///{run_dir / 'freqtrade.sqlite'}",
    }, RunMode.BACKTEST).get_config()
    if config["dry_run"] is not True or config["trading_mode"].value != "spot":
        raise ValueError("Only an isolated spot simulation is permitted")
    return config


def market_fixture():
    return {
        "id": "BTCEUR", "symbol": "BTC/EUR", "base": "BTC", "quote": "EUR",
        "baseId": "BTC", "quoteId": "EUR", "active": True,
        "type": "spot", "spot": True, "margin": False, "swap": False,
        "future": False, "option": False, "contract": False,
        "linear": None, "inverse": None, "contractSize": None,
        "precision": {"amount": 0.00001, "price": 0.01},
        "limits": {"amount": {"min": 0.00001, "max": 1_000_000},
                   "price": {"min": 0.01, "max": 1_000_000_000},
                   "cost": {"min": 5.0, "max": None},
                   "leverage": {"min": 1.0, "max": 1.0}},
        "maker": 0.001, "taker": 0.001, "info": {},
    }


def run_freqtrade(candles, run_dir: Path):
    from freqtrade.exchange.binance import Binance
    from freqtrade.optimize.backtesting import Backtesting

    config = load_config(run_dir)
    exchange = None
    try:
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Offline evaluation forbids HTTP")), \
                patch("aiohttp.ClientSession._request", new=AsyncMock(side_effect=AssertionError("Offline evaluation forbids HTTP"))):
            exchange = Binance(config, validate=False)
            markets = {"BTC/EUR": market_fixture()}
            exchange._markets = deepcopy(markets)
            exchange._api.set_markets(deepcopy(markets))
            exchange._api_async.set_markets(deepcopy(markets))
            engine = Backtesting(config, exchange=exchange)
            engine._set_strategy(engine.strategylist[0])
            frame = pd.DataFrame([{
                "date": pd.to_datetime(c.ts, unit="ms", utc=True),
                "open": c.open, "high": c.high, "low": c.low,
                "close": c.close, "volume": c.volume,
            } for c in candles])
            processed = engine.strategy.advise_all_indicators({"BTC/EUR": frame})
            start = frame.iloc[engine.required_startup]["date"].to_pydatetime()
            end = frame.iloc[-1]["date"].to_pydatetime()
            result = engine.backtest(processed, start, end)
            trades = result["results"]
            records = []
            for trade in trades.to_dict(orient="records"):
                records.append({name: (value.isoformat() if hasattr(value, "isoformat") else value)
                                for name, value in trade.items()
                                if name in ("open_date", "close_date", "open_rate", "close_rate",
                                            "amount", "stake_amount", "profit_abs", "exit_reason")})
            return {"engine": "installed Freqtrade Backtesting", "closed_trades": len(trades),
                    "final_balance": result["final_balance"], "trades": records,
                    "rejected_signals": result["rejected_signals"],
                    "end_handling": "Freqtrade forces remaining simulated trades closed",
                    "market_metadata": "synthetic ticks/lots/notional matching our simulator"}
    finally:
        Backtesting.cleanup()
        if exchange is not None:
            exchange.close()


def run_reference(candles, run_dir: Path):
    """Drive the actual server simulation with the same closed bars/next opens."""
    runtime = AutomationRuntime(run_dir / "reference.sqlite3", start_worker=False)
    interval = TIMEFRAME_MS["4h"]
    cursor = {"index": 120}

    def market(row, at_ms, clock_override=False):
        i = cursor["index"]
        return {"bars": candles[:i + 1], "price": Decimal(str(candles[i + 1].open)),
                "market_time_ms": candles[i + 1].ts, "next_cursor": None}

    try:
        runtime.configure({"source": "binance", "symbol": "BTC/EUR", "timeframe": "4h",
                           "strategy": "donchian_breakout", "params": {"entry_period": 120, "exit_period": 60},
                           "starting_cash": 300, "max_order_notional": 75, "position_fraction": 0.25,
                           "max_drawdown": 0.10, "news_policy": "observe"})
        runtime.control("start")
        with patch.object(runtime, "_market", side_effect=market):
            for i in range(120, len(candles) - 1):
                cursor["index"] = i
                state = runtime.tick_once(now_ms=candles[i + 1].ts)
                if state["status"]["error"]:
                    raise RuntimeError("Reference simulation failed: " + state["status"]["error"])
            before_close = state
            runtime.control("flatten")
            for _ in range(10):
                state = runtime.tick_once(now_ms=candles[-1].ts + interval)
                if not state["status"]["flatten_requested"]:
                    break
        # Read the complete experiment journal, not the API's bounded latest
        # order view. Join actual decision times rather than wall-clock time.
        with sqlite3.connect(run_dir / "reference.sqlite3") as connection:
            rows = connection.execute("""SELECT o.intent_json,o.snapshot_json,d.at_ms
                FROM execution_orders o JOIN automation_decisions d ON d.cycle_id=o.request_id
                WHERE o.account_id=? AND o.status='FILLED' ORDER BY d.id""",
                (state["account_id"],)).fetchall()
        filled = [{"intent": json.loads(intent), "snapshot": json.loads(snapshot), "at_ms": at_ms}
                  for intent, snapshot, at_ms in rows]
        return {"engine": "actual AutomationRuntime with fixture market input",
                "portfolio_before_forced_close": before_close["portfolio"],
                "final_portfolio": state["portfolio"], "filled_orders": state["metrics"]["n_filled"],
                "reported_orders": len(filled),
                "fee_quote_from_fills": float(sum((Decimal(o["snapshot"]["cumulative_quote_quantity"]) * Decimal("0.001") for o in filled), Decimal("0"))),
                "halted": state["status"]["state"] == "halted",
                "flatten_completed": not state["status"]["flatten_requested"],
                "end_handling": "bounded flatten at final available next-open price; dust remains valued",
                "orders": [{"side": o["intent"]["side"], "price": o["intent"]["limit_price"],
                            "quantity": o["snapshot"]["executed_quantity"], "at_ms": o["at_ms"]} for o in filled]}
    finally:
        runtime.shutdown()


def compare_fills(freqtrade_result, reference):
    expected = []
    for trade in freqtrade_result["trades"]:
        for side, time_key, price_key in (("BUY", "open_date", "open_rate"), ("SELL", "close_date", "close_rate")):
            expected.append({"side": side, "price": str(trade[price_key]), "quantity": str(trade["amount"]),
                             "at_ms": int(pd.Timestamp(trade[time_key]).timestamp() * 1000)})
    expected.sort(key=lambda row: row["at_ms"])
    actual = reference["orders"]
    mismatches = []
    if len(expected) != len(actual):
        mismatches.append({"kind": "fill_count", "freqtrade": len(expected), "reference": len(actual)})
    for i, (left, right) in enumerate(zip(expected, actual)):
        if (left["side"] != right["side"] or left["at_ms"] != right["at_ms"]
                or Decimal(left["price"]) != Decimal(right["price"])
                or Decimal(left["quantity"]) != Decimal(right["quantity"])):
            mismatches.append({"index": i, "freqtrade": left, "reference": right})
    delta = Decimal(str(freqtrade_result["final_balance"])) - Decimal(str(reference["final_portfolio"]["equity"]))
    flat = reference["flatten_completed"] and reference["final_portfolio"]["quantity"] == 0
    return {"scope": "this shared fixture only", "fills_compared": min(len(expected), len(actual)),
            "mismatches": mismatches, "final_balance_delta": str(delta), "reference_fully_flat": flat,
            "passed": not mismatches and flat and abs(delta) <= Decimal("0.00000001")}


def run_comparison(data_path, *, source_kind="unverified_csv"):
    candles = load_csv(data_path)
    if len(candles) < 122:
        raise ValueError("Need warmup plus at least two decision bars")
    if source_kind not in ("synthetic", "unverified_csv"):
        raise ValueError("Unknown input provenance")
    AutomationRuntime._validate_bars(candles, TIMEFRAME_MS["4h"], candles[-1].ts + TIMEFRAME_MS["4h"])
    with tempfile.TemporaryDirectory(prefix="freqtrade-engine-compare-") as directory:
        path = Path(directory)
        result = {"data_sha256": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
                  "bars": len(candles), "source": source_kind, "profitability_assessed": False,
                  "freqtrade": run_freqtrade(candles, path),
                  "reference": run_reference(candles, path),
                  "parity_scope": "same strategy input, not identical execution/risk/fill models",
                  "known_differences": ["Freqtrade mandatory -99% stop sentinel; reference has a 10% account drawdown trigger",
                                        "Freqtrade exits full position; reference caps each exit at EUR75",
                                        "Freqtrade fee/precision/end-liquidation and intrabar model differ from immediate simulation",
                                        "Offline fixture metadata is not verified live Binance market metadata"]}
        result["fixture_fill_and_balance_parity"] = compare_fills(result["freqtrade"], result["reference"])
        return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--source-kind", choices=("synthetic", "unverified_csv"), default="unverified_csv")
    args = parser.parse_args()
    result = run_comparison(args.data, source_kind=args.source_kind)
    output = json.dumps(result, indent=2, sort_keys=True, allow_nan=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n")
    print(output)
