# Tradingbot

Crypto research and paper trading with a server-owned simulation worker,
persistent SQLite memory, and an authenticated dashboard.

Start the current dashboard with [SERVER.md](SERVER.md). A fresh account starts
paused with EUR 300 of simulated funds. Docker and Fly deployment files are
included in [FLY_DEPLOY.md](FLY_DEPLOY.md); **the app has not been deployed to
Fly**. Automatic real-money orders and OpenAI analysis are not connected.

The core command-line tools use the Python standard library. The optional
server has separately pinned dependencies. The original CLI workflow includes:

1. **fetch** — download real historical prices (free, no account needed)
2. **backtest** — replay a strategy over that history with realistic costs
3. **paper** — run a strategy on live prices with fake money

```
idea  →  backtest (past data)  →  paper trade (live data, fake money)  →  maybe, one day, real money
```

For the command-line workflow, follow [FIRST_TRADE.md](FIRST_TRADE.md) — the
complete step-by-step walkthrough from zero to your first (paper) trade, and
the gated road to the first real one.

The earlier Netlify/Supabase frontend remains documented in [WEB.md](WEB.md).
It is separate from the current server dashboard in `dashboard/`.

---

## Reality check (read this first)

You heard about a kid who made a bot and bought a Lamborghini. Here is the
part of that story nobody posts about:

- **Survivorship bias.** Thousands of people tried the same thing. The ones
  who lost everything don't get talked about. You heard about the one winner
  because he's the one winner.
- **The math of $100.** A world-class hedge fund makes ~20–30% per year.
  On $100 that is $25. A Lamborghini from $100 requires roughly a 4,000,000%
  return — that's a lottery ticket, not a strategy.
- **Costs are brutal at this size.** Every trade pays ~0.1% fee plus
  slippage. The demo backtest below made 95 trades and burned **$17 of the
  $100 in costs alone**. The market can be neutral; the fees never are.
- **Most retail bots lose.** Not because bots are bad, but because the
  easy-looking edges are already taken by firms with microsecond
  connections and teams of PhDs. EU brokers are legally required to tell
  you that ~75–89% of retail accounts lose money. They are not joking.

**So why build one?** Because it's one of the best programming projects that
exists: real data, real APIs, statistics, risk management, and instant
punishment for sloppy thinking. Treat the $100 as tuition. The skill is the
asset. If the bot also makes money — great, that's a bonus, not the plan.

## Why crypto and not forex

- You can trade $10 positions on crypto. Forex effectively requires
  leverage to matter at this size, and **leverage is how small accounts die**.
- Crypto market data and APIs are free and open; forex data is broker-locked.
- Markets run 24/7, so an hourly bot always has something to chew on.
- Spot crypto means you own the coin — the worst case is the coin's price.
  With leveraged forex/CFDs the worst case is your whole account, fast.

## Quickstart

Needs only Python 3.10+. No `pip install`, nothing else.

```bash
git clone https://github.com/juresavron/tradingbot.git
cd tradingbot

# 1. Download a year of real hourly BTC prices
python3 -m tradingbot fetch --symbol BTC/USDT --timeframe 1h --days 365

# 2. Backtest a strategy on it
python3 -m tradingbot backtest --data data/BTCUSDT_1h_365d.csv --strategy sma_cross

# 3. Paper trade it on live prices (fake $100, Ctrl+C to stop, resumes anytime)
python3 -m tradingbot paper --strategy sma_cross --cash 100
```

Other useful commands:

```bash
python3 -m tradingbot strategies                  # list strategies + parameters
python3 -m tradingbot backtest --show-trades ...  # print every trade
python3 -m tradingbot backtest --stop 0.05 ...    # add a 5% stop-loss
python3 -m tradingbot synth --days 365            # offline fake data (for testing the machinery)
python3 -m unittest discover -s tests             # run the test suite
```

Before choosing a strategy for paper trading, use the chronological
training/holdout workflow in [RESEARCH.md](RESEARCH.md). The `research`
command selects on earlier data, tests the chosen candidate on later data,
and checks double trading costs. It includes an unproven, slower
`donchian_breakout` candidate. Passing research checks does not establish
future profitability.

For persistent memory, timestamped news from RSS or an existing Apify dataset,
and atomic paper execution, see [NEWS_BOT.md](NEWS_BOT.md). The `agent-paper`
command records news context, decisions, balances, and fills in SQLite. News
freshness can gate new entries; headlines do not automatically become orders.

The [execution milestone](EXECUTION.md) adds durable order intents, risk
checks, an offline execution simulator, and a Binance Spot Testnet adapter.
Use `python3 -m tradingbot execution --help` for its manual order lifecycle.
Simulation is the default; remote Testnet integration remains unverified.

The [server dashboard](SERVER.md) connects strategies to automatic simulated
buy/sell execution with persistent memory, authenticated controls, and a
responsive frontend. It runs independently of the browser. Deployment files
for one always-on Fly.io machine and SQLite volume are in [FLY_DEPLOY.md](FLY_DEPLOY.md).
OpenAI analysis and automatic real-money trading are not enabled.

