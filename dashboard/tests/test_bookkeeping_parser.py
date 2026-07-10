from __future__ import annotations

import unittest

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

    def test_confirmation_word_alone_is_not_a_new_bookkeeping_record(self) -> None:
        parsed = parse_bookkeeping_message("确认写入")
        self.assertEqual(parsed["intent"], "chat_only")


if __name__ == "__main__":
    unittest.main()
