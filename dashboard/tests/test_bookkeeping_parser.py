from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.api.portfolio_ai import enrich_cash_availability, parse_bookkeeping_message


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


    def test_ib_hkd_decrease_is_withdraw(self) -> None:
        parsed = parse_bookkeeping_message("ib港币减少4000")
        self.assertEqual(parsed["action_type"], "withdraw")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "IBKR")
        self.assertEqual(change["currency"], "HKD")
        self.assertEqual(change["amount"], 4000)

    def test_ib_hkd_change_to_exact_balance_is_set_cash(self) -> None:
        parsed = parse_bookkeeping_message("ib港币变为20.32")
        self.assertEqual(parsed["action_type"], "set_cash")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "IBKR")
        self.assertEqual(change["currency"], "HKD")
        self.assertAlmostEqual(change["amount"], 20.32)

    def test_cash_exchange_creates_atomic_out_and_in_changes(self) -> None:
        parsed = parse_bookkeeping_message("长桥500港币换成了20美元")
        self.assertEqual(parsed["action_type"], "fx_exchange")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(len(parsed["changes"]), 2)
        source, target = parsed["changes"]
        self.assertEqual(source["action_type"], "withdraw")
        self.assertEqual(source["account"], "长桥")
        self.assertEqual(source["currency"], "HKD")
        self.assertEqual(source["amount"], 500)
        self.assertEqual(target["action_type"], "deposit")
        self.assertEqual(target["account"], "长桥")
        self.assertEqual(target["currency"], "USD")
        self.assertEqual(target["amount"], 20)
        self.assertTrue(any("总资产仍按当前实时汇率折算" in item for item in parsed["warnings"]))

    def test_cash_exchange_without_account_can_revise_both_legs(self) -> None:
        first = parse_bookkeeping_message("500港币换成20美元")
        self.assertEqual(first["action_type"], "fx_exchange")
        self.assertEqual(first["missing_fields"], ["account"])
        pending = {
            "changes": first["changes"],
            "missing_fields": first["missing_fields"],
            "warnings": first["warnings"],
            "action_type": first["action_type"],
        }
        revised = parse_bookkeeping_message("长桥", previous=pending)
        self.assertEqual(revised["missing_fields"], [])
        self.assertTrue(all(change["account"] == "长桥" for change in revised["changes"]))

    def test_withdraw_supports_k_suffix(self) -> None:
        parsed = parse_bookkeeping_message("银河提现了5k元")
        self.assertEqual(parsed["action_type"], "withdraw")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "银河")
        self.assertEqual(change["currency"], "CNY")
        self.assertEqual(change["amount"], 5000)

    def test_cash_shorthand_variants(self) -> None:
        cases = [
            ("IB美元增加1.5k", "deposit", "IBKR", "USD", 1500),
            ("长桥港币减了2千", "withdraw", "长桥", "HKD", 2000),
            ("银河人民币改成3万", "set_cash", "银河", "CNY", 30000),
            ("富途现金余额是8000港币", "set_cash", "富途", "HKD", 8000),
        ]
        for text, action, account, currency, amount in cases:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], action)
                self.assertEqual(parsed["missing_fields"], [])
                change = parsed["changes"][0]
                self.assertEqual(change["account"], account)
                self.assertEqual(change["currency"], currency)
                self.assertEqual(change["amount"], amount)

    def test_exchange_rejects_same_currency(self) -> None:
        parsed = parse_bookkeeping_message("长桥500港币换成400港币")
        self.assertEqual(parsed["action_type"], "fx_exchange")
        self.assertIn("distinct_currencies", parsed["missing_fields"])


    def test_exchange_language_variants(self) -> None:
        cases = [
            ("长桥把500港币换成20美元", "长桥", "HKD", 500, "USD", 20),
            ("长桥用500HKD换20USD", "长桥", "HKD", 500, "USD", 20),
            ("IBKR港币500兑换为美元20", "IBKR", "HKD", 500, "USD", 20),
            ("银河人民币1万换成港币10800", "银河", "CNY", 10000, "HKD", 10800),
        ]
        for text, account, source_currency, source_amount, target_currency, target_amount in cases:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], "fx_exchange")
                self.assertEqual(parsed["missing_fields"], [])
                source, target = parsed["changes"]
                self.assertEqual(source["account"], account)
                self.assertEqual(source["currency"], source_currency)
                self.assertEqual(source["amount"], source_amount)
                self.assertEqual(target["currency"], target_currency)
                self.assertEqual(target["amount"], target_amount)

    def test_unrelated_change_word_is_chat_only(self) -> None:
        parsed = parse_bookkeeping_message("我换了手机")
        self.assertEqual(parsed["intent"], "chat_only")


    def test_cash_availability_blocks_negative_balance(self) -> None:
        parsed = parse_bookkeeping_message("ib港币减少4000")
        with patch(
            "backend.api.portfolio_ai.PortfolioWriteService.load_portfolio",
            return_value={"positions": [], "cash": 0, "cash_accounts": [
                {"account": "IBKR", "currency": "HKD", "amount": 100},
            ]},
        ):
            checked = enrich_cash_availability(parsed)
        self.assertIn("available_cash", checked["missing_fields"])
        self.assertTrue(any("当前100" in warning for warning in checked["warnings"]))

    def test_cash_availability_accepts_sufficient_exchange_source(self) -> None:
        parsed = parse_bookkeeping_message("长桥500港币换成20美元")
        with patch(
            "backend.api.portfolio_ai.PortfolioWriteService.load_portfolio",
            return_value={"positions": [], "cash": 0, "cash_accounts": [
                {"account": "长桥", "currency": "HKD", "amount": 1000},
            ]},
        ):
            checked = enrich_cash_availability(parsed)
        self.assertEqual(checked["missing_fields"], [])


    def test_galaxy_generic_increase_creates_confirmable_cny_deposit(self) -> None:
        parsed = parse_bookkeeping_message("银河增加2w")
        self.assertEqual(parsed["intent"], "bookkeeping")
        self.assertEqual(parsed["action_type"], "deposit")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "银河")
        self.assertEqual(change["currency"], "CNY")
        self.assertEqual(change["amount"], 20000)

    def test_noisy_xiaomi_sentence_with_ge_unit_is_fully_parsed(self) -> None:
        parsed = parse_bookkeeping_message("小米托存 尊嘉买入100个 20")
        self.assertEqual(parsed["intent"], "bookkeeping")
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "尊嘉")
        self.assertEqual(change["name"], "小米集团")
        self.assertEqual(change["code"], "1810")
        self.assertEqual(change["currency"], "HKD")
        self.assertEqual(change["asset_type"], "stock")
        self.assertEqual(change["quantity"], 100)
        self.assertEqual(change["cost_price"], 20)
        self.assertEqual(change["total_cost"], 2000)

    def test_broker_after_asset_phrase_is_still_detected(self) -> None:
        parsed = parse_bookkeeping_message("小米 尊嘉买入100股 20")
        self.assertEqual(parsed["changes"][0]["account"], "尊嘉")

    def test_confirmation_word_alone_is_not_a_new_bookkeeping_record(self) -> None:
        parsed = parse_bookkeeping_message("确认写入")
        self.assertEqual(parsed["intent"], "chat_only")


if __name__ == "__main__":
    unittest.main()
