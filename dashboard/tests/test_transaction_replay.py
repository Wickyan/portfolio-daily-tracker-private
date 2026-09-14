import unittest
from decimal import Decimal

from backend.ledger import ReplayError, Transaction, TransactionType, replay_transactions


def tx(event_type, effective_at, **kwargs):
    defaults = dict(
        event_type=event_type,
        effective_at=effective_at,
        entered_at="2026-09-15T05:00:00+08:00",
        account="长桥",
    )
    defaults.update(kwargs)
    return Transaction(**defaults)


class TransactionReplayTest(unittest.TestCase):
    def test_two_buys_use_exact_moving_average_cost(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-01-01", code="NVDA", currency="USD", quantity=2, price="100", fee="1"),
            tx(TransactionType.BUY, "2025-02-01", code="NVDA", currency="USD", quantity=3, price="120", fee="2"),
        ])
        pos = state.get_position("长桥", "NVDA", "USD")
        self.assertEqual(pos.quantity, Decimal("5"))
        self.assertEqual(pos.total_cost, Decimal("563"))
        self.assertEqual(pos.average_cost, Decimal("112.6"))

    def test_sell_reduces_cost_basis_and_tracks_realized_pnl(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-01-01", code="NVDA", currency="USD", quantity=10, price=100, fee=10),
            tx(TransactionType.SELL, "2025-02-01", code="NVDA", currency="USD", quantity=4, price=120, fee=2, tax=1),
        ])
        pos = state.get_position("长桥", "NVDA", "USD")
        self.assertEqual(pos.quantity, Decimal("6"))
        self.assertEqual(pos.total_cost, Decimal("606"))
        self.assertEqual(pos.average_cost, Decimal("101"))
        self.assertEqual(state.get_realized_pnl("长桥", "NVDA", "USD"), Decimal("73"))

    def test_backfilled_old_trade_changes_replayed_state_even_if_entered_later(self):
        newer = tx(
            TransactionType.BUY, "2025-02-01", entered_at="2026-01-01T00:00:00+00:00",
            code="GOOG", currency="USD", quantity=1, price=200,
        )
        backfilled = tx(
            TransactionType.BUY, "2024-02-01", entered_at="2026-09-15T00:00:00+00:00",
            code="GOOG", currency="USD", quantity=1, price=100,
        )
        state = replay_transactions([newer, backfilled])
        pos = state.get_position("长桥", "GOOG", "USD")
        self.assertEqual(pos.quantity, Decimal("2"))
        self.assertEqual(pos.average_cost, Decimal("150"))

    def test_as_of_date_means_end_of_that_day(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-03-12T15:30:00", code="AAPL", currency="USD", quantity=1, price=100),
            tx(TransactionType.BUY, "2025-03-13T09:30:00", code="AAPL", currency="USD", quantity=1, price=200),
        ], as_of="2025-03-12")
        pos = state.get_position("长桥", "AAPL", "USD")
        self.assertEqual(pos.quantity, Decimal("1"))
        self.assertEqual(pos.average_cost, Decimal("100"))

    def test_as_of_date_respects_reported_local_date_not_utc_conversion(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-03-12T23:30:00+08:00", code="AAPL", currency="USD", quantity=1, price=100),
            tx(TransactionType.BUY, "2025-03-13T00:30:00+08:00", code="AAPL", currency="USD", quantity=1, price=200),
        ], as_of="2025-03-12")
        pos = state.get_position("长桥", "AAPL", "USD")
        self.assertEqual(pos.quantity, Decimal("1"))
        self.assertEqual(pos.average_cost, Decimal("100"))

    def test_opening_position_establishes_historical_baseline(self):
        state = replay_transactions([
            tx(TransactionType.OPENING_POSITION, "2022-01-01", code="GOOG", currency="USD", quantity=5, price=120),
            tx(TransactionType.BUY, "2022-02-01", code="GOOG", currency="USD", quantity=1, price=180),
        ])
        pos = state.get_position("长桥", "GOOG", "USD")
        self.assertEqual(pos.quantity, Decimal("6"))
        self.assertEqual(pos.total_cost, Decimal("780"))

    def test_oversell_fails_closed(self):
        with self.assertRaises(ReplayError):
            replay_transactions([
                tx(TransactionType.BUY, "2025-01-01", code="NVDA", currency="USD", quantity=1, price=100),
                tx(TransactionType.SELL, "2025-02-01", code="NVDA", currency="USD", quantity=2, price=120),
            ])

    def test_cash_fx_and_dividend_are_replayed_without_inferring_trade_settlement(self):
        state = replay_transactions([
            tx(TransactionType.DEPOSIT, "2025-01-01", currency="HKD", amount=1000),
            tx(TransactionType.FX, "2025-01-02", currency="HKD", amount=500, counter_currency="USD", counter_amount=64),
            tx(TransactionType.DIVIDEND, "2025-01-03", code="AAPL", currency="USD", amount=10, tax=1),
        ])
        self.assertEqual(state.get_cash("长桥", "HKD"), Decimal("500"))
        self.assertEqual(state.get_cash("长桥", "USD"), Decimal("73"))

    def test_unimplemented_corporate_action_fails_explicitly(self):
        with self.assertRaisesRegex(ReplayError, "not replayable yet"):
            replay_transactions([
                tx(TransactionType.SPLIT, "2025-01-01"),
            ])


if __name__ == "__main__":
    unittest.main()
