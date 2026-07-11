from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from backend.api.portfolio_ai import (
    ConfirmRequest,
    ItemRequest,
    ItemReviseRequest,
    ai_confirm,
    ai_confirm_item,
    ai_preview,
    ai_revise_item,
    ai_rollback_item,
    get_pending,
    PreviewRequest,
)
from backend.services.portfolio_write_service import PortfolioWriteService


class BatchItemWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def preview(self, text: str) -> dict:
        return asyncio.run(ai_preview(PreviewRequest(message=text, input_type="text")))

    def position_map(self) -> dict:
        return {
            (item["account"], item["code"], item["currency"]): item
            for item in self.service.load_portfolio()["positions"]
        }

    def test_preview_exposes_independent_items(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR现在持有3股苹果，成本200美元"
        )
        self.assertEqual(len(card["items"]), 2)
        self.assertEqual([item["action_type"] for item in card["items"]], ["add_or_update", "set_position"])
        self.assertEqual([item["status"] for item in card["items"]], ["pending", "pending"])
        self.assertTrue(all(item["requires_confirmation"] for item in card["items"]))
        self.assertTrue(card["can_confirm_all"])
        self.assertEqual(card["pending_item_count"], 2)

    def test_confirm_one_item_only_writes_that_item(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元"
        )
        first, second = card["items"]
        result = asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=first["item_id"],
        )))
        updated = result["pending"]
        self.assertEqual(updated["items"][0]["status"], "confirmed")
        self.assertEqual(updated["items"][1]["status"], "pending")
        self.assertEqual(updated["status"], "partially_confirmed")
        self.assertEqual(updated["confirmed_item_count"], 1)
        self.assertEqual(updated["pending_item_count"], 1)
        positions = self.position_map()
        self.assertIn(("银河", "002594", "CNY"), positions)
        self.assertNotIn(("IBKR", "AAPL", "USD"), positions)
        self.assertIsNotNone(result["operation_id"])
        self.assertEqual(result["item_id"], first["item_id"])
        self.assertEqual(updated["items"][1]["item_id"], second["item_id"])

    def test_confirm_all_after_one_item_confirms_only_remaining(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元"
        )
        first = card["items"][0]
        first_result = asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=first["item_id"],
        )))
        first_operation = first_result["operation_id"]

        result = asyncio.run(ai_confirm(ConfirmRequest(pending_id=card["pending_id"])))
        self.assertEqual(len(result["operation_ids"]), 1)
        self.assertNotEqual(result["operation_ids"][0], first_operation)
        self.assertEqual(result["pending"]["status"], "confirmed")
        self.assertEqual(result["pending"]["confirmed_item_count"], 2)
        self.assertEqual(len(self.position_map()), 2)

    def test_confirm_all_creates_independent_child_operations(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元"
        )
        result = asyncio.run(ai_confirm(ConfirmRequest(pending_id=card["pending_id"])))
        self.assertEqual(len(result["operation_ids"]), 2)
        self.assertEqual(len(result["pending"]["operation_ids"]), 2)
        self.assertEqual(result["pending"]["status"], "confirmed")
        self.assertTrue(all(item["operation_id"] for item in result["pending"]["items"]))

    def test_each_confirmed_item_can_rollback_without_touching_sibling(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元"
        )
        confirmed = asyncio.run(ai_confirm(ConfirmRequest(pending_id=card["pending_id"])))
        first, second = confirmed["pending"]["items"]

        rolled = asyncio.run(ai_rollback_item(ItemRequest(
            pending_id=card["pending_id"], item_id=first["item_id"],
        )))
        self.assertEqual(rolled["pending"]["items"][0]["status"], "rolled_back")
        self.assertEqual(rolled["pending"]["items"][1]["status"], "confirmed")
        self.assertEqual(rolled["pending"]["status"], "partially_rolled_back")
        positions = self.position_map()
        self.assertNotIn(("银河", "002594", "CNY"), positions)
        self.assertIn(("IBKR", "AAPL", "USD"), positions)

        rolled_again = asyncio.run(ai_rollback_item(ItemRequest(
            pending_id=card["pending_id"], item_id=first["item_id"],
        )))
        self.assertTrue(rolled_again["already_rolled_back"])
        self.assertEqual(rolled_again["pending"]["items"][1]["item_id"], second["item_id"])

    def test_item_confirmation_is_idempotent(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        item = card["items"][0]
        first = asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=item["item_id"],
        )))
        second = asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=item["item_id"],
        )))
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertTrue(second["already_confirmed"])
        self.assertEqual(self.position_map()[("银河", "002594", "CNY")]["quantity"], 10)

    def test_revise_one_item_does_not_change_siblings(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元"
        )
        second = card["items"][1]
        revised = asyncio.run(ai_revise_item(ItemReviseRequest(
            pending_id=card["pending_id"], item_id=second["item_id"], message="数量5",
        )))
        self.assertEqual(revised["items"][0]["changes"][0]["quantity"], 10)
        self.assertEqual(revised["items"][0]["changes"][0]["cost_price"], 100)
        self.assertEqual(revised["items"][1]["changes"][0]["quantity"], 5)
        self.assertEqual(revised["items"][1]["changes"][0]["cost_price"], 200)
        self.assertEqual(revised["items"][1]["version"], 2)

    def test_item_correction_reverts_wrong_change_then_applies_new_one(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元"
        )
        second = card["items"][1]
        wrong = asyncio.run(ai_revise_item(ItemReviseRequest(
            pending_id=card["pending_id"], item_id=second["item_id"], message="数量5",
        )))
        corrected = asyncio.run(ai_revise_item(ItemReviseRequest(
            pending_id=card["pending_id"], item_id=second["item_id"], message="不对，成本价改成201",
        )))
        item = corrected["items"][1]
        self.assertEqual(item["changes"][0]["quantity"], 2)
        self.assertEqual(item["changes"][0]["cost_price"], 201)
        self.assertTrue(any(diff["kind"] == "reverted" and diff["field"] == "quantity" for diff in item["revision_diffs"]))
        self.assertTrue(any(diff["kind"] == "applied" and diff["field"] == "cost_price" for diff in item["revision_diffs"]))
        self.assertEqual(wrong["items"][1]["version"] + 1, item["version"])

    def test_item_can_switch_from_buy_to_set_position(self) -> None:
        self.service.safe_add_positions([{
            "action_type": "add_or_update", "account": "银河", "name": "比亚迪",
            "code": "002594", "currency": "CNY", "asset_type": "stock",
            "quantity": 100, "cost_price": 90,
        }], summary="seed")
        card = self.preview("银河买入500股比亚迪，均价102.742元")
        item = card["items"][0]
        revised = asyncio.run(ai_revise_item(ItemReviseRequest(
            pending_id=card["pending_id"], item_id=item["item_id"], message="不是买入，是更新持仓",
        )))
        self.assertEqual(revised["items"][0]["action_type"], "set_position")
        confirmed = asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=item["item_id"],
        )))
        position = self.position_map()[("银河", "002594", "CNY")]
        self.assertEqual(position["quantity"], 500)
        self.assertAlmostEqual(position["cost_price"], 102.742)
        self.assertEqual(confirmed["pending"]["items"][0]["status"], "confirmed")

    def test_invalid_item_does_not_block_confirming_valid_sibling(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入-2股苹果，均价200美元"
        )
        self.assertFalse(card["can_confirm_all"])
        self.assertTrue(card["items"][0]["requires_confirmation"])
        self.assertFalse(card["items"][1]["requires_confirmation"])
        result = asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=card["items"][0]["item_id"],
        )))
        self.assertEqual(result["pending"]["items"][0]["status"], "confirmed")
        self.assertEqual(result["pending"]["items"][1]["status"], "pending")
        self.assertIn(("银河", "002594", "CNY"), self.position_map())

    def test_sequential_cash_items_refresh_child_confirmability(self) -> None:
        card = self.preview("银河增加100元；银河提现50元")
        first, second = card["items"]
        self.assertTrue(card["can_confirm_all"])
        self.assertTrue(first["requires_confirmation"])
        self.assertFalse(second["requires_confirmation"])
        first_result = asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=first["item_id"],
        )))
        refreshed_second = first_result["pending"]["items"][1]
        self.assertTrue(refreshed_second["requires_confirmation"])
        asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=second["item_id"],
        )))
        cash = self.service.load_portfolio()["cash_accounts"]
        self.assertEqual(cash[0]["amount"], 50)

    def test_get_pending_reconciles_item_statuses(self) -> None:
        card = self.preview(
            "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元"
        )
        first = card["items"][0]
        asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=card["pending_id"], item_id=first["item_id"],
        )))
        public = asyncio.run(get_pending(card["pending_id"]))
        self.assertEqual(public["items"][0]["status"], "confirmed")
        self.assertEqual(public["items"][1]["status"], "pending")
        self.assertEqual(public["status"], "partially_confirmed")


if __name__ == "__main__":
    unittest.main()
