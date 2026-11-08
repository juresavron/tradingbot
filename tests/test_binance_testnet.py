import hashlib
import io
import json
import unittest
import urllib.error
import urllib.parse
from decimal import Decimal
from unittest.mock import patch

from tradingbot.binance_testnet import BASE_URL, MAX_RESPONSE_BYTES, BinanceTestnetBroker, _NoRedirect
from tradingbot.orders import BrokerUnavailable, OrderIntent, OrderNotFound, OrderRejected


NOW = 1_499_827_319_559
KEY = "testnet-example-key"
SECRET = "testnet-example-local-signing-secret"


def intent(side="BUY"):
    return OrderIntent("order-123", "BTC/EUR", side, "0.00100", "20000.05")


def order_response(order=None, **changes):
    order = order or intent()
    result = {
        "symbol": order.symbol, "orderId": 28, "clientOrderId": order.client_order_id,
        "transactTime": NOW, "price": str(order.limit_price), "origQty": str(order.quantity),
        "executedQty": "0", "cummulativeQuoteQty": "0", "status": "NEW",
        "timeInForce": "GTC", "type": "LIMIT", "side": order.side,
    }
    result.update(changes)
    return result


def exchange_info(**changes):
    row = {
        "symbol": "BTCEUR", "baseAsset": "BTC", "quoteAsset": "EUR", "status": "TRADING",
        "orderTypes": ["LIMIT", "MARKET"], "isSpotTradingAllowed": True,
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.00010", "maxQty": "100", "stepSize": "0.00005"},
            {"filterType": "PRICE_FILTER", "minPrice": "0.05", "maxPrice": "1000000", "tickSize": "0.05"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "10"},
            {"filterType": "NOTIONAL", "minNotional": "20", "maxNotional": "200"},
            {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "5"},
        ],
    }
    row.update(changes)
    return {"symbols": [row]}


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def http_error(status, code):
    return urllib.error.HTTPError(
        "https://testnet.binance.vision/SECRET?signature=SECRET", status, "SECRET server message", {},
        response({"code": code, "msg": "SECRET response body"}),
    )


