"""Backtester: replay a strategy over historical candles with realistic costs.

Design rules that keep results honest:

1. No lookahead. A signal computed at the close of candle i is executed at the
   OPEN of candle i+1. You cannot trade on a close you haven't seen finish.
2. Costs always on. Every fill pays taker fee + slippage, both sides. On a
   $100 account, costs are the main opponent.
3. Stops are pessimistic. A stop-loss fills at the stop price, or at the open
   if the market gapped straight past the stop (whichever is worse).

A backtest that passes here can still fail live (overfitting, regime change,
thin liquidity). The reverse is more reliable: a strategy that loses here
will definitely lose live. This tool is a filter, not a crystal ball.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .candles import Candle, bars_per_year
from .strategy import Strategy


@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    qty: float
    fees: float
    pnl: float
    pnl_pct: float
    reason: str  # "signal" | "stop" | "end"


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    strategy: str
    equity: list[tuple[int, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def max_drawdown(equity_values: list[float]) -> float:
    """Worst peak-to-trough drop, as a fraction (0.25 = -25%)."""
    peak = -math.inf
    worst = 0.0
    for v in equity_values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def sharpe_ratio(equity_values: list[float], periods_per_year: float) -> float:
    """Annualized Sharpe ratio of per-bar returns (risk-free rate assumed 0)."""
    if len(equity_values) < 3:
        return 0.0
    returns = []
    for prev, cur in zip(equity_values, equity_values[1:]):
        if prev > 0:
            returns.append(cur / prev - 1.0)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean / std * math.sqrt(periods_per_year)


def run_backtest(
    candles: list[Candle],
    strategy: Strategy,
    symbol: str = "?",
    timeframe: str = "1h",
    starting_cash: float = 100.0,
    fee: float = 0.001,
    slippage: float = 0.0005,
    size: float = 1.0,
    stop_pct: float | None = None,
) -> BacktestResult:
    if len(candles) < 3:
        raise ValueError("Need at least 3 candles to backtest")
    if not 0 < size <= 1:
        raise ValueError("size must be in (0, 1]")

    signals = strategy.signals(candles)
    result = BacktestResult(symbol=symbol, timeframe=timeframe, strategy=strategy.describe())

    cash = starting_cash
    qty = 0.0
    entry_ts = 0
    entry_price = 0.0
    entry_cost = 0.0
    entry_fee = 0.0
    stop_price: float | None = None
    pending: int | None = None
    fees_paid = 0.0
    bars_in_market = 0

    def close_position(ts: int, fill: float, reason: str) -> None:
        nonlocal cash, qty, fees_paid, stop_price
        proceeds = qty * fill
        exit_fee = proceeds * fee
        cash += proceeds - exit_fee
        fees_paid += exit_fee
        pnl = (proceeds - exit_fee) - entry_cost
        result.trades.append(
            Trade(
                entry_ts=entry_ts,
                entry_price=entry_price,
                exit_ts=ts,
                exit_price=fill,
                qty=qty,
                fees=entry_fee + exit_fee,
                pnl=pnl,
                pnl_pct=pnl / entry_cost if entry_cost else 0.0,
                reason=reason,
            )
        )
        qty = 0.0
        stop_price = None

    for i, c in enumerate(candles):
        # 1) Execute the order decided at the previous candle's close, at this open.
        if pending is not None:
            if pending == 1 and qty == 0.0:
                fill = c.open * (1 + slippage)
                budget = cash * size
                new_qty = budget / (fill * (1 + fee))
                buy_fee = new_qty * fill * fee
                cash -= budget
                qty = new_qty
                entry_ts, entry_price = c.ts, fill
                entry_cost, entry_fee = budget, buy_fee
                fees_paid += buy_fee
                stop_price = fill * (1 - stop_pct) if stop_pct else None
            elif pending == 0 and qty > 0.0:
                close_position(c.ts, c.open * (1 - slippage), "signal")
            pending = None

        # 2) Stop-loss may trigger anywhere inside this candle.
        if qty > 0.0 and stop_price is not None and c.low <= stop_price:
            close_position(c.ts, min(c.open, stop_price) * (1 - slippage), "stop")

        # 3) Mark equity at the close.
        equity_now = cash + qty * c.close
        result.equity.append((c.ts, equity_now))
        if qty > 0.0:
            bars_in_market += 1

        # 4) Decide what to do at the next open.
        target = signals[i]
        current = 1 if qty > 0.0 else 0
        if target != current:
            pending = target

    # Liquidate any open position at the final close so all PnL is realized.
    last = candles[-1]
    if qty > 0.0:
        close_position(last.ts, last.close * (1 - slippage), "end")
        result.equity[-1] = (last.ts, cash)

    equity_values = [v for _, v in result.equity]
    final_equity = equity_values[-1]
    days = (last.ts - candles[0].ts) / 86_400_000 or 1.0

    # Buy & hold benchmark with the same cost model AND the same execution
    # rule: enter at the second candle's open, the earliest moment any signal
    # can execute. Otherwise the benchmark gets a one-bar head start and even
    # the buy_hold strategy would appear to beat "buy & hold".
    bh_fill = candles[1].open * (1 + slippage)
    bh_qty = starting_cash / (bh_fill * (1 + fee))
    bh_curve = [starting_cash] + [bh_qty * c.close for c in candles[1:]]
    bh_final = bh_qty * last.close * (1 - slippage) * (1 - fee)

    wins = [t for t in result.trades if t.pnl > 0]
    losses = [t for t in result.trades if t.pnl <= 0]
    total_return = final_equity / starting_cash - 1.0
    bh_return = bh_final / starting_cash - 1.0

    result.metrics = {
        "starting_cash": starting_cash,
        "final_equity": final_equity,
        "total_return_pct": total_return * 100,
        "cagr_pct": ((final_equity / starting_cash) ** (365.0 / days) - 1.0) * 100
        if final_equity > 0
        else -100.0,
        "max_drawdown_pct": max_drawdown(equity_values) * 100,
        "sharpe": sharpe_ratio(equity_values, bars_per_year(timeframe)),
        "n_trades": len(result.trades),
        "win_rate_pct": len(wins) / len(result.trades) * 100 if result.trades else 0.0,
        "avg_win_pct": sum(t.pnl_pct for t in wins) / len(wins) * 100 if wins else 0.0,
        "avg_loss_pct": sum(t.pnl_pct for t in losses) / len(losses) * 100 if losses else 0.0,
        "exposure_pct": bars_in_market / len(candles) * 100,
        "fees_paid": fees_paid,
        "bh_return_pct": bh_return * 100,
        "bh_max_drawdown_pct": max_drawdown(bh_curve) * 100,
        "edge_vs_bh_pct": (total_return - bh_return) * 100,
        "days": days,
        "bars": len(candles),
    }
    return result
