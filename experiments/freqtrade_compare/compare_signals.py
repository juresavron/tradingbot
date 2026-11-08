"""Offline, identical-history parity checks for the isolated Freqtrade port.

Generated prices are SYNTHETIC software fixtures, never market/return evidence.
This checks targets and ideal next-open positions, not either engine's fills,
portfolio risk, PnL, live startup or rolling-window/restart compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from tradingbot.candles import TIMEFRAME_MS, Candle, load_csv, save_csv
from tradingbot.strategy import DonchianBreakout

from experiments.freqtrade_compare.strategies.DonchianBaseline import DonchianBaseline

FREQTRADE_VERSION = "2026.10"
UPSTREAM_COMMIT = "4cf0351e08d26e96a6429ef48103b726bb230cf8"
INTERVAL = TIMEFRAME_MS["4h"]
ORIGIN = 1_704_067_200_000  # 2024-01-01 UTC, fixed and 4h aligned.
SCALE = 600.0  # Fixture values near 100 become BTC prices near EUR 60,000.


def _bar(i, close, *, high=None, low=None, open_price=None, volume=2.0):
    high = close if high is None else high
    low = close if low is None else low
    open_price = close if open_price is None else open_price
    return Candle(ORIGIN + i * INTERVAL, open_price * SCALE, high * SCALE,
                  low * SCALE, close * SCALE, volume)


def generated_cases():
    cases = []
    equality = [_bar(i, 100, high=101, low=99) for i in range(120)]
    for close, high, low in [(101, 101, 99), (102, 150, 100), (99, 100, 99),
                             (98, 100, 50), (160, 162, 159), (160, 300, 159),
                             (50, 160, 49), (48, 52, 47), (300, 300, 299),
                             (301, 400, 300)]:
        equality.append(_bar(len(equality), close, high=high, low=low))
    cases.append(("synthetic_equality_price_gaps_BTC_EUR_4h", equality,
                  {119: 0, 120: 0, 121: 1, 122: 1, 123: 0, 124: 1,
                   125: 1, 126: 1, 127: 0, 128: 0, 129: 1}))

    cycles = [_bar(i, 100, high=101, low=99) for i in range(120)]
    for _ in range(4):
        entry = max(c.high for c in cycles[-120:]) / SCALE + 1
        cycles.append(_bar(len(cycles), entry, high=entry + 0.2, low=entry - 0.2))
        for offset in range(1, 9):
            price = entry + offset / 2
            cycles.append(_bar(len(cycles), price, high=price + 0.2, low=price - 0.2))
        exit_price = min(c.low for c in cycles[-60:]) / SCALE - 1
        cycles.append(_bar(len(cycles), exit_price, high=exit_price + 0.2, low=exit_price - 0.2))
        for _ in range(130):
            cycles.append(_bar(len(cycles), 100, high=100.5, low=99.5))
    cases.append(("synthetic_cycles_BTC_EUR_4h", cycles, {}))

    zero = [_bar(i, 100, high=101, low=99, volume=0) for i in range(120)]
    zero.append(_bar(120, 102, high=103, low=101, volume=0))
    zero.append(_bar(121, 98, high=99, low=97, volume=0))
    cases.append(("synthetic_zero_volume_BTC_EUR_4h", zero, {120: 1, 121: 0}))
    cases.append(("synthetic_empty", [], {}))
    return cases


def _validate(candles):
    for i, candle in enumerate(candles):
        prices = (candle.open, candle.high, candle.low, candle.close)
        if any(not math.isfinite(v) or v <= 0 for v in prices):
            raise ValueError(f"Candle {i}: invalid OHLC prices")
        if not candle.low <= candle.open <= candle.high or not candle.low <= candle.close <= candle.high:
            raise ValueError(f"Candle {i}: invalid OHLC range")
        if not math.isfinite(candle.volume) or candle.volume < 0:
            raise ValueError(f"Candle {i}: invalid volume")
        if type(candle.ts) is not int or i and candle.ts - candles[i - 1].ts != INTERVAL:
            raise ValueError(f"Candle {i}: timestamps must be strictly spaced at 4h")


def _frame(candles):
    frame = pd.DataFrame([[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in candles],
                         columns=["date", "open", "high", "low", "close", "volume"])
    frame["date"] = pd.to_datetime(frame["date"], unit="ms", utc=True)
    return frame


def _strategy():
    return DonchianBaseline({"stake_currency": "EUR", "dry_run": True, "trading_mode": "spot"})


def _signals(candles):
    strategy = _strategy()
    frame = strategy.populate_indicators(_frame(candles), {"pair": "BTC/EUR"})
    frame = strategy.populate_entry_trend(frame, {"pair": "BTC/EUR"})
    return strategy.populate_exit_trend(frame, {"pair": "BTC/EUR"})


def _digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _check_case(name, candles, expected, prefix_limit):
    _validate(candles)
    local = DonchianBreakout().signals(candles)
    frame = _signals(candles)
    port = frame["target_position"].astype(int).tolist()
    entries, exits = frame["enter_long"].tolist(), frame["exit_long"].tolist()
    position, reconstructed = 0, []
    for enter, exit_signal in zip(entries, exits):
        if position == 0 and enter == 1 and exit_signal == 0:
            position = 1
        elif position == 1 and exit_signal == 1 and enter == 0:
            position = 0
        reconstructed.append(position)
    mismatch = [i for i, pair in enumerate(zip(local, port)) if pair[0] != pair[1]]
    flag_mismatch = [i for i, pair in enumerate(zip(local, reconstructed)) if pair[0] != pair[1]]
    # The decision at completed bar i fills at bar i+1's open in this ideal
    # replay. This is not a Freqtrade order-fill or cash-accounting simulation.
    local_open = [0] + local[:-1] if candles else []
    port_open = [0] + reconstructed[:-1] if candles else []
    next_open_mismatch = [i for i, pair in enumerate(zip(local_open, port_open)) if pair[0] != pair[1]]
    expected_ok = all(local[i] == target for i, target in expected.items())
    if prefix_limit == 0 or len(candles) <= prefix_limit:
        stops = set(range(1, len(candles) + 1))
    else:
        stops = {1 + i * (len(candles) - 1) // (prefix_limit - 1) for i in range(prefix_limit)}
    stops.update(stop for stop in (119, 120, 121, 122, len(candles)) if 0 < stop <= len(candles))
    stops.update(i + 1 for i, target in enumerate(local) if i and local[i - 1] != target)
    causal_mismatches = []
    for stop in sorted(stops):
        prefix_local = DonchianBreakout().signals(candles[:stop])
        prefix_port = _signals(candles[:stop])["target_position"].astype(int).tolist()
        if prefix_local != local[:stop] or prefix_port != port[:stop]:
            causal_mismatches.append(stop)
    return {
        "name": name, "synthetic_fixture": name.startswith("synthetic_"), "bars": len(candles),
        "input_sha256": _digest([[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in candles]),
        "local_targets_sha256": _digest(local), "freqtrade_targets_sha256": _digest(port),
        "target_mismatches": mismatch, "flag_position_mismatches": flag_mismatch,
        "next_open_position_mismatches": next_open_mismatch,
        "expected_boundary_checks": len(expected), "expected_boundary_checks_passed": expected_ok,
        "causality_prefixes_checked": sorted(stops), "causality_mismatches": causal_mismatches,
        "entry_transitions": sum(t == 1 and (i == 0 or local[i - 1] == 0) for i, t in enumerate(local)),
        "exit_transitions": sum(t == 0 and i > 0 and local[i - 1] == 1 for i, t in enumerate(local)),
        "passed": not (mismatch or flag_mismatch or next_open_mismatch or causal_mismatches) and expected_ok,
    }


def _rolling_probe():
    candles = [_bar(i, 100, high=101, low=99) for i in range(120)]
    candles.append(_bar(120, 102, high=103, low=101))
    candles += [_bar(i, 101, high=102, low=100) for i in range(121, 641)]
    full = _signals(candles)["target_position"].astype(int).tolist()
    tail = _signals(candles[-240:])["target_position"].astype(int).tolist()
    local_full = DonchianBreakout().signals(candles)
    local_tail = DonchianBreakout().signals(candles[-240:])
    return {"status": "known_history_seed_difference_confirmed" if full[-1] != tail[-1] else "not_reproduced",
            "full_history_bars": len(candles), "truncated_history_bars": 240,
            "full_final_target": full[-1], "truncated_final_target": tail[-1],
            "both_implementations_agree_for_each_seed": full == local_full and tail == local_tail,
            "runtime_or_restart_parity_proven": False}


def _stake_checks():
    results = []
    for cash, minimum, maximum in [(300, 5, 300), (100, 5, 100), (10, 5, 10),
                                    (300, 80, 300), (300, 5, 20), (0, 5, 300)]:
        strategy = _strategy()
        strategy.wallets = SimpleNamespace(get_free=lambda currency, cash=cash: cash)
        actual = strategy.custom_stake_amount("BTC/EUR", datetime(2024, 1, 1, tzinfo=timezone.utc),
                                               60_000, 75, minimum, maximum, 1, None, "long")
        with localcontext() as context:
            context.prec = 100
            expected = min(Decimal("75"), Decimal(cash) * Decimal("0.25")) / Decimal("1.002")
            expected = min(expected, Decimal(maximum))
            if expected < Decimal(minimum):
                expected = Decimal("0")
        results.append({"free_cash": cash, "min_stake": minimum, "max_stake": maximum,
                        "freqtrade_callback_quote": actual, "local_budget_quote": float(expected),
                        "passed": actual == float(expected)})
    return results


def run_comparison(data_path=None, *, prefix_limit=64, fixtures_dir=None) -> dict:
    if type(prefix_limit) is not int or prefix_limit not in (0,) and prefix_limit < 2:
        raise ValueError("prefix_limit must be 0 (all prefixes) or an integer >= 2")
    version = importlib.metadata.version("freqtrade")
    if version != FREQTRADE_VERSION:
        raise ValueError(f"Use the isolated Freqtrade {FREQTRADE_VERSION} environment; found {version}")
    cases = generated_cases()
    if data_path is not None:
        path = Path(data_path)
        cases.append((path.stem, load_csv(path), {}))
    fixture_paths = []
    if fixtures_dir is not None:
        folder = Path(fixtures_dir)
        folder.mkdir(parents=True, exist_ok=True)
        for name, candles, _ in cases:
            if not name.startswith("synthetic_") or not candles:
                continue
            path = folder / (name + ".csv")
            if path.exists():
                if load_csv(path) != candles:
                    raise ValueError(f"Refusing to overwrite different fixture: {path}")
            else:
                save_csv(candles, path)
            fixture_paths.append(str(path))
    reports = [_check_case(name, candles, expected, prefix_limit) for name, candles, expected in cases]
    rolling, stakes = _rolling_probe(), _stake_checks()
    passed = all(case["passed"] for case in reports) and all(row["passed"] for row in stakes)
    passed = passed and rolling["status"] == "known_history_seed_difference_confirmed" and rolling["both_implementations_agree_for_each_seed"]
    return {
        "status": "passed" if passed else "failed", "scope": "identical_seeded_history_signal_parity",
        "profitability_assessed": False, "freqtrade_version": version, "upstream_tag_commit": UPSTREAM_COMMIT,
        "local_strategy_source_sha256": hashlib.sha256((REPO_ROOT / "tradingbot/strategy.py").read_bytes()).hexdigest(),
        "port_source_sha256": hashlib.sha256((Path(__file__).parent / "strategies/DonchianBaseline.py").read_bytes()).hexdigest(),
        "baseline": {"timeframe": "4h", "entry_period": 120, "exit_period": 60,
                     "max_entry_quote": 75, "cash_fraction": 0.25, "fee_buffer": 0.002,
                     "simulator_fee_assumption": 0.001, "mandatory_stoploss_sentinel": -0.99},
        "cases": reports, "stake_checks": stakes, "rolling_history_probe": rolling, "fixture_files": fixture_paths,
        "limitations": [
            "SYNTHETIC fixtures test software only; no real-market profitability claim is evaluated.",
            "Target and ideal next-open position parity do not establish fill, quantity, fee, PnL or portfolio-risk parity.",
            "The mandatory -0.99 Freqtrade stoploss is a research sentinel, not the local 10% account drawdown kill.",
            "Arbitrary rolling windows and restarts can change persistent targets; the included probe demonstrates this limitation.",
            "Venue precision/minimums, startup trimming, price selection, open-order handling and cash accounting still require engine comparison.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", help="optional local 4h candle CSV in the tradingbot format")
    parser.add_argument("--out", help="write the reproducible JSON report to this file")
    parser.add_argument("--fixtures-dir", help="write clearly labeled SYNTHETIC CSV fixtures here")
    parser.add_argument("--prefix-limit", type=int, default=64, help="sample count, or 0 for every prefix")
    args = parser.parse_args(argv)
    try:
        report = run_comparison(args.data, prefix_limit=args.prefix_limit, fixtures_dir=args.fixtures_dir)
        encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(encoded)
        print(encoded, end="")
        return 0 if report["status"] == "passed" else 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
