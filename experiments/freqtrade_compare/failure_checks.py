"""Execute installed Freqtrade failure-path checks against an offline exchange boundary.

No tradingbot production modules or Freqtrade source files are modified. The
real FreqtradeBot constructor, Wallets, Trade/Order SQLAlchemy models, entry,
exit, RPC pause and startup reconciliation methods execute. Venue HTTP/CCXT,
notifications, market-data provision, and pairlist I/O are deterministic
fixtures. These checks do not validate a live Binance connection or matching.

Run with this experiment's dedicated .venv, outside an upstream source tree.
An exercised safety failure is reported FAIL, never relabeled as a passing
test merely because it reproduced an expected weakness.
"""

from __future__ import annotations

import argparse
import copy
import importlib.metadata
import json
import logging
import socket
import sys
import tempfile
import threading
import time
import traceback
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from sqlalchemy import select
import freqtrade
from freqtrade.enums import CandleType, ExitCheckTuple, ExitType, MarginMode, RunMode, SignalDirection, State, TradingMode
from freqtrade.exceptions import TemporaryError
from freqtrade.exchange import Exchange
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Order, Trade
from freqtrade.persistence.usedb_context import enable_database_use
from freqtrade.rpc.rpc import RPC
from freqtrade.strategy import IStrategy

EXPECTED_VERSION = "2026.10"
UPSTREAM_SOURCE_PIN = "4cf0351e08d26e96a6429ef48103b726bb230cf8"
PAIR = "BTC/EUR"


