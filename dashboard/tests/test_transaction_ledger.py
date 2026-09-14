import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backend.ledger import Transaction, TransactionRepository, TransactionType


class TransactionModelTest(unittest.TestCase):
    def test_historical_effective_time_is_distinct_from_entry_time(self):
        tx = Transaction(
            event_type=TransactionType.BUY,
            effective_at="2025-03-12",
            entered_at="2026-09-15T04:30:00+08:00",
            account="长桥",
            code="NVDA",
            currency="USD",
            quantity=2,
            price=87.5,
            fee=1,
            source="voice",
        ).validated()

        self.assertEqual(tx.effective_at, "2025-03-12T00:00:00")
        self.assertEqual(tx.entered_at, "2026-09-15T04:30:00+08:00")
        self.assertNotEqual(tx.effective_at, tx.entered_at)

    def test_buy_requires_quantity_price_and_currency(self):
        with self.assertRaises(ValueError):
            Transaction(
                event_type=TransactionType.BUY,
                effective_at="2025-03-12",
                account="长桥",
                code="NVDA",
                quantity=2,
                price=87.5,
            ).validated()

    def test_fx_requires_two_different_currencies_and_amounts(self):
        tx = Transaction(
            event_type=TransactionType.FX,
            effective_at="2026-09-01",
            account="长桥",
            currency="HKD",
            amount=500,
            counter_currency="USD",
            counter_amount=64.1,
        ).validated()
        self.assertEqual(tx.counter_currency, "USD")


class TransactionRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TransactionRepository(Path(self.tmp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def make_buy(self, **overrides):
        values = dict(
            event_type=TransactionType.BUY,
            effective_at="2025-03-12T10:30:00+08:00",
            entered_at="2026-09-15T04:30:00+08:00",
            account="长桥",
            code="NVDA",
            name="NVIDIA",
            currency="USD",
            quantity=2,
            price=87.5,
            fee=1,
            source="voice",
        )
        values.update(overrides)
        return Transaction(**values)

    def test_append_and_read_back_preserves_event(self):
        stored = self.repo.append(self.make_buy())
        loaded = self.repo.get(stored.transaction_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.event_type, TransactionType.BUY)
        self.assertEqual(loaded.effective_at, "2025-03-12T10:30:00+08:00")
        self.assertEqual(loaded.entered_at, "2026-09-15T04:30:00+08:00")
        self.assertEqual(loaded.quantity, 2)
        self.assertEqual(loaded.price, 87.5)


    def test_decimal_values_round_trip_exactly(self):
        stored = self.repo.append(self.make_buy(price=Decimal("0.1"), fee=Decimal("0.01")))
        loaded = self.repo.get(stored.transaction_id)
        self.assertEqual(loaded.price, Decimal("0.1"))
        self.assertEqual(loaded.fee, Decimal("0.01"))

    def test_order_uses_effective_time_not_entry_time(self):
        later_entered_old_trade = self.make_buy(
            effective_at="2024-01-01",
            entered_at="2026-09-15T05:00:00+08:00",
            price=50,
        )
        earlier_entered_new_trade = self.make_buy(
            effective_at="2025-01-01",
            entered_at="2026-09-14T05:00:00+08:00",
            price=70,
        )
        self.repo.append(earlier_entered_new_trade)
        self.repo.append(later_entered_old_trade)

        rows = self.repo.list_transactions(code="NVDA")
        self.assertEqual([row.price for row in rows], [50, 70])

    def test_external_trade_id_is_idempotency_key_per_account(self):
        self.repo.append(self.make_buy(external_trade_id="broker-fill-123"))
        with self.assertRaises(ValueError):
            self.repo.append(self.make_buy(external_trade_id="broker-fill-123"))

    def test_same_external_trade_id_can_exist_in_different_accounts(self):
        self.repo.append(self.make_buy(external_trade_id="fill-1", account="长桥"))
        self.repo.append(self.make_buy(external_trade_id="fill-1", account="IBKR"))
        self.assertEqual(len(self.repo.list_transactions()), 2)


if __name__ == "__main__":
    unittest.main()
