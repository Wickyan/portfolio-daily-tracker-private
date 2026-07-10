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


if __name__ == "__main__":
    unittest.main()
