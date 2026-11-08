# Server dashboard and automatic simulation

The new server owns the trading loop. Closing the browser does not stop it.
The dashboard controls and observes that loop; it never holds exchange keys.
The server uses the existing strategy library, persistent order journal,
simulated broker, and SQLite memory together.

## Run locally

Python 3.12 is the tested server runtime. Keep using the existing checkout.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
read -r -s -p 'Private dashboard token (32+ random characters): ' TRADINGBOT_DASHBOARD_TOKEN
export TRADINGBOT_DASHBOARD_TOKEN
.venv/bin/python -m tradingbot serve
```

Retain the token in your password manager and enter it in the login screen.
Do not put it in chat, a URL, command arguments, source, or browser storage.
The default listener is loopback port 8080. For external hosting, set
`PUBLIC_ORIGIN` to the exact HTTPS origin and use `--host 0.0.0.0` behind TLS.
See [FLY_DEPLOY.md](FLY_DEPLOY.md) for the Fly machine, volume, secrets, and checks.

The application requires a long access token; it has no default password.
Sessions use HttpOnly, SameSite Strict cookies, with Secure enabled for HTTPS.
State and control APIs require authentication. Mutations require same-origin
JSON requests. Sessions expire after eight hours and are invalidated by a
server restart; logging in again does not reset the account.

## What runs automatically

On an empty database, the default account starts paused with EUR 300 of fake
funds, BTC/EUR, four-hour Donchian signals, a EUR 75 per-order cap, a 25% cash
entry allocation, and a 10% account drawdown threshold. These are prototype
simulation settings, not personalized limits or a validated profitable strategy.

Choose **Start bot** to enable entries. The background worker then:

1. Reads closed candles and a fresh price observation.
2. Evaluates the configured strategy and available news context.
3. Checks position ownership, order filters, sizing, and risk conditions.
4. Persists an order intent before its automatic simulated submission.
5. Reconciles any uncertain result, updates balances, and records the decision.

The dashboard shows actual records from this pipeline: equity, cash, exposure,
price source, drawdown, decision reasons, order status, and news timestamps.
Charts do not contain invented performance data or projections.

**Demo replay** is a finite, deterministic historical fixture. It advances one
fixture candle per evaluation, independent of real candle timing. It exists to
exercise buying, selling, and controls offline. It stops and closes tradable
exposure when the fixture ends. **Binance public data** uses actual prices to
drive the same simulated execution; it does not send exchange orders. Current
Binance access in this development environment remains unverified/blocked.

The new automatic server cannot select a Testnet or production order adapter.
The separate manual Testnet service in [EXECUTION.md](EXECUTION.md) remains
available for isolated testing; it is not connected to this dashboard worker.

## Controls and recovery

- **Pause entries** blocks new buys. Monitoring, strategy exits, and drawdown
  exits continue for an initialized account.
- **Run one cycle** evaluates immediately; it respects the paused-entry gate.
- **Flatten & pause** persists a request to close tradable exposure on a fresh
  observation. It does not pretend to fill when prices are unavailable.
- **New simulation** creates an isolated account revision while preserving old
  records. Pause and flatten first. Untradeable dust can remain visible in the
  archived account; it is not erased or counted as a new account's funds.

Start, pause, and flatten commands commit to a separate per-revision control
record. They do not wait for the cycle's market or broker requests. A response
acknowledges the recorded intent; it does not confirm that a position is closed.
Starting entries is rejected while a close remains pending. State exposes the
control generation and whether a close is pending.

The dashboard keeps pause and flatten available during a slow cycle, news
refresh, or settings request, and continues polling. Older revisions, control
generations, and superseded responses cannot replace newer displayed state.
A pending close shows the remaining position and disables Start until the
worker confirms completion or an untradeable remainder.

Each buy is authorized again after preflight, in the same short database
transaction that claims submission. A pause committed before that claim blocks
the buy. A pause followed by a start also invalidates the older buy plan; a new
decision is required. A command arriving after a submit claim cannot undo the
already authorized request: any uncertain outcome must be reconciled, and a
pending close is retained. Sells remain permitted while entries are paused.

Worker saves cannot overwrite operator intent. A completed close acknowledges
only the control generation it observed, so a newer request survives. Existing
databases receive control records once at startup from their saved running and
close intent; later restarts preserve those records. Configuration creation
and command targeting serialize in local write transactions. All broker calls
in configuration remain local simulation operations; an exchange adapter must
not put network I/O inside these transactions.

Exits larger than the per-order cap are divided across bounded orders. Minimum
quantity/notional rules can leave dust. The simulated fee is 0.10% in the quote
asset; there is no exchange queue, spread, slippage, or liquidity simulation.
The drawdown threshold cannot guarantee an exact maximum loss between polls
or while data/network/service availability fails.

Running intent persists across restarts: a previously started account resumes
without an open browser. Pause it before a deployment or recovery. The worker
holds an OS ownership lock for the database; use one process and one machine.
Inflight submissions reconcile through the journal without a second POST.
Unsent plans from a previous cycle are canceled/invalidated before a fresh
market/news decision, avoiding a fill at a pre-crash simulated price.

Unexpected worker errors are sanitized, exposed in status, and retried with a
bounded delay. `/healthz` checks process/worker liveness; it deliberately does
not fail merely because a publisher or exchange feed is unavailable, preventing
feed outages from causing machine restart loops.

`/readyz` reports operational readiness separately. It returns HTTP 503 while
storage cannot be read, the worker is unavailable, an error or halted state is
present, an order remains unresolved, or usable observations are missing/stale.
For Binance observations, freshness allows the larger of 60 seconds and four
worker polling intervals. Demo freshness uses the worker's wall-clock heartbeat,
not the historical fixture timestamp. Required fresh news also blocks readiness
when entries have been requested but no eligible article is available.
News eligibility uses the decision's market observation time for both sources:
an article collected today was not available at a historical demo bar. Use
`observe` for the ordinary offline fixture; never backdate news observations to
make a historical replay pass the freshness policy.

A paused account with a healthy worker and recent observations can be ready;
its `entries_requested` remains false. A new account awaiting its first
observation is not ready. The same readiness summary appears in `/api/state`,
using that request's existing state read. The public endpoint exposes only
boolean health fields and generic blocking codes, never balances, article text,
provider URLs, or credentials. Readiness is not authorization for a trade and
does not establish profitable or exchange-verified execution.

Dashboard news is limited to 50 recent article summaries without full bodies.
Full text remains in durable memory for deliberate audit lookup. Automated
off-volume backups, migration tooling, and external alerts are not implemented;
persistent database retention, backup integrity, and recovery remain operator
responsibilities.

## News and decision analysis

Optional background news intake runs independently of price polling. Configure
one source on the server:

- `TRADINGBOT_RSS_URL`: a public publisher RSS/Atom URL.
- `APIFY_DATASET_ID`: an existing dataset that a separately configured actor
  updates. Private access may need `APIFY_TOKEN`; no paid actor is started here.
- `APIFY_NEWS_FIELD_MAP`: optional JSON mapping of title/url/body/source/published
  to flat actor fields, as described in [NEWS_BOT.md](NEWS_BOT.md).

The collector filters for the selected BTC/ETH asset, reads at most 100 latest
items per refresh, and records availability after collection. Changing sources
or filters changes the experiment; use a separate database/revision deliberately
and retain its configuration. Bounded polling can miss larger bursts.

**Refresh news** performs a manual read using that server configuration. With
no source configured it reports that condition. Failed reads do not fabricate
articles or turn old articles into new ones. Source credentials remain outside
the browser and persisted news records.

The decision engine is **rules-based**. `observe` records news as context;
`require-fresh` blocks new entries without recent tagged articles. It does not
interpret a headline as bullish/bearish. OpenAI analysis is not connected, and
no language model can issue an order. The interface reports this explicitly.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests
```

Tests cover automatic buys/exits, pause/flatten/kill, controls during blocked
market/preflight/submission requests, stale or conflicting market data, news
timing, inflight recovery, restart deduplication, worker ownership,
authentication, origin checks, and oversized requests. Browser checks exercise
the real backend under its CSP on desktop/mobile. Container checks must also
verify non-root operation and persistent-volume restart behaviour before hosting
is claimed as complete.
