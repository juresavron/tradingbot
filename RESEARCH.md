# Research before risking money

Current target: Binance spot trading, a proposed €300 account, starting with
BTC/EUR on four-hour candles if that pair is available to your account. No
real-money execution is implemented. We have **not established a profitable
strategy**. Synthetic data is useful for checking software, never for proving
a trading edge.

## Run a chronological holdout study

Public prices need no exchange account credentials. Once the cloud environment
allows `data-api.binance.vision` and `api.binance.com`, fetch a historical sample:

```bash
cd /workspace/tradingbot
python3 -m tradingbot fetch --symbol BTC/EUR --timeframe 4h --days 1095
python3 -m tradingbot research --data data/BTCEUR_4h_1095d.csv \
  --timeframe 4h --cash 300 --fee 0.001 --slippage 0.0005 \
  --out data/research_btceur_4h_first.json
```

The fee of 0.10% and slippage of 0.05% per side are **assumptions**, not verified
account pricing. Confirm your Binance account's actual tier and pair-specific
fees before using results. Cash is in the CSV's quote currency: `--cash 300`
means €300 for BTC/EUR, but 300 USDT for BTC/USDT. Data from one pair is not
evidence about another pair's execution costs.

The command does the following:

1. Checks prices, OHLC ranges, volume, costs, and exact timestamp spacing.
   Gaps, duplicate bars, and a wrong `--timeframe` are errors; do not fabricate
   candles to make them pass.
2. Splits candles chronologically: the earliest 70% for training and the final
   30% for a holdout. It compares four predefined candidates on training data:
   two SMA settings, RSI mean reversion, and a Donchian breakout.
3. Selects the highest training Sharpe among candidates that traded; net return
   breaks ties. Only the selected candidate is evaluated on the holdout.
4. Uses earlier candles to warm up indicators, but starts a fresh, funded
   account in the holdout. The first possible fill is the second held-out
   candle's open. Buy and hold uses the same fill rule and costs.
5. Repeats that holdout with double fees and slippage. Reports returns,
   buy-and-hold performance, drawdown, trade count, and explicit research checks.
6. Writes settings, all candidate training metrics, holdout metrics, and the
   input CSV's SHA-256 to a new JSON report. Existing reports are not overwritten.

The screen asks for positive net returns, outperformance of buy and hold,
drawdown no larger than 25%, at least ten held-out trades, at least 90 held-out
days, and positive returns with acceptable drawdown under double costs.
These defaults are research filters, **not statistical proof**, a personalized
risk limit, or permission to trade. A short sample may legitimately fail them.
A successful command exits zero even if research checks fail; read the report's
`all_checks_passed` and individual `checks`.

The Donchian candidate enters after a close breaks above the highest high of
the preceding 120 bars and exits below the preceding 60-bar low. Channels
exclude the current bar. Signals hold their position between breakouts; this
is intended to reduce turnover relative to fast crossover trading, but it can
still lose money. Defaults mean approximately 20-day entries and 10-day exits
on four-hour bars. No parameters were selected from live market results here.

## Keep the holdout useful

Choose candidates and rules before looking at the holdout. After seeing it,
do not repeatedly retune until that same period passes. Reserve another later
period, test across different market conditions and pairs, and retain failed
reports. Even a good holdout can reflect chance. Fees, liquidity, minimum order
sizes, exchange outages, and changing market conditions can erase an apparent
edge.

Drawdown here measures candle-close equity. It does not capture all intrabar
losses, and `research --max-drawdown` only sets a screening threshold: it does
not simulate the paper bot's continuously polled kill switch. Backtest
`--stop` and the paper drawdown kill switch are different rules. Do not treat
those results as interchangeable.

## Paper trading comes next

After real-data checks and signal/execution parity are verified, run a paper
experiment for several weeks using a fixed configuration and a fresh state
file. Confirm the selected strategy, costs, symbol, timeframe, and initial
indicator history match what is being evaluated. A paper experiment validates
operation and fills under current conditions; several weeks alone cannot
prove profitability.

Paper accounts retain their initial candle snapshot and subsequent closed
candles, including across restarts. A sliding API window therefore no longer
resets a longstanding RSI or breakout position. Conflicting candles and missing
intervals stop the run instead of silently changing the signal history. History
and per-candle computation grow with the experiment; idle polls do not add
duplicate history. The initial snapshot can still be shorter than the research
warmup, so verify the initial stance rather than assuming exact parity.

New paper accounts record their experiment settings. Resuming with changed
strategy, capital, fees, slippage, sizing, risk threshold, or lookback is
rejected. Older account files lacking this metadata are also rejected rather
than silently guessing their historical assumptions. They are preserved;
start a separate experiment with a new `--state` path.

The cloud environment currently blocks both market-data hosts with proxy 403
responses. Their domain additions are saved in the environment draft but must
be applied through environment settings before real-data fetching or live
paper trading can be validated. No exchange API keys or deposit are needed
for this step.

For an offline software check, use clearly labelled synthetic data:

```bash
python3 -m tradingbot synth --days 1095 --timeframe 4h --seed 42
python3 -m tradingbot research --data data/synthetic_seed42_4h_1095d.csv \
  --timeframe 4h --cash 300
python3 -m unittest discover -s tests
```
