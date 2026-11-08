"""Server-owned strategy worker using the durable simulated execution engine.

The browser is a controller and observer, never the worker. Demo prices are a
finite deterministic historical fixture, not market data or performance proof.
Public Binance prices can drive the same simulation; this module contains no
exchange credentials, testnet orders, or production order adapter.
"""

from __future__ import annotations

import fcntl
import json
import math
import threading
import time
from contextlib import nullcontext
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path

from .candles import TIMEFRAME_MS, Candle
from .exchange import MarketData
from .execution import ExecutionEngine
from .memory import TradingMemory
from .order_store import ExecutionStore
from .orders import TERMINAL_STATUSES
from .risk import RiskLimits
from .simulated_broker import SimulatedBroker
from .strategy import STRATEGIES, make_strategy


DEFAULT_CONFIG = {
    "source": "demo", "symbol": "BTC/EUR", "timeframe": "4h",
    "strategy": "donchian_breakout", "params": {}, "starting_cash": 300,
    "max_order_notional": 75, "position_fraction": 0.25,
    "max_drawdown": 0.10, "news_policy": "observe",
}


def _now() -> int:
    return int(time.time() * 1000)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class AutomationRuntime:
    def __init__(self, db_path, *, poll_seconds: float = 5.0, start_worker: bool = True):
        if str(db_path) == ":memory:":
            raise ValueError("Automation requires a persistent database path")
        if (isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float))
                or not math.isfinite(poll_seconds) or not 0.01 <= poll_seconds <= 3600):
            raise ValueError("poll_seconds must be finite and between 0.01 and 3600")
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.poll_seconds = float(poll_seconds)
        self._cycle_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._worker_error = None
        self._ownership = open(self.db_path + ".automation.lock", "a")
        try:
            fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._ownership.close()
            self._ownership = None
            raise RuntimeError("An automation worker already owns this database") from None
        try:
            with TradingMemory(self.db_path) as memory:
                self._schema(memory)
                if self._load(memory) is None:
                    self._create_revision(memory, self._validate_config(DEFAULT_CONFIG))
            if start_worker:
                self.start_worker()
        except BaseException:
            self._release_ownership()
            raise

    @staticmethod
    def _schema(memory):
        with memory.transaction():
            for sql in (
                """CREATE TABLE IF NOT EXISTS automation_revisions (
                    revision INTEGER PRIMARY KEY, account_id TEXT UNIQUE NOT NULL,
                    config_json TEXT NOT NULL, state_json TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS automation_current (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS automation_controls (
                    revision INTEGER PRIMARY KEY, generation INTEGER NOT NULL,
                    desired_running INTEGER NOT NULL CHECK(desired_running IN (0,1)),
                    flatten_requested INTEGER NOT NULL CHECK(flatten_requested IN (0,1)),
                    action TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS automation_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL,
                    cycle_id TEXT NOT NULL, at_ms INTEGER NOT NULL, candle_ts INTEGER NOT NULL,
                    target INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
                    news_ids_json TEXT NOT NULL, price REAL NOT NULL, strategy TEXT NOT NULL,
                    UNIQUE(revision, cycle_id))""",
                """CREATE TABLE IF NOT EXISTS automation_equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, revision INTEGER NOT NULL,
                    at_ms INTEGER NOT NULL, equity REAL NOT NULL, price REAL NOT NULL)""",
            ):
                memory.connection.execute(sql)
            # Upgrade existing simulations once, preserving paused, halted,
            # and pending-close intent. Restart must never reset controls.
            for row in memory.connection.execute("""SELECT r.revision,r.state_json
                FROM automation_revisions r LEFT JOIN automation_controls c ON c.revision=r.revision
                WHERE c.revision IS NULL""").fetchall():
                state = json.loads(row["state_json"])
                flatten = bool(state["flatten"])
                running = bool(state["desired_running"] and not (flatten or state["killed"] or state["replay_complete"]))
                memory.connection.execute("INSERT OR IGNORE INTO automation_controls VALUES (?,?,?,?,?,?)",
                                          (row["revision"], 0, int(running), int(flatten),
                                           "flatten" if flatten else "start" if running else "pause", _now()))

    @staticmethod
    def _load(memory):
        row = memory.connection.execute("""
            SELECT r.* FROM automation_revisions r JOIN automation_current c
              ON c.revision=r.revision WHERE c.singleton=1
        """).fetchone()
        if row is None:
            return None
        result = {"revision": row["revision"], "account_id": row["account_id"],
                  "config": json.loads(row["config_json"]), "state": json.loads(row["state_json"])}
        AutomationRuntime._refresh_control(memory, result)
        return result

    @staticmethod
    def _refresh_control(memory, row):
        control = memory.connection.execute("SELECT * FROM automation_controls WHERE revision=?",
                                            (row["revision"],)).fetchone()
        if control is None:
            raise RuntimeError("Automation control state is missing")
        row["control"] = dict(control)
        state = row["state"]
        state["desired_running"] = bool(control["desired_running"] and not (state["killed"] or state["replay_complete"]))
        state["flatten"] = bool(state["flatten"] or control["flatten_requested"])

    @staticmethod
    def _write_state(memory, row):
        """Write worker state without replacing independent operator intent.

        Caller holds a short write transaction. Only the exact close request
        observed by this cycle can be acknowledged; a newer one survives.
        """
        state = row["state"]
        generation = row["control"]["generation"]
        if state["killed"] or state["replay_complete"]:
            memory.connection.execute("UPDATE automation_controls SET desired_running=0 WHERE revision=?",
                                      (row["revision"],))
        if not state["flatten"]:
            memory.connection.execute("""UPDATE automation_controls SET flatten_requested=0
                WHERE revision=? AND generation=?""", (row["revision"], generation))
        state["applied_control_generation"] = generation
        memory.connection.execute("UPDATE automation_revisions SET state_json=? WHERE revision=?",
                                  (_json(state), row["revision"]))

    @staticmethod
    def _save(memory, row):
        with memory.transaction():
            AutomationRuntime._write_state(memory, row)

    @staticmethod
    def _validate_config(value):
        if not isinstance(value, dict) or set(value) != set(DEFAULT_CONFIG):
            raise ValueError("Automation configuration contains unknown or missing fields")
        config = dict(value)
        if config["source"] not in ("demo", "binance"):
            raise ValueError("source must be demo or binance")
        if not isinstance(config["symbol"], str):
            raise ValueError("symbol must be a supported spot pair")
        config["symbol"] = config["symbol"].upper().replace("-", "/")
        if config["symbol"] not in ("BTC/EUR", "BTC/USDT", "ETH/EUR", "ETH/USDT"):
            raise ValueError("symbol must be BTC/EUR, BTC/USDT, ETH/EUR, or ETH/USDT")
        if not isinstance(config["timeframe"], str) or config["timeframe"] not in TIMEFRAME_MS:
            raise ValueError("Unsupported candle timeframe")
        if not isinstance(config["strategy"], str) or config["strategy"] not in STRATEGIES:
            raise ValueError("Unsupported strategy")
        if not isinstance(config["params"], dict):
            raise ValueError("params must be a dictionary")
        for key, item in config["params"].items():
            if not isinstance(key, str) or isinstance(item, (dict, list, bool)):
                raise ValueError("Strategy parameters must be scalar values")
        try:
            strategy = make_strategy(config["strategy"], {k: str(v) for k, v in config["params"].items()})
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid strategy parameters") from exc
        for key, item in vars(strategy).items():
            if key in ("fast", "slow", "period", "entry_period", "exit_period"):
                if type(item) is not int or not 1 <= item <= 999:
                    raise ValueError("Strategy periods must be integers between 1 and 999")
            elif not isinstance(item, (int, float)) or not math.isfinite(item) or not 0 <= item <= 100:
                raise ValueError("Invalid strategy thresholds")
        config["params"] = dict(vars(strategy))
        for key in ("starting_cash", "max_order_notional", "position_fraction", "max_drawdown"):
            item = config[key]
            if (isinstance(item, bool) or not isinstance(item, (int, float, str))
                    or not math.isfinite(float(item)) or float(item) <= 0):
                raise ValueError(f"{key} must be a positive finite number")
            config[key] = float(item)
        if not 5 <= config["starting_cash"] <= 1_000_000_000:
            raise ValueError("starting_cash must be between 5 and 1000000000")
        if not 5 <= config["max_order_notional"] <= config["starting_cash"]:
            raise ValueError("max_order_notional must be between 5 and starting_cash")
        if config["position_fraction"] > 1 or config["max_drawdown"] > 1:
            raise ValueError("position_fraction and max_drawdown cannot exceed 1")
        if config["news_policy"] not in ("observe", "require-fresh"):
            raise ValueError("news_policy must be observe or require-fresh")
        return config

    def _create_revision(self, memory, config):
        with (nullcontext() if memory.connection.in_transaction else memory.transaction()):
            revision = memory.connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM automation_revisions").fetchone()[0]
            row = {"revision": revision, "account_id": f"automation-r{revision}", "config": config,
                   "state": {"desired_running": False, "initialized": False, "killed": False,
                             "flatten": False, "exit_pending": False, "error": None, "heartbeat_ms": None,
                             "market_time_ms": None, "price": None, "history": [],
                             "last_decision_bar": None, "demo_cursor": self._warmup(config),
                             "demo_origin_ms": self._demo_origin(config),
                             "sequence": 0, "inflight": None, "peak_equity": str(config["starting_cash"]),
                             "last_action": "HOLD", "last_reason": "paused", "replay_complete": False}}
            # Wallet, execution identity, revision, and active pointer commit
            # together. A failure cannot reserve the next account ID with an
            # orphan wallet or stale risk configuration.
            self._execution(memory, row)
            memory.connection.execute("INSERT INTO automation_revisions VALUES (?,?,?,?)",
                                      (revision, row["account_id"], _json(config), _json(row["state"])))
            memory.connection.execute("INSERT INTO automation_controls VALUES (?,0,0,0,'pause',?)",
                                      (revision, _now()))
            memory.connection.execute("INSERT INTO automation_current VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision",
                                      (revision,))
            self._refresh_control(memory, row)
        return row

    @staticmethod
    def _warmup(config):
        return max(300, 1 + max((int(v) for k, v in config["params"].items()
                                if k in ("fast", "slow", "period", "entry_period", "exit_period")), default=1))

    @classmethod
    def _demo_origin(cls, config):
        interval = TIMEFRAME_MS[config["timeframe"]]
        return (_now() // interval - cls._warmup(config) - 151) * interval

    @staticmethod
    def _execution(memory, row):
        config = row["config"]
        quote = config["symbol"].partition("/")[2]
        broker = SimulatedBroker(memory, account_id=row["account_id"], initial_balances={quote: str(config["starting_cash"])})
        store = ExecutionStore(memory, account_id=row["account_id"], environment=broker.environment,
                               scope=broker.scope, limits=RiskLimits(max_order_notional=str(config["max_order_notional"])))
        return broker, store, ExecutionEngine(store, broker)

    @staticmethod
    def _strategy(config):
        return make_strategy(config["strategy"], {k: str(v) for k, v in config["params"].items()})

    def configure(self, config: dict) -> dict:
        if not isinstance(config, dict) or not set(config).issubset(DEFAULT_CONFIG):
            raise ValueError("Configuration contains unsupported fields")
        with self._cycle_lock, TradingMemory(self.db_path) as memory, memory.transaction():
            row = self._load(memory)
            state = row["state"]
            if state["desired_running"] or state["flatten"] or state["inflight"]:
                raise ValueError("Pause and flatten the current simulation before changing configuration")
            broker, store, _ = self._execution(memory, row)
            if self._unresolved(store):
                raise ValueError("Resolve pending orders before changing configuration")
            balances = broker.get_balances()
            rules = broker.get_rules(row["config"]["symbol"])
            quantity = balances.get(rules.base_asset, Decimal("0"))
            price = _decimal(state["price"]) if state["price"] is not None else Decimal("0")
            if quantity and (not price or quantity * price >= rules.min_notional):
                raise ValueError("Flatten the current position before changing configuration")
            candidate = dict(row["config"])
            candidate.update(config)
            if "strategy" in config and "params" not in config:
                candidate["params"] = {}
            self._create_revision(memory, self._validate_config(candidate))
        return self.snapshot()

    @staticmethod
    def _unresolved(store):
        return [order for order in store.list_orders(1000) if order["status"] not in TERMINAL_STATUSES]

    def control(self, action: str) -> dict:
        if action not in ("start", "pause", "flatten", "step"):
            raise ValueError("Unknown automation control action")
        if action == "step":
            return self.tick_once()
        # Never wait on the worker's cycle lock or invoke the broker here.
        # SQLite serializes this command with configuration and submit claims,
        # while network requests run outside write transactions.
        with TradingMemory(self.db_path) as memory, memory.transaction():
            row = self._load(memory)
            state = row["state"]
            if action == "start":
                if state["killed"] or state["replay_complete"]:
                    raise ValueError("This simulation is halted; configure a new isolated simulation")
                if state["flatten"] or state.get("exit_pending", False):
                    raise ValueError("Finish closing the current position before starting entries")
            memory.connection.execute("""UPDATE automation_controls SET generation=generation+1,
                desired_running=?, flatten_requested=?, action=?, updated_at_ms=? WHERE revision=?""",
                (int(action == "start"), int(action == "flatten" or row["control"]["flatten_requested"]),
                 action, _now(), row["revision"]))
        return self.snapshot()

    def _demo(self, config, cursor, origin_ms):
        warmup = self._warmup(config)
        prices = [100] * warmup + list(range(101, 161)) + list(range(159, 69, -1))
        if cursor > len(prices):
            return None
        interval = TIMEFRAME_MS[config["timeframe"]]
        origin = origin_ms
        bars = [Candle(origin + i * interval, p, p, p, p, 1.0) for i, p in enumerate(prices[:cursor])]
        return {"bars": bars, "price": _decimal(prices[cursor - 1]),
                "market_time_ms": bars[-1].ts + interval, "next_cursor": cursor + 1}

    @staticmethod
    def _validate_bars(bars, interval, now_ms):
        if not bars:
            raise ValueError("market_data_empty")
        for i, bar in enumerate(bars):
            if type(bar.ts) is not int or bar.ts + interval > now_ms:
                raise ValueError("market_data_invalid_time")
            values = (bar.open, bar.high, bar.low, bar.close)
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError("market_data_invalid_prices")
            if not bar.low <= bar.open <= bar.high or not bar.low <= bar.close <= bar.high:
                raise ValueError("market_data_invalid_range")
            if not isinstance(bar.volume, (int, float)) or not math.isfinite(bar.volume) or bar.volume < 0:
                raise ValueError("market_data_invalid_volume")
            if i and bar.ts - bars[i - 1].ts != interval:
                raise ValueError("market_data_gap")

    def _market(self, row, at_ms, clock_override=False):
        config, state = row["config"], row["state"]
        if config["source"] == "demo":
            origin = state.setdefault("demo_origin_ms", self._demo_origin(config))
            return self._demo(config, state["demo_cursor"], origin)
        client = MarketData()
        interval = TIMEFRAME_MS[config["timeframe"]]
        data_error = None
        try:
            bars = client.get_candles(config["symbol"], config["timeframe"], limit=1000)
        except Exception:
            bars, data_error = None, "market_data_unavailable"
        # The ticker is requested last, including when history failed, so
        # exits use a fresh price instead of waiting through a history call.
        value = client.get_price(config["symbol"])
        received_ms = at_ms if clock_override else _now()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("market_data_invalid_price")
        if bars is not None:
            try:
                self._validate_bars(bars, interval, received_ms)
                expected_open = received_ms // interval * interval - interval
                latest_open = bars[-1].ts
                # Permit a single publication lag at the exact boundary,
                # never a whole missing interval throughout the next candle.
                grace_ms = min(60_000, interval // 10)
                publication_grace = (received_ms % interval <= grace_ms
                                     and latest_open == expected_open - interval)
                if latest_open != expected_open and not publication_grace:
                    raise ValueError("market_data_stale")
            except ValueError as exc:
                bars, data_error = None, str(exc)
        return {"bars": bars, "price": _decimal(value), "market_time_ms": received_ms,
                "next_cursor": None, "data_error": data_error}

    def _merge_history(self, row, bars, now_ms):
        interval = TIMEFRAME_MS[row["config"]["timeframe"]]
        self._validate_bars(bars, interval, now_ms)
        previous = [Candle(*item) for item in row["state"]["history"]]
        by_ts = {bar.ts: bar for bar in previous}
        for bar in bars:
            if previous and bar.ts < previous[0].ts:
                raise ValueError("market_data_seed_changed")
            if bar.ts in by_ts and bar != by_ts[bar.ts]:
                raise ValueError("market_data_conflict")
            by_ts[bar.ts] = bar
        result = [by_ts[key] for key in sorted(by_ts)]
        self._validate_bars(result, interval, now_ms)
        return result

    @staticmethod
    def _portfolio(row, broker):
        config, state = row["config"], row["state"]
        base, quote = config["symbol"].split("/")
        balances = broker.get_balances()
        cash, quantity = balances.get(quote, Decimal("0")), balances.get(base, Decimal("0"))
        price = _decimal(state["price"]) if state["price"] is not None else Decimal("0")
        with localcontext() as context:
            context.prec = 100
            value = quantity * price
            equity = cash + value
            peak = max(_decimal(state["peak_equity"]), equity)
            drawdown = Decimal("1") - equity / peak if peak else Decimal("0")
            return {"equity": float(equity), "cash": float(cash), "quantity": float(quantity),
                    "position_value": float(value), "return_pct": float((equity / _decimal(config["starting_cash"]) - 1) * 100),
                    "drawdown_pct": float(drawdown * 100), "peak_equity": float(peak),
                    "quote_asset": quote, "base_asset": base}

    def _finish_cycle(self, memory, row, broker, plan, action, reason, *, advance=True):
        state = row["state"]
        state["last_action"], state["last_reason"] = action, reason
        state["heartbeat_ms"] = max(state["heartbeat_ms"] or 0, plan["at_ms"])
        if advance:
            state["last_decision_bar"] = plan["bar_ts"]
        state["sequence"] = plan["sequence"]
        if advance and plan["next_cursor"] is not None:
            state["demo_cursor"] = plan["next_cursor"]
        state["inflight"] = None
        portfolio = self._portfolio(row, broker)
        state["peak_equity"] = str(portfolio["peak_equity"])
        state["error"] = None
        if portfolio["position_value"] < float(broker.get_rules(row["config"]["symbol"]).min_notional):
            state["flatten"] = False
            state["exit_pending"] = False
        with memory.transaction():
            memory.connection.execute("""INSERT OR IGNORE INTO automation_decisions
                (revision,cycle_id,at_ms,candle_ts,target,action,reason,news_ids_json,price,strategy)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (row["revision"], plan["request_id"], plan["at_ms"], plan["bar_ts"], plan["target"], action,
                 reason, _json(plan["news_ids"]), state["price"], row["config"]["strategy"]))
            memory.connection.execute("INSERT INTO automation_equity(revision,at_ms,equity,price) VALUES (?,?,?,?)",
                                      (row["revision"], plan["market_time_ms"], portfolio["equity"], state["price"]))
            self._write_state(memory, row)

    def _authorize_entry(self, memory, row, plan):
        """Local-only check inside ExecutionEngine's submit-claim transaction."""
        current = self._load(memory)
        state = current["state"]
        return bool(current["revision"] == row["revision"]
                    and current["control"]["generation"] == plan["control_generation"]
                    and state["desired_running"]
                    and not (state["flatten"] or state["killed"] or state["replay_complete"]
                             or state.get("exit_pending", False) or self._stop.is_set()))

    def _process_inflight(self, memory, row, broker, store, engine, *, recovering=False):
        plan = row["state"]["inflight"]
        order = store.get_by_request(plan["request_id"])
        if recovering and (order is None or order["status"] == "PREPARED"):
            # An unsent plan from a previous cycle has no valid current price,
            # news, or risk decision. Cancel it, then require a fresh cycle and
            # a new request ID. A SUBMITTING/UNKNOWN order is reconciled below
            # instead; its absence must never authorize another submission.
            if order is not None:
                engine.cancel(order["client_order_id"])
            self._finish_cycle(memory, row, broker, plan, "BLOCKED",
                               "recovered_unsent_plan_requires_fresh_decision", advance=False)
            return
        if order is None:
            if plan["side"] == "BUY" and not row["state"]["desired_running"]:
                self._finish_cycle(memory, row, broker, plan, "BLOCKED", "paused_before_submission")
                return
            order = engine.prepare(request_id=plan["request_id"], symbol=row["config"]["symbol"],
                                   side=plan["side"], quantity=plan["quantity"], limit_price=plan["price"])
        if order["status"] == "PREPARED":
            if plan["side"] == "BUY":
                order = engine.submit(order["client_order_id"],
                                      authorize=lambda intent: self._authorize_entry(memory, row, plan))
            else:
                order = engine.submit(order["client_order_id"])
        elif order["status"] not in TERMINAL_STATUSES:
            order = engine.reconcile(order["client_order_id"])
        if order["status"] not in TERMINAL_STATUSES:
            row["state"]["last_action"] = "BLOCKED"
            row["state"]["error"] = "execution_outcome_unresolved"
            row["state"]["last_reason"] = "Resolve the pending order before new decisions"
            self._save(memory, row)
            return
        if order["last_error"] == "submission_not_authorized":
            self._refresh_control(memory, row)
            self._finish_cycle(memory, row, broker, plan, "BLOCKED",
                               "control_changed_before_submission", advance=False)
            return
        action = plan["side"] if order["status"] == "FILLED" else "BLOCKED"
        self._finish_cycle(memory, row, broker, plan, action,
                           plan["reason"] if order["status"] == "FILLED" else "order_" + order["status"].lower())

    def tick_once(self, now_ms=None) -> dict:
        at_ms = _now() if now_ms is None else now_ms
        if type(at_ms) is not int or at_ms <= 0:
            raise ValueError("now_ms must be positive integer milliseconds")
        with self._cycle_lock, TradingMemory(self.db_path) as memory:
            row = self._load(memory)
            state, config = row["state"], row["config"]
            entry_generation = row["control"]["generation"]
            broker, store, engine = self._execution(memory, row)
            state["heartbeat_ms"] = at_ms
            try:
                if state["inflight"] is not None:
                    self._process_inflight(memory, row, broker, store, engine, recovering=True)
                    return self.snapshot()
                if state["replay_complete"] and not state["flatten"]:
                    self._save(memory, row)
                    return self.snapshot()
                recovered_unsent = False
                for order in self._unresolved(store):
                    if order["status"] == "PREPARED":
                        engine.cancel(order["client_order_id"])
                        recovered_unsent = True
                    else:
                        engine.reconcile(order["client_order_id"])
                if recovered_unsent:
                    state["last_decision_bar"] = None
                if self._unresolved(store):
                    raise ValueError("execution_outcome_unresolved")
                market = self._market(row, at_ms, clock_override=now_ms is not None)
                # Observe commands recorded while the feed was in flight.
                # A BUY remains fenced to the generation at cycle start.
                self._refresh_control(memory, row)
                if market is None:
                    state.update(desired_running=False, replay_complete=True, last_reason="demo_replay_complete")
                    # A finite replay cannot leave a tradable simulated
                    # position unattended. Flatten using its final known
                    # fixture price, without inventing any future candles.
                    final_cursor = self._warmup(config) + 150
                    market = self._demo(config, final_cursor, state["demo_origin_ms"])
                    market["next_cursor"] = None
                    state["flatten"] = True
                state["initialized"] = True
                if config["source"] == "binance":
                    at_ms = market["market_time_ms"]
                    state["heartbeat_ms"] = at_ms
                state["price"] = float(market["price"])
                state["market_time_ms"] = market["market_time_ms"]
                portfolio = self._portfolio(row, broker)
                state["peak_equity"] = str(portfolio["peak_equity"])
                if portfolio["drawdown_pct"] >= config["max_drawdown"] * 100:
                    state.update(killed=True, desired_running=False, flatten=True)
                forced_exit = state["flatten"] or state["killed"] or state.get("exit_pending", False)
                if forced_exit:
                    # Fresh ticker prices can trigger risk exits even when
                    # historical candles are missing or inconsistent.
                    history = [Candle(*item) for item in state["history"]]
                    bar_ts = history[-1].ts if history else market["market_time_ms"]
                    target = 0
                else:
                    if market.get("data_error"):
                        raise ValueError(market["data_error"])
                    history = self._merge_history(row, market["bars"], market["market_time_ms"])
                    state["history"] = [[b.ts, b.open, b.high, b.low, b.close, b.volume] for b in history]
                    bar_ts = history[-1].ts
                    target = self._strategy(config).signals(history)[-1]
                    if bar_ts == state["last_decision_bar"]:
                        state["error"] = None
                        self._save(memory, row)
                        return self.snapshot()
                articles = memory.recent_news(market["market_time_ms"], config["symbol"].split("/")[0],
                                              86_400_000, limit=50, include_body=False)
                reason = "strategy_long" if target else "strategy_flat"
                quantity = Decimal("0")
                side = None
                rules = broker.get_rules(config["symbol"])
                balances = broker.get_balances()
                owned = balances.get(rules.base_asset, Decimal("0"))
                with localcontext() as context:
                    context.prec = 100
                    price = (market["price"] / rules.tick_size).to_integral_value(rounding=ROUND_DOWN) * rules.tick_size
                    if price <= 0:
                        raise ValueError("market_price_below_tick")
                    tradable_owned = owned >= rules.min_qty and owned * price >= rules.min_notional
                    if forced_exit or (owned and target == 0):
                        if owned:
                            quantity = min(owned, _decimal(config["max_order_notional"]) / price)
                            side = "SELL"
                            state["exit_pending"] = True
                            reason = ("kill_switch" if state["killed"] else "demo_replay_complete" if state["replay_complete"]
                                      else "manual_flatten" if state["flatten"] else "strategy_exit")
                    elif target and not tradable_owned and bar_ts != state["last_decision_bar"]:
                        if not state["desired_running"]:
                            reason = "entries_paused"
                        elif config["news_policy"] == "require-fresh" and not articles:
                            reason = "blocked_no_fresh_news"
                        elif not self._stop.is_set():
                            budget = min(_decimal(config["max_order_notional"]), balances.get(rules.quote_asset, Decimal("0")) * _decimal(config["position_fraction"]))
                            quantity = budget / (Decimal("1") + store.limits.fee_buffer) / price
                            side = "BUY"
                    quantity = (quantity / rules.step_size).to_integral_value(rounding=ROUND_DOWN) * rules.step_size
                if state["flatten"] and not owned:
                    state["flatten"] = False
                    reason = "already_flat"
                sequence = state["sequence"] + 1
                plan = {"request_id": f"automation:{row['revision']}:{bar_ts}:{side or 'HOLD'}:{sequence}",
                        "sequence": sequence, "at_ms": at_ms, "bar_ts": bar_ts, "target": target,
                        "reason": reason, "news_ids": [a["id"] for a in articles],
                        "market_time_ms": market["market_time_ms"], "next_cursor": market["next_cursor"],
                        "control_generation": entry_generation,
                        "side": side, "quantity": _text(quantity), "price": _text(price)}
                if side is not None and (not quantity or quantity * price < rules.min_notional):
                    side = plan["side"] = None
                    plan["reason"] = "position_or_budget_below_minimum_order"
                    if state["flatten"]:
                        state["flatten"] = False
                    state["exit_pending"] = False
                if side is None:
                    action = "BLOCKED" if reason.startswith(("blocked", "entries_paused")) else "HOLD"
                    self._finish_cycle(memory, row, broker, plan, action, plan["reason"])
                else:
                    state["inflight"] = plan
                    self._save(memory, row)
                    self._process_inflight(memory, row, broker, store, engine)
            except Exception as exc:
                safe_codes = {"market_data_empty", "market_data_invalid_time", "market_data_invalid_prices",
                              "market_data_invalid_price", "market_data_invalid_range", "market_data_invalid_volume",
                              "market_data_gap", "market_data_stale", "market_data_seed_changed", "market_data_conflict", "market_data_unavailable",
                              "execution_outcome_unresolved", "market_price_below_tick"}
                state["error"] = str(exc) if isinstance(exc, ValueError) and str(exc) in safe_codes else "market_or_execution_unavailable"
                state["last_action"] = "BLOCKED"
                state["last_reason"] = "Waiting for valid data or execution reconciliation"
                self._save(memory, row)
        return self.snapshot()

    def snapshot(self) -> dict:
        with TradingMemory(self.db_path) as memory:
            row = self._load(memory)
            config, state = row["config"], row["state"]
            broker = object.__new__(SimulatedBroker)
            broker.memory, broker.account_id = memory, row["account_id"]
            portfolio = self._portfolio(row, broker)
            orders = []
            for item in memory.connection.execute("SELECT * FROM execution_orders WHERE account_id=? ORDER BY created_at_ms DESC,client_order_id LIMIT 100", (row["account_id"],)):
                order = dict(item)
                for field in ("intent", "rules", "snapshot"):
                    raw = order.pop(field + "_json")
                    order[field] = None if raw is None else json.loads(raw)
                order["cancel_requested"] = bool(order["cancel_requested"])
                orders.append(order)
            decisions = []
            for item in memory.connection.execute("SELECT * FROM automation_decisions WHERE revision=? ORDER BY id DESC LIMIT 100", (row["revision"],)):
                decision = dict(item)
                decision["news_ids"] = json.loads(decision.pop("news_ids_json"))
                decisions.append(decision)
            equity = [dict(item) for item in memory.connection.execute("SELECT at_ms,equity,price FROM automation_equity WHERE revision=? ORDER BY id DESC LIMIT 200", (row["revision"],))][::-1]
            metrics = memory.connection.execute("SELECT COUNT(*),SUM(CASE WHEN status='FILLED' THEN 1 ELSE 0 END) FROM execution_orders WHERE account_id=?", (row["account_id"],)).fetchone()
            halted = state["killed"] or state["replay_complete"]
            if row["control"]["generation"] > state.get("applied_control_generation", 0):
                state["last_reason"] = {"start": "started", "pause": "paused", "flatten": "flatten_requested"}[row["control"]["action"]]
            label = "halted" if halted else "error" if state["error"] else "running" if state["desired_running"] else "paused"
            return {"config": config, "revision": row["revision"], "account_id": row["account_id"],
                    "status": {"state": label, "running": bool(state["desired_running"] and not halted and not state["error"]),
                               "desired_running": bool(state["desired_running"] and not halted),
                               "flatten_requested": bool(state["flatten"] or state.get("exit_pending", False)),
                               "control_generation": row["control"]["generation"],
                               "heartbeat_ms": state["heartbeat_ms"], "market_time_ms": state["market_time_ms"],
                               "price": state["price"], "source": config["source"], "mode": "simulation",
                               "error": state["error"], "last_action": state["last_action"], "last_reason": state["last_reason"],
                               "worker_enabled": self.worker_enabled, "worker_alive": self.worker_alive,
                               "worker_error": self._worker_error,
                               "heartbeat_stale": bool((state["desired_running"] or state["initialized"] and not halted)
                                   and (state["heartbeat_ms"] is None or _now() - state["heartbeat_ms"] > max(60000, int(self.poll_seconds * 4000))))},
                    "portfolio": portfolio, "equity": equity, "orders": orders, "decisions": decisions,
                    "news": memory.recent_news(_now(), config["symbol"].split("/")[0], 86_400_000,
                                               limit=50, include_body=False),
                    "metrics": {"n_orders": metrics[0], "n_filled": metrics[1] or 0},
                    "capabilities": {"execution": "simulation", "llm": False}}

    def start_worker(self):
        if self._stop.is_set():
            raise RuntimeError("This automation runtime has shut down")
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="tradingbot-automation", daemon=True)
        self._thread.start()

    def _worker(self):
        try:
            while not self._stop.wait(self.poll_seconds):
                try:
                    with TradingMemory(self.db_path) as memory:
                        state = self._load(memory)["state"]
                    if (state["desired_running"] or state["initialized"] and not state["replay_complete"]
                            or state["flatten"] or state["inflight"]):
                        self.tick_once()
                    self._worker_error = None
                except Exception:
                    self._worker_error = "worker_iteration_failed"
                    # Storage can be temporarily unavailable. Preserve all
                    # pending plans and execution markers, report a generic
                    # error when possible, and retry after the bounded poll.
                    try:
                        with self._cycle_lock, TradingMemory(self.db_path) as memory:
                            row = self._load(memory)
                            row["state"]["error"] = "worker_iteration_failed"
                            row["state"]["last_reason"] = "Worker is retrying after a storage or cycle error"
                            self._save(memory, row)
                    except Exception:
                        pass
        finally:
            if self._stop.is_set():
                self._release_ownership()

    @property
    def worker_enabled(self):
        return self._thread is not None

    @property
    def worker_alive(self):
        return bool(self._thread is not None and self._thread.is_alive())

    def _release_ownership(self):
        handle, self._ownership = self._ownership, None
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def shutdown(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        if self._thread is None or not self._thread.is_alive():
            self._release_ownership()
