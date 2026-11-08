"""Run the isolated comparison and retain an explicit adoption verdict.

Exit 0 means acceptance passed, 2 means the evaluation completed with adoption
blockers, and 1 means the evaluation harness itself failed. No real orders.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

EXPECTED_VERSION = "2026.10"
UPSTREAM_COMMIT = "4cf0351e08d26e96a6429ef48103b726bb230cf8"
EXPECTED_SOURCES = {
    "freqtradebot.py": "fccf601681620af409b6d5e76b0fb1cf60fd99b10f8661c3c95d68a7cc9b463f",
    "strategy/interface.py": "87f06f0f2df0c3298949002a250d99cf801b9cee794f332deffcc875c2c6f4a1",
    "exchange/exchange.py": "78d0485b0ce4298e7ff9f2804362e7f421665eaadb25ac969f654a642d7bf9c0",
    "optimize/backtesting.py": "1d45e0b34ddf30813bbdf51a20be6e8345a65b668f373f8ce2fbb6e93271b234",
    "persistence/trade_model.py": "a011ca8686a205fd279ef665a2a3d5a1a4e3fb8cec9cbbac7f429eed0da85c2d",
    "wallets.py": "6a4809524fbb3ed44afba98ba8bdc7f4d8b79627c25210585ef86165ae263370",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")


def source_manifest():
    import freqtrade

    version = importlib.metadata.version("freqtrade")
    if version != EXPECTED_VERSION or not Path(freqtrade.__file__).is_relative_to(Path(sys.prefix)):
        raise RuntimeError("Use the dedicated installed Freqtrade 2026.10 environment")
    package_root = Path(freqtrade.__file__).parent
    installed = {name: {"sha256": digest(package_root / name),
                        "matches_pinned_release_source": digest(package_root / name) == expected}
                 for name, expected in EXPECTED_SOURCES.items()}
    if not all(row["matches_pinned_release_source"] for row in installed.values()):
        raise RuntimeError("Installed execution code differs from the inspected stable release")
    inputs = ["tradingbot/strategy.py", "tradingbot/automation.py", "tradingbot/execution.py",
              "tradingbot/risk.py", "tradingbot/simulated_broker.py"]
    inputs += [str(path.relative_to(ROOT)) for path in HERE.glob("*.py")]
    inputs += [str((HERE / name).relative_to(ROOT)) for name in
               ("strategies/DonchianBaseline.py", "config.paper.json", "requirements.lock.txt")]
    return {"python": platform.python_version(), "freqtrade": version,
            "ccxt": importlib.metadata.version("ccxt"), "upstream_commit": UPSTREAM_COMMIT,
            "installed_execution_sources": installed,
            "comparison_input_hashes": {name: digest(ROOT / name) for name in sorted(inputs)}}


def evaluate(output_dir, *, online_paper=False, online_report=None):
    from backtest_engine import run_comparison as backtest
    from compare_signals import run_comparison as signals
    from failure_checks import run_checks

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = source_manifest()
    signal_result = signals(prefix_limit=0, fixtures_dir=output_dir / "fixtures")
    write_json(output_dir / "signal-parity.json", signal_result)
    fixture = output_dir / "fixtures/synthetic_cycles_BTC_EUR_4h.csv"
    engine_result = backtest(fixture, source_kind="synthetic")
    write_json(output_dir / "engine-comparison.json", engine_result)
    faults = run_checks()
    write_json(output_dir / "failure-checks.json", faults)
    if online_report is not None:
        online = json.loads(Path(online_report).read_text())
        if (online.get("dry_run") is not True or online.get("execution") != "simulation"
                or online.get("config_sha256") != digest(HERE / "config.paper.json")):
            raise ValueError("Prior probe does not match the current paper-only configuration")
        online["reused_prior_probe"] = True
    elif online_paper:
        from paper_probe import run_probe
        probe_id = datetime.now(UTC).strftime("probe-%Y%m%dT%H%M%S%fZ")
        online = run_probe(output_dir / "online" / probe_id, timeout_seconds=25)
    else:
        online = {"status": "not_attempted", "live_data_paper_validated": False}
    write_json(output_dir / "online-probe.json", online)

    blockers = [row["name"] for row in faults["checks"] if row["status"] != "PASS"]
    if signal_result["status"] != "passed":
        blockers.append("signal_parity_failed")
    if not engine_result["fixture_fill_and_balance_parity"]["passed"]:
        blockers.append("shared_fixture_execution_differs")
    blockers += ["ported_strategy_rolling_history_target_state_not_preserved",
                 "account_drawdown_policy_not_ported", "per_exit_cap_not_ported"]
    if not online.get("sustained_paper_trading_verified", False):
        blockers.append("live_data_paper_run_not_validated")
    cases = signal_result["cases"]
    result = {
        "evaluated_at_utc": datetime.now(UTC).isoformat(),
        "evaluation_complete": not faults["harness_errors"] and not faults["network_attempts_blocked"],
        "adoption_accepted": not blockers,
        "recommendation": "do_not_replace_current_bot_as_is" if blockers else "candidate_for_monitored_paper_integration",
        "profitability_assessed": False, "real_orders_sent": False,
        "environment": manifest,
        "signal_parity": {"status": signal_result["status"],
                          "bars": sum(row["bars"] for row in cases),
                          "prefixes_checked": sum(len(row["causality_prefixes_checked"]) for row in cases),
                          "fixture_cases": len(cases), "stake_checks": len(signal_result["stake_checks"]),
                          "rolling_history_probe": signal_result["rolling_history_probe"]},
        "shared_fixture": {"data_sha256": engine_result["data_sha256"],
                           "source": "synthetic", "bars": engine_result["bars"],
                           "freqtrade_final_balance": engine_result["freqtrade"]["final_balance"],
                           "reference_final_equity": engine_result["reference"]["final_portfolio"]["equity"],
                           "parity": engine_result["fixture_fill_and_balance_parity"]},
        "failure_checks": {"summary": faults["summary"], "checks": faults["checks"],
                           "harness_errors": faults["harness_errors"],
                           "network_attempts_blocked": faults["network_attempts_blocked"],
                           "limitations": faults["limitations"]},
        "online_paper": online, "adoption_blockers": blockers,
        "scope": "Controlled offline engine comparison plus optional credentialless native paper startup probe; no sustained forward validation",
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/freqtrade_compare")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    parser.add_argument("--online-paper", action="store_true", help="Attempt bounded credentialless native dry-run startup")
    parser.add_argument("--online-report", type=Path, help="Reuse a matching prior probe without repeating network requests")
    args = parser.parse_args()
    if args.online_paper and args.online_report:
        parser.error("Choose a fresh probe or a prior report")
    try:
        result = evaluate(args.output_dir, online_paper=args.online_paper, online_report=args.online_report)
    except Exception as exc:
        # Harness failures must be distinguishable from exercised acceptance
        # failures. Preserve the last completed report if this run aborts.
        print(f"Evaluation harness failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    write_json(args.out, result)
    print(json.dumps({"report": str(args.out), "evaluation_complete": result["evaluation_complete"],
                      "adoption_accepted": result["adoption_accepted"],
                      "failure_checks": result["failure_checks"]["summary"],
                      "online_paper_status": result["online_paper"]["status"]}, indent=2))
    if not result["evaluation_complete"]:
        return 1
    return 0 if result["adoption_accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
