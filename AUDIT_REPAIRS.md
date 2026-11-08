# Architecture audit repairs — 8 November 2026

The audit baseline is `7ebb8670c8e3a326d098678424d6cf14672bcc1f`.
These repairs preserve the current paper wallet, experiment configuration and
join-current-target strategy semantics. They do not add real-money execution.

## Repaired boundaries

| Audit | Resulting contract | Regression evidence |
| --- | --- | --- |
| A01 | A fill acknowledgement is built in a separate candidate state. Decision, cost, equity and acknowledgement commit atomically. Error handling reloads committed state before recording an error. | Begin, cost, decision, equity, state-update and commit failure matrix; lost commit acknowledgement; restart after a filled close. A fill is never resent to repair its journal. |
| A02 | A complete dashboard snapshot uses one deferred SQLite read transaction; WAL writers continue without taking the worker cycle lock. | Interleaved wallet fill and configuration/revision switch each return a complete old or new view. |
| A03 | The fixed entry policy is explicitly `join-current-target`, including a signal established before startup. A new breakout is not required for every activation. The dashboard and API state expose this contract. | Startup before/at/after a breakout, after an exit, and pause/resume across restart preserve the target and avoid duplicate fills. |
| A04 | Live prices come from a timestamped Binance aggregate trade. Exchange event time and local receipt time are separate. Unknown, stale, future and out-of-order events are rejected. Readiness reports event and receipt freshness separately. | Provider payload validation, missing/stale/skewed/event-order probes, readiness checks and expiry during submission preflight. |
| A05 | Session checks have a short lock independent of password hashing. Hashing concurrency is bounded and rate slots are reserved before expensive work. Fly identity is enabled only at the documented IPv4 HTTP ingress boundary. | Shared ingress with two clients, untrusted/malformed headers, saturation while authenticated state remains responsive and fail-closed configuration. |
| A06 | A valid quote and protective-action check precede history acquisition. History is cached between closed bars. Quote/history I/O have separate bounded lanes and one outstanding request each. | A timed-out history request stays blocked while the next fresh crash quote closes the position; late quotes are discarded and repeated timeouts do not create more request threads. |

## Market timing and degradation

- Exchange event age may be at most 60 seconds. Up to 2 seconds of forward
  clock skew is tolerated; larger disagreement blocks processing.
- The receipt used for a new entry may be at most 3 seconds old, checked again
  inside the durable submit claim. Readiness allows the normal worker polling
  interval between receipts, while still enforcing the 60-second event age.
- The worker waits at most 2 seconds for a quote and at most the remaining
  1-second history budget. These are application wait bounds, not a promise
  that SQLite, strategy calculation, host scheduling or the entire cycle will
  always finish within 3 seconds.
- Python cannot safely force-cancel a blocked socket operation. A daemon job
  retains its lane until completion, preventing accumulating threads. A late
  quote is discarded. Late history is reused only for its exact revision/bar
  key and revalidated before use. Normal socket timeouts and an HTTP request
  budget also bound ordinary provider failures.
- A stale or unknown quote blocks both new entries and simulated protective
  execution; the bot does not invent a price. Operator intent remains durable.
  A valid quote can trigger a protective exit without obtaining candle history.
- A last-trade reference is not an executable bid/ask spread or a guaranteed
  fill price. Immediate simulated fills and assumed costs remain model limits.

## Verification

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -q
```

The targeted regressions are in `tests/test_audit_recovery.py`,
`tests/test_audit_auth.py` and `tests/test_audit_market.py`. Tests inject faults
into isolated temporary databases and mocked market clients. No production
wallet is used for fault injection.

A matched 2,400-candle replay of the actual repaired runtime retained the
audited result: EUR315.7625741652 final virtual equity, 11 filled orders and 11
cost rows. Only the replay adapter was updated to supply explicit timestamps
for its frozen next-open observations. This is a parity check, not independent
evidence of future performance. The earlier full 8,444-candle result belongs
to the original audit; it must not be relabeled as a full replay of this repair.

Before release, build the image, pause entries, verify a consistent backup and
restore it locally. Replace the existing single machine and verify the source
hashes, saved account/configuration, balances, event freshness and authenticated
controls. Restore only the entry permission that existed before maintenance.

## Remaining architecture work

The audit's additional observations remain separate work: unify the legacy
backtester/PaperTrader/runtime model, reduce growing state serialization,
expose immutable experiment exports, version schema migrations, establish
external readiness alerts and test a Fly platform snapshot restore. This
release does not claim those broader projects are complete.

Primary references:

- [Binance market endpoints](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)
- [Fly request headers](https://docs.fly.io/networking/request-headers)
- [Fly app service listeners](https://docs.fly.io/networking/app-services/)
- [SQLite read transactions](https://www.sqlite.org/lang_transaction.html)