The current [infrastructure assessment](INFRASTRUCTURE_REVIEW.md) records
verified foundations, reproduced defects, and the remaining requirements
before unattended or real-money operation. The server is a simulation
prototype; a deployed dashboard and passing unit tests do not establish
production readiness or profitability.

The [architecture research](ARCHITECTURE_RESEARCH.md) compares seven existing
trading engines and AI frameworks against our requirements, with pinned
official sources and a proposed integration evaluation.

The [isolated Freqtrade evaluation](FREQTRADE_EVALUATION.md) now records an
executed stable-release comparison. Same-fixture signals and fills matched,
but recovery/control acceptance gaps and blocked live-data access prevent
adoption as-is. The current application was retained.

If `fetch` fails, the exchange API may be blocked on your network (some
countries/networks block Binance) — the bot automatically tries a mirror,
but if both fail, try another network.

## What a report looks like

Example run (on **synthetic** data — the development sandbox has no market
access; your `fetch` gets the real thing):

```
──────────────────────────────────────────────────────────
 Backtest  sma_cross(fast=20, slow=50)
 synthetic_seed42_1h_365d.csv · 1h · 2025-08-11 → 2026-08-11  (365 days, 8760 bars)
 fee 0.10%/side · slippage 0.05% · start $100.00
──────────────────────────────────────────────────────────
 Final equity          $111.04
 Total return          +11.04%
 CAGR                  +11.04%/yr
 Max drawdown          -39.23%
 Sharpe (annualized)   0.46
 Trades                95  (win rate 33%)
 Avg win / avg loss    +5.42% / -2.30%
 Time in market        52%
 Fees paid             $17.03
──────────────────────────────────────────────────────────
 Buy & hold return     +74.31%  (max DD -41.79%)
 Edge vs buy & hold    -63.27 pts
──────────────────────────────────────────────────────────
```

How to read it, in one paragraph: the strategy made +11% — sounds nice until
the last line, which says just *buying and holding* would have made +74%.
The strategy traded 95 times, paid $17 in costs, was wrong 67% of the time,
and at its worst moment was down 39% ("max drawdown"). This is the whole
point of the tool: **it stops you from believing a bad strategy.** The
benchmark to beat is never zero — it's doing nothing. (The live report
also draws a small ASCII chart of your equity over time.)

## What you'll learn from the code (reading guide)

Read the files in this order — each is short and commented:

| file | what it teaches |
|---|---|
| `tradingbot/candles.py` | how market data is structured (OHLCV bars) |
| `tradingbot/indicators.py` | SMA and RSI computed from scratch — no library black boxes |
| `tradingbot/strategy.py` | strategies as code: turning candles into buy/sell decisions |
| `tradingbot/backtest.py` | the heart. Fees, slippage, stop-losses, and the **no-lookahead rule** |
| `tradingbot/exchange.py` | talking to a real exchange REST API with plain `urllib` |
| `tradingbot/paper.py` | live trading mechanics minus the money: state, polling, kill switch |
| `tests/` | how to prove trading code is honest (the no-lookahead and fee tests) |

Three ideas matter more than everything else:

1. **No lookahead.** A signal computed at candle *i*'s close executes at
   candle *i+1*'s open. Backtests that cheat on this (accidentally trading
   on prices they couldn't have known yet) look amazing and are worthless.
   Most "95% winning bot!!" videos die right here.
2. **Costs always on.** Fee + slippage, both directions, every trade.
3. **Overfitting.** If you tune `--params` until history looks great, you've
   memorized the past, not predicted the future. Rule of thumb: tune on one
   year, then test untouched on a *different* year. If the edge vanishes,
   it was never there.

## The rules (non-negotiable)

1. **No leverage. Ever.** Spot only.
2. **Paper trade for at least 3–4 weeks** before any real money.
3. Real money only if paper results beat buy & hold **after costs**.
4. Never more than money you can lose without caring. $100, not $101.
5. The kill switch stays on: down 25% from peak → bot flattens and stops.
6. API keys (when we get there) live on your machine, never in git, and are
   created **without withdrawal permissions**.

## Roadmap

- [x] **Phase 0** — data, backtester, two baseline strategies, paper trading, tests
- [ ] **Phase 1** — backtest on real data across coins/timeframes/parameters; learn what fails and why
- [ ] **Phase 2** — build *our* strategy from what Phase 1 taught (better exits, volatility filters, longer timeframes to cut fee bleed)
- [ ] **Phase 3** — 3–4 weeks of live paper trading; keep only what beats buy & hold after costs
- [ ] **Phase 4** — (only if Phase 3 is convincing) live trading module: exchange API keys, tiny size, hard stop rules

## Will this make me rich?

Almost certainly not. It will make you *dangerous* in the best way: you'll
be able to read a strategy pitch and know within minutes whether it
survives fees, drawdown, and lookahead — which puts you ahead of ~95% of
people who trade. That skill compounds better than $100 does.
