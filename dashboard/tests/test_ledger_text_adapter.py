import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.ledger import TransactionType, parse_historical_bookkeeping_text

TZ = timezone(timedelta(hours=8))
REF = datetime(2026, 9, 18, 10, 0, tzinfo=TZ)


class HistoricalTextAdapterTest(unittest.TestCase):
    def test_historical_buy_with_chinese_commas_and_fee(self):
        result = parse_historical_bookkeeping_text(
            '去年3月12号在长桥买了2股英伟达，87.5美元一股，手续费1美元',
            reference=REF,
            source='voice',
        )
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.event_type, TransactionType.BUY)
        self.assertEqual(event.effective_at, '2025-03-12T00:00:00+08:00')
        self.assertEqual(event.account, '长桥')
        self.assertEqual(event.code, 'NVDA')
        self.assertEqual(event.quantity, Decimal('2'))
        self.assertEqual(event.price, Decimal('87.5'))
        self.assertEqual(event.fee, Decimal('1'))
        self.assertEqual(event.source, 'voice')

    def test_historical_sell_does_not_require_legacy_current_position(self):
        result = parse_historical_bookkeeping_text(
            '2025年8月5日在IBKR卖了1股苹果，成交价215美元',
            reference=REF,
        )
        event = result.events[0]
        self.assertEqual(event.event_type, TransactionType.SELL)
        self.assertEqual(event.account, 'IBKR')
        self.assertEqual(event.code, 'AAPL')
        self.assertEqual(event.price, Decimal('215'))

    def test_relative_cash_event(self):
        result = parse_historical_bookkeeping_text(
            '昨天长桥入金500美元',
            reference=REF,
        )
        event = result.events[0]
        self.assertEqual(event.event_type, TransactionType.DEPOSIT)
        self.assertEqual(event.effective_at, '2026-09-17T00:00:00+08:00')
        self.assertEqual(event.amount, Decimal('500'))

    def test_multi_clause_inherits_year_account_and_asset(self):
        result = parse_historical_bookkeeping_text(
            '去年3月12号长桥买2股英伟达87.5美元；5月8号又买3股92美元',
            reference=REF,
        )
        self.assertEqual(len(result.events), 2)
        first, second = result.events
        self.assertEqual(first.effective_at, '2025-03-12T00:00:00+08:00')
        self.assertEqual(second.effective_at, '2025-05-08T00:00:00+08:00')
        self.assertEqual(second.account, '长桥')
        self.assertEqual(second.code, 'NVDA')
        self.assertEqual(second.quantity, Decimal('3'))
        self.assertEqual(second.price, Decimal('92'))

    def test_total_trade_amount_is_divided_by_quantity(self):
        result = parse_historical_bookkeeping_text(
            '去年3月12号在长桥买了2股英伟达，总共175美元',
            reference=REF,
        )
        event = result.events[0]
        self.assertEqual(event.price, Decimal('87.5'))
        self.assertEqual(event.quantity, Decimal('2'))
        self.assertTrue(any('总成交金额' in warning for warning in result.warnings))

    def test_total_trade_amount_keeps_fee_separate(self):
        result = parse_historical_bookkeeping_text(
            '去年3月12号在长桥买了2股英伟达，总共175美元，手续费1美元',
            reference=REF,
        )
        event = result.events[0]
        self.assertEqual(event.price, Decimal('87.5'))
        self.assertEqual(event.fee, Decimal('1'))

    def test_explicit_per_share_price_wins_over_other_money(self):
        result = parse_historical_bookkeeping_text(
            '去年3月12号在长桥买了2股英伟达，87.5美元一股，手续费1美元',
            reference=REF,
        )
        event = result.events[0]
        self.assertEqual(event.price, Decimal('87.5'))
        self.assertEqual(event.fee, Decimal('1'))

    def test_sell_without_explicit_or_inherited_account_fails(self):
        with self.assertRaisesRegex(ValueError, '缺少字段|缺少账户'):
            parse_historical_bookkeeping_text(
                '2025年8月5日卖了1股苹果成交价215美元',
                reference=REF,
            )


if __name__ == '__main__':
    unittest.main()
