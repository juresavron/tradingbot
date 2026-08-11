# Your First Trade — the complete walkthrough

There are two "first trades", and they happen weeks apart:

1. **Your first paper trade — today.** The bot trades live prices with fake
   money. Costs nothing, teaches everything.
2. **Your first real trade — only after the graduation gate.** Real €100,
   real exchange, real fees, and a checklist that must pass first.

Skipping straight to step 2 is how the "kid with the Lambo" story usually
ends for everyone who isn't that one kid. Do them in order.

Rough timeline:

| when | what |
|---|---|
| today, ~1 hour | setup, learn the backtester, start the paper bot |
| weeks 1–4 | paper bot runs; you check it 2 minutes a day |
| after week 4 | graduation gate: pass → prepare real trading; fail → adjust strategy, repeat |

Everything below was executed and verified on a clean checkout (Python
3.11). If any command behaves differently for you, that's a bug — report it.

---

## Part 1 — Setup (10 minutes)

You need Python 3.10 or newer and nothing else. On Windows, use WSL or
`python` instead of `python3`.

```bash
git clone -b claude/trading-bot-exploration-qyomhj https://github.com/juresavron/tradingbot.git
cd tradingbot
python3 --version        # must say 3.10+
python3 -m unittest discover -s tests
```

The last command must end with:

```
Ran 33 tests in ...s

OK
```

If the tests pass, the machine is trustworthy. Never trade — even on paper —
with failing tests.

## Part 2 — Dry run on fake data (15 minutes)

First learn the tool where mistakes are free. Generate fake data and
backtest against it:

```bash
python3 -m tradingbot synth --days 365 --seed 42
python3 -m tradingbot backtest --data data/synthetic_seed42_1h_365d.csv --strategy sma_cross
```

You'll get a report like the one explained in the [README](README.md#what-a-report-looks-like).
Things to try, each teaching you a flag you'll need later:

```bash
# different strategy parameters
python3 -m tradingbot backtest --data data/synthetic_seed42_1h_365d.csv --strategy sma_cross --params fast=10,slow=30

# see every single trade the bot made
python3 -m tradingbot backtest --data data/synthetic_seed42_1h_365d.csv --strategy rsi_revert --show-trades

# add a 5% stop-loss
python3 -m tradingbot backtest --data data/synthetic_seed42_1h_365d.csv --strategy sma_cross --stop 0.05

# the benchmark itself, as a strategy (edge should be +0.00 pts)
python3 -m tradingbot backtest --data data/synthetic_seed42_1h_365d.csv --strategy buy_hold
```

Small print you might notice: on an exactly-365-day file, CAGR equals total
return (same thing over one year — not a bug).

## Part 3 — Real data, and the fee reality check (30 minutes)

Now real prices:

```bash
python3 -m tradingbot fetch --symbol BTC/USDT --timeframe 1h --days 365
python3 -m tradingbot backtest --data data/BTCUSDT_1h_365d.csv --strategy sma_cross
python3 -m tradingbot backtest --data data/BTCUSDT_1h_365d.csv --strategy rsi_revert
```

