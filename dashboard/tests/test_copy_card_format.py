from __future__ import annotations

import unittest

from backend.api.portfolio_ai import parse_bookkeeping_message


class CopyCardFormatTest(unittest.TestCase):
    def parse(self, text: str) -> dict:
        parsed = parse_bookkeeping_message(text)
        self.assertEqual(parsed["intent"], "bookkeeping")
        return parsed

    def test_position_copy_format_is_complete_and_editable(self) -> None:
        parsed = self.parse(
            "记账：账户=银河；操作=买入；标的=纳指ETF国泰；代码=513100；币种=CNY；数量=12700；成本价=1.243"
        )
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(parsed["missing_fields"], [])
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "银河")
        self.assertEqual(change["code"], "513100")
        self.assertEqual(change["name"], "纳指ETF国泰")
        self.assertEqual(change["quantity"], 12700)
        self.assertEqual(change["cost_price"], 1.243)
        self.assertAlmostEqual(change["total_cost"], 15786.1)

    def test_user_can_edit_copied_position_before_resubmitting(self) -> None:
        parsed = self.parse(
            "记账：账户=银河；操作=买入；标的=纳指ETF国泰；代码=513100；币种=CNY；数量=12710；成本价=1.244"
        )
        change = parsed["changes"][0]
        self.assertEqual(change["quantity"], 12710)
        self.assertEqual(change["cost_price"], 1.244)
        self.assertAlmostEqual(change["total_cost"], 15811.24)

    def test_field_order_does_not_matter_and_account_alias_is_normalized(self) -> None:
        parsed = self.parse(
            "记账：数量=100；币种=USD；代码=AAPL；标的=Apple；操作=买入；账户=IB；成本价=202.68"
        )
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "IBKR")
        self.assertEqual(change["code"], "AAPL")
        self.assertEqual(change["currency"], "USD")
        self.assertEqual(parsed["missing_fields"], [])

    def test_ascii_colon_and_semicolon_are_supported(self) -> None:
        parsed = self.parse(
            "记账:账户=银河;操作=买入;标的=比亚迪;代码=002594;币种=CNY;数量=500;成本价=102.742"
        )
        change = parsed["changes"][0]
        self.assertEqual(change["code"], "002594")
        self.assertEqual(change["quantity"], 500)
        self.assertEqual(change["cost_price"], 102.742)

    def test_prefixed_exchange_code_is_normalized(self) -> None:
        parsed = self.parse(
            "记账：账户=长桥；操作=买入；标的=腾讯控股；代码=HKG:0700；币种=HKD；数量=10；成本价=500"
        )
        change = parsed["changes"][0]
        self.assertEqual(change["code"], "0700")
        self.assertEqual(change["currency"], "HKD")
        self.assertEqual(parsed["missing_fields"], [])

    def test_fee_is_preserved_in_copy_format(self) -> None:
        parsed = self.parse(
            "记账：账户=IBKR；操作=买入；标的=Apple；代码=AAPL；币种=USD；数量=2；成本价=200；手续费=1.5"
        )
        change = parsed["changes"][0]
        self.assertEqual(change["fee"], 1.5)
        self.assertEqual(change["quantity"], 2)
        self.assertEqual(change["cost_price"], 200)

    def test_missing_code_stays_editable_but_not_confirmable(self) -> None:
        parsed = self.parse(
            "记账：账户=银河；操作=买入；标的=纳指ETF国泰；代码=；币种=CNY；数量=10；成本价=1.2"
        )
        self.assertIn("code", parsed["missing_fields"])
        self.assertEqual(parsed["changes"][0]["name"], "纳指ETF国泰")

    def test_invalid_position_numbers_are_blocked(self) -> None:
        negative_quantity = self.parse(
            "记账：账户=银河；操作=买入；标的=比亚迪；代码=002594；币种=CNY；数量=-5；成本价=100"
        )
        self.assertIn("positive_quantity", negative_quantity["missing_fields"])

        negative_price = self.parse(
            "记账：账户=银河；操作=买入；标的=比亚迪；代码=002594；币种=CNY；数量=5；成本价=-100"
        )
        self.assertIn("cost_price_non_negative", negative_price["missing_fields"])

    def test_code_currency_conflict_is_blocked(self) -> None:
        parsed = self.parse(
            "记账：账户=银河；操作=买入；标的=苹果；代码=AAPL；币种=HKD；数量=5；成本价=100"
        )
        self.assertIn("currency_conflict", parsed["missing_fields"])
        self.assertTrue(any("通常使用USD" in warning for warning in parsed["warnings"]))

    def test_sell_copy_format_does_not_require_price(self) -> None:
        parsed = self.parse(
            "记账：账户=银河；操作=卖出；标的=比亚迪；代码=002594；币种=CNY；数量=5；成交价="
        )
        self.assertEqual(parsed["action_type"], "sell")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(parsed["changes"][0]["quantity"], 5)

    def test_cash_copy_formats(self) -> None:
        deposit = self.parse("记账：账户=银河；操作=增加现金；币种=CNY；金额=20000")
        self.assertEqual(deposit["action_type"], "deposit")
        self.assertEqual(deposit["missing_fields"], [])
        self.assertEqual(deposit["changes"][0]["amount"], 20000)

        withdraw = self.parse("记账：账户=IB；操作=减少现金；币种=USD；金额=20")
        self.assertEqual(withdraw["action_type"], "withdraw")
        self.assertEqual(withdraw["changes"][0]["account"], "IBKR")

        set_cash = self.parse("记账：账户=银河；操作=设置现金余额；币种=CNY；金额=0")
        self.assertEqual(set_cash["action_type"], "set_cash")
        self.assertEqual(set_cash["missing_fields"], [])
        self.assertEqual(set_cash["changes"][0]["amount"], 0)

    def test_invalid_cash_values_are_blocked(self) -> None:
        negative = self.parse("记账：账户=银河；操作=增加现金；币种=CNY；金额=-100")
        self.assertIn("positive_amount", negative["missing_fields"])

        unsupported = self.parse("记账：账户=IBKR；操作=增加现金；币种=EUR；金额=100")
        self.assertIn("unsupported_currency", unsupported["missing_fields"])

    def test_fx_copy_format(self) -> None:
        parsed = self.parse("记账：账户=长桥；操作=换汇；换出=500HKD；换入=20USD")
        self.assertEqual(parsed["action_type"], "fx_exchange")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(parsed["changes"][0]["currency"], "HKD")
        self.assertEqual(parsed["changes"][0]["amount"], 500)
        self.assertEqual(parsed["changes"][1]["currency"], "USD")
        self.assertEqual(parsed["changes"][1]["amount"], 20)
        self.assertTrue(any("成交比率" in warning for warning in parsed["warnings"]))

    def test_same_currency_fx_is_blocked(self) -> None:
        parsed = self.parse("记账：账户=长桥；操作=换汇；换出=500HKD；换入=20HKD")
        self.assertIn("distinct_currencies", parsed["missing_fields"])
        self.assertTrue(any("不能相同" in warning for warning in parsed["warnings"]))

    def test_negative_fx_amount_is_blocked(self) -> None:
        parsed = self.parse("记账：账户=长桥；操作=换汇；换出=-500HKD；换入=20USD")
        self.assertIn("positive_amount", parsed["missing_fields"])

    def test_multi_cash_copy_format_is_atomic(self) -> None:
        parsed = self.parse(
            "记账1：账户=银河；操作=增加现金；币种=CNY；金额=100\n"
            "记账2：账户=长桥；操作=减少现金；币种=USD；金额=20"
        )
        self.assertEqual(parsed["action_type"], "multi_cash")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(len(parsed["changes"]), 2)
        self.assertTrue(any("原子操作" in warning for warning in parsed["warnings"]))

    def test_multiple_positions_can_be_recreated(self) -> None:
        parsed = self.parse(
            "记账1：账户=银河；操作=买入；标的=比亚迪；代码=002594；币种=CNY；数量=1；成本价=100\n"
            "记账2：账户=IBKR；操作=买入；标的=Apple；代码=AAPL；币种=USD；数量=1；成本价=200"
        )
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(len(parsed["changes"]), 2)

    def test_mixed_position_and_cash_copy_creates_independent_items(self) -> None:
        parsed = self.parse(
            "记账1：账户=银河；操作=买入；标的=比亚迪；代码=002594；币种=CNY；数量=1；成本价=100\n"
            "记账2：账户=银河；操作=增加现金；币种=CNY；金额=100"
        )
        self.assertEqual(parsed["action_type"], "multi_records")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(len(parsed["changes"]), 2)
        self.assertEqual([spec["action_type"] for spec in parsed["item_specs"]], ["add_or_update", "deposit"])


class CopyCardConfirmFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from backend.services.portfolio_write_service import PortfolioWriteService

        self._os = os
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        self._os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def create_pending(self, text: str) -> dict:
        from backend.api.portfolio_ai import make_pending, save_pending

        parsed = parse_bookkeeping_message(text)
        pending = make_pending(parsed, "text", text)
        save_pending(pending)
        return pending

    def test_copied_position_confirm_and_rollback(self) -> None:
        import asyncio
        from backend.api.portfolio_ai import ConfirmRequest, ai_confirm

        pending = self.create_pending(
            "记账：账户=IBKR；操作=买入；标的=Apple；代码=AAPL；币种=USD；数量=2；成本价=200；手续费=1.5"
        )
        self.assertTrue(pending["requires_confirmation"])
        result = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))

        position = self.service.load_portfolio()["positions"][0]
        self.assertEqual(position["quantity"], 2)
        self.assertAlmostEqual(position["total_cost"], 401.5)
        self.assertAlmostEqual(position["cost_price"], 200.75)

        self.service.rollback_operation(result["operation_id"])
        self.assertEqual(self.service.load_portfolio()["positions"], [])

    def test_copied_cash_confirm_is_idempotent_and_rollbackable(self) -> None:
        import asyncio
        from backend.api.portfolio_ai import ConfirmRequest, ai_confirm

        pending = self.create_pending(
            "记账：账户=银河；操作=增加现金；币种=CNY；金额=20000"
        )
        first = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        second = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertEqual(first["operation_id"], second["operation_id"])

        cash = self.service.load_portfolio()["cash_accounts"]
        self.assertEqual(cash, [{
            "account": "银河",
            "currency": "CNY",
            "amount": 20000.0,
            "updated_at": cash[0]["updated_at"],
        }])
        self.service.rollback_operation(first["operation_id"])
        self.assertEqual(self.service.load_portfolio()["cash_accounts"], [])

    def test_copied_fx_confirm_and_rollback(self) -> None:
        import asyncio
        from backend.api.portfolio_ai import ConfirmRequest, ai_confirm

        self.service.safe_set_cash_account("长桥", "HKD", 1000)
        pending = self.create_pending(
            "记账：账户=长桥；操作=换汇；换出=500HKD；换入=20USD"
        )
        result = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        balances = {
            (item["account"], item["currency"]): item["amount"]
            for item in self.service.load_portfolio()["cash_accounts"]
        }
        self.assertEqual(balances[("长桥", "HKD")], 500)
        self.assertEqual(balances[("长桥", "USD")], 20)

        self.service.rollback_operation(result["operation_id"])
        balances = {
            (item["account"], item["currency"]): item["amount"]
            for item in self.service.load_portfolio()["cash_accounts"]
        }
        self.assertEqual(balances, {("长桥", "HKD"): 1000})

    def test_same_currency_fx_cannot_be_confirmed_even_if_pending_is_tampered(self) -> None:
        import asyncio
        from fastapi import HTTPException
        from backend.api.portfolio_ai import ConfirmRequest, ai_confirm, save_pending

        pending = self.create_pending(
            "记账：账户=长桥；操作=换汇；换出=500HKD；换入=20HKD"
        )
        pending["missing_fields"] = []
        pending["requires_confirmation"] = True
        save_pending(pending)

        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("不能相同", context.exception.detail)
        self.assertEqual(self.service.load_portfolio()["cash_accounts"], [])

    def test_copied_multi_cash_is_atomic(self) -> None:
        import asyncio
        from backend.api.portfolio_ai import ConfirmRequest, ai_confirm

        pending = self.create_pending(
            "记账1：账户=银河；操作=增加现金；币种=CNY；金额=100\n"
            "记账2：账户=银河；操作=减少现金；币种=CNY；金额=50"
        )
        asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        cash = self.service.load_portfolio()["cash_accounts"]
        self.assertEqual(len(cash), 1)
        self.assertEqual(cash[0]["amount"], 50)


if __name__ == "__main__":
    unittest.main()
