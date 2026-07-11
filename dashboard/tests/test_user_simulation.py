from __future__ import annotations

import asyncio
import os
from pathlib import Path
import random
import tempfile
import unittest

from backend.api.portfolio_ai import (
    ConfirmRequest,
    ReviseRequest,
    ai_confirm,
    ai_revise,
    enrich_cash_availability,
    make_pending,
    parse_bookkeeping_message,
    save_pending,
)
from backend.services.portfolio_write_service import PortfolioWriteService


class UserSimulationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def create_pending(self, message: str) -> dict:
        parsed = enrich_cash_availability(parse_bookkeeping_message(message))
        pending = make_pending(parsed, "text", message)
        save_pending(pending)
        return pending

    def cash_map(self) -> dict[tuple[str, str], float]:
        return {
            (item["account"], item["currency"]): item["amount"]
            for item in self.service.load_portfolio().get("cash_accounts", [])
        }

    def seed_position(self, account: str, code: str = "AAPL", quantity: float = 10) -> None:
        self.service.safe_add_positions([{
            "account": account,
            "name": "Apple/苹果",
            "code": code,
            "currency": "USD",
            "asset_type": "stock",
            "quantity": quantity,
            "cost_price": 100,
        }], summary=f"seed {account}")

    def test_natural_cash_language_corpus(self) -> None:
        cases = [
            ("长桥加1000美元", "deposit", "长桥", "USD", 1000),
            ("IB港币减4000", "withdraw", "IBKR", "HKD", 4000),
            ("银河余额2w", "set_cash", "银河", "CNY", 20000),
            ("银河现金2w", "set_cash", "银河", "CNY", 20000),
            ("IB有港币2000", "set_cash", "IBKR", "HKD", 2000),
            ("IB港币有2000", "set_cash", "IBKR", "HKD", 2000),
            ("银河存了1000元", "deposit", "银河", "CNY", 1000),
            ("银河提了500元", "withdraw", "银河", "CNY", 500),
            ("IBKR美元减去100", "withdraw", "IBKR", "USD", 100),
            ("给IBKR入金100美元", "deposit", "IBKR", "USD", 100),
            ("往长桥转入200港币", "deposit", "长桥", "HKD", 200),
            ("向银河转出500元", "withdraw", "银河", "CNY", 500),
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

    def test_exchange_language_corpus(self) -> None:
        cases = [
            "长桥500港币换20美金",
            "长桥从500港币换到20美元",
            "长桥港元500兑换为美金20",
        ]
        for text in cases:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], "fx_exchange")
                self.assertEqual(parsed["missing_fields"], [])
                source, target = parsed["changes"]
                self.assertEqual((source["account"], source["currency"], source["amount"]), ("长桥", "HKD", 500))
                self.assertEqual((target["account"], target["currency"], target["amount"]), ("长桥", "USD", 20))

    def test_negations_and_how_to_questions_do_not_create_cards(self) -> None:
        cases = [
            "不要给IBKR入金100美元",
            "我没有从银河提现100元",
            "怎么给IBKR入金100美元？",
            "如果给IBKR入金100美元会怎样？",
            "不是银河增加100元，是长桥增加100美元",
            "取消给IBKR入金100美元",
        ]
        for text in cases:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["intent"], "chat_only")

    def test_number_formats_are_safe(self) -> None:
        valid = parse_bookkeeping_message("IBKR入金1,000.50美元")
        self.assertEqual(valid["changes"][0]["amount"], 1000.5)
        self.assertEqual(valid["missing_fields"], [])

        negative = parse_bookkeeping_message("IBKR入金-100美元")
        self.assertEqual(negative["changes"][0]["amount"], -100)
        self.assertIn("positive_amount", negative["missing_fields"])

        for text in ("IBKR入金1e6美元", "IBKR入金1,00美元", "IBKR入金1.2.3美元", "IBKR入金1万2千美元"):
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertIn("invalid_number", parsed["missing_fields"])
                self.assertFalse(make_pending(parsed, "text", text)["requires_confirmation"])

    def test_multi_cash_message_is_atomic_and_rollbackable(self) -> None:
        pending = self.create_pending("银河增加100元，长桥增加200美元")
        self.assertEqual(pending["action_type"], "multi_cash")
        self.assertTrue(pending["requires_confirmation"])
        result = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertEqual(self.cash_map(), {("银河", "CNY"): 100, ("长桥", "USD"): 200})

        self.service.rollback_operation(result["operation_id"])
        self.assertEqual(self.cash_map(), {})

    def test_multi_cash_applies_in_user_order(self) -> None:
        pending = self.create_pending("银河增加100元；银河提现50元")
        self.assertTrue(pending["requires_confirmation"])
        asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertEqual(self.cash_map()[("银河", "CNY")], 50)

        blocked = self.create_pending("银河提现100元；银河增加200元")
        self.assertIn("available_cash", blocked["missing_fields"])
        self.assertFalse(blocked["requires_confirmation"])

    def test_mixed_operations_are_not_silently_truncated(self) -> None:
        parsed = parse_bookkeeping_message("IBKR买入1股苹果均价200美元，银河增加100元")
        self.assertEqual(parsed["action_type"], "multiple_operations")
        self.assertIn("multiple_operations", parsed["missing_fields"])
        self.assertEqual(parsed["changes"], [])

    def test_cash_revision_language_corpus(self) -> None:
        cases = [
            ("账户改成长桥", "长桥", "CNY", 100),
            ("改成长桥", "长桥", "CNY", 100),
            ("金额改成3000", "银河", "CNY", 3000),
            ("改成3000港币", "银河", "HKD", 3000),
            ("3000港币", "银河", "HKD", 3000),
            ("币种改为美元", "银河", "USD", 100),
            ("2.2w美元", "银河", "USD", 22000),
        ]
        for revision, account, currency, amount in cases:
            with self.subTest(revision=revision):
                pending = self.create_pending("银河增加100元")
                revised = asyncio.run(ai_revise(ReviseRequest(
                    pending_id=pending["pending_id"],
                    message=revision,
                )))
                change = revised["changes"][0]
                self.assertEqual((change["account"], change["currency"], change["amount"]), (account, currency, amount))
                self.assertEqual(revised["missing_fields"], [])
                self.assertTrue(revised["requires_confirmation"])

    def test_invalid_cash_revision_stays_unconfirmable(self) -> None:
        pending = self.create_pending("银河增加100元")
        revised = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="-100美元",
        )))
        self.assertIn("positive_amount", revised["missing_fields"])
        self.assertFalse(revised["requires_confirmation"])

    def test_clear_all_selects_quantity_after_account_revision(self) -> None:
        self.seed_position("IBKR", quantity=10)
        self.seed_position("长桥", quantity=3)
        parsed = parse_bookkeeping_message("清仓苹果")
        self.assertIn("account", parsed["missing_fields"])
        pending = make_pending(parsed, "text", "清仓苹果")
        save_pending(pending)

        revised = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="IBKR",
        )))
        self.assertEqual(revised["changes"][0]["account"], "IBKR")
        self.assertEqual(revised["changes"][0]["quantity"], 10)
        self.assertEqual(revised["missing_fields"], [])
        self.assertTrue(revised["requires_confirmation"])

    def test_sell_quantity_revision_rechecks_available_holding(self) -> None:
        self.seed_position("IBKR", quantity=3)
        parsed = parse_bookkeeping_message("卖2股苹果")
        pending = make_pending(parsed, "text", "卖2股苹果")
        save_pending(pending)
        revised = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="数量5",
        )))
        self.assertIn("available_quantity", revised["missing_fields"])
        self.assertFalse(revised["requires_confirmation"])

    def test_known_account_aliases_are_canonical_at_storage_boundary(self) -> None:
        self.service.safe_set_cash_account("ibkr", "USD", 100)
        self.service.safe_add_positions([{
            "action_type": "deposit",
            "account": "IB",
            "currency": "USD",
            "amount": 50,
        }], summary="alias deposit")
        self.assertEqual(self.cash_map(), {("IBKR", "USD"): 150})

        self.service.safe_add_positions([{
            "account": "ibkr",
            "name": "Apple",
            "code": "AAPL",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": 1,
            "cost_price": 100,
        }], summary="alias position")
        self.service.safe_add_positions([{
            "account": "IBKR",
            "name": "Apple",
            "code": "AAPL",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": 2,
            "cost_price": 200,
        }], summary="canonical position")
        positions = self.service.load_portfolio()["positions"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["account"], "IBKR")
        self.assertEqual(positions[0]["quantity"], 3)

    def test_structured_lowercase_broker_is_canonicalized(self) -> None:
        parsed = parse_bookkeeping_message(
            "账户：ibkr 名称：Apple 代码：AAPL 币种：USD 类型：stock 数量：3 成本价：200"
        )
        self.assertEqual(parsed["changes"][0]["account"], "IBKR")
        self.assertEqual(parsed["missing_fields"], [])

    def test_random_spacing_and_case_variants(self) -> None:
        random.seed(3407)
        for _ in range(30):
            left = " " * random.randint(0, 3)
            middle = " " * random.randint(0, 3)
            account = random.choice(["IB", "ib", "IBKR", "ibkr"])
            currency = random.choice(["USD", "usd", "美元", "美金"])
            text = f"{left}{account}{middle}入金{middle}1.5k{currency}{left}"
            parsed = parse_bookkeeping_message(text)
            with self.subTest(text=text):
                self.assertEqual(parsed["action_type"], "deposit")
                self.assertEqual(parsed["missing_fields"], [])
                change = parsed["changes"][0]
                self.assertEqual(change["account"], "IBKR")
                self.assertEqual(change["currency"], "USD")
                self.assertEqual(change["amount"], 1500)

    def test_every_confirmable_cash_change_satisfies_storage_invariants(self) -> None:
        messages = [
            "银河增加100元",
            "长桥余额2w港币",
            "IB美元减100",
            "银河增加100元，长桥增加200美元",
            "长桥500港币换20美元",
            "IBKR入金-100美元",
            "IBKR入金100欧元",
        ]
        for text in messages:
            parsed = parse_bookkeeping_message(text)
            pending = make_pending(parsed, "text", text)
            if not pending["requires_confirmation"]:
                continue
            for change in pending["changes"]:
                action = change.get("action_type", pending["action_type"])
                if action not in {"deposit", "withdraw", "set_cash"}:
                    continue
                self.assertTrue(change.get("account"))
                self.assertIn(change.get("currency"), {"CNY", "USD", "HKD"})
                amount = float(change["amount"])
                self.assertGreaterEqual(amount, 0)
                if action in {"deposit", "withdraw"}:
                    self.assertGreater(amount, 0)


    def test_bare_buy_and_negative_trade_numbers(self) -> None:
        parsed = parse_bookkeeping_message("IBKR买100股苹果,均价20美元")
        self.assertEqual(parsed["action_type"], "add_or_update")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertEqual(parsed["changes"][0]["quantity"], 100)
        self.assertEqual(parsed["changes"][0]["cost_price"], 20)

        negative_quantity = parse_bookkeeping_message("IBKR买入-5股苹果均价20美元")
        self.assertIn("positive_quantity", negative_quantity["missing_fields"])
        self.assertNotIn("cost_price_non_negative", negative_quantity["missing_fields"])

        negative_price = parse_bookkeeping_message("IBKR买入5股苹果均价-20美元")
        self.assertIn("cost_price_non_negative", negative_price["missing_fields"])

    def test_full_width_and_currency_symbol_inputs(self) -> None:
        cases = [
            ("ＩＢＫＲ入金１，０００美元", "IBKR", "USD", 1000),
            ("IBKR入金$1000", "IBKR", "USD", 1000),
            ("IBKR入金HK$1000", "IBKR", "HKD", 1000),
            ("银河入金￥1000", "银河", "CNY", 1000),
        ]
        for text, account, currency, amount in cases:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["missing_fields"], [])
                change = parsed["changes"][0]
                self.assertEqual((change["account"], change["currency"], change["amount"]), (account, currency, amount))

    def test_uncertain_or_future_language_does_not_write(self) -> None:
        cases = [
            "明天给IBKR入金100美元",
            "我计划给IBKR入金100美元",
            "你觉得给IBKR入金100美元合适吗",
            "IBKR入金100美元的手续费是多少",
            "可能给IBKR入金100美元",
            "我想给IBKR入金100美元",
            "能给IBKR入金100美元吗",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_bookkeeping_message(text)["intent"], "chat_only")

    def test_compact_multi_cash_connectors(self) -> None:
        cases = [
            "银河增加100元然后长桥增加200美元",
            "银河增加100元和长桥增加200美元",
            "银河增加100元再提现50元",
        ]
        for text in cases:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], "multi_cash")
                self.assertEqual(len(parsed["changes"]), 2)
                self.assertEqual(parsed["missing_fields"], [])

    def test_suffixed_account_is_not_swallowed_by_ibkr_alias(self) -> None:
        parsed = parse_bookkeeping_message("IBKR2入金100美元")
        self.assertEqual(parsed["changes"][0]["account"], "IBKR2")
        self.assertEqual(parsed["missing_fields"], [])
        self.assertTrue(any("新账户分组" in warning for warning in parsed["warnings"]))

    def test_malformed_amount_can_be_corrected_without_stale_warning(self) -> None:
        pending = self.create_pending("IBKR入金1e6美元")
        self.assertIn("invalid_number", pending["missing_fields"])
        revised = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="金额1000000",
        )))
        self.assertEqual(revised["changes"][0]["amount"], 1000000)
        self.assertEqual(revised["missing_fields"], [])
        self.assertTrue(revised["requires_confirmation"])
        self.assertFalse(any("科学计数法" in warning for warning in revised["warnings"]))
        self.assertTrue(any("金额1000000" in warning for warning in revised["warnings"]))

    def test_cash_availability_warning_is_recomputed_after_revision(self) -> None:
        self.service.safe_set_cash_account("银河", "CNY", 100)
        pending = self.create_pending("银河提现200元")
        self.assertIn("available_cash", pending["missing_fields"])
        revised = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="金额50",
        )))
        self.assertEqual(revised["missing_fields"], [])
        self.assertTrue(revised["requires_confirmation"])
        self.assertFalse(any("现金不足" in warning for warning in revised["warnings"]))



    def test_instrument_currency_conflict_blocks_confirmation(self) -> None:
        parsed = parse_bookkeeping_message("IBKR买入100股小米，均价20美元")
        self.assertEqual(parsed["changes"][0]["code"], "1810")
        self.assertEqual(parsed["changes"][0]["currency"], "USD")
        self.assertIn("currency_conflict", parsed["missing_fields"])
        self.assertFalse(make_pending(parsed, "text", "conflict")["requires_confirmation"])
        self.assertTrue(any("通常使用HKD" in warning for warning in parsed["warnings"]))

        normal = parse_bookkeeping_message("尊嘉买入100股小米，均价20港币")
        self.assertNotIn("currency_conflict", normal["missing_fields"])



    def test_storage_boundary_rejects_instrument_currency_conflict(self) -> None:
        before = self.service.load_portfolio()
        with self.assertRaisesRegex(ValueError, "currency_conflict"):
            self.service.safe_add_positions([{
                "account": "IBKR",
                "name": "Xiaomi",
                "code": "1810",
                "currency": "USD",
                "asset_type": "stock",
                "quantity": 1,
                "cost_price": 20,
            }], summary="bad currency")
        self.assertEqual(self.service.load_portfolio()["positions"], before["positions"])



    def test_first_and_later_buy_fees_use_same_cost_basis_rule(self) -> None:
        base = {
            "account": "IBKR",
            "name": "Apple",
            "code": "AAPL",
            "currency": "USD",
            "asset_type": "stock",
        }
        self.service.safe_add_positions([{**base, "quantity": 10, "cost_price": 100, "fee": 10}], summary="first buy")
        first = self.service.load_portfolio()["positions"][0]
        self.assertAlmostEqual(first["total_cost"], 1010)
        self.assertAlmostEqual(first["cost_price"], 101)

        self.service.safe_add_positions([{**base, "quantity": 10, "cost_price": 200, "fee": 10}], summary="second buy")
        merged = self.service.load_portfolio()["positions"][0]
        self.assertAlmostEqual(merged["quantity"], 20)
        self.assertAlmostEqual(merged["total_cost"], 3020)
        self.assertAlmostEqual(merged["cost_price"], 151)



    def test_natural_correction_revision_uses_value_after_but(self) -> None:
        cases = [
            ("不是人民币，而是美元", "银河", "USD", 100),
            ("不是银河，是长桥", "长桥", "CNY", 100),
            ("不是100，是200港币", "银河", "HKD", 200),
        ]
        for revision, account, currency, amount in cases:
            with self.subTest(revision=revision):
                pending = self.create_pending("银河增加100元")
                revised = asyncio.run(ai_revise(ReviseRequest(
                    pending_id=pending["pending_id"],
                    message=revision,
                )))
                change = revised["changes"][0]
                self.assertEqual((change["account"], change["currency"], change["amount"]), (account, currency, amount))
                self.assertEqual(revised["missing_fields"], [])



    def test_natural_remaining_cash_phrases(self) -> None:
        cases = [
            "长桥有1000美元",
            "长桥1000美元现金",
            "长桥美元剩1000",
            "长桥港币剩下1000",
            "银河还剩1000元",
        ]
        expected = [
            ("长桥", "USD", 1000),
            ("长桥", "USD", 1000),
            ("长桥", "USD", 1000),
            ("长桥", "HKD", 1000),
            ("银河", "CNY", 1000),
        ]
        for text, target in zip(cases, expected):
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], "set_cash")
                self.assertEqual(parsed["missing_fields"], [])
                change = parsed["changes"][0]
                self.assertEqual((change["account"], change["currency"], change["amount"]), target)



    def test_concise_account_asset_quantity_price_is_bookkeeping(self) -> None:
        for text in (
            "银河 比亚迪 500个 102.742元",
            "银河 比亚迪 500股 102.742元",
        ):
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], "add_or_update")
                self.assertEqual(parsed["missing_fields"], [])
                change = parsed["changes"][0]
                self.assertEqual(change["account"], "银河")
                self.assertEqual(change["code"], "002594")
                self.assertEqual(change["quantity"], 500)
                self.assertEqual(change["cost_price"], 102.742)
                self.assertEqual(change["total_cost"], 51371)



    def test_unknown_concise_fund_entry_creates_incomplete_card(self) -> None:
        parsed = parse_bookkeeping_message("纳之大成 1.243 12700个")
        self.assertEqual(parsed["action_type"], "add_or_update")
        change = parsed["changes"][0]
        self.assertEqual(change["name"], "纳之大成")
        self.assertEqual(change["quantity"], 12700)
        self.assertEqual(change["cost_price"], 1.243)
        self.assertAlmostEqual(change["total_cost"], 15786.1)
        self.assertIn("account", parsed["missing_fields"])
        self.assertIn("code", parsed["missing_fields"])

    def test_combined_unknown_fund_entry_keeps_account_and_price(self) -> None:
        parsed = parse_bookkeeping_message("银河 纳之大成 1.243元 12700个")
        self.assertEqual(parsed["action_type"], "add_or_update")
        change = parsed["changes"][0]
        self.assertEqual(change["account"], "银河")
        self.assertEqual(change["name"], "纳之大成")
        self.assertEqual(change["currency"], "CNY")
        self.assertEqual(change["quantity"], 12700)
        self.assertEqual(change["cost_price"], 1.243)
        self.assertIn("code", parsed["missing_fields"])



if __name__ == "__main__":
    unittest.main()