(If `fetch` fails, see [Troubleshooting](#part-8--troubleshooting).)

Then do the single most important experiment in this guide. The default fee
(0.10%/side) matches big global exchanges — but the exchange we'd actually
use for real money in the EU charges **0.40% taker** at our size (Part 6).
Re-run your best backtest at real-world costs:

```bash
python3 -m tradingbot backtest --data data/BTCUSDT_1h_365d.csv --strategy sma_cross --fee 0.004
```

Watch what happens to a strategy that trades ~100 times a year when each
round trip costs ~0.9% instead of ~0.3%. Most hourly strategies die right
here. The classic escape is trading less often — try slower timeframes:

```bash
python3 -m tradingbot fetch --symbol BTC/USDT --timeframe 4h --days 365
python3 -m tradingbot backtest --data data/BTCUSDT_4h_365d.csv --timeframe 4h --strategy sma_cross --fee 0.004
```

**The decision gate for this part:** does *anything* beat `buy_hold`'s
return at `--fee 0.004`? Write down your three best configurations
(strategy + params + timeframe). Those are your paper trading candidates.

### Optional but smart: the overfitting test

If you tuned parameters until last year looked great, you may have just
memorized last year. Check on a year the tuning never saw:

```bash
python3 -m tradingbot fetch --symbol BTC/USDT --timeframe 1h --days 730
head -n 8761 data/BTCUSDT_1h_730d.csv > data/train.csv
{ head -n 1 data/BTCUSDT_1h_730d.csv; tail -n 8760 data/BTCUSDT_1h_730d.csv; } > data/test.csv
```

Tune on `train.csv` only. Then run the final choice ONCE on `test.csv`. If
the edge collapses, it was never real. This one habit puts you ahead of
almost every retail bot builder.

## Part 4 — Your first trade (paper, today)

Start the bot with your best configuration from Part 3:

```bash
python3 -m tradingbot paper --strategy sma_cross --cash 100
```

This is a real first trade in every sense except the money: live prices,
real strategy, real fees and slippage simulated. Here's an actual session
(prices differ, format is exact):

```
Paper trading BTC/USDT on 1h candles with sma_cross(fast=20, slow=50) — fake $100.00, live prices.
Signals are checked when a candle closes (every 60 min); prices every 30s. Ctrl+C to stop. State: paper_state.json

[17:00:05] BTC/USDT 49,301.40  flat  equity $100.00 (+0.00%)
[17:00:35] BTC/USDT 49,321.78  flat  equity $100.00 (+0.00%)
        ... (30s polls continue while the next candle forms) ...
[18:00:05] BTC/USDT 48,979.71  LONG  equity $99.85 (-0.15%)  << traded
[19:00:05] BTC/USDT 48,696.07  LONG  equity $99.27 (-0.73%)
        ... (signal stays long for 17 hours) ...
[15:00:05] BTC/USDT 47,220.55  flat  equity $96.12 (-3.88%)  << traded
```

That `<< traded` line at 18:00 is your first trade. Three things to
understand about what you're seeing:

- **Equity drops to -0.15% the instant you buy.** That's not a bug — it's
  the 0.10% fee + 0.05% slippage charged up front. Every trade starts in a
  small hole. This is the most honest thing the bot will ever teach you.
- **Boring is normal.** On 1h candles the bot decides once per hour; the
  other ~119 poll lines per hour will look identical. A bot that trades
  constantly is a bot paying constant fees.
- **It may trade on the very first poll.** The bot adopts the strategy's
  *current* stance immediately — if the moving averages are already
  crossed bullish when you start it, it buys right away rather than
  waiting weeks for a fresh crossover. Expected behavior.
- **Yes, the demo's first trade lost 3.88%.** Losing trades are most
  trades for a trend follower (the backtest showed a ~33% win rate). The
  strategy survives by making winners bigger than losers, not by being
  right often. If a single small loss makes you want to intervene, real
  money will be much worse.

Every trade is recorded in `paper_state.json` — open it any time:

```bash
python3 -m json.tool paper_state.json | head -30
```

```json
{
  "strategy": "sma_cross(fast=20, slow=50)",
  "starting_cash": 100.0,
  "cash": 96.1196,
  "qty": 0.0,
  "trades": [
    { "side": "BUY",  "price": 49004.19, "qty": 0.0020386, "reason": "signal" },
    { "side": "SELL", "price": 47196.93, "qty": 0.0020386, "pnl": -3.88, "reason": "signal" }
  ]
}
```

(The recorded fill price differs slightly from the console's poll price —
the state stores the slippage-adjusted price you'd realistically get, not
the last ticker print.)

### Keeping it running for weeks

Ctrl+C stops the bot; the state file survives; the same command resumes it.
For a multi-week run, keep it alive in the background:

```bash
nohup python3 -m tradingbot paper --strategy sma_cross --cash 100 >> paper.log 2>&1 &
tail -f paper.log        # watch it live; Ctrl+C stops the tail, not the bot
```

(Or run it inside `tmux`/`screen` if you know them. A Raspberry Pi or any
always-on machine is perfect for this.) The bot stops if the machine
sleeps — a laptop that closes its lid pauses the experiment, which is fine:
it resumes where it left off.

Daily 2-minute check: `tail -20 paper.log` and glance at equity. Weekly
check: compare your equity change against BTC's price change over the same
week — you're trying to beat *holding*, remember.

### The kill switch (you hope to never see this)

If equity falls 25% from its peak, the bot sells everything and stops:

```
[19:00:05] BTC/USDT 34,285.80  flat  equity $69.79 (-30.21%)  << traded

KILL SWITCH: equity fell 25% from its peak. Position closed, bot stopped. This is the system working as designed.
```

A killed account stays dead — restarting tells you to start a new state
file *after thinking about what went wrong*. That friction is deliberate.
(A gap can blow through the threshold before the switch fires — it caps
how long losses run, it can't guarantee -25.0% exactly.)

## Part 5 — The graduation gate (paper → real)

After 3–4 weeks of paper trading, real money is allowed only if **every**
box ticks:

- [ ] The bot ran ≥ 3 weeks without you overriding it even once
- [ ] Equity beat buy-and-hold over the same period, **after costs**
- [ ] You can explain every trade in `paper_state.json` (which signal, why)
- [ ] The kill switch never fired — or you know exactly why and changed something
- [ ] The €100 you'd fund is money you can lose without it mattering
- [ ] You've read the API-key security rules in Part 6 and understand them

Failing the gate is the normal outcome and not a defeat: adjust (slower
timeframe, different params, different strategy — bring me the state file
and we'll design the next iteration), then run another paper round. Most
professional strategies die in testing. That's testing working.

## Part 6 — The road to the first real trade

Read this now so nothing here surprises you later, but *act* on it only
after Part 5 passes.

### The 2026 EU reality (this changed recently!)

**Binance no longer serves EU customers** — it failed to obtain a MiCA
license and stopped EU spot trading, deposits, and sign-ups on 1 July 2026.
The bot's *market data* still comes from Binance's public, no-account
endpoints (unaffected — data isn't a customer service), but real trading
happens on a MiCA-licensed exchange. When we build the live module, we
switch data + execution to the same exchange for consistency.

### Exchange choice for Slovenia (verified August 2026)

**Recommended: Kraken** (kraken.com, MiCA license via Central Bank of
Ireland, passported EU-wide):

- Free SEPA deposits from any Slovenian bank (SEPA Instant supported)
- Trade **BTC/EUR** directly — deep EUR order book, no lossy EUR→USDT hop
- Minimum order 0.0001 BTC (≈ €10) → about 10 position-size steps on €100
- Mature, well-documented REST API + free public candle data (the bot will
  use `api.kraken.com/0/public/OHLC`)
- API keys with granular permissions: trading enabled, **withdrawals
  disabled**, IP-restricted
- Fees at our size: 0.25% maker / **0.40% taker** — this is why Part 3
  made you backtest with `--fee 0.004`

**Runner-up: Bitstamp** — founded 2011 in Slovenia (Nejc Kodrič and
Damijan Merlak), the oldest big exchange, now owned by Robinhood,
MiCA-licensed. Its killer feature: **0% fees below $1,000 of 30-day
volume**, which a €100 account trading a *slow* timeframe (4h/1d) can stay
under — potentially removing our biggest enemy entirely. Weaker API and
thinner EUR books than Kraken, and verify the current minimum order
(~€10) and fee terms before committing. If your winning paper strategy
trades rarely, we'll seriously evaluate Bitstamp; it would be a fitting
home for a Slovenian bot.

(Coinbase Advanced also works in the EU but charges 0.60% taker at our
tier — hard to justify. All of this is worth re-verifying the week you
open the account; exchange terms move.)

### The steps, in order

1. **Open the account & pass KYC** (ID + proof of address; usually a day).
2. **Deposit €100 by SEPA** (free; instant to ~3 business days).
3. **Create the API key — the security rules are absolute:**
   - Permissions: query balances/orders + create/cancel orders. **Withdrawal
     stays OFF.** A stolen key can then lose your €100 at worst, never move it.
   - IP-restrict the key to the machine running the bot.
   - The key lives in environment variables on your machine. It never goes
     in git, never in chat, never in a screenshot. (`.gitignore` already
     blocks `.env` — keep it that way.)
4. **We build the live module together.** Kraken data source + signed order
   endpoint + the same kill switch, with its own test suite. It doesn't
   exist yet, deliberately — it gets built at this gate, not before.
5. **First real trade rules:** start with ~€25 of the €100, one strategy,
   the slowest timeframe that passed testing, stop-loss on, kill switch on,
   and a weekly review of every fill. Scale to the rest of the €100 only
   after a clean month.

### Taxes in Slovenia (as of August 2026 — verify before you file)

Good news, verified against Slovenian sources: the proposed 25% tax on
individuals' crypto gains was **withdrawn in parliament in November 2025**
and did not take effect for 2026 — occasional personal crypto trading
gains are currently **not taxed** in Slovenia (habitual/business-scale
trading can be taxed differently). Two caveats: MiCA exchanges now report
EU users' transactions to tax authorities automatically (DAC8), so FURS
sees the trades regardless; and the tax is widely expected to return,
possibly 2027. Keep the bot's trade logs (it does this for you) and
re-check fu.gov.si — or an accountant — each year. This is a summary of
public sources, not tax advice.

## Part 7 — Pre-flight checklist (print me)

Before the paper bot starts:
- [ ] Tests pass (`python3 -m unittest discover -s tests`)
- [ ] Backtested at `--fee 0.004`, candidate beats buy & hold
- [ ] Out-of-sample check done (train/test split, Part 3)

Before the first real euro:
- [ ] Graduation gate in Part 5 — every box
- [ ] MiCA-licensed exchange, KYC done, SEPA deposit landed
- [ ] API key: no withdrawal permission, IP-restricted, stored in env vars only
- [ ] Live module built, reviewed, and its tests pass
- [ ] Starting size ~€25, stop-loss and kill switch armed
- [ ] You are 100% at peace with losing the entire €100

## Part 8 — Troubleshooting

**`fetch` fails with "Could not reach any market data host"** — your
network blocks the exchange API (some countries, offices, and VPN exits
do). Try another network first. The bot already retries a mirror host
automatically. For learning the machinery offline, `synth` data works with
every command.

**`error: cannot read data/... .csv`** — typo in the path, or you haven't
run `fetch`/`synth` yet. The error's hint tells you which command creates
the file.

**`State file ... is for BTC/USDT/1h, not ...`** — one state file = one
market + timeframe. Use `--state another_file.json` to run a second
experiment in parallel (each gets its own fake €100).

**"This account hit its kill switch"** — the account is intentionally
locked. Understand the loss first (read `trades` in the state file), then
start fresh with a new `--state` file.

**A raw Python `Traceback` anywhere** — that's a bug in *our* code, never
acceptable. Copy it into an issue or bring it to the next session.

---

*Next session goals, whenever you're ready: review your backtest results
from Part 3, pick paper candidates together — and if you're past the gate,
start building the Kraken live module.*
