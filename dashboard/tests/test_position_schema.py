from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = DASHBOARD_ROOT / "backend" / "services" / "portfolio_write_service.py"
spec = importlib.util.spec_from_file_location("portfolio_write_service_under_test", SERVICE_PATH)
assert spec and spec.loader
service_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_module)
PortfolioWriteService = service_module.PortfolioWriteService


class PositionSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    @staticmethod
    def position(account: str, quantity: float = 1, cost_price: float = 100) -> dict:
        return {
            "account": account,
            "name": "Apple",
            "code": "AAPL",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": quantity,
            "cost_price": cost_price,
            "source": "manual",
        }

    def test_legacy_aliases_are_read_but_not_stored(self) -> None:
        self.service.safe_add_positions(
            [{
                "group": "IBKR",
                "name": "Apple",
                "symbol": "NASDAQ:AAPL",
                "market": "us_stock",
                "exchange": "NASDAQ",
                "canonical_symbol": "AAPL.US",
                "quantity": 3,
                "cost_price": 202.68,
            }],
            summary="legacy alias test",
        )

        raw = json.loads(Path("data/portfolio.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["positions"]), 1)
        stored = raw["positions"][0]
        self.assertEqual(stored["account"], "IBKR")
        self.assertEqual(stored["code"], "AAPL")
        self.assertEqual(stored["currency"], "USD")
        for forbidden in ("group", "symbol", "market", "exchange", "canonical_symbol"):
            self.assertNotIn(forbidden, stored)


    def test_listed_fund_types_are_normalized_to_stock(self) -> None:
        self.service.safe_add_positions([{
            "account": "银河",
            "name": "海外科技LOF",
            "code": "501312",
            "currency": "CNY",
            "asset_type": "fund",
            "quantity": 100,
            "cost_price": 90,
        }], summary="legacy listed fund")
        position = self.service.load_portfolio()["positions"][0]
        self.assertEqual(position["asset_type"], "stock")

    def test_same_code_in_different_accounts_is_isolated(self) -> None:
        self.service.safe_add_positions(
            [self.position("IBKR"), self.position("长桥", quantity=2, cost_price=110)],
            summary="two accounts",
        )

        portfolio = self.service.load_portfolio()
        self.assertEqual(len(portfolio["positions"]), 2)

        self.service.safe_update_position(
            account="IBKR",
            code="AAPL",
            currency="USD",
            updates={"quantity": 5, "cost_price": 120},
        )
        updated = self.service.load_portfolio()["positions"]
        ibkr = next(p for p in updated if p["account"] == "IBKR")
        longbridge = next(p for p in updated if p["account"] == "长桥")
        self.assertEqual(ibkr["quantity"], 5)
        self.assertEqual(ibkr["cost_price"], 120)
        self.assertEqual(longbridge["quantity"], 2)
        self.assertEqual(longbridge["cost_price"], 110)

        self.service.safe_remove_position("IBKR", "AAPL", "USD")
        remaining = self.service.load_portfolio()["positions"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["account"], "长桥")

    def test_same_identity_merges_but_other_account_does_not(self) -> None:
        self.service.safe_add_positions([self.position("IBKR", 1, 100)], summary="first")
        self.service.safe_add_positions([self.position("IBKR", 2, 200)], summary="second")
        self.service.safe_add_positions([self.position("银河", 4, 300)], summary="other account")

        positions = self.service.load_portfolio()["positions"]
        self.assertEqual(len(positions), 2)
        ibkr = next(p for p in positions if p["account"] == "IBKR")
        self.assertEqual(ibkr["quantity"], 3)
        self.assertAlmostEqual(ibkr["cost_price"], 500 / 3)

    def test_sell_cannot_create_negative_position(self) -> None:
        self.service.safe_add_positions([self.position("IBKR", 3, 100)], summary="seed")
        with self.assertRaisesRegex(ValueError, "暂不支持卖空"):
            self.service.safe_add_positions([{
                **self.position("IBKR", 5, 120),
                "action_type": "sell",
            }], summary="oversell")
        positions = self.service.load_portfolio()["positions"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["quantity"], 3)

    def test_rollback_latest_marks_pending_and_prevents_repeat(self) -> None:
        pending_id = "pending-rollback-test"
        pending_dir = Path("data/pending_actions")
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_path = pending_dir / f"{pending_id}.json"
        pending_path.write_text(
            json.dumps({
                "pending_id": pending_id,
                "status": "pending",
                "requires_confirmation": True,
            }),
            encoding="utf-8",
        )

        result = self.service.safe_add_positions(
            [self.position("IBKR", 2, 200)],
            summary="latest write",
            pending_action={
                "pending_id": pending_id,
                "changes": [self.position("IBKR", 2, 200)],
            },
        )
        operation_id = result["operation_id"]
        rollback = self.service.rollback_latest_operation()
        self.assertEqual(rollback["rolled_back_operation_id"], operation_id)
        self.assertEqual(self.service.load_portfolio()["positions"], [])

        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        self.assertEqual(pending["status"], "rolled_back")
        self.assertFalse(pending["requires_confirmation"])

        summaries = self.service.list_operations()
        original = next(item for item in summaries if item["operation_id"] == operation_id)
        rollback_summary = next(item for item in summaries if item["type"] == "rollback")
        self.assertFalse(original["can_rollback"])
        self.assertEqual(original["status"], "rolled_back")
        self.assertFalse(rollback_summary["can_rollback"])

        with self.assertRaisesRegex(ValueError, "已经撤回"):
            self.service.rollback_operation(operation_id)
        with self.assertRaisesRegex(ValueError, "回滚操作本身"):
            self.service.rollback_operation(rollback["rollback_operation_id"])



    def test_backups_are_unique_within_same_second(self) -> None:
        first = self.service.backup_portfolio()
        second = self.service.backup_portfolio()
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first.replace("dashboard/", "")).exists())
        self.assertTrue(Path(second.replace("dashboard/", "")).exists())

    def test_account_cash_set_deposit_withdraw_and_rollback(self) -> None:
        set_result = self.service.safe_set_cash_account("IBKR", "USD", 1000)
        portfolio = self.service.load_portfolio()
        self.assertEqual(portfolio["cash_accounts"][0]["amount"], 1000)

        deposit = self.service.safe_add_positions([{
            "action_type": "deposit",
            "account": "IBKR",
            "currency": "USD",
            "amount": 250,
        }], summary="deposit")
        self.assertEqual(self.service.load_portfolio()["cash_accounts"][0]["amount"], 1250)

        withdraw = self.service.safe_add_positions([{
            "action_type": "withdraw",
            "account": "IBKR",
            "currency": "USD",
            "amount": 200,
        }], summary="withdraw")
        self.assertEqual(self.service.load_portfolio()["cash_accounts"][0]["amount"], 1050)

        self.service.rollback_operation(withdraw["operation_id"])
        self.assertEqual(self.service.load_portfolio()["cash_accounts"][0]["amount"], 1250)
        self.service.rollback_operation(deposit["operation_id"])
        self.assertEqual(self.service.load_portfolio()["cash_accounts"][0]["amount"], 1000)
        self.service.rollback_operation(set_result["operation_id"])
        self.assertEqual(self.service.load_portfolio()["cash_accounts"], [])


    def test_same_account_can_hold_multiple_cash_currencies(self) -> None:
        usd = self.service.safe_set_cash_account("IBKR", "USD", 1000)
        hkd = self.service.safe_set_cash_account("IBKR", "HKD", 2000)

        cash_accounts = self.service.load_portfolio()["cash_accounts"]
        by_identity = {
            (item["account"], item["currency"]): item["amount"]
            for item in cash_accounts
        }
        self.assertEqual(by_identity[("IBKR", "USD")], 1000)
        self.assertEqual(by_identity[("IBKR", "HKD")], 2000)
        self.assertEqual(len(by_identity), 2)

        self.service.rollback_operation(hkd["operation_id"])
        cash_accounts = self.service.load_portfolio()["cash_accounts"]
        self.assertEqual(len(cash_accounts), 1)
        self.assertEqual(cash_accounts[0]["currency"], "USD")

        self.service.rollback_operation(usd["operation_id"])
        self.assertEqual(self.service.load_portfolio()["cash_accounts"], [])

    def test_cash_withdraw_cannot_go_negative(self) -> None:
        self.service.safe_set_cash_account("银河", "CNY", 100)
        with self.assertRaisesRegex(ValueError, "现金不足"):
            self.service.safe_add_positions([{
                "action_type": "withdraw",
                "account": "银河",
                "currency": "CNY",
                "amount": 101,
            }], summary="overspend")

    def test_selective_rollback_preserves_later_unrelated_position(self) -> None:
        first = self.service.safe_add_positions(
            [self.position("IBKR", 2, 100)],
            summary="first apple",
        )
        second_position = {
            "account": "长桥",
            "name": "NVIDIA",
            "code": "NVDA",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": 4,
            "cost_price": 200,
            "source": "manual",
        }
        self.service.safe_add_positions([second_position], summary="later nvidia")

        self.service.rollback_operation(first["operation_id"])
        positions = self.service.load_portfolio()["positions"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["account"], "长桥")
        self.assertEqual(positions[0]["code"], "NVDA")
        self.assertEqual(positions[0]["quantity"], 4)

    def test_selective_rollback_removes_only_earlier_same_identity_delta(self) -> None:
        first = self.service.safe_add_positions(
            [self.position("IBKR", 2, 100)],
            summary="first apple",
        )
        self.service.safe_add_positions(
            [self.position("IBKR", 3, 200)],
            summary="later apple",
        )

        self.service.rollback_operation(first["operation_id"])
        positions = self.service.load_portfolio()["positions"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["quantity"], 3)
        self.assertAlmostEqual(positions[0]["cost_price"], 200)


if __name__ == "__main__":
    unittest.main()