class TestBinanceTestnet(unittest.TestCase):
    def setUp(self):
        self.opener_patch = patch("tradingbot.binance_testnet.urllib.request.build_opener")
        self.factory = self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        self.open = self.factory.return_value.open
        self.clock_patch = patch("tradingbot.binance_testnet.time.time", return_value=NOW / 1000)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.broker = BinanceTestnetBroker(api_key=KEY, api_secret=SECRET)

    def signed_responses(self, *payloads):
        self.open.side_effect = [response({"serverTime": NOW})] + [response(payload) for payload in payloads]

    def test_fixed_environment_scope_redacted_repr_and_constructor_validation(self):
        self.assertEqual(BASE_URL, "https://testnet.binance.vision")
        self.assertEqual(self.broker.environment, "binance-spot-testnet")
        self.assertEqual(self.broker.scope, hashlib.sha256(KEY.encode()).hexdigest())
        self.assertNotIn(KEY, repr(self.broker))
        self.assertNotIn(SECRET, repr(self.broker))
        self.assertIsInstance(self.factory.call_args.args[0], _NoRedirect)
        with self.assertRaises(TypeError):
            BinanceTestnetBroker(api_key=KEY, api_secret=SECRET, base_url="https://api.binance.com")
        for kwargs in ({"api_key": ""}, {"api_secret": "SECRET\nHeader:bad"}, {"timeout": float("nan")}):
            settings = dict(api_key=KEY, api_secret=SECRET)
            settings.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BinanceTestnetBroker(**settings)

    def test_hmac_matches_official_binance_documentation_vector(self):
        documented_secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        broker = BinanceTestnetBroker(api_key=KEY, api_secret=documented_secret)
        broker._server_offset_ms = 0
        self.open.return_value = response({})
        broker._request("POST", "/api/v3/order", {
            "symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "1", "price": "0.1",
        }, signed=True)
        request = self.open.call_args.args[0]
        expected = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
                    "&recvWindow=5000&timestamp=1499827319559"
                    "&signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")
        self.assertEqual(request.data.decode(), expected)

    def test_public_clock_sync_then_exact_decimal_post_with_no_key_on_public_request(self):
        self.open.side_effect = [response({"serverTime": NOW + 1234}), response(order_response())]
        snapshot = self.broker.place_order(intent())
        self.assertEqual(snapshot.status, "NEW")
        first, post = [call.args[0] for call in self.open.call_args_list]
        self.assertEqual(first.full_url, BASE_URL + "/api/v3/time")
        self.assertIsNone(first.get_header("X-mbx-apikey"))
        self.assertEqual(post.full_url, BASE_URL + "/api/v3/order")
        self.assertEqual(post.get_method(), "POST")
        values = urllib.parse.parse_qs(post.data.decode())
        self.assertEqual(values["quantity"], ["0.00100"])
        self.assertEqual(values["price"], ["20000.05"])
        self.assertEqual(values["timestamp"], [str(NOW + 1234)])
        self.assertEqual(values["recvWindow"], ["5000"])
        self.assertEqual(values["newClientOrderId"], [intent().client_order_id])
        self.assertEqual(values["newOrderRespType"], ["RESULT"])
        self.assertEqual(post.get_header("X-mbx-apikey"), KEY)
        self.assertNotIn(KEY, post.full_url)
        self.assertNotIn(SECRET, post.full_url + post.data.decode())

    def test_static_rules_combine_notional_filters_and_validate_non_power_of_ten_ticks(self):
        self.open.return_value = response(exchange_info())
        rules = self.broker.get_rules("BTC/EUR")
        self.assertEqual(rules.symbol, "BTCEUR")
        self.assertEqual(rules.min_notional, Decimal("20"))
        self.assertEqual(rules.max_notional, Decimal("200"))
        self.assertEqual(rules.step_size, Decimal("0.00005"))
        rules.validate(intent())
        with self.assertRaises(ValueError):
            rules.validate(OrderIntent("bad-price", "BTCEUR", "BUY", "0.001", "20000.03"))
        request = self.open.call_args.args[0]
        self.assertIsNone(request.get_header("X-mbx-apikey"))

    def test_missing_unknown_conflicting_or_nontrading_rules_are_unavailable(self):
        cases = [exchange_info(status="BREAK"), exchange_info(orderTypes=["MARKET"]),
                 exchange_info(isSpotTradingAllowed=False), exchange_info(filters=[])]
        unknown = exchange_info()
        unknown["symbols"][0]["filters"].append({"filterType": "UNKNOWN_NEW_FILTER"})
        cases.append(unknown)
        bad_notional = exchange_info()
        bad_notional["symbols"][0]["filters"][-2]["maxNotional"] = "5"
        cases.append(bad_notional)
        for payload in cases:
            with self.subTest(payload=payload):
                self.open.return_value = response(payload)
                with self.assertRaises(BrokerUnavailable):
                    self.broker.get_rules("BTCEUR")

    def test_balances_are_exact_free_spot_only_and_malformed_balances_rejected(self):
        self.signed_responses({"accountType": "SPOT", "canTrade": True,
                               "balances": [{"asset": "EUR", "free": "299.12345678", "locked": "100"}]})
        self.assertEqual(self.broker.get_balances(), {"EUR": Decimal("299.12345678")})
        for payload in ({"balances": [{"asset": "EUR", "free": "NaN"}]},
                        {"accountType": "MARGIN", "balances": []},
                        {"canTrade": False, "balances": []}):
            self.open.side_effect = None
            self.open.return_value = response(payload)
            with self.assertRaises(BrokerUnavailable):
                self.broker.get_balances()

    def test_known_preaccept_rejections_and_ambiguous_failures_are_not_retried(self):
        for code, expected in [(-1013, OrderRejected), (-1100, OrderRejected), (-1102, OrderRejected),
                               (-1111, OrderRejected), (-2015, OrderRejected), (-2010, BrokerUnavailable),
                               (-1021, BrokerUnavailable)]:
            with self.subTest(code=code):
                self.broker._server_offset_ms = 0
                self.open.reset_mock()
                self.open.side_effect = http_error(400, code)
                with self.assertRaises(expected) as caught:
                    self.broker.place_order(intent())
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(self.open.call_count, 1)
        for error in (http_error(500, -1013), TimeoutError("SECRET signature"), urllib.error.URLError("SECRET proxy")):
            with self.subTest(error=type(error).__name__):
                self.open.reset_mock()
                self.open.side_effect = error
                with self.assertRaises(BrokerUnavailable) as caught:
                    self.broker.place_order(intent())
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(self.open.call_count, 1)

    def test_invalid_json_or_mismatched_response_is_uncertain_after_single_post(self):
        cases = [b"SECRET invalid JSON", order_response(clientOrderId="different"), order_response(origQty="0.002"),
                 order_response(type="MARKET"), order_response(timeInForce="IOC"), order_response(symbol="BTCUSDT"),
                 order_response(status="FILLED", executedQty="NaN"), order_response(price="20000.06"),
                 order_response(status="FILLED", executedQty="0.00100", cummulativeQuoteQty="30")]
        for payload in cases:
            with self.subTest(payload_type=type(payload).__name__):
                self.broker._server_offset_ms = 0
                self.open.reset_mock()
                self.open.side_effect = None
                self.open.return_value = io.BytesIO(payload) if isinstance(payload, bytes) else response(payload)
                with self.assertRaises(BrokerUnavailable) as caught:
                    self.broker.place_order(intent())
                self.assertNotIn("SECRET", str(caught.exception))
                self.assertEqual(self.open.call_count, 1)

    def test_partial_cancel_preserves_cumulative_execution_and_original_client_identity(self):
        alias = self.broker._cancel_id(intent())
        self.signed_responses(order_response(
            clientOrderId=alias, origClientOrderId="order-123", status="CANCELED",
            executedQty="0.0005", cummulativeQuoteQty="10.000025",
        ))
        snapshot = self.broker.cancel_order(intent())
        self.assertEqual(snapshot.client_order_id, "order-123")
        self.assertEqual(snapshot.status, "CANCELED")
        self.assertEqual(snapshot.executed_quantity, Decimal("0.0005"))
        self.assertEqual(snapshot.cumulative_quote_quantity, Decimal("10.000025"))
        cancel = self.open.call_args.args[0]
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(cancel.full_url).query)
        self.assertEqual(cancel.get_method(), "DELETE")
        self.assertEqual(values["origClientOrderId"], ["order-123"])
        self.assertEqual(values["newClientOrderId"], [alias])

    def test_lookup_uses_cancel_alias_only_after_original_order_not_found(self):
        alias = self.broker._cancel_id(intent())
        self.open.side_effect = [response({"serverTime": NOW}), http_error(400, -2013),
                                 response(order_response(clientOrderId=alias, status="CANCELED"))]
        snapshot = self.broker.get_order(intent())
        self.assertEqual(snapshot.client_order_id, "order-123")
        requests = [call.args[0] for call in self.open.call_args_list]
        self.assertEqual([r.get_method() for r in requests], ["GET", "GET", "GET"])
        self.assertIn("origClientOrderId=order-123", requests[1].full_url)
        self.assertIn("origClientOrderId=" + alias, requests[2].full_url)
        self.broker._server_offset_ms = 0
        self.open.reset_mock()
        self.open.side_effect = [http_error(400, -2013), http_error(400, -2013)]
        with self.assertRaises(OrderNotFound):
            self.broker.get_order(intent())
        self.assertEqual(self.open.call_count, 2)

    def test_known_order_id_recovers_manual_ui_cancel_with_arbitrary_client_alias(self):
        self.signed_responses(order_response(clientOrderId="manual-ui-cancel-alias", status="CANCELED",
                                             executedQty="0.0005", cummulativeQuoteQty="10.000025"))
        snapshot = self.broker.get_order(intent(), exchange_order_id="28")
        self.assertEqual(snapshot.client_order_id, "order-123")
        self.assertEqual(snapshot.exchange_order_id, "28")
        self.assertEqual(snapshot.executed_quantity, Decimal("0.0005"))
        request = self.open.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(query["orderId"], ["28"])
        self.assertNotIn("origClientOrderId", query)
        # A response from some different numeric identity is never accepted.
        self.open.side_effect = None
        self.open.return_value = response(order_response(clientOrderId="manual-alias", orderId=29))
        with self.assertRaises(BrokerUnavailable):
            self.broker.get_order(intent(), exchange_order_id="28")
        self.open.return_value = response(order_response(clientOrderId="manual-alias", origQty="0.002"))
        with self.assertRaises(BrokerUnavailable):
            self.broker.get_order(intent(), exchange_order_id="28")

    def test_known_order_id_cancel_uses_stable_identity_and_checks_returned_order_id(self):
        alias = self.broker._cancel_id(intent())
        self.signed_responses(order_response(clientOrderId=alias, origClientOrderId="previous-ui-alias", status="CANCELED"))
        snapshot = self.broker.cancel_order(intent(), exchange_order_id="28")
        self.assertEqual(snapshot.client_order_id, "order-123")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.open.call_args.args[0].full_url).query)
        self.assertEqual(query["orderId"], ["28"])
        self.assertNotIn("origClientOrderId", query)
        self.assertEqual(query["newClientOrderId"], [alias])
        self.open.side_effect = None
        self.open.return_value = response(order_response(clientOrderId=alias, origClientOrderId="previous-ui-alias", orderId=29))
        with self.assertRaises(BrokerUnavailable):
            self.broker.cancel_order(intent(), exchange_order_id="28")
        self.open.reset_mock()
        for invalid in ("", "-1", "28&symbol=BTCUSDT", "0", 28, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.broker.get_order(intent(), exchange_order_id=invalid)
        self.open.assert_not_called()

    def test_redirects_and_oversize_responses_are_uncertain_without_credential_exposure(self):
        handler = self.factory.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://api.binance.com"))
        self.broker._server_offset_ms = 0
        self.open.side_effect = http_error(302, -1013)
        with self.assertRaises(BrokerUnavailable):
            self.broker.place_order(intent())
        self.open.side_effect = None
        self.open.return_value = io.BytesIO(b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(BrokerUnavailable):
            self.broker.get_order(intent())


if __name__ == "__main__":
    unittest.main()
