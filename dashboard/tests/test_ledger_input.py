import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from backend.ledger import (
    TransactionType,
    fx_transaction_from_changes,
    replay_transactions,
    resolve_effective_time,
    transaction_from_change,
)


CN_TZ = timezone(timedelta(hours=8))
REF = datetime(2026, 9, 18, 1, 19, tzinfo=CN_TZ)


class EffectiveTimeResolverTest(unittest.TestCase):
    def test_absolute_chinese_date_and_afternoon_time(self):
        result = resolve_effective_time("2025年3月12日下午3点30分买了英伟达", reference=REF)
        self.assertEqual(result.effective_at, "2025-03-12T15:30:00+08:00")
        self.assertEqual(result.precision, "minute")

    def test_relative_days(self):
        self.assertEqual(resolve_effective_time("昨天买了苹果", reference=REF).effective_at, "2026-09-17T00:00:00+08:00")
        self.assertEqual(resolve_effective_time("前天卖了苹果", reference=REF).effective_at, "2026-09-16T00:00:00+08:00")

    def test_last_year_month_day(self):
        result = resolve_effective_time("去年3月12号买了2股", reference=REF)
        self.assertEqual(result.effective_at, "2025-03-12T00:00:00+08:00")

    def test_no_date_defaults_to_now(self):
        result = resolve_effective_time("长桥买了2股英伟达", reference=REF)
        self.assertEqual(result.effective_at, REF.isoformat())
        self.assertFalse(result.explicit)
        self.assertEqual(result.precision, "now")

    def test_invalid_calendar_date_fails(self):
        with self.assertRaises(ValueError):
            resolve_effective_time("2025年2月30日买了苹果", reference=REF)


class ChangeConversionTest(unittest.TestCase):
    def test_buy_change_becomes_historical_buy_event(self):
        resolved = resolve_effective_time("去年3月12号下午3点", reference=REF)
        event = transaction_from_change({
            "action_type": "add_or_update",
            "account": "长桥",
            "name": "NVIDIA",
            "code": "NVDA",
            "currency": "USD",
            "quantity": 2,
            "cost_price": 87.5,
            "fee": 1,
        }, effective_at=resolved.effective_at, entered_at=REF.isoformat(), source="voice", date_precision=resolved.precision)
        self.assertEqual(event.event_type, TransactionType.BUY)
        self.assertEqual(event.effective_at, "2025-03-12T15:00:00+08:00")
        self.assertEqual(event.quantity, Decimal("2"))
        self.assertEqual(event.price, Decimal("87.5"))
        self.assertEqual(event.metadata["date_precision"], "minute")

    def test_set_position_becomes_opening_position_not_fake_buy(self):
        event = transaction_from_change({
            "action_type": "set_position", "account": "IBKR", "code": "GOOG",
            "name": "Google", "currency": "USD", "quantity": 5, "cost_price": 120,
        }, effective_at="2022-01-01T00:00:00+08:00")
        self.assertEqual(event.event_type, TransactionType.OPENING_POSITION)

    def test_set_cash_becomes_opening_cash_baseline(self):
        event = transaction_from_change({
            "action_type": "set_cash", "account": "IBKR", "currency": "USD", "amount": 5000,
        }, effective_at="2022-01-01T00:00:00+08:00")
        self.assertEqual(event.event_type, TransactionType.OPENING_CASH)
        state = replay_transactions([event])
        self.assertEqual(state.get_cash("IBKR", "USD"), Decimal("5000"))

    def test_existing_fx_pair_becomes_one_atomic_event(self):
        event = fx_transaction_from_changes([
            {"action_type": "withdraw", "account": "长桥", "currency": "HKD", "amount": 500},
            {"action_type": "deposit", "account": "长桥", "currency": "USD", "amount": 64.1},
        ], effective_at="2026-09-01T00:00:00+08:00")
        self.assertEqual(event.event_type, TransactionType.FX)
        self.assertEqual(event.amount, Decimal("500"))
        self.assertEqual(event.counter_amount, Decimal("64.1"))


if __name__ == "__main__":
    unittest.main()
