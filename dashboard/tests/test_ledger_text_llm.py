import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from core.llm.base import LLMResponse
from backend.ledger import text_llm
from backend.ledger.models import TransactionType


class FakeTextProvider:
    def __init__(self, payload):
        self.payload = payload

    async def chat(self, messages, **kwargs):
        return LLMResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="fake-deepseek",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            finish_reason="stop",
        )


REF = datetime(2026, 10, 16, 8, 0, tzinfo=timezone(timedelta(hours=8)))


class LedgerTextLLMTest(unittest.IsolatedAsyncioTestCase):
    async def test_arbitrary_account_and_xiaomi_trade_are_preserved(self):
        payload = {
            "rows": [{
                "event_type": "BUY",
                "effective_at": "2026-10-06",
                "account": "账户③",
                "code": "01810.HK",
                "name": "小米集团-W",
                "currency": "HKD",
                "quantity": "200",
                "price": "23.88",
                "total_amount": None,
                "amount": None,
                "fee": "6.39",
                "tax": None,
                "confidence": 0.99,
                "inferred_fields": [],
                "raw_text": "账户③买入小米200股，23.88，手续费6.39港币",
            }],
            "warnings": [],
        }
        with patch.object(
            text_llm,
            "create_configured_text_provider",
            return_value=(FakeTextProvider(payload), "fake / deepseek-chat"),
        ):
            result = await text_llm.parse_historical_bookkeeping_text_with_llm(
                "10月6号账户③买回200股小米，23.88，手续费6.39港币",
                reference=REF,
            )
        event = result.events[0]
        self.assertEqual(event.event_type, TransactionType.BUY)
        self.assertEqual(event.account, "账户③")
        self.assertEqual(event.code, "01810.HK")
        self.assertEqual(str(event.quantity), "200")
        self.assertEqual(str(event.price), "23.88")
        self.assertEqual(str(event.fee), "6.39")
        self.assertTrue(any("V3确定性校验" in item for item in result.warnings))

    async def test_total_amount_is_converted_to_unit_price(self):
        payload = {
            "rows": [{
                "event_type": "BUY",
                "effective_at": "2026-10-06",
                "account": "IBKR",
                "code": "NVDA",
                "name": "NVIDIA",
                "currency": "USD",
                "quantity": "2",
                "price": None,
                "total_amount": "175",
                "amount": None,
                "fee": None,
                "tax": None,
                "confidence": 0.9,
                "inferred_fields": [],
                "raw_text": "IBKR买2股NVDA，一共175美元",
            }],
            "warnings": [],
        }
        with patch.object(
            text_llm,
            "create_configured_text_provider",
            return_value=(FakeTextProvider(payload), "fake"),
        ):
            result = await text_llm.parse_historical_bookkeeping_text_with_llm(
                "IBKR买2股NVDA，一共175美元",
                reference=REF,
            )
        self.assertEqual(str(result.events[0].price), "87.5")
        self.assertTrue(any("总成交金额" in item for item in result.warnings))

    async def test_missing_required_field_is_rejected_by_v3_validation(self):
        payload = {
            "rows": [{
                "event_type": "BUY",
                "effective_at": "2026-10-06",
                "account": "IBKR",
                "code": "NVDA",
                "currency": "USD",
                "quantity": None,
                "price": "100",
            }],
            "warnings": [],
        }
        with patch.object(
            text_llm,
            "create_configured_text_provider",
            return_value=(FakeTextProvider(payload), "fake"),
        ):
            with self.assertRaisesRegex(ValueError, "数量"):
                await text_llm.parse_historical_bookkeeping_text_with_llm(
                    "IBKR买NVDA",
                    reference=REF,
                )


if __name__ == "__main__":
    unittest.main()
