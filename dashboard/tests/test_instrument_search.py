from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.api.portfolio_ai import enrich_with_online_instrument_search, instrument_search_keywords, parse_bookkeeping_message
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


    def test_nasdaq_dacheng_alias_prefers_listed_etf_query(self) -> None:
        keywords = instrument_search_keywords("纳指达成")
        self.assertEqual(keywords[0], "纳斯达克100ETF 大成")
        self.assertIn("159513", keywords)
        self.assertIn("纳指大成", keywords)

    async def test_otc_feeder_funds_are_not_offered_as_candidates(self) -> None:
        parsed = parse_bookkeeping_message("银河 纳指达成 1.243元 12700个")
        listed = {
            "code": "159513",
            "name": "纳斯达克100ETF大成",
            "currency": "CNY",
            "asset_type": "stock",
            "score": 210,
            "classify": "Fund",
            "source": "test",
        }
        otc_a = {
            "code": "000834",
            "name": "大成纳斯达克100ETF联接(QDII)A",
            "currency": "CNY",
            "asset_type": "stock",
            "score": 220,
            "classify": "OTCFUND",
            "source": "test",
        }
        otc_c = {
            "code": "008971",
            "name": "大成纳斯达克100ETF联接(QDII)C",
            "currency": "CNY",
            "asset_type": "stock",
            "score": 220,
            "classify": "OTCFUND",
            "source": "test",
        }
        with patch.object(
            InstrumentSearchService,
            "search",
            new=AsyncMock(return_value=[otc_a, otc_c, listed]),
        ):
            enriched = await enrich_with_online_instrument_search(
                parsed,
                "银河 纳指达成 1.243元 12700个",
            )

        self.assertEqual(enriched["changes"][0]["code"], "159513")
        self.assertEqual(enriched["changes"][0]["name"], "纳斯达克100ETF大成")
        self.assertEqual(enriched["instrument_candidates"], [listed])
        self.assertNotIn("000834", str(enriched))
        self.assertNotIn("008971", str(enriched))



if __name__ == "__main__":
    unittest.main()
