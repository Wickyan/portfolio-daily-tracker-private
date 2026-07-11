from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from backend.services.portfolio_write_service import PortfolioWriteService


class AtomicItemOperationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def buy(self, account: str, code: str, name: str, currency: str, quantity: float, cost: float) -> dict:
        return {
            "action_type": "add_or_update",
            "account": account,
            "name": name,
            "code": code,
            "currency": currency,
            "asset_type": "stock",
            "quantity": quantity,
            "cost_price": cost,
        }

    def test_atomic_batch_creates_one_operation_per_child(self) -> None:
        result = self.service.safe_add_items_atomic(
            [
                {"item_id": "item-a", "changes": [self.buy("银河", "002594", "比亚迪", "CNY", 10, 100)]},
                {"item_id": "item-b", "changes": [self.buy("IBKR", "AAPL", "Apple", "USD", 2, 200)]},
            ],
            pending_id="pending-batch",
            summary="batch",
        )
        self.assertEqual(len(result["item_operations"]), 2)
        self.assertEqual({item["item_id"] for item in result["item_operations"]}, {"item-a", "item-b"})
        portfolio = self.service.load_portfolio()
        self.assertEqual(len(portfolio["positions"]), 2)
        self.assertIsNotNone(self.service.find_confirm_operation_by_pending_item("pending-batch", "item-a"))
        self.assertIsNotNone(self.service.find_confirm_operation_by_pending_item("pending-batch", "item-b"))
        self.assertIsNone(self.service.find_confirm_operation_by_pending_id("pending-batch"))

    def test_each_child_can_be_rolled_back_independently(self) -> None:
        result = self.service.safe_add_items_atomic(
            [
                {"item_id": "item-a", "changes": [self.buy("银河", "002594", "比亚迪", "CNY", 10, 100)]},
                {"item_id": "item-b", "changes": [self.buy("IBKR", "AAPL", "Apple", "USD", 2, 200)]},
            ],
            pending_id="pending-batch",
            summary="batch",
        )
        op_by_item = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(op_by_item["item-a"])
        portfolio = self.service.load_portfolio()
        self.assertEqual([(p["account"], p["code"]) for p in portfolio["positions"]], [("IBKR", "AAPL")])
        self.service.rollback_operation(op_by_item["item-b"])
        self.assertEqual(self.service.load_portfolio()["positions"], [])

    def test_two_children_on_same_identity_still_rollback_selectively(self) -> None:
        result = self.service.safe_add_items_atomic(
            [
                {"item_id": "item-a", "changes": [self.buy("银河", "002594", "比亚迪", "CNY", 10, 100)]},
                {"item_id": "item-b", "changes": [self.buy("银河", "002594", "比亚迪", "CNY", 5, 110)]},
            ],
            pending_id="pending-batch",
            summary="batch",
        )
        op_by_item = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(op_by_item["item-a"])
        position = self.service.load_portfolio()["positions"][0]
        self.assertEqual(position["quantity"], 5)
        self.assertAlmostEqual(position["cost_price"], 110)
        self.service.rollback_operation(op_by_item["item-b"])
        self.assertEqual(self.service.load_portfolio()["positions"], [])

    def test_invalid_child_writes_nothing_and_creates_no_operations(self) -> None:
        with self.assertRaises(ValueError):
            self.service.safe_add_items_atomic(
                [
                    {"item_id": "item-a", "changes": [self.buy("银河", "002594", "比亚迪", "CNY", 10, 100)]},
                    {"item_id": "item-b", "changes": [self.buy("IBKR", "AAPL", "Apple", "USD", -2, 200)]},
                ],
                pending_id="pending-batch",
                summary="batch",
            )
        self.assertEqual(self.service.load_portfolio()["positions"], [])
        self.assertEqual(self.service.find_confirm_operations_by_pending_id("pending-batch"), [])


if __name__ == "__main__":
    unittest.main()
