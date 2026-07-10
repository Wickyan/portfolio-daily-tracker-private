from __future__ import annotations

import unittest

from core.models import Market
from providers.market_data.tencent_provider import TencentDirectProvider


class TencentProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = TencentDirectProvider()

    def test_parse_us_quote(self) -> None:
        payload = (
            'v_usNVDA="200~英伟达~NVDA.OQ~208.27~202.78~202.00~61422993~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~~'
            '2026-07-10 11:42:20~5.49~2.71~209.48~201.92~USD~61422993~12679891505";'
        )
        quote = self.provider._parse_payload("NVDA", Market.US_STOCK, payload)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.stock.symbol, "NVDA")
        self.assertEqual(quote.stock.name, "英伟达")
        self.assertEqual(quote.price, 208.27)
        self.assertEqual(quote.prev_close, 202.78)

    def test_parse_a_share_quote(self) -> None:
        payload = (
            'v_sz002594="51~比亚迪~002594~90.00~86.87~86.84~709332~409211~300122~89.99~23~89.98~311~89.97~11~89.96~18~89.95~46~90.00~2414~90.01~2~90.04~4~90.05~12~90.06~1~~'
            '20260710161421~3.13~3.60~90.83~84.88~90.00/709332/6283278804~709332~6283278804";'
        )
        quote = self.provider._parse_payload("002594", Market.A_SHARE, payload)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote.stock.symbol, "002594")
        self.assertEqual(quote.stock.name, "比亚迪")
        self.assertEqual(quote.price, 90.0)
        self.assertEqual(quote.high, 90.83)
        self.assertEqual(quote.low, 84.88)


if __name__ == "__main__":
    unittest.main()