class FixtureStrategy(IStrategy):
    timeframe = "4h"
    minimal_roi = {"0": 100.0}
    stoploss = -0.10
    startup_candle_count = 0
    order_types = {"entry": "limit", "exit": "limit", "stoploss": "limit", "stoploss_on_exchange": False}
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe["enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe["exit_long"] = 0
        return dataframe

    def get_entry_signal(self, pair, timeframe, dataframe):
        return SignalDirection.LONG, "failure-check-fixture"


class BoundaryExchange(Exchange):
    """Fake venue boundary; inherited fee interpretation remains real Freqtrade."""

    id = "binance"
    name = "Binance failure-check fixture"
    precisionMode = 2
    precision_mode_price = 2

    def __init__(self):
        self._config = {"stake_currency": "EUR", "exchange": {}}
        self.trading_mode = TradingMode.SPOT
        self.margin_mode = MarginMode.NONE
        self._markets = {PAIR: {"symbol": PAIR, "base": "BTC", "quote": "EUR", "spot": True,
                                "active": True, "precision": {"amount": 6, "price": 2},
                                "limits": {"cost": {"min": 5, "max": 1000000}}}}
        self.remote_orders = {}
        self.balances = {"EUR": 300.0, "BTC": 0.0, "BNB": 1.0}
        self.create_calls = 0
        self.fetch_calls = 0
        self.next_status = "closed"
        self.lose_response = False
        self.rate_hook = None

    def validate_config(self, config):
        return None

    def get_proxy_coin(self):
        return "EUR"

    def get_balances(self):
        return {asset: {"free": value, "used": 0.0, "total": value} for asset, value in self.balances.items()}

    def fetch_positions(self, *args, **kwargs):
        return []

    def get_rate(self, pair, *, side="entry", refresh=False, **kwargs):
        if self.rate_hook and side == "entry" and refresh:
            self.rate_hook()
        return 60000.0

    def get_min_pair_stake_amount(self, *args, **kwargs):
        return 5.0

    def get_max_pair_stake_amount(self, *args, **kwargs):
        return 1000000.0

    def get_fee(self, *args, **kwargs):
        return 0.001

    def get_precision_amount(self, *args):
        return 6

    def get_precision_price(self, *args):
        return 2

    def get_contract_size(self, *args):
        return 1.0

    def get_funding_fees(self, *args, **kwargs):
        return 0.0

    def get_option(self, name, default=None):
        return default

    def check_candle_type_support(self, *args):
        return True

    def get_conversion_rate(self, coin, currency, **kwargs):
        return 300.0 if (coin, currency) == ("BNB", "EUR") else 1.0

    def get_trades_for_order(self, *args, **kwargs):
        return []

    def refresh_latest_ohlcv(self, *args, **kwargs):
        # No historical wallet-report migration feed in this order harness.
        # Real startup order reconciliation remains enabled and unmodified.
        return {}

    def check_order_canceled_empty(self, order):
        return order["status"] in ("canceled", "expired", "rejected") and not order.get("filled")

    def create_order(self, **values):
        self.create_calls += 1
        order_id = f"fixture-{self.create_calls}"
        stamp = int(time.time() * 1000)
        order = {"id": order_id, "symbol": values["pair"], "type": values["ordertype"],
                 "side": values["side"], "amount": values["amount"], "price": values["rate"],
                 "average": values["rate"], "filled": 0.0, "remaining": values["amount"],
                 "cost": 0.0, "status": "open", "timestamp": stamp,
                 "lastTradeTimestamp": stamp, "fee": None, "trades": [], "info": {}}
        self.remote_orders[order_id] = order
        if self.next_status == "closed":
            self.set_filled(order_id, values["amount"], status="closed")
        if self.lose_response:
            raise TemporaryError("deterministic accepted order response loss")
        return copy.deepcopy(self.remote_orders[order_id])

    def set_filled(self, order_id, filled, *, status="open"):
        order = self.remote_orders[order_id]
        delta = filled - order["filled"]
        if delta < 0:
            raise ValueError("Remote fixture cannot undo actual fills")
        cost = delta * order["price"]
        if delta:
            direction = 1 if order["side"] == "buy" else -1
            self.balances["BTC"] += direction * delta
            self.balances["EUR"] -= direction * cost
            self.balances["BNB"] -= 0.0001
        order.update(filled=filled, remaining=order["amount"] - filled, cost=filled * order["price"], status=status)
        if filled:
            order["fee"] = {"currency": "BNB", "cost": 0.0001, "rate": None}
        return copy.deepcopy(order)

    def fetch_order_or_stoploss_order(self, order_id, *args, **kwargs):
        self.fetch_calls += 1
        return copy.deepcopy(self.remote_orders[order_id])

    def fetch_order(self, order_id, *args, **kwargs):
        return self.fetch_order_or_stoploss_order(order_id)

    def fetch_orders(self, *args, **kwargs):
        return copy.deepcopy(list(self.remote_orders.values()))

    def cancel_order_with_result(self, order_id, *args, **kwargs):
        self.remote_orders[order_id]["status"] = "canceled"
        return copy.deepcopy(self.remote_orders[order_id])

    def ws_connection_reset(self):
        return None

    def close(self):
        return None


def config_for(path):
    return {
        "max_open_trades": 1, "stake_currency": "EUR", "stake_amount": 75.0,
        "timeframe": "4h", "dry_run": False, "dry_run_wallet": 300.0,
        "cancel_open_orders_on_exit": False, "minimal_roi": {"0": 100.0},
        "tradable_balance_ratio": 1.0, "stoploss": -0.10,
        "amend_last_stake_amount": False, "last_stake_amount_min_ratio": 0.5,
        "unfilledtimeout": {"entry": 10, "exit": 30, "unit": "minutes"},
        "entry_pricing": {"price_side": "same", "price_last_balance": 0.0, "use_order_book": False,
                          "check_depth_of_market": {"enabled": False}},
        "exit_pricing": {"price_side": "same", "use_order_book": False},
        "exchange": {"name": "binance", "key": "", "secret": "", "enable_ws": False,
                     "pair_whitelist": [PAIR], "pair_blacklist": []},
        "pairlists": [{"method": "StaticPairList"}], "telegram": {"enabled": False, "token": "", "chat_id": ""},
        "initial_state": "running",
        "db_url": "sqlite:///" + str(path), "user_data_dir": path.parent,
        "datadir": path.parent, "strategy": "FixtureStrategy", "strategy_path": str(path.parent),
        "disableparamexport": True, "internals": {}, "export": "none",
        "dataformat_ohlcv": "feather", "dataformat_trades": "feather",
        "runmode": RunMode.LIVE, "trading_mode": TradingMode.SPOT,
        "margin_mode": MarginMode.NONE, "candle_type_def": CandleType.SPOT,
        "original_config": {},
    }


class Harness:
    def __init__(self, directory):
        self.db = Path(directory) / "freqtrade.sqlite3"
        self.exchange = BoundaryExchange()
        self.stack = ExitStack()
        self.bot = None

    def start(self):
        self.stack.close()
        self.stack = ExitStack()
        # Backtesting in the comparison process can toggle model database use.
        # This harness specifically exercises real persistent live-mode APIs.
        enable_database_use()
        config = config_for(self.db)
        strategy = FixtureStrategy(config)
        frame = pd.DataFrame({"date": [datetime.now(UTC) - timedelta(hours=4)], "open": [60000.0],
                              "high": [60000.0], "low": [60000.0], "close": [60000.0], "volume": [1.0],
                              "enter_long": [1], "exit_long": [0]})
        provider = MagicMock()
        provider.get_analyzed_dataframe.return_value = (frame, datetime.now(UTC))
        self.stack.enter_context(patch("freqtrade.freqtradebot.ExchangeResolver.load_exchange", return_value=self.exchange))
        self.stack.enter_context(patch("freqtrade.freqtradebot.StrategyResolver.load_strategy", return_value=strategy))
        self.stack.enter_context(patch("freqtrade.freqtradebot.RPCManager", MagicMock()))
        self.stack.enter_context(patch("freqtrade.freqtradebot.DataProvider", return_value=provider))
        self.stack.enter_context(patch("freqtrade.freqtradebot.PairListManager", MagicMock()))
        self.stack.enter_context(patch("freqtrade.freqtradebot.FreqtradeBot._refresh_active_whitelist", return_value=[PAIR]))
        self.bot = FreqtradeBot(config)
        return self.bot

    def crash_restart(self):
        # Discard uncommitted work exactly as a lost SQLite connection would;
        # do not call graceful cleanup(), which may commit staged work.
        bind = Trade.session.get_bind()
        Trade.session.rollback()
        Trade.session.remove()
        bind.dispose()
        bot = self.start()
        bot.startup()
        return bot

    def close(self):
        try:
            if self.bot is not None:
                self.bot.cleanup()
            if hasattr(Trade, "session"):
                bind = Trade.session.get_bind()
                Trade.session.remove()
                bind.dispose()
        finally:
            self.stack.close()


def trades():
    Trade.session.expire_all()
    return Trade.get_open_trades()


def case_lost_response(h):
    bot = h.start()
    h.exchange.lose_response = True
    error = None
    entered = None
    try:
        entered = bot.enter_positions(1)
    except TemporaryError as exc:
        error = type(exc).__name__
    bot = h.crash_restart()
    known = trades()
    observed = {"accepted_orders": len(h.exchange.remote_orders), "exchange_btc": h.exchange.balances["BTC"],
                "sqlite_open_trades_after_restart": len(known), "lookup_calls": h.exchange.fetch_calls,
                "sqlite_orders_after_restart": len(Order.session.scalars(select(Order)).all()),
                "enter_positions_return": entered, "entry_exception": error}
    safe = bool(known) and abs(sum(t.amount for t in known) - h.exchange.balances["BTC"]) < 1e-12
    h.exchange.lose_response = False
    bot.enter_positions(1)
    observed.update({"accepted_orders_after_next_entry_cycle": len(h.exchange.remote_orders),
                     "exchange_btc_after_next_entry_cycle": h.exchange.balances["BTC"],
                     "sqlite_open_trades_after_next_entry_cycle": len(trades())})
    return safe, observed, "Accepted exposure must be correlated to durable trade/order state after restart."


def case_crash_before_commit(h):
    bot = h.start()
    with patch.object(Trade, "commit", side_effect=RuntimeError("injected crash before first trade commit")):
        try:
            bot.execute_entry(PAIR, 75.0)
        except RuntimeError:
            pass
    h.crash_restart()
    known = trades()
    observed = {"accepted_orders": len(h.exchange.remote_orders), "exchange_btc": h.exchange.balances["BTC"],
                "sqlite_open_trades_after_restart": len(known), "lookup_calls": h.exchange.fetch_calls,
                "sqlite_orders_after_restart": len(Order.session.scalars(select(Order)).all())}
    safe = bool(known) and abs(sum(t.amount for t in known) - h.exchange.balances["BTC"]) < 1e-12
    return safe, observed, "A crash after venue acceptance but before the first local commit must not hide exposure."


def case_restart_known_order(h):
    bot = h.start()
    h.exchange.next_status = "open"
    bot.execute_entry(PAIR, 75.0)
    order_id = next(iter(h.exchange.remote_orders))
    expected = h.exchange.remote_orders[order_id]["amount"]
    h.exchange.set_filled(order_id, expected, status="closed")
    h.crash_restart()
    known = trades()
    observed = {"create_calls": h.exchange.create_calls, "lookup_calls": h.exchange.fetch_calls,
                "sqlite_trades": len(known), "sqlite_amount": known[0].amount if known else None,
                "exchange_btc": h.exchange.balances["BTC"]}
    safe = len(known) == 1 and abs(known[0].amount - expected) < 1e-12 and h.exchange.create_calls == 1
    return safe, observed, "A persisted open order that fills offline must recover its exposure without another entry."


def case_partial_cancel(h):
    bot = h.start()
    h.exchange.next_status = "open"
    bot.execute_entry(PAIR, 75.0)
    trade = trades()[0]
    order_id = trade.orders[0].order_id
    expected = h.exchange.remote_orders[order_id]["amount"] * 0.4
    partial = h.exchange.set_filled(order_id, expected, status="open")
    bot.update_trade_state(trade, order_id, partial, send_msg=False)
    bot.handle_cancel_enter(trade, partial, trade.orders[0], "failure-check timeout")
    Trade.commit()
    h.crash_restart()
    trade = trades()[0]
    observed = {"sqlite_amount": trade.amount, "exchange_btc": h.exchange.balances["BTC"],
                "order_status": trade.orders[0].status, "order_open": trade.orders[0].ft_is_open,
                "filled": trade.orders[0].filled}
    safe = abs(trade.amount - expected) < 1e-12 and trade.orders[0].status == "canceled" and not trade.orders[0].ft_is_open
    return safe, observed, "Canceling a partly filled entry must retain exactly the owned partial quantity across restart."


def case_bnb_fee(h):
    bot = h.start()
    bot.execute_entry(PAIR, 75.0)
    trade = trades()[0]
    observed = {"fee_open_currency": trade.fee_open_currency, "fee_open_cost_in_quote_eur": trade.fee_open_cost,
                "fee_open_rate": trade.fee_open, "sqlite_amount": trade.amount,
                "exchange_btc": h.exchange.balances["BTC"], "exchange_bnb": h.exchange.balances["BNB"],
                "fixture_bnb_eur_rate": 300.0, "raw_order_fee_bnb": 0.0001}
    # Freqtrade explicitly stores fee_open_cost in quote currency, while
    # fee_open_currency records the original paid asset (trade_model schema).
    safe = (trade.fee_open_currency == "BNB" and abs(trade.fee_open_cost - 0.03) < 1e-12
            and abs(trade.fee_open - 0.0004) < 1e-12 and abs(trade.amount - 0.00125) < 1e-12)
    return safe, observed, "A BNB-paid commission must be identified and converted without subtracting it from BTC quantity."


def case_pause_during_preflight(h):
    bot = h.start()
    reached = threading.Event()
    release = threading.Event()
    failures = []

    def slow_rate():
        reached.set()
        if not release.wait(5):
            raise RuntimeError("fixture rate wait expired")

    h.exchange.rate_hook = slow_rate

    def enter():
        try:
            bot.enter_positions(1)
        except BaseException as exc:
            failures.append(type(exc).__name__ + ": " + str(exc))
        finally:
            Trade.session.remove()

    worker = threading.Thread(target=enter)
    worker.start()
    if not reached.wait(5):
        release.set()
        worker.join(5)
        raise RuntimeError("Entry did not reach the delayed venue pricing boundary: " + repr(failures))
    response = RPC(bot)._rpc_pause()
    at_ack = h.exchange.create_calls
    release.set()
    worker.join(5)
    if worker.is_alive() or failures:
        raise RuntimeError("Entry worker did not complete: " + repr(failures))
    observed = {"rpc_pause_reply": response, "bot_state": str(bot.state),
                "create_calls_at_pause_ack": at_ack, "create_calls_after_preflight": h.exchange.create_calls,
                "sqlite_open_trades": len(trades())}
    return h.exchange.create_calls == at_ack, observed, "Acknowledged entry pause must prevent an entry still waiting on price preflight."


def case_exit_while_paused(h):
    bot = h.start()
    bot.execute_entry(PAIR, 75.0)
    RPC(bot)._rpc_pause()
    trade = trades()[0]
    result = bot.execute_trade_exit(trade, 60010.0, ExitCheckTuple(ExitType.EXIT_SIGNAL))
    Trade.commit()
    observed = {"exit_method_return": result, "bot_state": str(bot.state),
                "exchange_btc": h.exchange.balances["BTC"], "sqlite_open_trades": len(trades()),
                "remote_sides": [o["side"] for o in h.exchange.remote_orders.values()]}
    return bool(result) and not trades() and abs(h.exchange.balances["BTC"]) < 1e-12, observed, "Entry pause must continue to permit exits."


def case_duplicate_order_update(h):
    bot = h.start()
    bot.execute_entry(PAIR, 75.0)
    trade = trades()[0]
    order = copy.deepcopy(next(iter(h.exchange.remote_orders.values())))
    for _ in range(3):
        bot.update_trade_state(trade, order["id"], order, send_msg=False)
    Trade.commit()
    trade = trades()[0]
    observed = {"trade_amount": trade.amount, "order_count": len(trade.orders),
                "order_filled": trade.orders[0].filled, "fee_open_cost_in_quote_eur": trade.fee_open_cost}
    return abs(trade.amount - 0.00125) < 1e-12 and len(trade.orders) == 1 and abs(trade.fee_open_cost - 0.03) < 1e-12, observed, "Repeated identical terminal snapshots must not multiply quantity or fees."


def case_out_of_order_snapshot(h):
    bot = h.start()
    bot.execute_entry(PAIR, 75.0)
    trade = trades()[0]
    order = copy.deepcopy(next(iter(h.exchange.remote_orders.values())))
    # Give the final and earlier partial fill a plausible relative timeline:
    # creation -> partial -> final. The method still receives the older update
    # last; no fill is dated before its order was created.
    order["lastTradeTimestamp"] = order["timestamp"] + 2000
    h.exchange.remote_orders[order["id"]]["lastTradeTimestamp"] = order["lastTradeTimestamp"]
    bot.update_trade_state(trade, order["id"], order, send_msg=False)
    Trade.commit()
    stale = copy.deepcopy(order)
    stale.update(status="open", filled=order["amount"] * 0.4, remaining=order["amount"] * 0.6,
                 cost=order["amount"] * 0.4 * order["price"], lastTradeTimestamp=order["timestamp"] + 1000)
    bot.update_trade_state(trade, order["id"], stale, send_msg=False)
    Trade.commit()
    Trade.session.refresh(trade)
    observed = {"actual_exchange_filled": order["filled"], "stored_order_filled": trade.orders[0].filled,
                "stored_order_status": trade.orders[0].status, "stored_order_open": trade.orders[0].ft_is_open,
                "trade_amount": trade.amount,
                "fill_times_ms_after_creation": {"older_partial": 1000, "latest_full": 2000}}
    safe = (trade.orders[0].filled >= order["filled"] and trade.orders[0].status == "closed"
            and not trade.orders[0].ft_is_open and abs(trade.amount - order["amount"]) < 1e-12)
    return safe, observed, "A stale lower-cumulative snapshot must not reopen a terminal order or reduce recorded fills."


CASES = [
    ("accepted_order_lost_response", case_lost_response),
    ("crash_after_acceptance_before_trade_commit", case_crash_before_commit),
    ("restart_recovers_known_order", case_restart_known_order),
    ("partial_entry_cancel", case_partial_cancel),
    ("bnb_commission_conversion", case_bnb_fee),
    ("pause_during_slow_entry_preflight", case_pause_during_preflight),
    ("exit_while_entry_paused", case_exit_while_paused),
    ("duplicate_terminal_order_snapshot", case_duplicate_order_update),
    ("out_of_order_order_snapshot", case_out_of_order_snapshot),
]


def run_checks():
    version = importlib.metadata.version("freqtrade")
    module_path = str(Path(freqtrade.__file__).resolve())
    if version != EXPECTED_VERSION or not Path(module_path).is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Use the dedicated installed Freqtrade 2026.10 environment, outside an upstream checkout")
    report = {
        "package_version": version, "package_path": module_path, "source_review_pin": UPSTREAM_SOURCE_PIN,
        "harness": "Actual FreqtradeBot constructor, persistence, Wallets, entry/exit/pause/reconciliation; deterministic Exchange boundary",
        "external_network_allowed": False, "real_orders_sent": False,
        "limitations": [
            "Venue boundary is a deterministic fixture, not Binance, CCXT transport, or a matching engine.",
            "RPC notifications, candle provider and pairlist I/O are replaced; full worker scheduling is not validated.",
            "Crash injection rolls back and removes an uncommitted SQLite session; it does not SIGKILL an operating-system process.",
            "Out-of-order coverage directly invokes update_trade_state on a terminal order with an older snapshot; normal manage_open_orders would not ordinarily poll that terminal order again. This tests method-level monotonic defense, not observed Binance scheduling or private-event behavior.",
            "No profitability, strategy selection, WebSocket connectivity, geographic eligibility or production reliability claim is made.",
        ], "not_exercised": [
            "Private trade-event duplicate IDs and out-of-order individual fills; only cumulative order-update paths were exercised.",
            "Native CCXT HTTP timeout/retry implementation; the injected loss is at the Exchange.create_order API boundary.",
            "OS SIGKILL, disk corruption, power loss and multi-process worker ownership.",
        ], "checks": [],
    }
    network_attempts = []

    def deny_network(*args, **kwargs):
        network_attempts.append(True)
        raise RuntimeError("Network access disabled by offline comparison harness")

    with patch.object(socket.socket, "connect", side_effect=deny_network), patch.object(socket, "create_connection", side_effect=deny_network):
        for name, function in CASES:
            with tempfile.TemporaryDirectory(prefix="freqtrade-failure-check-") as directory:
                harness = Harness(directory)
                try:
                    safe, observations, invariant = function(harness)
                    report["checks"].append({"name": name, "status": "PASS" if safe else "FAIL",
                                             "safety_invariant": invariant, "observations": observations})
                except Exception as exc:
                    report["checks"].append({"name": name, "status": "BLOCKED",
                                             "blocker": type(exc).__name__ + ": " + str(exc),
                                             "traceback": traceback.format_exc(limit=8)})
                finally:
                    try:
                        harness.close()
                    except Exception:
                        pass
    report["network_attempts_blocked"] = len(network_attempts)
    report["harness_errors"] = [{"name": row["name"], "error": row["blocker"]}
                                for row in report["checks"] if row["status"] == "BLOCKED"]
    report["summary"] = {status: sum(row["status"] == status for row in report["checks"])
                         for status in ("PASS", "FAIL", "BLOCKED")}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", "--out", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    logging.disable(logging.CRITICAL)
    report = run_checks()
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")
    return int(bool(report["summary"]["FAIL"] or report["summary"]["BLOCKED"] or report["network_attempts_blocked"]))


if __name__ == "__main__":
    raise SystemExit(main())
