# Live-market paper trial

The existing Donchian breakout candidate remains fixed at 120/60 candles on
BTC/EUR, 4-hour candles. Public Binance REST data is read from the deployed
Frankfurt machine; only closed candles generate decisions. Orders remain
entirely inside `SimulatedBroker`, without exchange credentials.

The forward experiment starts with EUR300 virtual cash, a EUR75 order cap,
25% of available cash per entry, a 10% account drawdown trigger, a 0.10%
simulated fee per fill and 0.05% adverse slippage per fill. These are explicit
model assumptions. Tick rounding can increase modeled slippage slightly.
Synthetic lot and minimum-notional rules are not verified exchange filters.

Cost assumptions are immutable within a simulation revision. Existing
revisions migrate with their original fee (0.10%) and zero slippage. Starting
a new experiment retains the old wallet and journal. Fill costs, decisions,
equity and sanitized operational errors/recoveries are retained in SQLite;
the dashboard shows recent records and cumulative cost totals.

## Historical research

`scripts/evaluate_live_paper.py` reproduces the fixed-strategy screen from a
CSV and its `source.json` download manifest. It checks the CSV hash and count,
uses 2023-2025 for the training segment, and keeps 2026 separate in this run.
It writes a new report and refuses to overwrite previously observed results.

The screen includes normal/double costs and buy-and-hold benchmarks with 25%
and 100% initial allocation. Its simplified research engine has no account
drawdown halt, capped exits or lot/minimum-notional rules, so its returns are
not an exact replay of the deployed execution model. The report states this
limitation alongside the results. A 2026 sample of five completed trades fails
the predeclared ten-trade screen; it is not evidence to advance to real money.

Example from the repository root:

```sh
python scripts/evaluate_live_paper.py data/BTCEUR_4h.csv --out data/new-report.json
```

The forward trial is a separate, persistent experiment. Restarts retain its
configuration, positions, journal and requested running state. No real-money
adapter or exchange keys are introduced by enabling it.
