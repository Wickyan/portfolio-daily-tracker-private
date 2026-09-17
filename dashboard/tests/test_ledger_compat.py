import unittest
from decimal import Decimal

from backend.ledger import Transaction, TransactionType, replay_state_to_portfolio, replay_transactions
from backend.services.portfolio_valuation import calculate_portfolio_valuation


def tx(event_type, date, **kwargs):
    base = dict(event_type=event_type, effective_at=date, account="IBKR")
    base.update(kwargs)
    return Transaction(**base)


class LedgerCompatibilityTest(unittest.TestCase):
    def test_replay_state_matches_legacy_portfolio_shape(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-01-01", code="AAPL", name="Apple", currency="USD", quantity=2, price=100),
            tx(TransactionType.BUY, "2025-02-01", code="AAPL", name="Apple", currency="USD", quantity=1, price=130),
            tx(TransactionType.DEPOSIT, "2025-01-01", currency="USD", amount=500),
        ])
        raw = replay_state_to_portfolio(
            state,
            current_prices={("IBKR", "AAPL", "USD"): Decimal("150")},
            generated_at="2026-09-18T00:00:00+00:00",
        )
        self.assertEqual(raw["positions"][0]["quantity"], 3.0)
        self.assertEqual(raw["positions"][0]["cost_price"], 110.0)
        self.assertEqual(raw["positions"][0]["current_price"], 150.0)
        self.assertEqual(raw["cash_accounts"][0]["amount"], 500.0)
        self.assertEqual(raw["positions"][0]["source"], "ledger-v3")

    def test_existing_valuation_can_value_v3_compat_output(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-01-01", code="AAPL", name="Apple", currency="USD", quantity=2, price=100),
            tx(TransactionType.DEPOSIT, "2025-01-01", currency="USD", amount=50),
        ])
        raw = replay_state_to_portfolio(
            state,
            current_prices={("IBKR", "AAPL", "USD"): Decimal("150")},
        )
        valued = calculate_portfolio_valuation(raw, {"USD": 7.0, "CNY": 1.0})
        self.assertEqual(valued["total_market_value"], 2100.0)
        self.assertEqual(valued["cash"], 350.0)
        self.assertEqual(valued["total_assets"], 2450.0)
        self.assertEqual(valued["total_profit"], 700.0)

    def test_realized_pnl_survives_as_v3_extension(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-01-01", code="AAPL", currency="USD", quantity=2, price=100),
            tx(TransactionType.SELL, "2025-02-01", code="AAPL", currency="USD", quantity=1, price=150),
        ])
        raw = replay_state_to_portfolio(state)
        self.assertEqual(raw["positions"][0]["realized_pnl"], 50.0)
        self.assertEqual(raw["ledger_v3"]["realized_pnl"]["IBKR|AAPL|USD"], 50.0)

    def test_legacy_boundary_does_not_change_decimal_replay_state(self):
        state = replay_transactions([
            tx(TransactionType.BUY, "2025-01-01", code="AAPL", currency="USD", quantity="0.1", price="0.1"),
        ])
        before = state.get_position("IBKR", "AAPL", "USD").total_cost
        replay_state_to_portfolio(state)
        after = state.get_position("IBKR", "AAPL", "USD").total_cost
        self.assertEqual(before, Decimal("0.01"))
        self.assertEqual(after, Decimal("0.01"))


if __name__ == "__main__":
    unittest.main()
