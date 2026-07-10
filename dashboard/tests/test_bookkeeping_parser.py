from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.api.portfolio_ai import parse_bookkeeping_message


class BookkeepingParserTest(unittest.TestCase):
    def test_structured_chinese_labels_are_recognized(self) -> None:
        parsed = parse_bookkeeping_message(
            "账户：IBKR 名称：Apple 代码：AAPL 币种：USD 类型：stock 数量：3 成本价：202.68"
        )
        self.assertEqual(parsed["intent"], "bookkeeping")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "IBKR")
        self.assertEqual(change["name"], "Apple")
        self.assertEqual(change["code"], "AAPL")
        self.assertEqual(change["currency"], "USD")
        self.assertEqual(change["asset_type"], "stock")
        self.assertEqual(change["quantity"], 3)
        self.assertEqual(change["cost_price"], 202.68)

    def test_ib_alias_is_normalized_to_ibkr(self) -> None:
        first = parse_bookkeeping_message("买了5股英伟达，成本价135.6美元")
        pending = {
            "changes": first["changes"],
            "missing_fields": first["missing_fields"],
            "warnings": first["warnings"],
            "action_type": first["action_type"],
        }
        revised = parse_bookkeeping_message("IB", previous=pending)
        self.assertEqual(revised["changes"][0]["account"], "IBKR")
        self.assertEqual(revised["missing_fields"], [])
        self.assertFalse(any("新账户分组" in warning for warning in revised["warnings"]))

    def test_sell_uses_only_existing_account(self) -> None:
        positions = [{
            "account": "IBKR",
            "name": "NVIDIA/英伟达",
            "code": "NVDA",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": 10,
            "cost_price": 135.6,
        }]
        with patch("backend.api.portfolio_ai.compact_positions", return_value=positions):
            parsed = parse_bookkeeping_message("卖5股英伟达，均价2000美元")
        self.assertEqual(parsed["action_type"], "sell")
        self.assertEqual(parsed["changes"][0]["account"], "IBKR")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertTrue(any("自动选择账户：IBKR" in warning for warning in parsed["warnings"]))

    def test_sell_requires_choice_when_multiple_accounts_hold_asset(self) -> None:
        positions = [
            {"account": "IBKR", "name": "NVIDIA/英伟达", "code": "NVDA", "currency": "USD", "asset_type": "stock", "quantity": 10, "cost_price": 135.6},
            {"account": "长桥", "name": "NVIDIA/英伟达", "code": "NVDA", "currency": "USD", "asset_type": "stock", "quantity": 3, "cost_price": 150},
        ]
        with patch("backend.api.portfolio_ai.compact_positions", return_value=positions):
            parsed = parse_bookkeeping_message("卖2股英伟达，均价200美元")
        self.assertNotIn("account", parsed["changes"][0])
        self.assertIn("account", parsed["missing_fields"])
        self.assertTrue(any("IBKR、长桥" in warning for warning in parsed["warnings"]))

    def test_sell_account_choice_is_validated(self) -> None:
        positions = [
            {"account": "IBKR", "name": "NVIDIA/英伟达", "code": "NVDA", "currency": "USD", "asset_type": "stock", "quantity": 10, "cost_price": 135.6},
            {"account": "长桥", "name": "NVIDIA/英伟达", "code": "NVDA", "currency": "USD", "asset_type": "stock", "quantity": 2, "cost_price": 150},
        ]
        with patch("backend.api.portfolio_ai.compact_positions", return_value=positions):
            first = parse_bookkeeping_message("卖5股英伟达，均价200美元")
            pending = {
                "changes": first["changes"],
                "missing_fields": first["missing_fields"],
                "warnings": first["warnings"],
                "action_type": first["action_type"],
            }
            revised = parse_bookkeeping_message("长桥", previous=pending)
        self.assertEqual(revised["changes"][0]["account"], "长桥")
        self.assertIn("available_quantity", revised["missing_fields"])
        self.assertTrue(any("暂不支持卖空" in warning for warning in revised["warnings"]))

    def test_sell_more_than_holding_is_not_confirmable(self) -> None:
        positions = [{
            "account": "IBKR", "name": "NVIDIA/英伟达", "code": "NVDA",
            "currency": "USD", "asset_type": "stock", "quantity": 3, "cost_price": 135.6,
        }]
        with patch("backend.api.portfolio_ai.compact_positions", return_value=positions):
            parsed = parse_bookkeeping_message("卖5股英伟达，均价200美元")
        self.assertEqual(parsed["changes"][0]["account"], "IBKR")
        self.assertIn("available_quantity", parsed["missing_fields"])
        self.assertTrue(any("暂不支持卖空" in warning for warning in parsed["warnings"]))

    def test_sell_without_existing_holding_is_not_confirmable(self) -> None:
        with patch("backend.api.portfolio_ai.compact_positions", return_value=[]):
            parsed = parse_bookkeeping_message("卖5股英伟达，均价200美元")
        self.assertIn("existing_position", parsed["missing_fields"])
        self.assertTrue(any("暂不支持卖空" in warning for warning in parsed["warnings"]))

    def test_concise_name_quantity_total_is_bookkeeping(self) -> None:
        parsed = parse_bookkeeping_message("海外科技 100股票 一共花了9000元")
        self.assertEqual(parsed["intent"], "bookkeeping")
        self.assertEqual(parsed["changes"][0]["name"], "海外科技")
        self.assertEqual(parsed["changes"][0]["quantity"], 100)
        self.assertEqual(parsed["changes"][0]["total_cost"], 9000)
        self.assertEqual(parsed["changes"][0]["cost_price"], 90)

    def test_existing_nvidia_and_total_amount_are_inferred(self) -> None:
        positions = [{
            "account": "IBKR",
            "name": "NVIDIA/英伟达",
            "code": "NVDA",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": 10,
            "cost_price": 135.6,
        }]
        with patch("backend.api.portfolio_ai.compact_positions", return_value=positions):
            parsed = parse_bookkeeping_message("在长桥买了200股nVidia 一共40000刀")
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "长桥")
        self.assertEqual(change["name"], "NVIDIA/英伟达")
        self.assertEqual(change["code"], "NVDA")
        self.assertEqual(change["currency"], "USD")
        self.assertEqual(change["asset_type"], "stock")
        self.assertEqual(change["quantity"], 200)
        self.assertEqual(change["total_cost"], 40000)
        self.assertEqual(change["cost_price"], 200)
        self.assertEqual(parsed["missing_fields"], [])


    def test_cash_deposit_is_parsed(self) -> None:
        parsed = parse_bookkeeping_message("IBKR入金500美元")
        self.assertEqual(parsed["action_type"], "deposit")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "IBKR")
        self.assertEqual(change["currency"], "USD")
        self.assertEqual(change["amount"], 500)

    def test_cash_withdraw_is_parsed(self) -> None:
        parsed = parse_bookkeeping_message("银河出金1000元")
        self.assertEqual(parsed["action_type"], "withdraw")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(parsed["changes"][0]["amount"], 1000)

    def test_cash_balance_is_parsed(self) -> None:
        parsed = parse_bookkeeping_message("长桥账户现金5000港币")
        self.assertEqual(parsed["action_type"], "set_cash")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "长桥")
        self.assertEqual(change["currency"], "HKD")
        self.assertEqual(change["amount"], 5000)

    def test_code_bearing_lof_is_stock_type(self) -> None:
        parsed = parse_bookkeeping_message(
            "账户：银河 名称：海外科技LOF 代码：501312 币种：CNY 类型：fund 数量：100 成本价：90"
        )
        self.assertEqual(parsed["changes"][0]["asset_type"], "stock")


    def test_ib_with_leading_zai_is_account_alias(self) -> None:
        parsed = parse_bookkeeping_message("在IB买10股nvidia一共花了2000刀")
        self.assertEqual(parsed["changes"][0]["account"], "IBKR")

    def test_asset_name_is_not_mistaken_for_account(self) -> None:
        parsed = parse_bookkeeping_message("半导体ETF买了200份，一共20000元")
        self.assertNotIn("account", parsed["changes"][0])
        self.assertEqual(parsed["changes"][0]["name"], "半导体ETF")

    def test_cash_phrase_with_number_between_have_and_cash(self) -> None:
        parsed = parse_bookkeeping_message("IB有2000美元现金")
        self.assertEqual(parsed["action_type"], "set_cash")
        self.assertEqual(parsed["changes"][0]["account"], "IBKR")
        self.assertEqual(parsed["changes"][0]["amount"], 2000)

    def test_confirmation_word_alone_is_not_a_new_bookkeeping_record(self) -> None:
        parsed = parse_bookkeeping_message("确认写入")
        self.assertEqual(parsed["intent"], "chat_only")


if __name__ == "__main__":
    unittest.main()
