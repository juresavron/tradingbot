"""Reproduce the fixed BTC/EUR screen without optimizing its parameters.

Run: python scripts/evaluate_live_paper.py DATA.csv --out NEW_REPORT.json
The CSV needs a sibling source.json containing its download provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingbot.backtest import run_backtest
from tradingbot.candles import load_csv
from tradingbot.research import Candidate, evaluate
from tradingbot.strategy import BuyHold, DonchianBreakout, Strategy


class WarmStrategy(Strategy):
    def __init__(self, history):
        self.history = history

    def signals(self, candles):
        signals = DonchianBreakout(120, 60).signals(self.history + candles)
        return signals[len(self.history):]


def evaluate_file(path: Path) -> dict:
    bars = load_csv(path)
    source = json.loads(path.with_name("source.json").read_text())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if source["sha256"] != digest or source["bars"] != len(bars):
        raise ValueError("CSV does not match its recorded source manifest")
    boundary = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    split = next((i for i, bar in enumerate(bars) if bar.ts >= boundary), len(bars))
    if split < 3 or len(bars) - split < 3:
        raise ValueError("Need history before and after the fixed 2026 evaluation boundary")
    settings = dict(symbol="BTC/EUR", timeframe="4h", starting_cash=300,
                    fee=0.001, slippage=0.0005, size=0.25)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fixed_strategy": "donchian_breakout(120,60)",
        "selection": "existing production strategy; parameters not optimized",
        "input_sha256": digest, "source": source, "assumptions": settings, "periods": [],
    }
    periods = [("Full period", 0, len(bars)), ("Training 2023-2025", 0, split),
               ("Held-out 2026", split, len(bars))]
    for year in (2023, 2024, 2025, 2026):
        indexes = [i for i, bar in enumerate(bars)
                   if datetime.fromtimestamp(bar.ts / 1000, timezone.utc).year == year]
        if indexes:
            periods.append((str(year), indexes[0], indexes[-1] + 1))
    for label, first, end in periods:
        sample = bars[first:end]
        strategy = run_backtest(sample, WarmStrategy(bars[:first]), **settings)
        stress = run_backtest(sample, WarmStrategy(bars[:first]),
                              **{**settings, "fee": 0.002, "slippage": 0.001})
        benchmark = run_backtest(sample, BuyHold(), **settings)
        full_benchmark = run_backtest(sample, BuyHold(), **{**settings, "size": 1})
        report["periods"].append({
            "label": label, "start_ms": sample[0].ts, "last_open_ms": sample[-1].ts,
            "bars": len(sample), "strategy": strategy.metrics,
            "double_cost_strategy": stress.metrics, "buy_hold_25pct": benchmark.metrics,
            "buy_hold_100pct": full_benchmark.metrics, "equity": strategy.equity,
            "benchmark_equity": benchmark.equity, "trades": [vars(t) for t in strategy.trades],
        })
    report["holdout_screen"] = evaluate(
        bars, candidates=(Candidate("donchian_breakout", {"entry_period": "120", "exit_period": "60"}),),
        timeframe="4h", train_fraction=(split + 0.1) / len(bars), starting_cash=300,
        fee=0.001, slippage=0.0005, size=0.25, max_drawdown=0.1, min_trades=10,
    )
    report["limitations"] = [
        "Research uses next-open fills and 25% of available cash per entry; the deployed simulator also applies an EUR75 order cap, lot/minimum-notional rules and a 10% drawdown halt.",
        "The research engine does not simulate the deployed drawdown halt, capped exits, dust, partial fills, liquidity or network outages.",
        "Fee (0.10% per side) and slippage (0.05% per side) are declared assumptions, not account-specific exchange charges.",
        "The 25% buy-and-hold benchmark invests 25% initially and retains 75% cash. The 100% benchmark has greater exposure.",
        "Yearly results reset to EUR300 each year with prior candles used only for signal warmup; they are not compounded.",
        "2026 is held separate within this run. Its results are now observed and must not be reused for tuning; prior strategy development may already have seen this market period.",
        "A forward paper account begins today and is not represented by historical research results.",
    ]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Refusing to overwrite observed research results")
    report = evaluate_file(args.data)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(json.dumps({"checks": report["holdout_screen"]["checks"],
                      "report": str(args.out), "input_sha256": report["input_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
