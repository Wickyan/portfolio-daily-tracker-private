from __future__ import annotations

import unittest

from backend.api.portfolio_ai import parse_bookkeeping_message


class CopyCardFormatTest(unittest.TestCase):
    def test_position_copy_format_is_complete_and_editable(self) -> None:
        parsed = parse_bookkeeping_message(
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
        parsed = parse_bookkeeping_message(
            "记账：账户=银河；操作=买入；标的=纳指ETF国泰；代码=513100；币种=CNY；数量=12710；成本价=1.244"
        )
        change = parsed["changes"][0]
        self.assertEqual(change["quantity"], 12710)
        self.assertEqual(change["cost_price"], 1.244)
        self.assertAlmostEqual(change["total_cost"], 15811.24)

    def test_cash_copy_format(self) -> None:
        parsed = parse_bookkeeping_message(
            "记账：账户=银河；操作=增加现金；币种=CNY；金额=20000"
        )
        self.assertEqual(parsed["action_type"], "deposit")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(parsed["changes"][0]["amount"], 20000)

    def test_fx_copy_format(self) -> None:
        parsed = parse_bookkeeping_message(
            "记账：账户=长桥；操作=换汇；换出=500HKD；换入=20USD"
        )
        self.assertEqual(parsed["action_type"], "fx_exchange")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(parsed["changes"][0]["currency"], "HKD")
        self.assertEqual(parsed["changes"][0]["amount"], 500)
        self.assertEqual(parsed["changes"][1]["currency"], "USD")
        self.assertEqual(parsed["changes"][1]["amount"], 20)

    def test_multi_cash_copy_format_is_atomic(self) -> None:
        parsed = parse_bookkeeping_message(
            "记账1：账户=银河；操作=增加现金；币种=CNY；金额=100\n"
            "记账2：账户=长桥；操作=减少现金；币种=USD；金额=20"
        )
        self.assertEqual(parsed["action_type"], "multi_cash")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(len(parsed["changes"]), 2)
        self.assertTrue(any("原子操作" in warning for warning in parsed["warnings"]))


if __name__ == "__main__":
    unittest.main()
