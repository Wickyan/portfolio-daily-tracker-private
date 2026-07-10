from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.api.portfolio_ai import enrich_with_online_instrument_search, parse_bookkeeping_message
from backend.services.instrument_search_service import InstrumentSearchService


class InstrumentSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_overseas_technology_high_confidence_auto_match(self) -> None:
        parsed = parse_bookkeeping_message("海外科技 100股票 一共花了9000元")
        candidates = [
            {
                "code": "501312",
                "name": "海外科技LOF",
                "currency": "CNY",
                "asset_type": "stock",
                "score": 180,
                "classify": "Fund",
                "source": "test",
            },
            {
                "code": "017204",
                "name": "华宝海外科技股票(QDII-LOF)C",
                "currency": "CNY",
                "asset_type": "stock",
                "score": 85,
                "classify": "OTCFUND",
                "source": "test",
            },
        ]
        with patch.object(InstrumentSearchService, "search", new=AsyncMock(return_value=candidates)):
            enriched = await enrich_with_online_instrument_search(parsed, "海外科技 100股票 一共花了9000元")

        change = enriched["changes"][0]
        self.assertEqual(change["code"], "501312")
        self.assertEqual(change["name"], "海外科技LOF")
        self.assertEqual(change["currency"], "CNY")
        self.assertEqual(change["asset_type"], "stock")
        self.assertNotIn("code", enriched["missing_fields"])
        self.assertTrue(any("已联网匹配" in warning for warning in enriched["warnings"]))


    def test_company_search_penalizes_leveraged_etfs(self) -> None:
        service = InstrumentSearchService()
        company = {"Code": "GOOG", "Name": "谷歌-C", "Classify": "UsStock", "MarketType": "7"}
        leveraged = {"Code": "GGLL", "Name": "二倍做多谷歌ETF-Direxion", "Classify": "UsStock", "MarketType": "7"}
        self.assertGreater(service._score("Google", company), service._score("Google", leveraged))

    def test_equal_top_scores_are_not_auto_selected(self) -> None:
        candidates = [
            {"code": "159501", "score": 150},
            {"code": "159660", "score": 150},
        ]
        self.assertIsNone(InstrumentSearchService.choose_confident(candidates))


if __name__ == "__main__":
    unittest.main()
