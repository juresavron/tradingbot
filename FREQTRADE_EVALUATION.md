# Freqtrade integration evaluation

Evaluated 8 November 2026. **Verdict: do not replace the current bot with this configuration as-is.** The isolated comparison ran successfully, exposed concrete acceptance gaps, and did not establish live-data paper readiness or profitability.

Machine-readable evidence: [results.json](experiments/freqtrade_compare/results.json). Reproduction scripts/configuration: [experiments/freqtrade_compare](experiments/freqtrade_compare/README.md).

## What was actually executed

- Installed stable **Freqtrade 2026.10** and its dependencies in a separate Python 3.12 environment. The production environment and application were not replaced.
- Compared six critical installed implementation files with official release commit [`4cf0351e08d26e96a6429ef48103b726bb230cf8`](https://github.com/freqtrade/freqtrade/tree/4cf0351e08d26e96a6429ef48103b726bb230cf8). All hashes matched. Dependencies are frozen in the experiment lock file.
- Ported Donchian entry/exit periods **120/60**, 4h candles, spot BTC/EUR, €300 initial virtual balance, one position, and entry budget `min(€75, 25% free cash) / 1.002`. The reserve is distinct from the 0.1% simulated fill fee.
- Ran the installed Freqtrade Backtesting engine with its actual Binance adapter, declared synthetic market metadata, and forbidden HTTP. Compared it with our actual `AutomationRuntime` receiving the same completed candles and next-open quotes.
- Exercised real FreqtradeBot, Wallets, SQLite Trade/Order persistence, RPC pause, entry/exit and startup recovery against a controlled exchange fixture.
- Attempted native credentialless `freqtrade trade --dry-run` startup with public Binance access, inherited proxy/CA trust, TLS verification and REST-only data. It returned **proxy CONNECT HTTP 403**. No sustained live-data paper session was verified.

No real exchange orders, paid model calls or deployment changes occurred. The failure harness deliberately selects live-account code branches behind a fake exchange and a network-denial guard; it is not a live deployment.

## Strategy and accounting comparison

| Check | Observed result |
| --- | --- |
| Identically seeded target signals | Zero mismatches over 932 synthetic bars |
| Prefix causality | All 932 nonempty prefixes checked, zero mismatches |
| Equality/extrema/zero-volume boundaries | 13 explicit assertions passed |
| Stake/reserve boundaries | Six checks passed |
| Actual engine comparison | Four completed round trips; eight fills matched in time, side, price and quantity |
| Final virtual balance/equity on that fixture | Both €293.268453; both fully flat |

These prices are generated software fixtures, not historical BTC/EUR observations. The balance is an accounting comparison, not a strategy performance result. Matching one controlled execution model does not establish fill, fee, liquidity or risk parity in general.

The port also reproduces a history-seeding limitation: the same strategy ends long on a 641-bar history but flat on its 240-bar suffix, because the earlier entry that established its persistent target was removed. Both implementations agree when supplied the same seed. Our worker retains its history; the port needs an explicit target-state/reconstruction contract before being used with Freqtrade's rolling data windows or restarts. A fixed startup candle count does not resolve this.

## Runtime acceptance checks

**Five passed, four were unmet, zero harness errors, zero network attempts in the offline checks.** These are our strict acceptance criteria, not an upstream test-suite score or a complete audit of Freqtrade.

| Scenario | Result | Evidence and scope |
| --- | --- | --- |
| Filled entry, response lost | Unmet | After new bot/startup: venue fixture owns 0.00125 BTC, SQLite has zero Trade/Order rows and no order lookup occurred. The next entry cycle accepts a second buy, reaching 0.0025 BTC with only one tracked trade. |
| Acceptance before first trade commit, then interruption | Unmet | Discarding the uncommitted session and constructing a new bot/startup leaves the fixture exposure uncorrelated. This simulates lost uncommitted state; it is not an OS SIGKILL test. |
| Persisted open order fills while stopped | Passed | Startup queries the known order, restores the filled amount and does not submit another entry. |
| Partially filled entry canceled | Passed | 0.0005 BTC of owned exposure remains represented after restart. |
| Commission paid in BNB | Passed | 0.0001 BNB at the fixture's €300/BNB converts to €0.03 quote cost, retaining the fee currency and BTC quantity. This is a separate fee fixture from the quote-fee backtest. |
| Pause acknowledged during slow entry pricing | Unmet | An already-running entry finishes preflight and calls create_order after the acknowledgement. This differs from our stricter pre-submit authorization boundary; it does not mean all pause behavior fails. |
| Exit while entries are paused | Passed | A close remains permitted and clears the fixture exposure. |
| Repeated identical terminal snapshot | Passed | Quantity and commission are not multiplied. This does not cover every private trade-event deduplication path. |
| Injected older snapshot after a terminal order | Unmet | Direct `update_trade_state` input reopens stored Order fields and reduces recorded filled quantity from 0.00125 to 0.0005. Trade.amount remains 0.00125. Normal open-order polling would ordinarily skip that terminal order; this demonstrates missing method-level monotonic defense under injected input, not observed Binance scheduling. |

The last case uses a coherent creation → partial fill → final fill timeline and delivers the partial snapshot last. Its scheduling qualification is important. The loss/crash checks replace the exchange boundary, so actual CCXT retries, client-ID behavior, Binance acceptance and venue recovery remain untested.

## Policies that still differ

- The port uses a **−99% mandatory stoploss sentinel solely for comparison**. It is not our 10% account-equity drawdown trigger and is not a recommended deployment policy. No ROI/trailing override is added.
- Freqtrade normally exits the full position. Our current worker caps each exit at €75 and may close over several cycles.
- Live filters, fee schedules, order-book liquidity, partial fills and timing can differ from the declared offline metadata and matching rules.
- Freqtrade and our simulator handle end liquidation, minimum stakes, rounding and retained dust differently. The shared fixture happens to finish fully flat without those differences changing its final result.
- Operator controls and uncertain-submission recovery need to preserve our intended guarantees across any adapter or engine migration.

## Live-data blocker

The native startup attempt honored the inherited proxy and CA trust and failed with CONNECT 403 for Binance. The effective cloud environment permits package/GitHub access but has no applied custom Binance rule. The existing saved configuration draft already lists `api.binance.com`; that draft is not the running policy.

Live-data paper trading requires applying the Binance outbound allowance in the cloud environment settings, followed by a fresh connectivity/startup check. No Binance trading key is required for this public-data dry run. This evaluation did not bypass the policy, change exchange endpoints, or disable TLS. The final report reuses the recorded native probe after the offline fixture correction rather than issuing another network attempt.

## Decision and next milestone

Retain the current simulation app and its commit-before-submit marker, ambiguous-outcome reconciliation, durable operator controls and monotonic order checks. Freqtrade remains a potential platform, but the scoped evidence does not support an immediate replacement.

Before an adoption decision, demonstrate a durable intent/account reconciliation layer that discovers accepted-but-untracked exposure, fences a paused entry at submission, and preserves target state across rolling windows/restarts. Define the account drawdown and exit-cap policies explicitly. Then repeat the affected acceptance checks and perform real Testnet/forward-paper validation with backups, alerts and recovery drills. Fixing these boundaries may require an integration wrapper or upstream changes; its maintenance cost should be compared with completing the existing ledger/reconciliation implementation.

The isolated evaluation is complete. Sustained Binance paper operation, production deployment and profitable performance remain unverified.
