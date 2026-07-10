from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from backend.api.portfolio_ai import (
    ReviseRequest,
    ai_revise,
    load_pending,
    make_pending,
    parse_bookkeeping_message,
    save_pending,
)
from backend.services.portfolio_valuation import calculate_portfolio_valuation
from backend.services.portfolio_write_service import PortfolioWriteService


class CashLanguageFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def cash_map(self) -> dict[tuple[str, str], float]:
        return {
            (item["account"], item["currency"]): item["amount"]
            for item in self.service.load_portfolio().get("cash_accounts", [])
        }

    def test_user_requested_cash_language_end_to_end(self) -> None:
        self.service.safe_set_cash_account("IBKR", "HKD", 10000)
        withdraw = parse_bookkeeping_message("ib港币减少4000")
        self.service.safe_add_positions(withdraw["changes"], summary=withdraw["summary"])
        self.assertEqual(self.cash_map()[("IBKR", "HKD")], 6000)

        exact = parse_bookkeeping_message("ib港币变为20.32")
        self.service.safe_add_positions(exact["changes"], summary=exact["summary"])
        self.assertAlmostEqual(self.cash_map()[("IBKR", "HKD")], 20.32)

        self.service.safe_set_cash_account("长桥", "HKD", 1000)
        exchange = parse_bookkeeping_message("长桥500港币换成了20美元")
        result = self.service.safe_add_positions(exchange["changes"], summary=exchange["summary"])
        balances = self.cash_map()
        self.assertEqual(balances[("长桥", "HKD")], 500)
        self.assertEqual(balances[("长桥", "USD")], 20)

        valuation = calculate_portfolio_valuation(
            self.service.load_portfolio(),
            {"CNY": 1.0, "USD": 7.0, "HKD": 0.9},
        )
        expected_cash_cny = 20.32 * 0.9 + 500 * 0.9 + 20 * 7.0
        self.assertAlmostEqual(valuation["cash"], expected_cash_cny)
        self.assertAlmostEqual(valuation["total_assets"], expected_cash_cny)

        self.service.rollback_operation(result["operation_id"])
        balances = self.cash_map()
        self.assertEqual(balances[("长桥", "HKD")], 1000)
        self.assertNotIn(("长桥", "USD"), balances)

        self.service.safe_set_cash_account("银河", "CNY", 10000)
        withdrawal = parse_bookkeeping_message("银河提现了5k元")
        self.service.safe_add_positions(withdrawal["changes"], summary=withdrawal["summary"])
        self.assertEqual(self.cash_map()[("银河", "CNY")], 5000)


    def test_currency_revision_creates_successor_pending_card(self) -> None:
        parsed = parse_bookkeeping_message("银河增加2w")
        original = make_pending(parsed, "text", "银河增加2w")
        save_pending(original)

        revised = asyncio.run(ai_revise(ReviseRequest(
            pending_id=original["pending_id"],
            message="港币",
        )))

        self.assertNotEqual(revised["pending_id"], original["pending_id"])
        self.assertEqual(revised["revises_pending_id"], original["pending_id"])
        self.assertEqual(revised["changes"][0]["account"], "银河")
        self.assertEqual(revised["changes"][0]["currency"], "HKD")
        self.assertEqual(revised["changes"][0]["amount"], 20000)
        self.assertTrue(revised["requires_confirmation"])

        old = load_pending(original["pending_id"])
        self.assertEqual(old["status"], "superseded")
        self.assertFalse(old["requires_confirmation"])
        self.assertEqual(old["revised_to_pending_id"], revised["pending_id"])

    def test_two_cash_currencies_can_be_applied_second_then_first(self) -> None:
        first_hkd = parse_bookkeeping_message("银河增加2.2w港币")
        second_usd = parse_bookkeeping_message("银河增加2.2w美元")

        self.service.safe_add_positions(second_usd["changes"], summary="confirm second USD")
        self.service.safe_add_positions(first_hkd["changes"], summary="confirm first HKD")

        balances = self.cash_map()
        self.assertEqual(balances[("银河", "USD")], 22000)
        self.assertEqual(balances[("银河", "HKD")], 22000)
        self.assertEqual(len(balances), 2)



if __name__ == "__main__":
    unittest.main()
