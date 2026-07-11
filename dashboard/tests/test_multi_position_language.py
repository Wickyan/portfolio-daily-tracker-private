from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.api.portfolio_ai import (
    ConfirmRequest,
    ReviseRequest,
    ai_confirm,
    ai_revise,
    apply_indexed_multi_revision,
    enrich_sell_availability,
    enrich_with_online_instrument_search,
    make_pending,
    parse_bookkeeping_message,
    pending_path,
    save_pending,
)
from backend.services.portfolio_write_service import PortfolioWriteService


class MultiPositionLanguageParseTest(unittest.TestCase):
    def assert_two_buys(self, text: str) -> list[dict]:
        parsed = parse_bookkeeping_message(text)
        self.assertEqual(parsed["intent"], "bookkeeping")
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(len(parsed["changes"]), 2)
        self.assertTrue(any("分别解析为2条持仓记录" in warning for warning in parsed["warnings"]))
        return parsed["changes"]

    def test_semicolon_separates_two_buy_records(self) -> None:
        changes = self.assert_two_buys(
            "银河买了500股比亚迪，均价102.742元；IBKR买了3股苹果，均价202.68美元"
        )
        self.assertEqual(
            [(item["account"], item["code"], item["quantity"], item["cost_price"]) for item in changes],
            [("银河", "002594", 500, 102.742), ("IBKR", "AAPL", 3, 202.68)],
        )

    def test_then_connector_separates_two_buy_records(self) -> None:
        changes = self.assert_two_buys(
            "我在银河买了500股比亚迪，均价102.742元，然后在IBKR买了3股苹果，均价202.68美元"
        )
        self.assertEqual(changes[0]["account"], "银河")
        self.assertEqual(changes[1]["account"], "IBKR")

    def test_newline_separates_concise_records(self) -> None:
        changes = self.assert_two_buys(
            "银河 比亚迪 500股 102.742元\nIBKR 苹果 3股 202.68美元"
        )
        self.assertEqual(changes[0]["code"], "002594")
        self.assertEqual(changes[1]["code"], "AAPL")

    def test_letter_labels_are_removed_before_parsing(self) -> None:
        changes = self.assert_two_buys(
            "A：银河，比亚迪500股，成本价102.742元；B：IBKR，苹果3股，成本价202.68美元"
        )
        self.assertEqual(changes[0]["account"], "银河")
        self.assertEqual(changes[1]["account"], "IBKR")

    def test_numeric_and_chinese_record_labels_are_supported(self) -> None:
        changes = self.assert_two_buys(
            "1.银河，比亚迪500股，成本价102.742元\n第二条：IBKR，苹果3股，成本价202.68美元"
        )
        self.assertEqual(changes[0]["account"], "银河")
        self.assertEqual(changes[1]["account"], "IBKR")

    def test_chinese_record_label_without_colon_does_not_hide_account(self) -> None:
        changes = self.assert_two_buys(
            "第一条银河，比亚迪500股，成本价102.742元\n第二条IBKR，苹果3股，成本价202.68美元"
        )
        self.assertEqual(changes[0]["account"], "银河")
        self.assertEqual(changes[1]["account"], "IBKR")

    def test_new_broker_names_with_broker_suffix_are_kept_separate(self) -> None:
        changes = self.assert_two_buys(
            "华泰证券买了100股比亚迪，均价100元；招商证券买了2股苹果，均价200美元"
        )
        self.assertEqual(changes[0]["account"], "华泰证券")
        self.assertEqual(changes[1]["account"], "招商证券")

    def test_second_record_can_inherit_previous_account(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河买了500股比亚迪，均价102.742元；再买了100股小米，均价20港币"
        )
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual([item["account"] for item in parsed["changes"]], ["银河", "银河"])
        self.assertTrue(any("第2条未单独填写账户" in warning for warning in parsed["warnings"]))

    def test_different_brokers_are_kept_separate(self) -> None:
        changes = self.assert_two_buys(
            "长桥买了100股腾讯，均价500港币，银河买了500股比亚迪，均价102.742元"
        )
        self.assertEqual(changes[0]["account"], "长桥")
        self.assertEqual(changes[1]["account"], "银河")

    def test_comma_without_connector_still_separates_new_broker_record(self) -> None:
        changes = self.assert_two_buys(
            "银河买了500股比亚迪，均价102.742元，IBKR买了3股苹果，均价202.68美元"
        )
        self.assertEqual(changes[0]["account"], "银河")
        self.assertEqual(changes[1]["account"], "IBKR")

    def test_broker_can_appear_after_asset_and_quantity(self) -> None:
        changes = self.assert_two_buys(
            "比亚迪买了500股，银河，均价102.742元；苹果买了3股，IBKR，均价202.68美元"
        )
        self.assertEqual(changes[0]["account"], "银河")
        self.assertEqual(changes[1]["account"], "IBKR")

    def test_explicit_broker_label_inside_each_record(self) -> None:
        changes = self.assert_two_buys(
            "买了500股比亚迪，券商银河，均价102.742元；买了3股苹果，券商IBKR，均价202.68美元"
        )
        self.assertEqual(changes[0]["account"], "银河")
        self.assertEqual(changes[1]["account"], "IBKR")

    def test_buy_and_cash_remain_blocked_as_mixed_operations(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河增加100元；长桥买入2股苹果，均价200美元"
        )
        self.assertEqual(parsed["action_type"], "multiple_operations")
        self.assertIn("multiple_operations", parsed["missing_fields"])
        self.assertEqual(parsed["changes"], [])

    def test_buy_and_sell_can_share_one_position_batch(self) -> None:
        parsed = parse_bookkeeping_message(
            "IBKR买入2股苹果，均价200美元；IBKR卖出1股苹果"
        )
        self.assertEqual(parsed["action_type"], "multi_position")
        self.assertEqual(
            [change["action_type"] for change in parsed["changes"]],
            ["add_or_update", "sell"],
        )

    def test_one_invalid_record_blocks_whole_batch(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河买了500股比亚迪，均价102.742元；IBKR买了-3股苹果，均价202.68美元"
        )
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(len(parsed["changes"]), 2)
        self.assertIn("positive_quantity", parsed["missing_fields"])
        self.assertFalse(make_pending(parsed, "text", "batch")["requires_confirmation"])

    def test_record_like_unparsed_clause_blocks_partial_import(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河买了500股比亚迪，均价102.742元；IBKR苹果3 202美元"
        )
        self.assertEqual(parsed["action_type"], "multiple_records_incomplete")
        self.assertEqual(parsed["changes"], [])
        self.assertIn("unparsed_record", parsed["missing_fields"])
        self.assertTrue(any("第2条" in warning and "不会只写入" in warning for warning in parsed["warnings"]))

    def test_non_record_trailing_comment_does_not_block_valid_record(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河买了500股比亚迪，均价102.742元；谢谢"
        )
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(len(parsed["changes"]), 1)
        self.assertEqual(parsed["missing_fields"], [])


class MultiPositionSearchAndRevisionTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_missing_instrument_is_searched_independently(self) -> None:
        parsed = {
            "intent": "bookkeeping",
            "summary": "two",
            "action_type": "add_or_update",
            "changes": [
                {
                    "action_type": "add_or_update",
                    "account": "银河",
                    "name": "甲产品",
                    "currency": "CNY",
                    "asset_type": "stock",
                    "quantity": 1,
                    "cost_price": 10,
                },
                {
                    "action_type": "add_or_update",
                    "account": "IBKR",
                    "name": "乙产品",
                    "currency": "USD",
                    "asset_type": "stock",
                    "quantity": 2,
                    "cost_price": 20,
                },
            ],
            "missing_fields": ["code"],
            "warnings": [],
        }

        async def fake_search(keyword: str, extra_keywords=None):
            if keyword == "甲产品":
                return ([{
                    "code": "000001", "name": "甲产品", "currency": "CNY",
                    "asset_type": "stock", "score": 200, "match_score": 1.0,
                }], [])
            return ([
                {
                    "code": "BBB", "name": "乙产品B", "currency": "USD",
                    "asset_type": "stock", "score": 100, "match_score": 0.80,
                },
                {
                    "code": "BBC", "name": "乙产品C", "currency": "USD",
                    "asset_type": "stock", "score": 100, "match_score": 0.78,
                },
            ], [])

        with patch("backend.api.portfolio_ai.search_ranked_listed_candidates", new=fake_search):
            enriched = await enrich_with_online_instrument_search(parsed, "two")

        self.assertEqual(enriched["changes"][0]["code"], "000001")
        self.assertFalse(enriched["changes"][1].get("code"))
        self.assertIn("code", enriched["missing_fields"])
        self.assertTrue(enriched["instrument_candidates"])
        self.assertTrue(all(item["change_index"] == 1 for item in enriched["instrument_candidates"]))
        self.assertTrue(any("第2条未能唯一确定标的" in warning for warning in enriched["warnings"]))

    async def test_indexed_revision_updates_only_target_record(self) -> None:
        pending = {
            "action_type": "add_or_update",
            "changes": [
                {
                    "action_type": "add_or_update", "account": "银河", "name": "比亚迪",
                    "code": "002594", "currency": "CNY", "asset_type": "stock",
                    "quantity": 500, "cost_price": 100,
                },
                {
                    "action_type": "add_or_update", "account": "IBKR", "name": "Apple/苹果",
                    "code": "AAPL", "currency": "USD", "asset_type": "stock",
                    "quantity": 3, "cost_price": 200,
                },
            ],
            "warnings": [],
            "instrument_candidates": [],
        }
        revised = await apply_indexed_multi_revision(pending, "第2条数量5")
        self.assertIsNotNone(revised)
        self.assertEqual(revised["changes"][0]["quantity"], 500)
        self.assertEqual(revised["changes"][1]["quantity"], 5)
        self.assertEqual(revised["changes"][1]["cost_price"], 200)
        self.assertEqual(revised["summary"], "已修改第2条记录，请重新确认")

    async def test_chinese_index_and_optional_de_are_supported(self) -> None:
        pending = {
            "action_type": "add_or_update",
            "changes": [
                {
                    "action_type": "add_or_update", "account": "银河", "name": "比亚迪",
                    "code": "002594", "currency": "CNY", "asset_type": "stock",
                    "quantity": 500, "cost_price": 100,
                },
                {
                    "action_type": "add_or_update", "account": "IBKR", "name": "Apple/苹果",
                    "code": "AAPL", "currency": "USD", "asset_type": "stock",
                    "quantity": 3, "cost_price": 200,
                },
            ],
            "warnings": [],
            "instrument_candidates": [],
        }
        revised = await apply_indexed_multi_revision(pending, "第二条的数量5")
        self.assertIsNotNone(revised)
        self.assertEqual(revised["changes"][0]["quantity"], 500)
        self.assertEqual(revised["changes"][1]["quantity"], 5)

    async def test_out_of_range_record_index_is_rejected(self) -> None:
        pending = {
            "action_type": "add_or_update",
            "changes": [
                {"action_type": "add_or_update", "quantity": 1, "cost_price": 10},
                {"action_type": "add_or_update", "quantity": 2, "cost_price": 20},
            ],
            "warnings": [],
            "instrument_candidates": [],
        }
        with self.assertRaises(HTTPException) as context:
            await apply_indexed_multi_revision(pending, "第三条数量3")
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("没有第3条", context.exception.detail)

    async def test_indexed_candidate_code_updates_correct_record(self) -> None:
        pending = {
            "action_type": "add_or_update",
            "changes": [
                {
                    "action_type": "add_or_update", "account": "银河", "name": "比亚迪",
                    "code": "002594", "currency": "CNY", "asset_type": "stock",
                    "quantity": 500, "cost_price": 100,
                },
                {
                    "action_type": "add_or_update", "account": "银河", "name": "纳指",
                    "currency": "CNY", "asset_type": "stock", "quantity": 10, "cost_price": 1.2,
                },
            ],
            "warnings": [],
            "instrument_candidates": [{
                "code": "513100", "name": "纳指ETF国泰", "currency": "CNY",
                "asset_type": "stock", "change_index": 1,
            }],
        }
        revised = await apply_indexed_multi_revision(pending, "第2条代码513100")
        self.assertEqual(revised["changes"][0]["code"], "002594")
        self.assertEqual(revised["changes"][1]["code"], "513100")
        self.assertEqual(revised["changes"][1]["name"], "纳指ETF国泰")
        self.assertEqual(revised["missing_fields"], [])
        self.assertEqual(revised["instrument_candidates"], [])
        self.assertFalse(any("未能唯一确定标的" in warning for warning in revised["warnings"]))


class MultiPositionWriteFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def create_pending(self, text: str) -> dict:
        parsed = parse_bookkeeping_message(text)
        pending = make_pending(parsed, "text", text)
        save_pending(pending)
        return pending

    def test_two_buys_confirm_atomically_and_rollback_together(self) -> None:
        pending = self.create_pending(
            "银河买了500股比亚迪，均价102.742元；IBKR买了3股苹果，均价202.68美元"
        )
        self.assertTrue(pending["requires_confirmation"])
        result = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        portfolio = self.service.load_portfolio()
        identities = {
            (item["account"], item["code"], item["currency"]): item["quantity"]
            for item in portfolio["positions"]
        }
        self.assertEqual(identities[("银河", "002594", "CNY")], 500)
        self.assertEqual(identities[("IBKR", "AAPL", "USD")], 3)
        self.assertEqual(result["imported_positions"], 2)

        for operation_id in reversed(result["operation_ids"]):
            self.service.rollback_operation(operation_id)
        self.assertEqual(self.service.load_portfolio()["positions"], [])

    def test_one_invalid_child_prevents_any_write(self) -> None:
        pending = self.create_pending(
            "银河买了500股比亚迪，均价102.742元；IBKR买了-3股苹果，均价202.68美元"
        )
        self.assertFalse(pending["requires_confirmation"])
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(self.service.load_portfolio()["positions"], [])

    def test_unindexed_revision_is_rejected_for_multi_record_card(self) -> None:
        pending = self.create_pending(
            "银河买了500股比亚迪，均价102.742元；IBKR买了3股苹果，均价202.68美元"
        )
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_revise(ReviseRequest(
                pending_id=pending["pending_id"],
                message="数量5",
            )))
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("第几条", context.exception.detail)

    def test_indexed_revision_creates_new_multi_record_card(self) -> None:
        pending = self.create_pending(
            "银河买了500股比亚迪，均价102.742元；IBKR买了3股苹果，均价202.68美元"
        )
        created: list[str] = [pending["pending_id"]]
        try:
            revised = asyncio.run(ai_revise(ReviseRequest(
                pending_id=pending["pending_id"],
                message="第2条数量5",
            )))
            created.append(revised["pending_id"])
            self.assertEqual(revised["changes"][0]["quantity"], 500)
            self.assertEqual(revised["changes"][1]["quantity"], 5)
            self.assertTrue(revised["requires_confirmation"])
        finally:
            for pending_id in created:
                pending_path(pending_id).unlink(missing_ok=True)

    def test_multi_confirm_is_idempotent(self) -> None:
        pending = self.create_pending(
            "银河买了500股比亚迪，均价102.742元；IBKR买了3股苹果，均价202.68美元"
        )
        first = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        second = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertEqual(first["operation_id"], second["operation_id"])
        portfolio = self.service.load_portfolio()
        identities = {
            (item["account"], item["code"], item["currency"]): item["quantity"]
            for item in portfolio["positions"]
        }
        self.assertEqual(identities[("银河", "002594", "CNY")], 500)
        self.assertEqual(identities[("IBKR", "AAPL", "USD")], 3)

    def test_correction_reverts_wrong_row_edit_before_applying_new_edit(self) -> None:
        pending = self.create_pending(
            "银河买了500股比亚迪，均价102.742元；IBKR买了3股苹果，均价202.68美元"
        )
        created: list[str] = [pending["pending_id"]]
        try:
            wrong = asyncio.run(ai_revise(ReviseRequest(
                pending_id=pending["pending_id"],
                message="第2条数量5",
            )))
            created.append(wrong["pending_id"])
            self.assertEqual(wrong["changes"][1]["quantity"], 5)

            corrected = asyncio.run(ai_revise(ReviseRequest(
                pending_id=wrong["pending_id"],
                message="不对，第二条成本价改成201",
            )))
            created.append(corrected["pending_id"])
            self.assertEqual(corrected["changes"][0]["quantity"], 500)
            self.assertEqual(corrected["changes"][0]["cost_price"], 102.742)
            self.assertEqual(corrected["changes"][1]["quantity"], 3)
            self.assertEqual(corrected["changes"][1]["cost_price"], 201)
            self.assertTrue(corrected["requires_confirmation"])
            self.assertTrue(any(
                item["kind"] == "reverted"
                and item["change_index"] == 1
                and item["field"] == "quantity"
                and item["before"] == 5
                and item["after"] == 3
                for item in corrected["revision_diffs"]
            ))
            self.assertTrue(any(
                item["kind"] == "applied"
                and item["change_index"] == 1
                and item["field"] == "cost_price"
                and item["before"] == 202.68
                and item["after"] == 201
                for item in corrected["revision_diffs"]
            ))
        finally:
            for pending_id in created:
                pending_path(pending_id).unlink(missing_ok=True)

    def test_multi_sell_checks_cumulative_available_quantity(self) -> None:
        self.service.safe_add_positions([{
            "account": "IBKR", "name": "Apple/苹果", "code": "AAPL",
            "currency": "USD", "asset_type": "stock", "quantity": 10, "cost_price": 100,
        }], summary="seed")
        parsed = parse_bookkeeping_message(
            "IBKR卖出6股苹果；IBKR卖出5股苹果"
        )
        enriched = enrich_sell_availability(parsed)
        self.assertEqual(len(enriched["changes"]), 2)
        self.assertIn("available_quantity", enriched["missing_fields"])
        self.assertTrue(any("第2条卖出数量5" in warning for warning in enriched["warnings"]))


if __name__ == "__main__":
    unittest.main()
