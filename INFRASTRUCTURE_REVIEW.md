# Infrastructure assessment — 8 November 2026

**Verdict: a functional research and simulation prototype. It is not ready for
unattended real-money trading.** Local tests and a working dashboard do not
establish exchange correctness, operational reliability, or a trading edge.

The target is a single-user Binance spot bot on relatively slow candles, with
a $10,000 development/operating budget and separate trading capital. Python,
one worker, and SQLite on one persistent machine are reasonable initial
choices. A distributed platform would not solve the current gaps.

## Foundations worth retaining

- Exact decimal order values, quantity/price/notional filters, and balance checks.
- Immutable order intents and submit claims committed before broker I/O.
- Uncertain submissions reconciled rather than blindly resubmitted.
- Local worker ownership and persistent simulation balances/history.
- News publication and first-observed timestamps kept separately.
- Browser authentication, origin checks, and a non-root container process.

These properties have local automated coverage. The container/browser checks
exercise a simulated broker; signed Binance Testnet behaviour and a deployed
Fly app have not been verified.

## Current gaps, in priority order

| Priority | Gap | What is needed |
| --- | --- | --- |
| Before real funds | Automatic execution is hardwired to `SimulatedBroker`. The separate Testnet adapter is not validated against the exchange. | A staged Testnet execution path with recorded acceptance/fill/cancel/reconnect tests before any production adapter. |
| Before real funds | The journal stores cumulative execution quantities, not a complete trade/commission/account ledger. | Deduplicate exchange trades by trade ID, retain commission amounts/assets, and reconcile expected cash/positions against the exchange at startup and continuously. |
| Before unattended operation | Backup, restore, and schema upgrades are described rather than automated and exercised. | Scheduled off-volume backups, a verified restore drill, schema/state migrations, and explicit recovery objectives. |
| Before unattended operation | External readiness alerts are not configured. | Connect the separate readiness signal to an external monitor and exercise an alert/recovery drill. |
| Before release | Fly hosting, exchange connectivity, and sustained operation remain unverified. | Deploy a versioned build, exercise restart/outage/recovery, and retain operational evidence from a monitored paper run. |
| Before performance claims | Synthetic results and basic price strategies establish no market advantage. News currently supplies context/freshness only. | Real historical validation after costs, untouched evaluation periods, forward evidence, and comparison against simple benchmarks. |

The drawdown threshold is an exit trigger after an available price observation.
It cannot guarantee a maximum loss during a price gap, service outage, network
failure, or a blocked control path. Exchange-side protection and independent
operational monitoring are separate engineering work.

Single-machine downtime is a deliberate initial tradeoff. WAL and durable local
transactions protect committed writes; they do not recover a destroyed volume.
Adding a second worker against separate local state would introduce new risks.

## Findings addressed during this review

The code review reproduced two bugs despite the previous passing test suite:

1. The candle-age check allowed a four-hour series whose latest close was six
   hours old to trigger a simulated buy. The corrected policy requires the
   latest expected closed candle, with a short, bounded publication grace.
2. Wallet/account initialization committed before its configuration revision.
   An interruption could leave an orphan account and prevent a later different
   configuration. Account creation and revision selection now share one
   transaction; component initialization participates in that transaction.

Both now have regression coverage. This is a reason to judge the system
by demonstrated properties and failure cases, not by the headline test count.

The subsequent control milestone removes the shared cycle lock from operator
commands. Start/pause/flatten intent now lives in independent durable records,
and worker saves cannot replace it. Buy authorization and the submission claim
share a short transaction after preflight. A pause committed before that claim
prevents the buy; an already claimed request can still fill and must be
reconciled. Close completion uses a generation check so it cannot erase a newer
request. Tests exercise hanging market requests, paused/resumed preflight,
uncertain submissions, concurrent close requests, HTTP responsiveness, and
restart preservation. Acknowledging a close request does not confirm a fill.

Operational hardening in the same review separates `/healthz` process/worker liveness
from `/readyz` worker/storage/data readiness. The dashboard receives this
readiness result rather than deriving health from a successful API fetch.
Database failure, stale observations, unresolved execution, halted automation,
and missing required news can therefore report not-ready while the web process
remains available for inspection. This endpoint is a signal, not a deployed
external monitoring service or permission to place an order.

Server news context and API snapshots now use at most 50 article summaries
without full bodies. This bounds the news payload; loading retained account
history can still grow runtime memory. Retention and disk monitoring still
need an operational policy.

The simplified interface distinguishes API availability from
worker/data readiness, describes the retained chart coverage accurately, and
removes decorative copy and duplicate information.

## Concrete next milestone

Complete the trade/commission ledger and exchange/account reconciliation before
adding more strategies or AI features. Then implement and exercise recovery,
monitoring, and deployment. Only after those checks should an automatic
exchange adapter become a candidate for a separately bounded live pilot.

The current UI redesign improves usability; it does not close those engineering
gaps or make the system professionally validated.
