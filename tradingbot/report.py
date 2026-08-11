"""Terminal report formatting for backtest results."""

from __future__ import annotations

from datetime import datetime, timezone

from .backtest import BacktestResult

RULE = "─" * 58


def _date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def ascii_chart(values: list[float], width: int = 58, height: int = 9) -> str:
    """Filled area chart drawn with block characters. Zero dependencies."""
    if len(values) < 2:
        return "(not enough data to chart)"
    points = []
    for col in range(width):
        pos = col * (len(values) - 1) / (width - 1)
        i = int(pos)
        frac = pos - i
        v = values[i] if i + 1 >= len(values) else values[i] * (1 - frac) + values[i + 1] * frac
        points.append(v)
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    lines = []
    for row in range(height, 0, -1):
        threshold = lo + span * (row - 0.5) / height
        body = "".join("█" if p >= threshold else " " for p in points)
        if row == height:
            label = f"{hi:>10.2f} ┤"
        elif row == 1:
            label = f"{lo:>10.2f} ┤"
        else:
            label = "           │"
        lines.append(label + body)
    return "\n".join(lines)


def format_report(result: BacktestResult, fee: float, slippage: float, show_trades: bool = False) -> str:
    m = result.metrics
    first_ts = result.equity[0][0]
    last_ts = result.equity[-1][0]

    def row(label: str, value: str) -> str:
        return f" {label:<22}{value}"

    lines = [
        RULE,
        f" Backtest  {result.strategy}",
        f" {result.symbol} · {result.timeframe} · {_date(first_ts)} → {_date(last_ts)}"
        f"  ({m['days']:.0f} days, {m['bars']:.0f} bars)",
        f" fee {fee * 100:.2f}%/side · slippage {slippage * 100:.2f}% · start ${m['starting_cash']:.2f}",
        RULE,
        row("Final equity", f"${m['final_equity']:.2f}"),
        row("Total return", f"{m['total_return_pct']:+.2f}%"),
        row("CAGR", f"{m['cagr_pct']:+.2f}%/yr"),
        row("Max drawdown", f"-{m['max_drawdown_pct']:.2f}%"),
        row("Sharpe (annualized)", f"{m['sharpe']:.2f}"),
        row("Trades", f"{m['n_trades']:.0f}  (win rate {m['win_rate_pct']:.0f}%)"),
        row("Avg win / avg loss", f"{m['avg_win_pct']:+.2f}% / {m['avg_loss_pct']:+.2f}%"),
        row("Time in market", f"{m['exposure_pct']:.0f}%"),
        row("Fees paid", f"${m['fees_paid']:.2f}"),
        RULE,
        row("Buy & hold return", f"{m['bh_return_pct']:+.2f}%  (max DD -{m['bh_max_drawdown_pct']:.2f}%)"),
        row("Edge vs buy & hold", f"{m['edge_vs_bh_pct']:+.2f} pts"),
        RULE,
        " Equity curve ($)",
        ascii_chart([v for _, v in result.equity]),
        RULE,
    ]

    if show_trades and result.trades:
        lines.append(f" {'entry':<12}{'exit':<12}{'entry $':>12}{'exit $':>12}{'pnl %':>9}  reason")
        for t in result.trades:
            lines.append(
                f" {_date(t.entry_ts):<12}{_date(t.exit_ts):<12}"
                f"{t.entry_price:>12.2f}{t.exit_price:>12.2f}{t.pnl_pct * 100:>+8.2f}%  {t.reason}"
            )
        lines.append(RULE)

    return "\n".join(lines)
