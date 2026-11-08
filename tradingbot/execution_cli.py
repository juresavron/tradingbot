"""Manual order-service commands; simulation is the default environment."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from .execution import ExecutionEngine
from .memory import TradingMemory
from .orders import decimal_text, decimal_value
from .risk import RiskLimits


def _initial_balances(pairs) -> dict:
    if not pairs:
        return {"EUR": "300", "BTC": "0", "ETH": "0"}
    balances = {}
    for pair in pairs:
        asset, separator, amount = pair.partition("=")
        if not separator or not asset.isascii() or not asset.isalnum() or len(asset) > 30:
            raise ValueError("--balance requires an asset code and exact amount, e.g. EUR=300")
        asset = asset.upper()
        if asset in balances:
            raise ValueError("Duplicate asset in initial --balance settings")
        balances[asset] = decimal_text(decimal_value(amount, "initial balance", allow_zero=True))
    return balances


def _broker(memory, args, account):
    if args.mode == "simulation":
        from .simulated_broker import SimulatedBroker
        return SimulatedBroker(memory, account_id=account, initial_balances=_initial_balances(args.balance),
                               fee=args.simulation_fee)
    if args.balance or args.simulation_fee != "0.001":
        raise ValueError("Simulation balances/fees do not configure a Binance Testnet account")
    from .binance_testnet import BinanceTestnetBroker
    key = os.environ.get("BINANCE_TESTNET_API_KEY")
    secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
    if not key or not secret:
        raise ValueError(
            "Testnet requires BINANCE_TESTNET_API_KEY and a locally usable BINANCE_TESTNET_API_SECRET. "
            "Configure them securely on the execution host; never paste their values in chat or arguments."
        )
    return BinanceTestnetBroker(api_key=key, api_secret=secret)


def _inspect(memory, account, request_id, limit):
    from .order_store import ExecutionStore
    try:
        row = memory.connection.execute(
            "SELECT * FROM execution_accounts WHERE account_id = ?", (account,)
        ).fetchone()
    except sqlite3.OperationalError:
        raise ValueError("No execution accounts exist in this database") from None
    if row is None:
        raise ValueError("Execution account does not exist")
    store = ExecutionStore(memory, account_id=account, environment=row["environment"],
                           scope=row["scope"], limits=RiskLimits(**json.loads(row["limits_json"])))
    if request_id:
        order = store.get_by_request(request_id)
        if order is None:
            raise ValueError("Request does not exist in this execution account")
        return {"order": order, "events": store.events(order["client_order_id"])}
    return {"orders": store.list_orders(limit=limit)}


def cmd_execution(args) -> int:
    from .order_store import ExecutionStore

    account = args.account or ("simulation-v1" if args.mode == "simulation" else "binance-testnet-v1")
    if args.execution_command == "list" and not Path(args.db).is_file():
        raise ValueError("Execution database does not exist")
    try:
        with TradingMemory(args.db) as memory:
            if args.execution_command == "list":
                result = _inspect(memory, account, args.request, args.limit)
                print(json.dumps(result, indent=2, allow_nan=False))
                return 0
            limits = RiskLimits(max_order_notional=args.cap, fee_buffer=args.fee_buffer)
            broker = _broker(memory, args, account)
            store = ExecutionStore(memory, account_id=account, environment=broker.environment,
                                   scope=broker.scope, limits=limits)
            if args.execution_command == "balances":
                print(json.dumps({"environment": broker.environment, "account": account,
                                  "balances": {asset: decimal_text(value) for asset, value in broker.get_balances().items()}},
                                 indent=2))
                return 0
            engine = ExecutionEngine(store, broker)
            if args.execution_command == "prepare":
                result = engine.prepare(request_id=args.request, symbol=args.symbol, side=args.side,
                                        quantity=args.quantity, limit_price=args.limit_price)
            else:
                order = store.get_by_request(args.request)
                if order is None:
                    raise ValueError("Request does not exist; prepare its intent first")
                action = getattr(engine, args.execution_command)
                result = action(order["client_order_id"])
            print(json.dumps({"environment": broker.environment, "order": result}, indent=2, allow_nan=False))
            return 1 if result["status"] in {"SUBMITTING", "CANCELING", "UNKNOWN", "REJECTED"} else 0
    except (sqlite3.Error, OSError) as error:
        # Never include HTTP bodies, credentials or signed request URLs.
        print(f"error: execution storage unavailable ({type(error).__name__})", file=sys.stderr)
        return 1


def add_execution_commands(sub):
    p = sub.add_parser("execution", help="durable order intents, simulation, and Binance Spot Testnet")
    p.add_argument("--db", default="data/execution.sqlite3")
    p.add_argument("--account", help="persistent execution account (separate from agent-paper)")
    p.add_argument("--mode", choices=["simulation", "testnet"], default="simulation")
    p.add_argument("--cap", default="300", help="maximum order notional in the pair's quote units, excluding fees")
    p.add_argument("--fee-buffer", default="0.002", help="additional quote reserve checked for buys")
    p.add_argument("--simulation-fee", default="0.001", help="simulator quote-asset fee fraction")
    p.add_argument("--balance", action="append", help="initial simulator balances; e.g. EUR=300 (repeat)")
    commands = p.add_subparsers(dest="execution_command", required=True)
    prepare = commands.add_parser("prepare", help="validate and persist an immutable LIMIT GTC intent")
    prepare.add_argument("--request", required=True, help="stable request ID; reuse only for the identical intent")
    prepare.add_argument("--symbol", default="BTC/EUR")
    prepare.add_argument("--side", choices=["BUY", "SELL"], required=True)
    prepare.add_argument("--quantity", required=True, help="exact base-asset quantity")
    prepare.add_argument("--limit-price", required=True, help="exact quote price per base asset")
    for name in ("submit", "reconcile", "cancel"):
        child = commands.add_parser(name)
        child.add_argument("--request", required=True)
    commands.add_parser("balances", help="inspect available simulated or testnet balances")
    inspect = commands.add_parser("list", help="inspect persisted orders without keys or network access")
    inspect.add_argument("--request", help="include this order's event history")
    inspect.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_execution)
