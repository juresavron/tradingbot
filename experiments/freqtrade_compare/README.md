# Isolated Freqtrade comparison

This directory is a reproducible evaluation, separate from the application's
server and trading worker. Its result is **do not migrate as-is**. Read
[the evaluation](../../FREQTRADE_EVALUATION.md) and [results.json](results.json).

The port's mandatory `stoploss=-0.99` is a research sentinel. It does not
implement our 10% account drawdown policy. This configuration must not be
promoted into a funded deployment.

## Install and reproduce

From the repository root, using Python 3.12 on Linux:

```bash
python3 -m venv experiments/freqtrade_compare/.venv
experiments/freqtrade_compare/.venv/bin/python -m pip install -r experiments/freqtrade_compare/requirements.lock.txt
experiments/freqtrade_compare/.venv/bin/python experiments/freqtrade_compare/evaluate.py
```

The lock records the tested Linux/Python 3.12 environment; other platforms may
require compatible binary packages. The existing main `.venv` is not used.

The evaluation writes `results.json` here and full reports/generated fixtures
under ignored `data/freqtrade_compare/`. Exit code **2** means the comparison
completed with adoption blockers; **1** means the harness failed; **0** means
acceptance passed. Four unmet checks are expected for the recorded release.
These must not be relabeled as passing tests.

For a bounded, credentialless native public-data paper startup attempt:

```bash
experiments/freqtrade_compare/.venv/bin/python experiments/freqtrade_compare/evaluate.py --online-paper
```

This forces dry-run mode, removes Freqtrade environment overrides, rejects
credentials/additional configs, and creates a fresh isolated ledger directory.
The native process runs for at most 25 seconds plus a five-second stop grace.
It preserves proxy/CA trust, enables TLS verification and disables WebSockets
for this REST-only probe. Its existence alone is never reported as sustained
market readiness. Current result: Binance proxy CONNECT 403.

To reuse a matching earlier probe while rerunning only offline checks:

```bash
experiments/freqtrade_compare/.venv/bin/python experiments/freqtrade_compare/evaluate.py --online-report data/freqtrade_compare/online-probe.json
```

## Components and boundaries

- `strategies/DonchianBaseline.py`: independent port of our Donchian 120/60
  target and entry-budget rules. Same-history parity is the tested contract.
- `compare_signals.py`: deterministic, labeled synthetic fixtures; complete
  prefix checks, boundary checks and a known rolling-history divergence probe.
- `backtest_engine.py`: real installed Freqtrade Backtesting/Binance adapter
  with fixture market metadata and HTTP denied, versus our actual automation
  runtime with the same closed candles and next-open quotes.
- `failure_checks.py`: real FreqtradeBot/Wallets/SQLite order lifecycle behind
  a deterministic Exchange fixture. Network is denied. LIVE code branches are
  selected only behind this fixture to exercise real-account handling; no real
  order is possible through the harness.
- `paper_probe.py`: actual native `freqtrade trade --dry-run` subprocess with
  no exchange credentials. Raw logs stay under ignored data directories.
- `evaluate.py`: verifies the installed 2026.10 package and six source hashes
  against release commit `4cf0351e08d26e96a6429ef48103b726bb230cf8`, collects
  results, records input hashes and emits a separate adoption verdict.

The offline balance comparison is generated test data. It is not a real-market
backtest, a forward paper account, an audited exchange integration, or evidence
of a profitable strategy. Freqtrade is a GPL-3.0-only dependency; this experiment
does not vendor or modify its source.
