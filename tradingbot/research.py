"""Chronological strategy selection followed by one held-out evaluation.

This is a research screen, not a profitability prediction or deployment gate.
Repeatedly changing candidates after seeing the holdout turns it into training
data. All candidate strategies must use only present and past candles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .backtest import BacktestResult, run_backtest
from .candles import Candle
from .strategy import Strategy, make_strategy


@dataclass(frozen=True)
class Candidate:
    name: str
    params: dict[str, str] = field(default_factory=dict)

    def build(self) -> Strategy:
        return make_strategy(self.name, self.params)


DEFAULT_CANDIDATES = (
    Candidate("sma_cross", {"fast": "20", "slow": "50"}),
    Candidate("sma_cross", {"fast": "60", "slow": "180"}),
    Candidate("rsi_revert"),
    Candidate("donchian_breakout"),
)


class _HoldoutSignals(Strategy):
    """Warm up signals on past data, but fund and trade only the holdout.

    The first held-out close generates the first tradable signal; its fill is
    the following open, matching the held-out buy-and-hold benchmark. No
    training-period account positions or returns cross the boundary.
    """

    def __init__(self, strategy: Strategy, history: list[Candle], split: int):
        self.strategy = strategy
        self.history = history
        self.split = split

    def signals(self, candles: list[Candle]) -> list[int]:
        return self.strategy.signals(self.history + candles)[self.split:]

    def describe(self) -> str:
        return self.strategy.describe()


def evaluate(
    candles: list[Candle],
    *,
    candidates: tuple[Candidate, ...] = DEFAULT_CANDIDATES,
    timeframe: str = "4h",
    train_fraction: float = 0.7,
    starting_cash: float = 300.0,
    fee: float = 0.001,
    slippage: float = 0.0005,
    size: float = 1.0,
    max_drawdown: float = 0.25,
    min_trades: int = 10,
) -> dict:
    """Select by training Sharpe, with return breaking ties; never rank on test.

    A candidate with no training trades is ineligible. The selected candidate
    alone is evaluated on the holdout at normal and double fees/slippage.
    Drawdown is measured at candle closes, not an intrabar risk guarantee.
    """
    if not math.isfinite(train_fraction) or not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be in (0, 1)")
    if not math.isfinite(max_drawdown) or not 0 < max_drawdown <= 1:
        raise ValueError("max_drawdown must be in (0, 1]")
    if not isinstance(min_trades, int) or isinstance(min_trades, bool) or min_trades < 1:
        raise ValueError("min_trades must be a positive integer")
    if not candidates:
        raise ValueError("At least one candidate is required")
    split = int(len(candles) * train_fraction)
    if split < 3 or len(candles) - split < 3:
        raise ValueError("Need at least 3 candles in both training and holdout")

    kwargs = dict(timeframe=timeframe, starting_cash=starting_cash,
                  fee=fee, slippage=slippage, size=size)
    # Validate the entire series, including spacing at the split boundary,
    # before ranking anything. A flat signal never places an order.
    class Flat(Strategy):
        def signals(self, bars):
            return [0] * len(bars)

    run_backtest(candles, Flat(), **kwargs)
    if fee * 2 >= 1 or slippage * 2 >= 1:
        raise ValueError("Double-cost stress test requires fee/slippage below 0.5")

    training = []
    for candidate in candidates:
        result = run_backtest(candles[:split], candidate.build(), **kwargs)
        training.append((candidate, result))
    eligible = [(c, r) for c, r in training if r.metrics["n_trades"] > 0]
    if not eligible:
        raise ValueError("No candidate traded in training; use more history or review candidates")
    winner, selected = max(eligible, key=lambda pair: (
        pair[1].metrics["sharpe"], pair[1].metrics["total_return_pct"]))

    def held_out(stress: bool = False) -> BacktestResult:
        options = dict(kwargs)
        if stress:
            options.update(fee=fee * 2, slippage=slippage * 2)
        strategy = _HoldoutSignals(winner.build(), candles[:split], split)
        return run_backtest(candles[split:], strategy, **options)

    holdout, stressed = held_out(), held_out(stress=True)
    m, s = holdout.metrics, stressed.metrics
    checks = {
        "positive_holdout_return_after_costs": m["total_return_pct"] > 0,
        "holdout_beats_buy_and_hold_after_costs": m["edge_vs_bh_pct"] > 0,
        "holdout_drawdown_within_limit": m["max_drawdown_pct"] <= max_drawdown * 100,
        "enough_holdout_trades": m["n_trades"] >= min_trades,
        "at_least_90_days_in_holdout": m["days"] >= 90,
        "positive_return_at_double_costs": s["total_return_pct"] > 0,
        "double_cost_drawdown_within_limit": s["max_drawdown_pct"] <= max_drawdown * 100,
    }
    return {
        "method": "chronological_holdout",
        "selection_rule": "training Sharpe, then training net return; requires a training trade",
        "settings": {**kwargs, "train_fraction": train_fraction,
                     "max_drawdown": max_drawdown, "min_trades": min_trades},
        "split": {"train_bars": split, "holdout_bars": len(candles) - split,
                  "train_start_ts": candles[0].ts, "train_end_ts": candles[split - 1].ts,
                  "holdout_start_ts": candles[split].ts, "holdout_end_ts": candles[-1].ts},
        "training": [{"strategy": r.strategy, "metrics": r.metrics,
                      "eligible": r.metrics["n_trades"] > 0} for _, r in training],
        "selected": {"name": winner.name,
                     "params": {k: str(v) for k, v in vars(winner.build()).items()},
                     "strategy": selected.strategy},
        "holdout": m,
        "double_cost_holdout": s,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
        "limitations": [
            "A single holdout does not establish a durable edge or future profitability.",
            "Do not retune against this holdout; reserve new unseen data for further tests.",
            "Drawdown uses candle-close equity; no drawdown kill switch is simulated.",
            "Fees and slippage are assumptions; minimum order sizes, spread changes, and outages are not simulated.",
            "Paper initializes indicators from its first price snapshot; match initial history and verify live execution separately.",
        ],
    }


def format_research(report: dict) -> str:
    split, settings = report["split"], report["settings"]
    lines = [
        f"Research: {split['train_bars']} training bars / {split['holdout_bars']} held-out bars",
        f"Costs: fee {settings['fee'] * 100:.2f}%/side, slippage {settings['slippage'] * 100:.2f}%/side",
        "Training candidates (selection never uses held-out results):",
    ]
    for row in report["training"]:
        m = row["metrics"]
        status = "" if row["eligible"] else " [ineligible: no trades]"
        lines.append(f"  {row['strategy']}: Sharpe {m['sharpe']:.2f}, net {m['total_return_pct']:+.2f}%{status}")
    lines.append(f"Selected: {report['selected']['strategy']}")
    for label, key in [("Holdout", "holdout"), ("Holdout at double costs", "double_cost_holdout")]:
        m = report[key]
        lines.append(f"{label}: net {m['total_return_pct']:+.2f}%, buy & hold {m['bh_return_pct']:+.2f}%, "
                     f"close-to-close drawdown {m['max_drawdown_pct']:.2f}%, trades {m['n_trades']}")
    lines.append("Research checks:")
    for name, passed in report["checks"].items():
        lines.append(f"  {'PASS' if passed else 'FAIL'} {name.replace('_', ' ')}")
    lines.append("Checks passed." if report["all_checks_passed"] else "Checks failed; no evidence to advance this candidate.")
    lines.extend(report["limitations"])
    return "\n".join(lines)
