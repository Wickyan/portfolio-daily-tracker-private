from __future__ import annotations

import unittest

from backend.services.portfolio_valuation import calculate_portfolio_valuation


class PortfolioValuationTest(unittest.TestCase):
    def test_mixed_currency_positions_and_cash_are_converted_to_cny(self) -> None:
        portfolio = {
            "positions": [
                {
                    "account": "IBKR",
                    "code": "AAPL",
                    "name": "Apple",
                    "currency": "USD",
                    "asset_type": "stock",
                    "quantity": 10,
                    "cost_price": 100,
                    "current_price": 120,
                },
                {
                    "account": "银河",
                    "code": "002594",
                    "name": "比亚迪",
                    "currency": "CNY",
                    "asset_type": "stock",
                    "quantity": 100,
                    "cost_price": 80,
                    "current_price": 90,
                },
                {
                    "account": "长桥",
                    "code": "0700",
                    "name": "腾讯",
                    "currency": "HKD",
                    "asset_type": "stock",
                    "quantity": 20,
                    "cost_price": 300,
                    "current_price": 320,
                },
            ],
            "cash": 0,
            "cash_accounts": [
                {"account": "IBKR", "currency": "USD", "amount": 500},
                {"account": "银河", "currency": "CNY", "amount": 10000},
                {"account": "长桥", "currency": "HKD", "amount": 1000},
            ],
        }
        rates = {"CNY": 1, "USD": 7, "HKD": 0.9}
        result = calculate_portfolio_valuation(portfolio, rates)

        expected_market = 10 * 120 * 7 + 100 * 90 + 20 * 320 * 0.9
        expected_cash = 500 * 7 + 10000 + 1000 * 0.9
        expected_profit = (10 * (120 - 100) * 7) + (100 * (90 - 80)) + (20 * (320 - 300) * 0.9)
        self.assertAlmostEqual(result["total_market_value"], expected_market)
        self.assertAlmostEqual(result["cash"], expected_cash)
        self.assertAlmostEqual(result["total_assets"], expected_market + expected_cash)
        self.assertAlmostEqual(result["total_profit"], expected_profit)
        self.assertEqual(len(result["cash_accounts"]), 3)
        self.assertAlmostEqual(result["cash_accounts"][0]["amount_cny"], 3500)
        total_weight = sum(item["asset_weight_pct"] for item in result["positions"])
        total_weight += sum(item["asset_weight_pct"] for item in result["cash_accounts"])
        self.assertAlmostEqual(total_weight, 100.0)
        for item in result["positions"]:
            expected_weight = item["market_value_cny"] / result["total_assets"] * 100
            self.assertAlmostEqual(item["asset_weight_pct"], expected_weight)


    def test_same_account_multiple_cash_currencies_use_separate_live_rates(self) -> None:
        portfolio = {
            "positions": [],
            "cash": 0,
            "cash_accounts": [
                {"account": "IBKR", "currency": "USD", "amount": 1000},
                {"account": "IBKR", "currency": "HKD", "amount": 2000},
            ],
        }
        rates = {"CNY": 1, "USD": 7.1, "HKD": 0.91}
        result = calculate_portfolio_valuation(portfolio, rates)

        self.assertEqual(len(result["cash_accounts"]), 2)
        self.assertAlmostEqual(result["cash"], 1000 * 7.1 + 2000 * 0.91)
        self.assertAlmostEqual(result["total_assets"], 1000 * 7.1 + 2000 * 0.91)
        usd = next(item for item in result["cash_accounts"] if item["currency"] == "USD")
        hkd = next(item for item in result["cash_accounts"] if item["currency"] == "HKD")
        self.assertAlmostEqual(usd["amount_cny"], 7100)
        self.assertAlmostEqual(hkd["amount_cny"], 1820)
        self.assertAlmostEqual(usd["asset_weight_pct"] + hkd["asset_weight_pct"], 100.0)


    def test_position_weights_sum_to_100_without_cash(self) -> None:
        portfolio = {
            "positions": [
                {"account": "A", "code": "1", "currency": "CNY", "quantity": 1, "cost_price": 10, "current_price": 30},
                {"account": "B", "code": "2", "currency": "CNY", "quantity": 1, "cost_price": 10, "current_price": 70},
            ],
            "cash": 0,
            "cash_accounts": [],
        }
        result = calculate_portfolio_valuation(portfolio, {"CNY": 1})
        self.assertAlmostEqual(result["positions"][0]["asset_weight_pct"], 30.0)
        self.assertAlmostEqual(result["positions"][1]["asset_weight_pct"], 70.0)
        self.assertAlmostEqual(sum(item["holding_weight_pct"] for item in result["positions"]), 100.0)

    def test_each_new_write_changes_total_by_exact_converted_value(self) -> None:
        rates = {"CNY": 1, "USD": 6.8, "HKD": 0.87}
        portfolio = {"positions": [], "cash": 0, "cash_accounts": []}
        first = calculate_portfolio_valuation(portfolio, rates)
        self.assertEqual(first["total_assets"], 0)

        portfolio["positions"].append({
            "account": "银河", "code": "501312", "name": "海外科技LOF",
            "currency": "CNY", "asset_type": "stock", "quantity": 100,
            "cost_price": 90, "current_price": 95,
        })
        second = calculate_portfolio_valuation(portfolio, rates)
        self.assertAlmostEqual(second["total_assets"], 9500)

        portfolio["positions"].append({
            "account": "IBKR", "code": "NVDA", "name": "NVIDIA",
            "currency": "USD", "asset_type": "stock", "quantity": 5,
            "cost_price": 200, "current_price": 210,
        })
        third = calculate_portfolio_valuation(portfolio, rates)
        self.assertAlmostEqual(third["total_assets"] - second["total_assets"], 5 * 210 * 6.8)

        portfolio["cash_accounts"].append({"account": "IBKR", "currency": "USD", "amount": 1000})
        fourth = calculate_portfolio_valuation(portfolio, rates)
        self.assertAlmostEqual(fourth["total_assets"] - third["total_assets"], 1000 * 6.8)


if __name__ == "__main__":
    unittest.main()
