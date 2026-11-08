import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext

from tradingbot.orders import OrderIntent
from tradingbot.risk import RiskLimits, SymbolRules, notional


def rules(**changes):
    values = dict(
        symbol="BTCEUR", base_asset="BTC", quote_asset="EUR",
        min_qty="0.005", max_qty="2", step_size="0.005",
        min_price="0.05", max_price="100000", tick_size="0.05",
        min_notional="5", max_notional="100000",
    )
    values.update(changes)
    return SymbolRules(**values)


def order(quantity="0.01", price="20000", side="BUY", symbol="BTCEUR"):
    return OrderIntent("research_order_1", symbol, side, quantity, price)


class TestSymbolRules(unittest.TestCase):
    def test_exact_filters_normalize_and_round_trip_without_float_conversion(self):
        limits = rules(symbol="btc/eur", base_asset="btc", quote_asset="eur")
        limits.validate(order())
        self.assertEqual(limits.symbol, "BTCEUR")
        self.assertEqual(limits.step_size, Decimal("0.005"))
        self.assertTrue(all(isinstance(v, str) for v in limits.to_dict().values()))
        self.assertEqual(SymbolRules(**limits.to_dict()), limits)
        with self.assertRaises(FrozenInstanceError):
            limits.min_qty = Decimal("0")

    def test_non_power_of_ten_quantity_and_price_steps_are_enforced(self):
        limits = rules(min_notional="0")
        limits.validate(order("0.015", "20000.15"))
        for intent, expected in (
            (order("0.016", "20000.15"), "step_size"),
            (order("0.015", "20000.16"), "tick_size"),
        ):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                limits.validate(intent)

    def test_minimum_maximum_and_symbol_filters(self):
        checks = [
            (rules(), order("0.001"), "min_qty"),
            (rules(), order("2.005"), "max_qty"),
            (rules(), order(price="0.01"), "min_price"),
            (rules(), order(price="100000.05"), "max_price"),
            (rules(), order("0.005", "100"), "min_notional"),
            (rules(max_notional="100"), order(), "max_notional"),
            (rules(), order(symbol="ETHEUR"), "symbol"),
        ]
        for limits, intent, expected in checks:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                limits.validate(intent)

    def test_zero_disables_filters_without_disabling_positive_order_values(self):
        disabled = rules(min_qty="0", max_qty="0", step_size="0", min_price="0",
                         max_price="0", tick_size="0", min_notional="0", max_notional="0")
        disabled.validate(order("0.00000001", "0.00000001"))
        for quantity, price in (("0", "100"), ("1", "0"), ("-1", "1")):
            with self.subTest(quantity=quantity, price=price), self.assertRaises(ValueError):
                disabled.validate(order(quantity, price))

    def test_decimal_math_is_exact_under_low_global_precision(self):
        intent = order("123456789.123456789", "0.123456789")
        disabled = rules(min_qty="0", max_qty="0", step_size="0.000000001",
                         min_price="0", max_price="0", tick_size="0.000000001",
                         min_notional="0", max_notional="0")
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(notional(intent), Decimal("15241578.765432099750190521"))
            disabled.validate(intent)
            self.assertEqual(context.prec, 6)

    def test_rejects_invalid_rules(self):
        changes = [
            {"step_size": 0.005}, {"tick_size": True}, {"min_qty": "NaN"},
            {"max_price": "Infinity"}, {"min_notional": "-1"},
            {"min_qty": "3", "max_qty": "2"},
            {"min_price": "10", "max_price": "1"},
            {"min_notional": "100", "max_notional": "10"},
            {"base_asset": "EUR"}, {"quote_asset": "EUR/USDT"},
            {"symbol": "BTC EUR"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                rules(**change)

    def test_rejects_symbol_with_mismatched_base_or_quote_asset(self):
        for changes in ({"base_asset": "ETH"}, {"quote_asset": "USDT"},
                        {"symbol": "BTCUSDT"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "base_asset.*quote_asset"):
                rules(**changes)


class TestRiskLimits(unittest.TestCase):
    def test_buy_reserves_exact_fee_buffer_and_accepts_boundary(self):
        limits = RiskLimits()
        intent = order()
        limits.validate(intent, rules(), {"EUR": Decimal("200.4")})
        with self.assertRaisesRegex(ValueError, "fee buffer"):
            limits.validate(intent, rules(), {"EUR": Decimal("200.39999999999999999999")})

    def test_per_order_cap_is_exact_and_cannot_be_overridden_by_large_balance(self):
        limits = RiskLimits()
        limits.validate(order("0.015"), rules(), {"EUR": Decimal("10000")})
        with self.assertRaisesRegex(ValueError, "max_order_notional"):
            limits.validate(order("0.02"), rules(), {"EUR": Decimal("10000")})

    def test_spot_sale_requires_owned_base_and_missing_balances_mean_zero(self):
        limits = RiskLimits()
        limits.validate(order(side="SELL"), rules(), {"BTC": Decimal("0.01")})
        for balances in ({}, {"EUR": Decimal("300")}, {"BTC": Decimal("0.00999999")}):
            with self.subTest(balances=balances), self.assertRaisesRegex(ValueError, "BTC"):
                limits.validate(order(side="SELL"), rules(), balances)
        with self.assertRaisesRegex(ValueError, "EUR"):
            limits.validate(order(), rules(), {})

    def test_invalid_balances_and_limits_are_rejected(self):
        for balance in ("-0.01", "NaN", "Infinity", 300.0, True):
            with self.subTest(balance=balance), self.assertRaises(ValueError):
                RiskLimits().validate(order(), rules(), {"EUR": balance})
        for changes in (
            {"max_order_notional": "0"}, {"max_order_notional": "NaN"},
            {"max_order_notional": 300.0}, {"fee_buffer": "-0.001"},
            {"fee_buffer": "0.10000001"}, {"fee_buffer": "Infinity"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                RiskLimits(**changes)
        RiskLimits(fee_buffer="0")
        RiskLimits(fee_buffer="0.1")

    def test_risk_validation_keeps_decimal_fee_math_exact(self):
        with localcontext() as context:
            context.prec = 4
            RiskLimits().validate(order(), rules(), {"EUR": Decimal("200.4")})
            with self.assertRaises(ValueError):
                RiskLimits().validate(order(), rules(), {"EUR": Decimal("200.399999")})
            self.assertEqual(context.prec, 4)

    def test_derived_balances_retain_up_to_sixty_fractional_places(self):
        above = Decimal("200.4" + "0" * 58 + "1")
        below = Decimal("200.399" + "9" * 57)
        balances = {"EUR": above, "BTC": Decimal("1e-60")}
        with localcontext() as context:
            context.prec = 4
            RiskLimits().validate(order(), rules(), balances)
            self.assertEqual(balances["EUR"], above)
            with self.assertRaisesRegex(ValueError, "fee buffer"):
                RiskLimits().validate(order(), rules(), {"EUR": below})
            with self.assertRaisesRegex(ValueError, "precision/range"):
                RiskLimits().validate(order(), rules(), {"EUR": "300", "BTC": Decimal("1e-61")})
            self.assertEqual(context.prec, 4)


if __name__ == "__main__":
    unittest.main()
