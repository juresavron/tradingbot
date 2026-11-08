import unittest
from decimal import Decimal

from tradingbot.orders import OrderIntent, OrderSnapshot, decimal_text, decimal_value


class TestExactOrderValues(unittest.TestCase):
    def test_zero_exponents_cannot_expand_into_unbounded_output(self):
        for text in ("0E-1000000000", "-0E+1000000000", "0.000000"):
            with self.subTest(text=text):
                value = decimal_value(text, "balance", allow_zero=True)
                self.assertEqual(value.as_tuple().exponent, 0)
                self.assertEqual(decimal_text(value), "0")
        self.assertEqual(decimal_text(Decimal("0E-1000000000")), "0")
        with self.assertRaises(ValueError):
            decimal_value("0." + "0" * 1000, "balance", allow_zero=True)

    def test_order_values_reject_nonfinite_float_and_excessive_precision(self):
        for value in ("NaN", "Infinity", "-1", "0", "1E-21", "1E21", 0.1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OrderIntent("order-1", "BTC/EUR", "BUY", value, "60000")
        with self.assertRaises(ValueError):
            OrderIntent("contains/unsupported", "BTC/EUR", "BUY", "1", "60000")

    def test_snapshots_cannot_claim_filled_without_full_consistent_execution(self):
        base = dict(client_order_id="order-1", symbol="BTC/EUR", side="BUY", quantity="1",
                    limit_price="100", status="FILLED", executed_quantity="1",
                    cumulative_quote_quantity="100", exchange_order_id="123", updated_at_ms=1000)
        for change in ({"executed_quantity": "0.5"}, {"executed_quantity": "2"},
                       {"status": "NEW"}, {"cumulative_quote_quantity": "0"},
                       {"updated_at_ms": 0}, {"status": "unsupported"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                OrderSnapshot(**{**base, **change})


if __name__ == "__main__":
    unittest.main()
