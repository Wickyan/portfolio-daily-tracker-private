from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from backend.services.portfolio_write_service import PortfolioWriteService
from providers.portfolio.manual import ManualPortfolioProvider


class FakeMarketProvider:
    async def get_quotes(self, symbols, market):
        return [
            SimpleNamespace(
                stock=SimpleNamespace(symbol=symbol),
                price=999.0 if symbol == "AAPL" else 123.0,
            )
            for symbol in symbols
        ]

    async def get_quote(self, symbol, market):
        return SimpleNamespace(stock=SimpleNamespace(symbol=symbol), price=999.0)


class ManualPortfolioRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.portfolio_file = Path("portfolio.json")
        self.write_service = PortfolioWriteService(self.portfolio_file)
        self.write_service.safe_add_positions([
            {
                "account": "IBKR",
                "name": "Apple",
                "code": "AAPL",
                "currency": "USD",
                "asset_type": "stock",
                "quantity": 1,
                "cost_price": 100,
            }
        ], summary="seed position")

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def test_refresh_preserves_cash_and_positions_added_after_provider_load(self) -> None:
        provider = ManualPortfolioProvider(
            data_file=str(self.portfolio_file),
            market_provider=FakeMarketProvider(),
        )

        # These writes happen after the provider loaded its in-memory snapshot.
        self.write_service.safe_add_positions([
            {"action_type": "deposit", "account": "长桥", "currency": "USD", "amount": 3000},
            {"action_type": "deposit", "account": "长桥", "currency": "HKD", "amount": 30000},
        ], summary="cash after provider load")
        self.write_service.safe_add_positions([
            {
                "account": "银河",
                "name": "比亚迪",
                "code": "002594",
                "currency": "CNY",
                "asset_type": "stock",
                "quantity": 10,
                "cost_price": 90,
            }
        ], summary="position after provider load")

        asyncio.run(provider.refresh())
        portfolio = self.write_service.load_portfolio()

        cash = {
            (item["account"], item["currency"]): item["amount"]
            for item in portfolio["cash_accounts"]
        }
        self.assertEqual(cash[("长桥", "USD")], 3000)
        self.assertEqual(cash[("长桥", "HKD")], 30000)

        positions = {
            (item["account"], item["code"], item["currency"]): item
            for item in portfolio["positions"]
        }
        self.assertIn(("银河", "002594", "CNY"), positions)
        self.assertEqual(positions[("IBKR", "AAPL", "USD")]["current_price"], 999.0)

    def test_legacy_full_save_does_not_drop_cash_accounts(self) -> None:
        provider = ManualPortfolioProvider(
            data_file=str(self.portfolio_file),
            market_provider=FakeMarketProvider(),
        )
        self.write_service.safe_add_positions([
            {"action_type": "deposit", "account": "银河", "currency": "USD", "amount": 22000},
            {"action_type": "deposit", "account": "银河", "currency": "HKD", "amount": 22000},
        ], summary="two cash currencies")

        provider._save()
        cash = {
            (item["account"], item["currency"]): item["amount"]
            for item in self.write_service.load_portfolio()["cash_accounts"]
        }
        self.assertEqual(cash[("银河", "USD")], 22000)
        self.assertEqual(cash[("银河", "HKD")], 22000)


if __name__ == "__main__":
    unittest.main()
