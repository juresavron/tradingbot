import sqlite3
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

from tradingbot.memory import TradingMemory
from tradingbot.orders import OrderIntent, OrderNotFound, OrderRejected
from tradingbot.simulated_broker import SimulatedBroker


def intent(client_id="sim-order-1", side="BUY", quantity="0.001", price="100000.00", symbol="BTCEUR"):
    return OrderIntent(client_id, symbol, side, quantity, price)


class TestSimulatedBroker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "simulation.sqlite3"
        self.memory = TradingMemory(self.path)
        self.broker = SimulatedBroker(self.memory, account_id="paper-300", initial_balances={"EUR": "300"})

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def test_buy_and_sell_charge_exact_quote_fees(self):
        bought = self.broker.place_order(intent())
        self.assertEqual(bought.status, "FILLED")
        self.assertEqual(bought.cumulative_quote_quantity, Decimal("100"))
        self.assertEqual(self.broker.get_balances(), {"EUR": Decimal("199.9"), "BTC": Decimal("0.001")})
        sold = self.broker.place_order(intent("sell-1", "SELL", "0.0005", "110000"))
        self.assertEqual(sold.executed_quantity, Decimal("0.0005"))
        self.assertEqual(self.broker.get_balances(), {"EUR": Decimal("254.845"), "BTC": Decimal("0.0005")})
        self.assertEqual(self.broker.environment, "simulation")
        self.assertEqual(self.broker.scope, "simulation:paper-300")

    def test_insufficient_quote_including_fee_and_no_short_sale(self):
        initial = self.broker.get_balances()
        for order in (intent(quantity="0.003"), intent(side="SELL")):
            with self.subTest(order=order), self.assertRaises(OrderRejected):
                self.broker.place_order(order)
            self.assertEqual(self.broker.get_balances(), initial)
            with self.assertRaises(OrderNotFound):
                self.broker.get_order(order)

    def test_identical_retries_lookup_and_cancel_never_move_balances_twice(self):
        order = intent()
        first = self.broker.place_order(order)
        balances = self.broker.get_balances()
        equivalent = intent(quantity="0.0010000", price="100000")
        for snapshot in (self.broker.place_order(equivalent), self.broker.get_order(order), self.broker.cancel_order(order)):
            self.assertEqual(snapshot, first)
        self.assertEqual(self.broker.get_balances(), balances)
        for changed in (intent(quantity="0.002"), intent(price="90000"), intent(side="SELL")):
            with self.subTest(changed=changed):
                for operation in (self.broker.place_order, self.broker.get_order, self.broker.cancel_order):
                    with self.assertRaises(OrderRejected):
                        operation(changed)
        self.assertEqual(self.broker.get_balances(), balances)

    def test_restart_preserves_wallet_orders_and_freezes_initial_settings(self):
        first = self.broker.place_order(intent())
        with TradingMemory(self.path) as restarted_memory:
            restarted = SimulatedBroker(restarted_memory, account_id="paper-300", initial_balances={"eur": "300.00"})
            self.assertEqual(restarted.get_order(intent()), first)
            self.assertEqual(restarted.get_balances(), self.broker.get_balances())
            for options in ({"initial_balances": {"EUR": "301"}}, {"initial_balances": {"EUR": "300"}, "fee": "0.002"}):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, "changed"):
                    SimulatedBroker(restarted_memory, account_id="paper-300", **options)
            self.assertEqual(restarted.get_balances(), self.broker.get_balances())

    def test_exchange_order_id_must_match_without_mutating_wallet(self):
        order = intent()
        first = self.broker.place_order(order)
        balances = self.broker.get_balances()
        for operation in (self.broker.get_order, self.broker.cancel_order):
            self.assertEqual(operation(order, exchange_order_id=first.exchange_order_id), first)
            for wrong_id in ("another-order", "", 123):
                with self.subTest(operation=operation.__name__, wrong_id=wrong_id):
                    with self.assertRaises(OrderNotFound):
                        operation(order, exchange_order_id=wrong_id)
                    self.assertEqual(self.broker.get_balances(), balances)

    def test_client_id_cannot_be_reused_by_another_account(self):
        self.broker.place_order(intent())
        other = SimulatedBroker(self.memory, account_id="other", initial_balances={"EUR": "300"})
        for operation in (other.place_order, other.get_order, other.cancel_order):
            with self.assertRaises(OrderRejected):
                operation(intent())
        self.assertEqual(other.get_balances(), {"EUR": Decimal("300")})

    def test_wallet_update_rolls_back_if_order_persistence_fails(self):
        self.memory.connection.execute("""
            CREATE TRIGGER reject_simulated_order BEFORE INSERT ON simulation_orders
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.broker.place_order(intent())
        self.assertEqual(self.broker.get_balances(), {"EUR": Decimal("300")})
        with self.assertRaises(OrderNotFound):
            self.broker.get_order(intent())
        self.memory.connection.execute("DROP TRIGGER reject_simulated_order")
        self.broker.place_order(intent())
        self.assertEqual(self.broker.get_balances()["EUR"], Decimal("199.9"))

    def test_synthetic_filters_are_enforced_and_are_not_live_metadata(self):
        for symbol in ("BTC/EUR", "BTCUSDT", "ETHEUR", "eth-usdt"):
            rules = self.broker.get_rules(symbol)
            self.assertEqual(rules.tick_size, Decimal("0.01"))
            self.assertEqual(rules.min_notional, Decimal("5"))
        self.assertEqual(self.broker.get_rules("BTCEUR").step_size, Decimal("0.00001"))
        self.assertEqual(self.broker.get_rules("ETHEUR").step_size, Decimal("0.0001"))
        for order in (intent(quantity="0.000001"), intent(price="100000.001"), intent(quantity="0.00001")):
            with self.subTest(order=order), self.assertRaises(OrderRejected):
                self.broker.place_order(order)
        with self.assertRaises(OrderRejected):
            self.broker.get_rules("DOGEEUR")

    def test_decimal_calculation_ignores_low_process_precision(self):
        with localcontext() as context:
            context.prec = 3
            self.broker.place_order(intent(quantity="0.00123000000000000000", price="123456.78000000000000"))
            self.assertEqual(self.broker.get_balances()["BTC"], Decimal("0.00123"))
            self.assertEqual(self.broker.get_balances()["EUR"], Decimal("147.9963087606"))
            self.assertEqual(context.prec, 3)

    def test_invalid_balances_fee_and_account_names_are_rejected(self):
        for options in (
            {"initial_balances": {"EUR": 300.0}}, {"initial_balances": {"EUR": "-1"}},
            {"initial_balances": {"EUR": "NaN"}}, {"initial_balances": {"EUR/USDT": "300"}},
            {"initial_balances": {"eur": "1", "EUR": "1"}},
            {"fee": "0.1"}, {"fee": "-0.01"}, {"fee": 0.001}, {"account_id": "../outside"},
        ):
            settings = dict(account_id="bad", initial_balances={"EUR": "300"})
            settings.update(options)
            with self.subTest(options=options), self.assertRaises(ValueError):
                SimulatedBroker(self.memory, **settings)


if __name__ == "__main__":
    unittest.main()
