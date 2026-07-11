from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from backend.api.portfolio_ai import make_pending, parse_bookkeeping_message, public_pending
from backend.services.portfolio_write_service import PortfolioWriteService


class PositionActionSemanticParseTest(unittest.TestCase):
    def test_explicit_buy_is_incremental(self) -> None:
        parsed = parse_bookkeeping_message("银河买了500股比亚迪，均价102.742元")
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(parsed["changes"][0]["action_type"], "add_or_update")

    def test_current_holding_phrases_are_set_position(self) -> None:
        samples = [
            "银河账户里有500股比亚迪，成本价102.742元",
            "银河现在持有500股比亚迪，成本价102.742元",
            "银河目前持有500股比亚迪，成本价102.742元",
            "银河的比亚迪持仓为500股，成本价102.742元",
            "银河更新持仓：比亚迪500股，成本价102.742元",
        ]
        for text in samples:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], "set_position")
                self.assertEqual(parsed["changes"][0]["action_type"], "set_position")
                self.assertEqual(parsed["changes"][0]["quantity"], 500)

    def test_sell_remains_sell(self) -> None:
        parsed = parse_bookkeeping_message("银河卖出100股比亚迪")
        self.assertEqual(parsed["action_type"], "sell")
        self.assertEqual(parsed["changes"][0]["action_type"], "sell")

    def test_mixed_buy_and_set_position_are_kept_in_one_position_batch(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河买入100股比亚迪，均价100元；IBKR现在持有3股苹果，成本200美元"
        )
        self.assertEqual(parsed["action_type"], "multi_position")
        self.assertEqual(
            [change["action_type"] for change in parsed["changes"]],
            ["add_or_update", "set_position"],
        )
        self.assertEqual(parsed["missing_fields"], [])

    def test_leading_current_holding_phrase_applies_to_following_shorthand_rows(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河账户里有500股比亚迪，成本价102.742元；"
            "12700份纳指大成，成本价1.243元；"
            "2052份海外科技，成本价2.436元"
        )
        self.assertEqual(parsed["action_type"], "set_position")
        self.assertEqual(
            [change["action_type"] for change in parsed["changes"]],
            ["set_position", "set_position", "set_position"],
        )
        self.assertEqual([change["account"] for change in parsed["changes"]], ["银河", "银河", "银河"])
        self.assertEqual(parsed["changes"][1]["name"], "纳指大成")
        self.assertEqual(parsed["changes"][2]["name"], "海外科技")

    def test_holdings_and_cash_can_share_one_parent_with_independent_items(self) -> None:
        text = (
            "银河账户里有500股比亚迪，成本价102.742元；"
            "12700份纳指大成，成本价1.243元；"
            "2052份海外科技，成本价2.436元；"
            "另外人民币现金余额是20006.74元"
        )
        parsed = parse_bookkeeping_message(text)
        self.assertEqual(parsed["action_type"], "multi_records")
        self.assertEqual(
            [spec["action_type"] for spec in parsed["item_specs"]],
            ["set_position", "set_position", "set_position", "set_cash"],
        )
        card = public_pending(make_pending(parsed, "text", text))
        self.assertEqual(len(card["items"]), 4)
        self.assertEqual(
            [item["action_type"] for item in card["items"]],
            ["set_position", "set_position", "set_position", "set_cash"],
        )
        self.assertTrue(card["items"][0]["requires_confirmation"])
        self.assertFalse(card["items"][1]["requires_confirmation"])
        self.assertFalse(card["items"][2]["requires_confirmation"])
        self.assertTrue(card["items"][3]["requires_confirmation"])

    def test_mixed_canonical_copy_preserves_child_grouping(self) -> None:
        parsed = parse_bookkeeping_message(
            "记账1：账户=银河；操作=买入；标的=比亚迪；代码=002594；币种=CNY；数量=10；成本价=100\n"
            "记账2：账户=IBKR；操作=更新持仓；标的=Apple；代码=AAPL；币种=USD；数量=3；成本价=200\n"
            "记账3：账户=银河；操作=设置现金余额；币种=CNY；金额=20000"
        )
        self.assertEqual(parsed["action_type"], "multi_records")
        self.assertEqual(
            [spec["action_type"] for spec in parsed["item_specs"]],
            ["add_or_update", "set_position", "set_cash"],
        )
        card = public_pending(make_pending(parsed, "text", "copied"))
        self.assertEqual([item["action_type"] for item in card["items"]], ["add_or_update", "set_position", "set_cash"])

    def test_canonical_copy_supports_set_position(self) -> None:
        parsed = parse_bookkeeping_message(
            "记账：账户=银河；操作=更新持仓；标的=比亚迪；代码=002594；币种=CNY；数量=500；成本价=102.742"
        )
        self.assertEqual(parsed["action_type"], "set_position")
        self.assertEqual(parsed["changes"][0]["action_type"], "set_position")
        self.assertEqual(parsed["missing_fields"], [])


class PositionActionWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def seed(self) -> None:
        self.service.safe_add_positions([{
            "action_type": "add_or_update",
            "account": "银河",
            "name": "比亚迪",
            "code": "002594",
            "currency": "CNY",
            "asset_type": "stock",
            "quantity": 100,
            "cost_price": 100,
        }], summary="seed")

    def get_position(self) -> dict:
        return self.service.load_portfolio()["positions"][0]

    def test_buy_adds_quantity_and_recalculates_weighted_cost(self) -> None:
        self.seed()
        self.service.safe_add_positions([{
            "action_type": "add_or_update",
            "account": "银河",
            "name": "比亚迪",
            "code": "002594",
            "currency": "CNY",
            "asset_type": "stock",
            "quantity": 50,
            "cost_price": 110,
        }], summary="buy")
        position = self.get_position()
        self.assertEqual(position["quantity"], 150)
        self.assertAlmostEqual(position["cost_price"], (100 * 100 + 50 * 110) / 150)

    def test_set_position_replaces_quantity_and_cost_instead_of_adding(self) -> None:
        self.seed()
        self.service.safe_add_positions([{
            "action_type": "set_position",
            "account": "银河",
            "name": "比亚迪",
            "code": "002594",
            "currency": "CNY",
            "asset_type": "stock",
            "quantity": 500,
            "cost_price": 102.742,
        }], summary="set holding")
        position = self.get_position()
        self.assertEqual(position["quantity"], 500)
        self.assertEqual(position["available_qty"], 500)
        self.assertAlmostEqual(position["cost_price"], 102.742)
        self.assertAlmostEqual(position["total_cost"], 500 * 102.742)

    def test_set_position_can_create_a_new_position(self) -> None:
        self.service.safe_add_positions([{
            "action_type": "set_position",
            "account": "银河",
            "name": "比亚迪",
            "code": "002594",
            "currency": "CNY",
            "asset_type": "stock",
            "quantity": 500,
            "cost_price": 102.742,
        }], summary="set holding")
        position = self.get_position()
        self.assertEqual(position["quantity"], 500)
        self.assertAlmostEqual(position["cost_price"], 102.742)

    def test_set_position_rollback_restores_previous_state(self) -> None:
        self.seed()
        result = self.service.safe_add_positions([{
            "action_type": "set_position",
            "account": "银河",
            "name": "比亚迪",
            "code": "002594",
            "currency": "CNY",
            "asset_type": "stock",
            "quantity": 500,
            "cost_price": 102.742,
        }], summary="set holding")
        self.service.rollback_operation(result["operation_id"])
        position = self.get_position()
        self.assertEqual(position["quantity"], 100)
        self.assertAlmostEqual(position["cost_price"], 100)


if __name__ == "__main__":
    unittest.main()
