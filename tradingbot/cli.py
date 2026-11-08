"""Command-line interface. Run `python3 -m tradingbot --help`."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import __version__
from .backtest import run_backtest
from .candles import TIMEFRAME_MS, load_csv, save_csv
from .exchange import MarketData, fetch_history, normalize_symbol
from .paper import PaperTrader
from .report import format_report
from .research import evaluate, format_research
from .strategy import STRATEGIES, make_strategy
from .synth import generate
from .webreport import WebReporter
from .workflow import add_workflow_commands
from .execution_cli import add_execution_commands


def parse_params(raw: str | None) -> dict[str, str]:
    """Parse 'fast=10,slow=30' into a dict."""
    if not raw:
        return {}
    out = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise SystemExit(f"Bad --params entry '{pair}' (expected key=value)")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def add_common_strategy_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", default="sma_cross", choices=sorted(STRATEGIES))
    p.add_argument("--params", help="strategy parameters, e.g. fast=10,slow=30")
    p.add_argument("--cash", type=float, default=100.0, help="starting cash in USDT (default 100)")
    p.add_argument("--fee", type=float, default=0.001, help="taker fee per side (default 0.001 = 0.10%%)")
    p.add_argument("--slippage", type=float, default=0.0005, help="slippage per side (default 0.0005 = 0.05%%)")
    p.add_argument("--size", type=float, default=1.0, help="fraction of cash used per entry (default 1.0)")


def cmd_strategies(_: argparse.Namespace) -> int:
    print("Available strategies (all long/flat, spot only, no leverage):\n")
    for name, cls in sorted(STRATEGIES.items()):
        doc = (cls.__doc__ or "").strip().splitlines()[0]
        print(f"  {name:<12} {doc}")
        print(f"  {'':<12} defaults: {cls().describe()}\n")
    print("Override parameters with --params, e.g.:")
    print("  python3 -m tradingbot backtest --strategy sma_cross --params fast=10,slow=30")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    out = args.out or f"data/{normalize_symbol(args.symbol)}_{args.timeframe}_{args.days}d.csv"
    print(f"Fetching {args.days} days of {args.symbol} {args.timeframe} candles...")
    candles = fetch_history(args.symbol, args.timeframe, args.days)
    if not candles:
        print("No candles returned — check the symbol (e.g. BTC/USDT).", file=sys.stderr)
        return 1
    path = save_csv(candles, out)
    print(f"Saved {len(candles)} candles to {path}")
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    out = args.out or f"data/synthetic_seed{args.seed}_{args.timeframe}_{args.days}d.csv"
    candles = generate(
        days=args.days,
        timeframe=args.timeframe,
        seed=args.seed,
        start_price=args.start_price,
        drift=args.drift,
        vol=args.vol,
    )
    path = save_csv(candles, out)
    print(f"Saved {len(candles)} SYNTHETIC candles to {path}")
    print("Reminder: fake data. Good for testing the machinery, meaningless for profits.")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    strategy = make_strategy(args.strategy, parse_params(args.params))
    if args.data:
        try:
            candles = load_csv(args.data)
        except OSError as e:
            print(f"error: cannot read {args.data}: {e.strerror or e}", file=sys.stderr)
            print(
                "hint: create a data file first with `python3 -m tradingbot fetch` "
                "or `python3 -m tradingbot synth`",
                file=sys.stderr,
            )
            return 1
        except (KeyError, ValueError, TypeError) as e:
            print(f"error: {args.data} is not a valid candle CSV ({e})", file=sys.stderr)
            return 1
        source = Path(args.data).name
    else:
        print(f"No --data file given, fetching {args.days} days of {args.symbol} {args.timeframe}...")
        candles = fetch_history(args.symbol, args.timeframe, args.days)
        source = args.symbol
    if len(candles) < 10:
        print("Not enough candles to backtest.", file=sys.stderr)
        return 1
    result = run_backtest(
        candles,
        strategy,
        symbol=source,
        timeframe=args.timeframe,
        starting_cash=args.cash,
        fee=args.fee,
        slippage=args.slippage,
        size=args.size,
        stop_pct=args.stop,
    )
    print(format_report(result, fee=args.fee, slippage=args.slippage, show_trades=args.show_trades))
    if "synthetic" in source.lower():
        print(" NOTE: synthetic data — this run only proves the machinery works.")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    strategy = make_strategy(args.strategy, parse_params(args.params))
    trader = PaperTrader(
        client=MarketData(),
        symbol=args.symbol,
        timeframe=args.timeframe,
        strategy=strategy,
        state_path=args.state,
        starting_cash=args.cash,
        fee=args.fee,
        slippage=args.slippage,
        size=args.size,
        max_drawdown_stop=args.max_drawdown,
        reporter=WebReporter.from_env(),
    )
    trader.run(poll_seconds=args.poll)
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    path = Path(args.data)
    try:
        candles = load_csv(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as e:
        print(f"error: cannot read {path}: {e.strerror or e}", file=sys.stderr)
        return 1
    except (KeyError, ValueError, TypeError) as e:
        print(f"error: {path} is not a valid candle CSV ({e})", file=sys.stderr)
        return 1
    report = evaluate(
        candles, timeframe=args.timeframe, train_fraction=args.train_fraction,
        starting_cash=args.cash, fee=args.fee, slippage=args.slippage,
        size=args.size, max_drawdown=args.max_drawdown, min_trades=args.min_trades,
    )
    report["source"] = {"file": str(path), "sha256": digest,
                        "synthetic_filename": "synthetic" in path.name.lower()}
    if args.out:
        try:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            # Preserve prior research: seeing a holdout changes its status.
            with out.open("x") as f:
                json.dump(report, f, indent=2, allow_nan=False)
                f.write("\n")
        except OSError as e:
            print(f"error: cannot create report {args.out}: {e.strerror or e}", file=sys.stderr)
            return 1
    print(format_research(report))
    if report["source"]["synthetic_filename"]:
        print("SYNTHETIC DATA: these results only test the software, not a market edge.")
    if args.out:
        print(f"Saved research report to {args.out} (input SHA-256: {digest})")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
        from .server import create_app
    except ImportError:
        raise RuntimeError("Install the optional server dependencies with pip install -r requirements-server.txt") from None
    import os
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("PUBLIC_ORIGIN"):
        raise ValueError("Set PUBLIC_ORIGIN to your HTTPS dashboard origin before binding to a public interface")
    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be in [1, 65535]")
    app = create_app(db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port, workers=1,
                proxy_headers=False, access_log=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradingbot",
        description="A small, honest crypto trading bot: fetch data, backtest, paper trade.",
    )
    parser.add_argument("--version", action="version", version=f"tradingbot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("strategies", help="list available strategies")
    p.set_defaults(func=cmd_strategies)

    p = sub.add_parser("fetch", help="download historical candles to a CSV")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--timeframe", default="1h", choices=sorted(TIMEFRAME_MS))
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--out", help="output CSV path")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("synth", help="generate labeled synthetic candles (offline testing)")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--timeframe", default="1h", choices=sorted(TIMEFRAME_MS))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--start-price", type=float, default=50_000.0)
    p.add_argument("--drift", type=float, default=0.10, help="annualized drift (0.10 = +10%%/yr)")
    p.add_argument("--vol", type=float, default=0.60, help="annualized volatility (0.60 = 60%%)")
    p.add_argument("--out", help="output CSV path")
    p.set_defaults(func=cmd_synth)

    p = sub.add_parser("backtest", help="replay a strategy over historical candles")
    p.add_argument("--data", help="CSV from `fetch` or `synth`; omit to fetch fresh data")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--timeframe", default="1h", choices=sorted(TIMEFRAME_MS))
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--stop", type=float, help="stop-loss fraction, e.g. 0.05 = sell 5%% below entry")
    p.add_argument("--show-trades", action="store_true", help="print every trade")
    add_common_strategy_args(p)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("research", help="select on training data, evaluate once on a later holdout")
    p.add_argument("--data", required=True, help="historical candle CSV; keep the holdout unseen")
    p.add_argument("--timeframe", required=True, choices=sorted(TIMEFRAME_MS))
    p.add_argument("--train-fraction", type=float, default=0.7)
    p.add_argument("--cash", type=float, default=300.0, help="capital in the data's quote currency")
    p.add_argument("--fee", type=float, default=0.001, help="fee per side; confirm your account's actual tier")
    p.add_argument("--slippage", type=float, default=0.0005, help="assumed slippage per side")
    p.add_argument("--size", type=float, default=1.0)
    p.add_argument("--max-drawdown", type=float, default=0.25, help="research screen only; does not simulate a kill switch")
    p.add_argument("--min-trades", type=int, default=10, help="minimum held-out trades for the research screen")
    p.add_argument("--out", help="new JSON report file (existing files are never overwritten)")
    p.set_defaults(func=cmd_research)

    p = sub.add_parser("paper", help="trade live prices with fake money")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--timeframe", default="1h", choices=sorted(TIMEFRAME_MS))
    p.add_argument("--state", default="paper_state.json", help="JSON file holding the fake account")
    p.add_argument("--poll", type=int, default=30, help="seconds between price checks")
    p.add_argument("--max-drawdown", type=float, default=0.25, help="kill switch: stop if equity drops this far from peak")
    add_common_strategy_args(p)
    p.set_defaults(func=cmd_paper)

    add_workflow_commands(sub)
    add_execution_commands(sub)

    p = sub.add_parser("serve", help="run the authenticated dashboard and automatic simulation worker")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--db", help="persistent server database (default: TRADINGBOT_DB or data/server.sqlite3)")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
