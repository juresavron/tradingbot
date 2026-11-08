"""Exact LIMIT/GTC orders on the fixed Binance Spot Test Network only.

Credentials must be real locally available testnet HMAC credentials, not a
proxy-secret placeholder: signing happens in this process. This module has no
production-origin option, automatic submission/cancellation retries, or actor
integration. Snapshots report cumulative base/quote execution, not commissions
or a reconciled trade ledger.

Cancellation IDs use v1 ``bc-`` plus the first 32 hexadecimal characters of
SHA-256(original client ID). Binance replaces an order's client ID on cancel;
lookups check this deterministic alias only after the original is not found.
Once an exchange order ID is known, query/cancel use that stable identity so
manual UI cancellations and their arbitrary client-ID replacements reconcile.
Other exchange filters, including percentage-price and account-wide limits,
remain enforced by Binance in addition to the static local SymbolRules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, localcontext

from .orders import BrokerUnavailable, OrderIntent, OrderNotFound, OrderRejected, OrderSnapshot, decimal_text, decimal_value
from .risk import SymbolRules

BASE_URL = "https://testnet.binance.vision"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PREACCEPT_REJECTIONS = frozenset({-1013, -1100, -1102, -1111, -2015})
KNOWN_FILTERS = frozenset({
    "PRICE_FILTER", "LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL", "PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE",
    "MAX_NUM_ORDERS", "MAX_POSITION", "MAX_NUM_ALGO_ORDERS", "MAX_NUM_ICEBERG_ORDERS", "ICEBERG_PARTS",
    "MARKET_LOT_SIZE", "TRAILING_DELTA", "MAX_NUM_ORDER_AMENDS", "MAX_NUM_ORDER_LISTS",
})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class BinanceTestnetBroker:
    environment = "binance-spot-testnet"

    def __init__(self, *, api_key: str, api_secret: str, timeout: float = 10):
        for value in (api_key, api_secret):
            if (not isinstance(value, str) or not value or len(value) > 512
                    or any(not 33 <= ord(char) <= 126 for char in value)):
                raise ValueError("Testnet API credentials must be nonempty local printable ASCII values")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Testnet timeout must be finite and positive")
        self._api_key = api_key
        self._api_secret = api_secret.encode("ascii")
        self.timeout = timeout
        self.scope = hashlib.sha256(api_key.encode("ascii")).hexdigest()
        self._server_offset_ms: int | None = None
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self):
        return "<BinanceTestnetBroker environment='binance-spot-testnet'>"

    @staticmethod
    def _symbol(symbol: str) -> str:
        if not isinstance(symbol, str):
            raise ValueError("A Binance spot symbol is required")
        normalized = symbol.upper().replace("/", "").replace("-", "")
        if not re.fullmatch(r"[A-Z0-9]{2,30}", normalized):
            raise ValueError("A Binance spot symbol is required")
        return normalized

    @staticmethod
    def _cancel_id(intent: OrderIntent) -> str:
        return "bc-" + hashlib.sha256(intent.client_order_id.encode()).hexdigest()[:32]

    @staticmethod
    def _exchange_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value) or not 0 < int(value) < 2**63:
            raise ValueError("A known positive Binance exchange order ID is required")
        return str(int(value))

    @staticmethod
    def _error(code, *, method: str, path: str):
        if type(code) is int:
            if method == "GET" and path == "/api/v3/order" and code == -2013:
                raise OrderNotFound("Testnet order was not found; submission state remains unresolved") from None
            if method == "POST" and path == "/api/v3/order" and code in PREACCEPT_REJECTIONS:
                raise OrderRejected(f"Testnet rejected the order before acceptance (code {code})") from None
        raise BrokerUnavailable("Testnet request failed; exchange order state is uncertain") from None

    def _request(self, method: str, path: str, params: dict | None = None, *, signed: bool = False):
        params = dict(params or {})
        headers = {"Accept": "application/json", "User-Agent": "tradingbot/binance-spot-testnet"}
        if signed:
            if self._server_offset_ms is None:
                self._sync_time()
            params.update(recvWindow=5000, timestamp=int(time.time() * 1000) + self._server_offset_ms)
            query = urllib.parse.urlencode(params)
            signature = hmac.new(self._api_secret, query.encode("ascii"), hashlib.sha256).hexdigest()
            query += "&signature=" + signature
            headers["X-MBX-APIKEY"] = self._api_key
        else:
            query = urllib.parse.urlencode(params)
        url = BASE_URL + path
        body = None
        if method == "POST":
            body = query.encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif query:
            url += "?" + query
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500 or 300 <= exc.code < 400:
                raise BrokerUnavailable("Testnet HTTP response is uncertain; reconcile before further execution") from None
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
                payload = json.loads(raw) if len(raw) <= MAX_RESPONSE_BYTES else None
                code = payload.get("code") if isinstance(payload, dict) else None
            except (ValueError, OSError):
                code = None
            self._error(code, method=method, path=path)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise BrokerUnavailable("Testnet network or TLS response is uncertain; reconcile before further execution") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BrokerUnavailable("Testnet response exceeded the bounded response size")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise BrokerUnavailable("Testnet response could not be decoded; exchange order state is uncertain") from None
        if isinstance(payload, dict) and "code" in payload and payload["code"] != 0:
            self._error(payload["code"], method=method, path=path)
        return payload

    def _sync_time(self) -> None:
        before_ms = int(time.time() * 1000)
        payload = self._request("GET", "/api/v3/time")
        after_ms = int(time.time() * 1000)
        if not isinstance(payload, dict) or type(payload.get("serverTime")) is not int or payload["serverTime"] <= 0:
            raise BrokerUnavailable("Testnet server time response is invalid")
        self._server_offset_ms = payload["serverTime"] - (before_ms + after_ms) // 2

    def get_rules(self, symbol: str) -> SymbolRules:
        symbol = self._symbol(symbol)
        payload = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        try:
            rows = payload["symbols"]
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError("wrong symbol count")
            row = rows[0]
            if (row["symbol"] != symbol or row["status"] != "TRADING" or not isinstance(row["orderTypes"], list)
                    or "LIMIT" not in row["orderTypes"]
                    or ("isSpotTradingAllowed" in row and row["isSpotTradingAllowed"] is not True)):
                raise ValueError("not tradable")
            for key in ("baseAsset", "quoteAsset"):
                if not isinstance(row[key], str) or not re.fullmatch(r"[A-Z0-9]{1,30}", row[key]):
                    raise ValueError("invalid asset")
            filters = {}
            if not isinstance(row["filters"], list):
                raise ValueError("missing filters")
            for entry in row["filters"]:
                kind = entry["filterType"]
                if kind not in KNOWN_FILTERS or kind in filters:
                    raise ValueError("unknown or duplicate filter")
                filters[kind] = entry
            lot, price = filters["LOT_SIZE"], filters["PRICE_FILTER"]
            minimums = [Decimal("0")]
            maximums = []
            if "MIN_NOTIONAL" in filters:
                minimums.append(decimal_value(filters["MIN_NOTIONAL"]["minNotional"], "minimum notional", allow_zero=True))
            if "NOTIONAL" in filters:
                notional = filters["NOTIONAL"]
                minimums.append(decimal_value(notional["minNotional"], "minimum notional", allow_zero=True))
                maximum = decimal_value(notional["maxNotional"], "maximum notional", allow_zero=True)
                if maximum:
                    maximums.append(maximum)
            rules = SymbolRules(
                symbol=symbol, base_asset=row["baseAsset"], quote_asset=row["quoteAsset"],
                min_qty=lot["minQty"], max_qty=lot["maxQty"], step_size=lot["stepSize"],
                min_price=price["minPrice"], max_price=price["maxPrice"], tick_size=price["tickSize"],
                min_notional=max(minimums), max_notional=min(maximums) if maximums else Decimal("0"),
            )
        except (KeyError, TypeError, ValueError):
            raise BrokerUnavailable("Testnet symbol rules are missing, unsupported, or not tradable") from None
        return rules

    def get_balances(self) -> dict[str, Decimal]:
        payload = self._request("GET", "/api/v3/account", signed=True)
        try:
            if not isinstance(payload, dict) or not isinstance(payload["balances"], list):
                raise ValueError("missing balances")
            if "accountType" in payload and payload["accountType"] != "SPOT":
                raise ValueError("not spot")
            if "canTrade" in payload and payload["canTrade"] is not True:
                raise ValueError("trading disabled")
            balances = {}
            for row in payload["balances"]:
                asset = row["asset"]
                if not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,30}", asset) or asset in balances:
                    raise ValueError("invalid or duplicate asset")
                balances[asset] = decimal_value(row["free"], "free balance", allow_zero=True)
        except (KeyError, TypeError, ValueError):
            raise BrokerUnavailable("Testnet free balances response is invalid or spot trading is unavailable") from None
        return balances

    @staticmethod
    def _snapshot(payload, intent: OrderIntent, *, expected_id: str | None = None,
                  cancellation=False, expected_exchange_order_id: str | None = None) -> OrderSnapshot:
        expected_id = expected_id or intent.client_order_id
        try:
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            client_id = payload["clientOrderId"]
            # A known numeric identity survives manual cancellation/replacement
            # of the client ID. Without it, the requested client ID must match.
            if not isinstance(client_id, str) or not client_id or len(client_id) > 128:
                raise ValueError("invalid client order ID")
            if ((expected_exchange_order_id is None or cancellation) and client_id != expected_id):
                raise ValueError("client ID mismatch")
            if (payload["symbol"] != intent.symbol
                    or payload["side"] != intent.side or payload["type"] != "LIMIT" or payload["timeInForce"] != "GTC"):
                raise ValueError("intent mismatch")
            if cancellation:
                original_id = payload["origClientOrderId"]
                if not isinstance(original_id, str) or not original_id or len(original_id) > 128:
                    raise ValueError("invalid original client ID")
                if expected_exchange_order_id is None and original_id != intent.client_order_id:
                    raise ValueError("original client ID mismatch")
            quantity = decimal_value(payload["origQty"], "original quantity")
            price = decimal_value(payload["price"], "original price")
            if quantity != intent.quantity or price != intent.limit_price:
                raise ValueError("original order values mismatch")
            order_id = payload["orderId"]
            if (type(order_id) is not int or order_id <= 0) and (
                not isinstance(order_id, str) or not re.fullmatch(r"[0-9]+", order_id) or int(order_id) <= 0
            ):
                raise ValueError("invalid order ID")
            if expected_exchange_order_id is not None and str(order_id) != expected_exchange_order_id:
                raise ValueError("known exchange order ID mismatch")
            updated = payload.get("updateTime", payload.get("transactTime", payload.get("time")))
            snapshot = OrderSnapshot(
                client_order_id=intent.client_order_id, symbol=payload["symbol"], side=payload["side"],
                quantity=quantity, limit_price=price, status=payload["status"],
                executed_quantity=payload["executedQty"], cumulative_quote_quantity=payload["cummulativeQuoteQty"],
                exchange_order_id=str(order_id), updated_at_ms=updated,
            )
            with localcontext() as context:
                context.prec = 100
                bound = snapshot.executed_quantity * intent.limit_price
                if ((intent.side == "BUY" and snapshot.cumulative_quote_quantity > bound)
                        or (intent.side == "SELL" and snapshot.cumulative_quote_quantity < bound)):
                    raise ValueError("executed quote violates limit price")
        except (KeyError, TypeError, ValueError):
            raise BrokerUnavailable("Testnet order response does not establish the original order's execution state") from None
        return snapshot

    def place_order(self, intent: OrderIntent) -> OrderSnapshot:
        payload = self._request("POST", "/api/v3/order", {
            "symbol": intent.symbol, "side": intent.side, "type": "LIMIT", "timeInForce": "GTC",
            "quantity": decimal_text(intent.quantity), "price": decimal_text(intent.limit_price),
            "newClientOrderId": intent.client_order_id, "newOrderRespType": "RESULT",
        }, signed=True)
        return self._snapshot(payload, intent)

    def get_order(self, intent: OrderIntent, *, exchange_order_id: str | None = None) -> OrderSnapshot:
        if exchange_order_id is not None:
            known_id = self._exchange_id(exchange_order_id)
            payload = self._request("GET", "/api/v3/order", {
                "symbol": intent.symbol, "orderId": known_id,
            }, signed=True)
            return self._snapshot(payload, intent, expected_exchange_order_id=known_id)
        try:
            payload = self._request("GET", "/api/v3/order", {
                "symbol": intent.symbol, "origClientOrderId": intent.client_order_id,
            }, signed=True)
            expected_id = intent.client_order_id
        except OrderNotFound:
            expected_id = self._cancel_id(intent)
            payload = self._request("GET", "/api/v3/order", {
                "symbol": intent.symbol, "origClientOrderId": expected_id,
            }, signed=True)
        return self._snapshot(payload, intent, expected_id=expected_id)

    def cancel_order(self, intent: OrderIntent, *, exchange_order_id: str | None = None) -> OrderSnapshot:
        alias = self._cancel_id(intent)
        known_id = self._exchange_id(exchange_order_id) if exchange_order_id is not None else None
        identity = {"orderId": known_id} if known_id is not None else {"origClientOrderId": intent.client_order_id}
        payload = self._request("DELETE", "/api/v3/order", {
            "symbol": intent.symbol, **identity, "newClientOrderId": alias,
        }, signed=True)
        return self._snapshot(payload, intent, expected_id=alias, cancellation=True,
                              expected_exchange_order_id=known_id)
