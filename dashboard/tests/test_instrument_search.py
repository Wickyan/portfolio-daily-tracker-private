from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.api.portfolio_ai import apply_contextual_revision, contextual_instrument_queries, enrich_with_online_instrument_search, instrument_search_keywords, parse_bookkeeping_message
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


    def test_generic_search_keywords_do_not_hardcode_typo_pairs(self) -> None:
        keywords = instrument_search_keywords("纳指达成")
        self.assertIn("纳斯达克达成", keywords)
        self.assertIn("纳斯达克", keywords)
        self.assertNotIn("纳指大成", keywords)
        self.assertNotIn("159513", keywords)

        keywords = instrument_search_keywords("纳之大成")
        self.assertIn("ETF大成", keywords)
        self.assertNotIn("纳指大成", keywords)

    def test_phonetic_similarity_handles_generic_homophone_typos(self) -> None:
        self.assertEqual(
            InstrumentSearchService.fuzzy_similarity("纳指达成", "纳斯达克100ETF大成"),
            1.0,
        )
        self.assertEqual(
            InstrumentSearchService.fuzzy_similarity("纳之大成", "纳斯达克100ETF大成"),
            1.0,
        )

    def test_fuzzy_ranking_can_infer_single_likely_typo(self) -> None:
        candidates = [
            {"code": "159513", "name": "纳斯达克100ETF大成", "score": 80},
            {"code": "159501", "name": "纳指ETF嘉实", "score": 80},
            {"code": "159660", "name": "纳指ETF汇添富", "score": 80},
        ]
        ranked = InstrumentSearchService.rank_candidates("纳指达成", candidates)
        self.assertEqual(ranked[0]["code"], "159513")
        self.assertGreaterEqual(ranked[0]["match_score"], 0.72)
        self.assertEqual(InstrumentSearchService.choose_confident(ranked)["code"], "159513")

    async def test_typo_is_inferred_with_warning_without_hardcoded_rewrite(self) -> None:
        parsed = parse_bookkeeping_message("银河 纳指达成 1.243元 12700个")
        listed = {
            "code": "159513",
            "name": "纳斯达克100ETF大成",
            "currency": "CNY",
            "asset_type": "stock",
            "score": 80,
            "classify": "Fund",
            "source": "test",
        }
        alternatives = [
            {
                "code": "159501",
                "name": "纳指ETF嘉实",
                "currency": "CNY",
                "asset_type": "stock",
                "score": 80,
                "classify": "Fund",
                "source": "test",
            }
        ]

        async def fake_search(_self, keyword: str, limit: int = 20):
            if "纳斯达克" in keyword:
                return [listed, *alternatives]
            return []

        with patch.object(InstrumentSearchService, "search", new=fake_search):
            enriched = await enrich_with_online_instrument_search(
                parsed,
                "银河 纳指达成 1.243元 12700个",
            )

        self.assertEqual(enriched["changes"][0]["code"], "159513")
        self.assertTrue(any("可能包含简称或错别字" in warning for warning in enriched["warnings"]))

    async def test_ambiguous_fuzzy_search_returns_choices_instead_of_guessing(self) -> None:
        parsed = parse_bookkeeping_message("银河 纳指基金 1.243元 12700个")
        choices = [
            {
                "code": "159501", "name": "纳指ETF嘉实", "currency": "CNY",
                "asset_type": "stock", "score": 80, "classify": "Fund", "source": "test",
            },
            {
                "code": "159660", "name": "纳指ETF汇添富", "currency": "CNY",
                "asset_type": "stock", "score": 80, "classify": "Fund", "source": "test",
            },
        ]
        with patch.object(InstrumentSearchService, "search", new=AsyncMock(return_value=choices)):
            enriched = await enrich_with_online_instrument_search(parsed, "银河 纳指基金 1.243元 12700个")

        self.assertFalse(enriched["changes"][0].get("code"))
        self.assertEqual(len(enriched["instrument_candidates"]), 2)
        self.assertTrue(any("未能唯一确定标的" in warning for warning in enriched["warnings"]))

    async def test_otc_feeder_funds_are_not_offered_as_candidates(self) -> None:
        parsed = parse_bookkeeping_message("银河 纳指达成 1.243元 12700个")
        listed = {
            "code": "159513", "name": "纳斯达克100ETF大成", "currency": "CNY",
            "asset_type": "stock", "score": 80, "classify": "Fund", "source": "test",
        }
        otc = {
            "code": "000834", "name": "大成纳斯达克100ETF联接(QDII)A", "currency": "CNY",
            "asset_type": "stock", "score": 220, "classify": "OTCFUND", "source": "test",
        }
        with patch.object(InstrumentSearchService, "search", new=AsyncMock(return_value=[otc, listed])):
            enriched = await enrich_with_online_instrument_search(parsed, "银河 纳指达成 1.243元 12700个")

        self.assertEqual(enriched["changes"][0]["code"], "159513")
        self.assertNotIn("000834", str(enriched))


    def test_contextual_queries_combine_short_issuer_with_existing_etf_theme(self) -> None:
        pending = {
            "changes": [{"name": "纳斯达克", "currency": "CNY"}],
            "instrument_candidates": [
                {"code": "159513", "name": "纳斯达克100ETF大成"},
                {"code": "513100", "name": "纳指ETF国泰"},
            ],
        }
        queries = contextual_instrument_queries(pending, "国泰")
        self.assertIn("纳指ETF国泰", queries)

    async def test_llm_context_revision_selects_guotai_and_preserves_other_fields(self) -> None:
        pending = {
            "action_type": "add_or_update",
            "changes": [{
                "account": "银河",
                "name": "纳斯达克",
                "currency": "CNY",
                "asset_type": "stock",
                "quantity": 12700.0,
                "cost_price": 1.243,
                "total_cost": 15786.1,
            }],
            "missing_fields": ["code"],
            "warnings": ["旧候选提示"],
            "instrument_candidates": [
                {"code": "159513", "name": "纳斯达克100ETF大成", "currency": "CNY", "asset_type": "stock", "classify": "Fund", "score": 80},
            ],
        }
        guotai = {
            "code": "513100",
            "name": "纳指ETF国泰",
            "currency": "CNY",
            "asset_type": "stock",
            "score": 210,
            "classify": "Fund",
            "source": "test",
        }

        async def fake_search(_self, keyword: str, limit: int = 20):
            return [guotai] if "国泰" in keyword else []

        llm_patch = {
            "field_updates": {},
            "instrument_query": "纳指ETF国泰",
            "reason": "用户强调基金管理人为国泰，其他字段保持不变",
        }
        with patch("backend.api.portfolio_ai.resolve_revision_with_llm", new=AsyncMock(return_value=llm_patch)), patch.object(InstrumentSearchService, "search", new=fake_search):
            revised = await apply_contextual_revision(pending, "国泰")

        self.assertIsNotNone(revised)
        change = revised["changes"][0]
        self.assertEqual(change["code"], "513100")
        self.assertEqual(change["name"], "纳指ETF国泰")
        self.assertEqual(change["account"], "银河")
        self.assertEqual(change["quantity"], 12700)
        self.assertEqual(change["cost_price"], 1.243)
        self.assertAlmostEqual(change["total_cost"], 15786.1)
        self.assertEqual(revised["missing_fields"], [])
        self.assertTrue(any("结合原确认卡" in warning for warning in revised["warnings"]))

    async def test_contextual_revision_has_deterministic_fallback_when_llm_is_unavailable(self) -> None:
        pending = {
            "action_type": "add_or_update",
            "changes": [{
                "account": "银河",
                "name": "纳斯达克",
                "currency": "CNY",
                "asset_type": "stock",
                "quantity": 12700.0,
                "cost_price": 1.243,
                "total_cost": 15786.1,
            }],
            "missing_fields": ["code"],
            "warnings": [],
            "instrument_candidates": [
                {"code": "159513", "name": "纳斯达克100ETF大成", "currency": "CNY", "asset_type": "stock", "classify": "Fund", "score": 80},
            ],
        }
        guotai = {
            "code": "513100", "name": "纳指ETF国泰", "currency": "CNY",
            "asset_type": "stock", "score": 210, "classify": "Fund", "source": "test",
        }

        async def fake_search(_self, keyword: str, limit: int = 20):
            return [guotai] if "国泰" in keyword else []

        with patch("backend.api.portfolio_ai.resolve_revision_with_llm", new=AsyncMock(return_value=None)), patch.object(InstrumentSearchService, "search", new=fake_search):
            revised = await apply_contextual_revision(pending, "国泰")

        self.assertEqual(revised["changes"][0]["code"], "513100")
        self.assertEqual(revised["changes"][0]["name"], "纳指ETF国泰")
        self.assertEqual(revised["missing_fields"], [])



if __name__ == "__main__":
    unittest.main()
