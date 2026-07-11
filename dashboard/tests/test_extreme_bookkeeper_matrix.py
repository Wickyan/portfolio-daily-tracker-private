from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import os

import httpx
from pathlib import Path
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

import backend.api.portfolio_ai as portfolio_ai_module
from backend.api.portfolio_ai import (
    CancelRequest,
    ConfirmRequest,
    ItemRequest,
    ItemReviseRequest,
    PreviewRequest,
    ai_cancel,
    ai_confirm,
    ai_confirm_item,
    ai_preview,
    ai_revise_item,
    ai_rollback_item,
    get_pending,
    load_pending,
    parse_bookkeeping_message,
    save_pending,
)
from backend.services.portfolio_write_service import PortfolioWriteService


class TempLedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    @staticmethod
    def position(
        quantity: float,
        cost_price: float,
        action_type: str = "add_or_update",
        *,
        account: str = "银河",
        code: str = "002594",
        name: str = "比亚迪",
        currency: str = "CNY",
    ) -> dict:
        return {
            "action_type": action_type,
            "account": account,
            "name": name,
            "code": code,
            "currency": currency,
            "asset_type": "stock",
            "quantity": quantity,
            "cost_price": cost_price,
        }

    @staticmethod
    def cash(action_type: str, amount: float, account: str = "银河", currency: str = "CNY") -> dict:
        return {
            "action_type": action_type,
            "account": account,
            "currency": currency,
            "amount": amount,
        }

    def preview(self, text: str) -> dict:
        return asyncio.run(ai_preview(PreviewRequest(message=text, input_type="text")))

    def position_map(self) -> dict:
        return {
            (item["account"], item["code"], item["currency"]): item
            for item in self.service.load_portfolio()["positions"]
        }

    def cash_map(self) -> dict:
        return {
            (item["account"], item["currency"]): item["amount"]
            for item in self.service.load_portfolio()["cash_accounts"]
        }


class RollbackReplaySemanticsTest(TempLedgerCase):
    def test_rollback_first_absolute_position_preserves_later_absolute_position(self) -> None:
        self.service.safe_add_positions([self.position(100, 90)], summary="seed")
        result = self.service.safe_add_items_atomic([
            {"item_id": "set-1", "changes": [self.position(500, 100, "set_position")]},
            {"item_id": "set-2", "changes": [self.position(600, 110, "set_position")]},
        ], pending_id="pending-pos-absolute", summary="absolute positions")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["set-1"])
        position = self.position_map()[("银河", "002594", "CNY")]
        self.assertEqual(position["quantity"], 600)
        self.assertAlmostEqual(position["cost_price"], 110)

    def test_rollback_first_absolute_cash_preserves_later_absolute_cash(self) -> None:
        self.service.safe_set_cash_account("银河", "CNY", 100)
        result = self.service.safe_add_items_atomic([
            {"item_id": "cash-1", "changes": [self.cash("set_cash", 200)]},
            {"item_id": "cash-2", "changes": [self.cash("set_cash", 300)]},
        ], pending_id="pending-cash-absolute", summary="absolute cash")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["cash-1"])
        self.assertEqual(self.cash_map()[("银河", "CNY")], 300)

    def test_rollback_buy_before_later_absolute_position_keeps_absolute_target(self) -> None:
        self.service.safe_add_positions([self.position(100, 90)], summary="seed")
        result = self.service.safe_add_items_atomic([
            {"item_id": "buy", "changes": [self.position(50, 100, "add_or_update")]},
            {"item_id": "set", "changes": [self.position(600, 110, "set_position")]},
        ], pending_id="pending-buy-set", summary="buy then set")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["buy"])
        position = self.position_map()[("银河", "002594", "CNY")]
        self.assertEqual(position["quantity"], 600)
        self.assertAlmostEqual(position["cost_price"], 110)

    def test_rollback_absolute_position_replays_later_buy_on_original_state(self) -> None:
        self.service.safe_add_positions([self.position(100, 90)], summary="seed")
        result = self.service.safe_add_items_atomic([
            {"item_id": "set", "changes": [self.position(500, 100, "set_position")]},
            {"item_id": "buy", "changes": [self.position(50, 110, "add_or_update")]},
        ], pending_id="pending-set-buy", summary="set then buy")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["set"])
        position = self.position_map()[("银河", "002594", "CNY")]
        self.assertEqual(position["quantity"], 150)
        self.assertAlmostEqual(position["cost_price"], (100 * 90 + 50 * 110) / 150)

    def test_rollback_deposit_before_later_set_cash_keeps_absolute_target(self) -> None:
        self.service.safe_set_cash_account("银河", "CNY", 100)
        result = self.service.safe_add_items_atomic([
            {"item_id": "deposit", "changes": [self.cash("deposit", 50)]},
            {"item_id": "set", "changes": [self.cash("set_cash", 300)]},
        ], pending_id="pending-deposit-set", summary="deposit then set")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["deposit"])
        self.assertEqual(self.cash_map()[("银河", "CNY")], 300)

    def test_rollback_set_cash_replays_later_deposit_on_original_state(self) -> None:
        self.service.safe_set_cash_account("银河", "CNY", 100)
        result = self.service.safe_add_items_atomic([
            {"item_id": "set", "changes": [self.cash("set_cash", 300)]},
            {"item_id": "deposit", "changes": [self.cash("deposit", 50)]},
        ], pending_id="pending-set-deposit", summary="set then deposit")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["set"])
        self.assertEqual(self.cash_map()[("银河", "CNY")], 150)

    def test_rollback_fx_preserves_later_absolute_currency_balance(self) -> None:
        self.service.safe_set_cash_account("长桥", "HKD", 1000)
        self.service.safe_set_cash_account("长桥", "USD", 100)
        result = self.service.safe_add_items_atomic([
            {"item_id": "fx", "changes": [
                self.cash("withdraw", 500, "长桥", "HKD"),
                self.cash("deposit", 60, "长桥", "USD"),
            ]},
            {"item_id": "set-usd", "changes": [self.cash("set_cash", 500, "长桥", "USD")]},
        ], pending_id="pending-fx-set", summary="fx then set")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["fx"])
        cash = self.cash_map()
        self.assertEqual(cash[("长桥", "HKD")], 1000)
        self.assertEqual(cash[("长桥", "USD")], 500)

    def test_rollback_buy_fails_when_later_sell_depends_on_it(self) -> None:
        result = self.service.safe_add_items_atomic([
            {"item_id": "buy", "changes": [self.position(10, 100)]},
            {"item_id": "sell", "changes": [self.position(8, 100, "sell")]},
        ], pending_id="pending-dependent-sell", summary="buy then sell")
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.service.rollback_operation(operations["buy"])
        self.assertEqual(self.position_map()[("银河", "002594", "CNY")]["quantity"], 2)

    def test_operation_order_uses_created_at_when_file_mtimes_are_identical(self) -> None:
        result = self.service.safe_add_items_atomic([
            {"item_id": "first", "changes": [self.position(10, 100)]},
            {"item_id": "second", "changes": [self.position(5, 110)]},
        ], pending_id="pending-same-mtime", summary="same mtime")
        for path in Path("data/operations").glob("*.json"):
            os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        loaded = self.service._load_all_operations()
        item_order = [operation.get("pending_action", {}).get("item_id") for operation in loaded]
        self.assertEqual(item_order[:2], ["second", "first"])
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["first"])
        position = self.position_map()[("银河", "002594", "CNY")]
        self.assertEqual(position["quantity"], 5)
        self.assertAlmostEqual(position["cost_price"], 110)

    def test_quote_price_and_unrelated_identity_survive_replay_rollback(self) -> None:
        self.service.safe_add_positions([
            self.position(100, 90),
            self.position(2, 200, account="IBKR", code="AAPL", name="Apple", currency="USD"),
        ], summary="seed")
        result = self.service.safe_add_items_atomic([
            {"item_id": "set", "changes": [self.position(500, 100, "set_position")]},
            {"item_id": "buy", "changes": [self.position(50, 110)]},
        ], pending_id="pending-metadata", summary="metadata")
        self.service.safe_update_prices({
            ("银河", "002594", "CNY"): 123.45,
            ("IBKR", "AAPL", "USD"): 234.56,
        })
        operations = {item["item_id"]: item["operation_id"] for item in result["item_operations"]}
        self.service.rollback_operation(operations["set"])
        positions = self.position_map()
        self.assertEqual(positions[("银河", "002594", "CNY")]["current_price"], 123.45)
        self.assertEqual(positions[("IBKR", "AAPL", "USD")]["quantity"], 2)
        self.assertEqual(positions[("IBKR", "AAPL", "USD")]["current_price"], 234.56)

    def test_manual_absolute_update_replay_preserves_later_buy(self) -> None:
        self.service.safe_add_positions([self.position(100, 90)], summary="seed")
        updated = self.service.safe_update_position(
            "银河", "002594", "CNY", {"quantity": 500, "cost_price": 100}
        )
        self.service.safe_add_positions([self.position(50, 110)], summary="later buy")
        self.service.rollback_operation(updated["operation_id"])
        position = self.position_map()[("银河", "002594", "CNY")]
        self.assertEqual(position["quantity"], 150)
        self.assertAlmostEqual(position["cost_price"], (100 * 90 + 50 * 110) / 150)

    def test_manual_delete_replay_restores_original_plus_later_buy(self) -> None:
        self.service.safe_add_positions([self.position(100, 90)], summary="seed")
        deleted = self.service.safe_remove_position("银河", "002594", "CNY")
        self.service.safe_add_positions([self.position(50, 110)], summary="later buy")
        self.service.rollback_operation(deleted["operation_id"])
        position = self.position_map()[("银河", "002594", "CNY")]
        self.assertEqual(position["quantity"], 150)
        self.assertAlmostEqual(position["cost_price"], (100 * 90 + 50 * 110) / 150)

    def test_random_position_buy_set_sequences_match_reference_after_each_rollback(self) -> None:
        rng = random.Random(3407)
        for trial in range(30):
            with self.subTest(trial=trial):
                # Isolate every randomized trial.
                with tempfile.TemporaryDirectory() as td:
                    old = Path.cwd()
                    os.chdir(td)
                    try:
                        service = PortfolioWriteService(Path("data/portfolio.json"))
                        service.safe_add_positions([self.position(100, 90)], summary="seed")
                        actions: list[dict] = []
                        item_ops: dict[str, str] = {}
                        for index in range(8):
                            if rng.random() < 0.5:
                                change = self.position(rng.randint(1, 50), rng.randint(70, 140), "add_or_update")
                            else:
                                change = self.position(rng.randint(50, 700), rng.randint(70, 140), "set_position")
                            item_id = f"item-{index}"
                            actions.append(change)
                            result = service.safe_add_items_atomic(
                                [{"item_id": item_id, "changes": [change]}],
                                pending_id=f"pending-random-pos-{trial}",
                                summary=item_id,
                            )
                            item_ops[item_id] = result["operation_ids"][0]

                        rolled: set[int] = set()
                        order = list(range(len(actions)))
                        rng.shuffle(order)
                        for rolled_index in order:
                            service.rollback_operation(item_ops[f"item-{rolled_index}"])
                            rolled.add(rolled_index)
                            quantity = 100.0
                            total_cost = 100.0 * 90.0
                            for index, change in enumerate(actions):
                                if index in rolled:
                                    continue
                                if change["action_type"] == "set_position":
                                    quantity = float(change["quantity"])
                                    total_cost = quantity * float(change["cost_price"])
                                else:
                                    quantity += float(change["quantity"])
                                    total_cost += float(change["quantity"]) * float(change["cost_price"])
                            position = {
                                (item["account"], item["code"], item["currency"]): item
                                for item in service.load_portfolio()["positions"]
                            }[("银河", "002594", "CNY")]
                            self.assertAlmostEqual(position["quantity"], quantity)
                            self.assertAlmostEqual(position["cost_price"], total_cost / quantity)
                    finally:
                        os.chdir(old)

    def test_random_cash_deposit_set_sequences_match_reference_after_each_rollback(self) -> None:
        rng = random.Random(3408)
        for trial in range(30):
            with self.subTest(trial=trial):
                with tempfile.TemporaryDirectory() as td:
                    old = Path.cwd()
                    os.chdir(td)
                    try:
                        service = PortfolioWriteService(Path("data/portfolio.json"))
                        service.safe_set_cash_account("银河", "CNY", 1000)
                        actions: list[dict] = []
                        operation_ids: list[str] = []
                        for index in range(8):
                            action_type = "deposit" if rng.random() < 0.5 else "set_cash"
                            amount = rng.randint(1, 500) if action_type == "deposit" else rng.randint(100, 3000)
                            change = self.cash(action_type, amount)
                            actions.append(change)
                            result = service.safe_add_items_atomic(
                                [{"item_id": f"cash-{index}", "changes": [change]}],
                                pending_id=f"pending-random-cash-{trial}",
                                summary=f"cash-{index}",
                            )
                            operation_ids.append(result["operation_ids"][0])
                        rolled: set[int] = set()
                        order = list(range(len(actions)))
                        rng.shuffle(order)
                        for rolled_index in order:
                            service.rollback_operation(operation_ids[rolled_index])
                            rolled.add(rolled_index)
                            balance = 1000.0
                            for index, change in enumerate(actions):
                                if index in rolled:
                                    continue
                                if change["action_type"] == "set_cash":
                                    balance = float(change["amount"])
                                else:
                                    balance += float(change["amount"])
                            cash = {
                                (item["account"], item["currency"]): item["amount"]
                                for item in service.load_portfolio()["cash_accounts"]
                            }
                            self.assertAlmostEqual(cash[("银河", "CNY")], balance)
                    finally:
                        os.chdir(old)


class DependencyAndStateMachineTest(TempLedgerCase):
    def test_two_sells_each_individually_valid_but_batch_invalid(self) -> None:
        self.service.safe_add_positions([self.position(10, 100)], summary="seed")
        card = self.preview("银河卖出6股比亚迪；银河卖出6股比亚迪")
        self.assertEqual([item["requires_confirmation"] for item in card["items"]], [True, True])
        self.assertFalse(card["can_confirm_all"])
        first = card["items"][0]
        result = asyncio.run(ai_confirm_item(ItemRequest(pending_id=card["pending_id"], item_id=first["item_id"])))
        self.assertEqual(self.position_map()[("银河", "002594", "CNY")]["quantity"], 4)
        self.assertFalse(result["pending"]["items"][1]["requires_confirmation"])
        self.assertIn("available_quantity", result["pending"]["items"][1]["missing_fields"])

    def test_reverse_cash_dependency_can_be_resolved_by_confirming_deposit_first(self) -> None:
        card = self.preview("银河提现100元；银河增加200元")
        self.assertFalse(card["can_confirm_all"])
        self.assertFalse(card["items"][0]["requires_confirmation"])
        self.assertTrue(card["items"][1]["requires_confirmation"])
        deposit = card["items"][1]
        result = asyncio.run(ai_confirm_item(ItemRequest(pending_id=card["pending_id"], item_id=deposit["item_id"])))
        self.assertTrue(result["pending"]["items"][0]["requires_confirmation"])
        withdraw = result["pending"]["items"][0]
        asyncio.run(ai_confirm_item(ItemRequest(pending_id=card["pending_id"], item_id=withdraw["item_id"])))
        self.assertEqual(self.cash_map()[("银河", "CNY")], 100)

    def test_rollback_deposit_is_blocked_when_later_withdraw_consumed_it(self) -> None:
        card = self.preview("银河增加100元；银河提现80元")
        confirmed = asyncio.run(ai_confirm(ConfirmRequest(pending_id=card["pending_id"])))
        deposit = confirmed["pending"]["items"][0]
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_rollback_item(ItemRequest(pending_id=card["pending_id"], item_id=deposit["item_id"])))
        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(self.cash_map()[("银河", "CNY")], 20)

    def test_partial_cancel_survives_reload_without_reviving_open_items(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元")
        first = card["items"][0]
        asyncio.run(ai_confirm_item(ItemRequest(pending_id=card["pending_id"], item_id=first["item_id"])))
        cancelled = asyncio.run(ai_cancel(CancelRequest(pending_id=card["pending_id"])))
        self.assertEqual(cancelled["status"], "partially_cancelled")
        reloaded = asyncio.run(get_pending(card["pending_id"]))
        self.assertEqual(reloaded["status"], "partially_cancelled")
        self.assertEqual([item["status"] for item in reloaded["items"]], ["confirmed", "cancelled"])
        self.assertEqual(reloaded["pending_item_count"], 0)
        self.assertFalse(reloaded["requires_confirmation"])

    def test_full_cancel_survives_reload(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        cancelled = asyncio.run(ai_cancel(CancelRequest(pending_id=card["pending_id"])))
        self.assertEqual(cancelled["status"], "cancelled")
        reloaded = asyncio.run(get_pending(card["pending_id"]))
        self.assertEqual(reloaded["status"], "cancelled")
        self.assertEqual(reloaded["items"][0]["status"], "cancelled")
        self.assertFalse(reloaded["requires_confirmation"])

    def test_partial_expiration_has_distinct_status(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元")
        asyncio.run(ai_confirm_item(ItemRequest(pending_id=card["pending_id"], item_id=card["items"][0]["item_id"])))
        pending = load_pending(card["pending_id"])
        pending["expires_at"] = "2000-01-01T00:00:00"
        save_pending(pending)
        reloaded = asyncio.run(get_pending(card["pending_id"]))
        self.assertEqual(reloaded["status"], "partially_expired")
        self.assertEqual([item["status"] for item in reloaded["items"]], ["confirmed", "expired"])

    def test_confirmed_status_without_operation_is_repaired_to_pending(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        pending = load_pending(card["pending_id"])
        pending["items"][0]["status"] = "confirmed"
        pending["items"][0]["operation_id"] = "fake-operation"
        save_pending(pending)
        repaired = asyncio.run(get_pending(card["pending_id"]))
        self.assertEqual(repaired["items"][0]["status"], "pending")
        self.assertIsNone(repaired["items"][0]["operation_id"])
        self.assertTrue(any("没有对应operation" in warning for warning in repaired["items"][0]["warnings"]))

    def test_duplicate_item_ids_fail_closed(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元")
        pending = load_pending(card["pending_id"])
        pending["items"][1]["item_id"] = pending["items"][0]["item_id"]
        save_pending(pending)
        with self.assertRaisesRegex(RuntimeError, "item_id重复"):
            asyncio.run(get_pending(card["pending_id"]))

    def test_invalid_item_status_fails_closed(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        pending = load_pending(card["pending_id"])
        pending["items"][0]["status"] = "hacked"
        save_pending(pending)
        with self.assertRaisesRegex(RuntimeError, "状态无效"):
            asyncio.run(get_pending(card["pending_id"]))

    def test_item_action_mismatch_is_blocked_before_write(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        pending = load_pending(card["pending_id"])
        pending["items"][0]["action_type"] = "sell"
        save_pending(pending)
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_confirm_item(ItemRequest(
                pending_id=card["pending_id"],
                item_id=card["items"][0]["item_id"],
            )))
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("操作类型", context.exception.detail)
        self.assertEqual(self.service.load_portfolio()["positions"], [])

    def test_cancelled_partial_and_rolled_back_cards_cannot_confirm_again(self) -> None:
        cancelled = self.preview("银河买入10股比亚迪，均价100元")
        asyncio.run(ai_cancel(CancelRequest(pending_id=cancelled["pending_id"])))
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_confirm(ConfirmRequest(pending_id=cancelled["pending_id"])))
        self.assertEqual(context.exception.status_code, 409)

        partial = self.preview("银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元")
        asyncio.run(ai_confirm_item(ItemRequest(
            pending_id=partial["pending_id"], item_id=partial["items"][0]["item_id"],
        )))
        asyncio.run(ai_cancel(CancelRequest(pending_id=partial["pending_id"])))
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_confirm(ConfirmRequest(pending_id=partial["pending_id"])))
        self.assertEqual(context.exception.status_code, 409)

        rolled = self.preview("长桥买入2股腾讯，均价500港币")
        confirmed = asyncio.run(ai_confirm(ConfirmRequest(pending_id=rolled["pending_id"])))
        item = confirmed["pending"]["items"][0]
        asyncio.run(ai_rollback_item(ItemRequest(
            pending_id=rolled["pending_id"], item_id=item["item_id"],
        )))
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_confirm(ConfirmRequest(pending_id=rolled["pending_id"])))
        self.assertEqual(context.exception.status_code, 409)

    def test_fully_confirmed_card_remains_idempotent(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        first = asyncio.run(ai_confirm(ConfirmRequest(pending_id=card["pending_id"])))
        second = asyncio.run(ai_confirm(ConfirmRequest(pending_id=card["pending_id"])))
        self.assertTrue(second["already_confirmed"])
        self.assertEqual(first["operation_ids"], second["operation_ids"])
        self.assertEqual(self.position_map()[("银河", "002594", "CNY")]["quantity"], 10)

    def test_concurrent_double_rollback_has_one_effect_only(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        confirmed = asyncio.run(ai_confirm(ConfirmRequest(pending_id=card["pending_id"])))
        item_id = confirmed["pending"]["items"][0]["item_id"]

        def rollback_once(_: int):
            try:
                return asyncio.run(ai_rollback_item(ItemRequest(
                    pending_id=card["pending_id"], item_id=item_id,
                )))
            except Exception as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(rollback_once, [1, 2]))
        successes = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(successes), 2)
        self.assertTrue(any(result.get("already_rolled_back") for result in successes))
        self.assertEqual(self.service.load_portfolio()["positions"], [])
        rollback_ops = [
            operation for operation in self.service._load_all_operations()
            if operation.get("type") == "rollback"
        ]
        self.assertEqual(len(rollback_ops), 1)

    def test_concurrent_revisions_of_same_item_allow_only_one_winner(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        item_id = card["items"][0]["item_id"]
        barrier = threading.Barrier(2)
        original = portfolio_ai_module.apply_direct_field_revision

        def delayed_revision(pending: dict, message: str):
            result = original(pending, message)
            barrier.wait(timeout=5)
            return result

        def revise(message: str):
            try:
                return asyncio.run(ai_revise_item(ItemReviseRequest(
                    pending_id=card["pending_id"], item_id=item_id, message=message,
                )))
            except Exception as exc:  # returned for assertion below
                return exc

        with patch.object(portfolio_ai_module, "apply_direct_field_revision", side_effect=delayed_revision):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(revise, ["数量20", "数量30"]))

        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, HTTPException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].status_code, 409)
        final = asyncio.run(get_pending(card["pending_id"]))
        self.assertIn(final["items"][0]["changes"][0]["quantity"], {20, 30})

    def test_confirm_and_revise_race_never_creates_untracked_state(self) -> None:
        card = self.preview("银河买入10股比亚迪，均价100元")
        item_id = card["items"][0]["item_id"]

        async def race():
            return await asyncio.gather(
                ai_confirm_item(ItemRequest(pending_id=card["pending_id"], item_id=item_id)),
                ai_revise_item(ItemReviseRequest(pending_id=card["pending_id"], item_id=item_id, message="数量20")),
                return_exceptions=True,
            )

        results = asyncio.run(race())
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, HTTPException) for result in results), 1)
        final = asyncio.run(get_pending(card["pending_id"]))
        position_count = len(self.service.load_portfolio()["positions"])
        if final["items"][0]["status"] == "confirmed":
            self.assertEqual(position_count, 1)
            self.assertEqual(final["items"][0]["changes"][0]["quantity"], 10)
        else:
            self.assertEqual(position_count, 0)
            self.assertEqual(final["items"][0]["changes"][0]["quantity"], 20)


class ParserAndBatchFuzzTest(TempLedgerCase):
    def test_action_inheritance_resets_when_new_explicit_action_appears(self) -> None:
        self.service.safe_add_positions([self.position(100, 90)], summary="seed")
        parsed = parse_bookkeeping_message(
            "银河买入10股比亚迪，均价100元；"
            "5股比亚迪，均价110元；"
            "卖出3股比亚迪；"
            "再2股比亚迪"
        )
        self.assertEqual(
            [change["action_type"] for change in parsed["changes"]],
            ["add_or_update", "add_or_update", "sell", "sell"],
        )

    def test_mixed_action_and_cash_batch_preserves_item_order(self) -> None:
        parsed = parse_bookkeeping_message(
            "银河增加100元；"
            "银河买入10股比亚迪，均价100元；"
            "银河现金余额改为500元；"
            "银河卖出2股比亚迪"
        )
        self.assertEqual(
            [spec["action_type"] for spec in parsed["item_specs"]],
            ["deposit", "add_or_update", "set_cash", "sell"],
        )

    def test_cash_set_phrase_matrix(self) -> None:
        accounts = ["银河", "长桥", "IBKR", "尊嘉", "华盛通"]
        currencies = [("元", "CNY"), ("人民币", "CNY"), ("美元", "USD"), ("港币", "HKD")]
        verbs = ["余额是", "现金余额是", "余额改为", "现金设为"]
        values = ["100", "1,000.50", "2.2w", "5k", "３００"]
        for account in accounts:
            for token, expected_currency in currencies:
                for verb in verbs:
                    for value in values:
                        forms = [f"{account}{verb}{value}{token}"]
                        if token != "元":
                            forms.append(f"{account}{token}{verb}{value}")
                        for text in forms:
                            with self.subTest(text=text):
                                parsed = parse_bookkeeping_message(text)
                                self.assertEqual(parsed["action_type"], "set_cash")
                                self.assertEqual(parsed["changes"][0]["currency"], expected_currency)
                                self.assertEqual(parsed["missing_fields"], [])

    def test_fx_punctuation_phrase_matrix(self) -> None:
        accounts = ["长桥", "IBKR", "尊嘉", "华盛通", "银河"]
        pairs = [
            ("500港币", "20美元", "HKD", "USD"),
            ("100美元", "720人民币", "USD", "CNY"),
            ("1000人民币", "1100港币", "CNY", "HKD"),
        ]
        verbs = ["换成", "换为", "兑换成", "兑成", "换到"]
        separators = ["", " ", "，", "、"]
        for account in accounts:
            for source, target, source_currency, target_currency in pairs:
                for verb in verbs:
                    for left in separators:
                        for right in separators:
                            for prefix in ("", "把"):
                                text = f"{account}{prefix}{source}{left}{verb}{right}{target}"
                                with self.subTest(text=text):
                                    parsed = parse_bookkeeping_message(text)
                                    self.assertEqual(parsed["action_type"], "fx_exchange")
                                    self.assertEqual(len(parsed["changes"]), 2)
                                    self.assertEqual(
                                        [change["currency"] for change in parsed["changes"]],
                                        [source_currency, target_currency],
                                    )
                                    self.assertEqual(parsed["missing_fields"], [])

    def test_large_canonical_batch_has_unique_item_ids_and_stable_order(self) -> None:
        lines = []
        for index in range(1, 81):
            code = f"{600000 + index:06d}"
            lines.append(
                f"记账{index}：账户=银河；操作=买入；标的=股票{index}；代码={code}；币种=CNY；数量={index}；成本价={10 + index / 10}"
            )
        card = self.preview("\n".join(lines))
        self.assertEqual(len(card["items"]), 80)
        self.assertEqual(len({item["item_id"] for item in card["items"]}), 80)
        self.assertEqual(card["items"][0]["changes"][0]["code"], "600001")
        self.assertEqual(card["items"][-1]["changes"][0]["code"], "600080")
        self.assertTrue(card["can_confirm_all"])

    def test_valid_numeric_format_permutations_never_produce_nonfinite_values(self) -> None:
        rng = random.Random(20260712)
        templates = [
            "银河买入{q}股比亚迪，均价{p}元",
            "银河增加{q}元",
            "银河现金余额是{q}元",
        ]
        for _ in range(500):
            quantity = rng.uniform(0.01, 999999)
            price = rng.uniform(0, 99999)
            q = f"{quantity:,.4f}" if rng.random() < 0.5 else f"{quantity:.4f}"
            p = f"{price:,.4f}" if rng.random() < 0.5 else f"{price:.4f}"
            text = rng.choice(templates).format(q=q, p=p)
            parsed = parse_bookkeeping_message(text)
            for change in parsed.get("changes", []):
                for field in ("quantity", "cost_price", "total_cost", "amount"):
                    value = change.get(field)
                    if value is not None:
                        self.assertTrue(math.isfinite(float(value)), (text, field, value))

    def test_malformed_numeric_corpus_never_becomes_confirmable(self) -> None:
        malformed = [
            "NaN", "nan", "INF", "Infinity", "-Infinity", "1e309", "1e6",
            "1,00", "1,000,00", "1.2.3", "--1", "+-1", "1万2千", "零点一",
            "0x10", "1_000", "1/2", "..1", "1,", "1,,000", "1, 000",
        ]
        templates = [
            "银河买入{value}股比亚迪，均价100元",
            "银河买入10股比亚迪，均价{value}元",
            "银河增加{value}元",
            "银河现金余额是{value}元",
        ]
        accidental: list[str] = []
        for value in malformed:
            for template in templates:
                text = template.format(value=value)
                parsed = parse_bookkeeping_message(text)
                if parsed.get("intent") == "bookkeeping" and not parsed.get("missing_fields"):
                    accidental.append(text)
        self.assertEqual(accidental, [])

    def test_action_number_punctuation_is_accepted(self) -> None:
        cases = [
            ("银河买入，10股比亚迪，均价100元", "add_or_update"),
            ("银河买入、10股比亚迪，均价、100元", "add_or_update"),
            ("银河增加，100元", "deposit"),
            ("银河提现、100元", "withdraw"),
            ("银河余额改为，100元", "set_cash"),
            ("长桥500港币，换成，20美元", "fx_exchange"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                parsed = parse_bookkeeping_message(text)
                self.assertEqual(parsed["action_type"], expected)
                self.assertNotIn("invalid_number", parsed.get("missing_fields", []))

    def test_future_negated_and_question_corpus_never_creates_writable_card(self) -> None:
        subjects = [
            "银河买入10股比亚迪，均价100元",
            "银河增加100元",
            "IBKR卖出1股苹果",
            "长桥500港币换成60美元",
        ]
        wrappers = [
            "明天准备{}", "以后可能{}", "我计划{}", "不要{}", "并没有{}", "没有{}",
            "怎么{}", "能不能{}？", "如果{}会怎样", "你觉得{}好吗", "考虑{}",
        ]
        accidental: list[str] = []
        for subject in subjects:
            for wrapper in wrappers:
                text = wrapper.format(subject)
                parsed = parse_bookkeeping_message(text)
                if parsed.get("intent") == "bookkeeping" and not parsed.get("missing_fields"):
                    accidental.append(text)
        self.assertEqual(accidental, [])

    def test_repeated_previews_generate_unique_pending_and_item_ids(self) -> None:
        pending_ids: set[str] = set()
        item_ids: set[str] = set()
        for _ in range(200):
            card = self.preview("银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元")
            self.assertNotIn(card["pending_id"], pending_ids)
            pending_ids.add(card["pending_id"])
            for item in card["items"]:
                self.assertNotIn(item["item_id"], item_ids)
                item_ids.add(item["item_id"])
        self.assertEqual(len(pending_ids), 200)
        self.assertEqual(len(item_ids), 400)


class HttpRouteWorkflowTest(TempLedgerCase):
    def test_http_preview_revise_confirm_and_rollback_flow(self) -> None:
        app = FastAPI()
        app.include_router(portfolio_ai_module.router, prefix="/api/portfolio")

        async def run_flow() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                preview_response = await client.post("/api/portfolio/ai-preview", json={
                    "message": "银河买入10股比亚迪，均价100元；IBKR买入2股苹果，均价200美元",
                    "input_type": "text",
                })
                self.assertEqual(preview_response.status_code, 200, preview_response.text)
                card = preview_response.json()
                self.assertEqual(len(card["items"]), 2)

                second = card["items"][1]
                revise_response = await client.post("/api/portfolio/ai-revise-item", json={
                    "pending_id": card["pending_id"],
                    "item_id": second["item_id"],
                    "message": "成本价改成201",
                })
                self.assertEqual(revise_response.status_code, 200, revise_response.text)
                revised = revise_response.json()
                self.assertEqual(revised["items"][0]["changes"][0]["cost_price"], 100)
                self.assertEqual(revised["items"][1]["changes"][0]["cost_price"], 201)

                first = revised["items"][0]
                confirm_one = await client.post("/api/portfolio/ai-confirm-item", json={
                    "pending_id": card["pending_id"],
                    "item_id": first["item_id"],
                })
                self.assertEqual(confirm_one.status_code, 200, confirm_one.text)
                partial = confirm_one.json()["pending"]
                self.assertEqual([item["status"] for item in partial["items"]], ["confirmed", "pending"])

                confirm_rest = await client.post("/api/portfolio/ai-confirm", json={
                    "pending_id": card["pending_id"],
                })
                self.assertEqual(confirm_rest.status_code, 200, confirm_rest.text)
                confirmed = confirm_rest.json()["pending"]
                self.assertEqual(confirmed["status"], "confirmed")

                rollback_response = await client.post("/api/portfolio/ai-rollback-item", json={
                    "pending_id": card["pending_id"],
                    "item_id": first["item_id"],
                })
                self.assertEqual(rollback_response.status_code, 200, rollback_response.text)
                rolled = rollback_response.json()["pending"]
                self.assertEqual(rolled["items"][0]["status"], "rolled_back")
                self.assertEqual(rolled["items"][1]["status"], "confirmed")

                get_response = await client.get(f"/api/portfolio/pending/{card['pending_id']}")
                self.assertEqual(get_response.status_code, 200, get_response.text)
                self.assertEqual(get_response.json()["status"], "partially_rolled_back")

        asyncio.run(run_flow())
        positions = self.position_map()
        self.assertNotIn(("银河", "002594", "CNY"), positions)
        self.assertEqual(positions[("IBKR", "AAPL", "USD")]["quantity"], 2)
        self.assertAlmostEqual(positions[("IBKR", "AAPL", "USD")]["cost_price"], 201)

    def test_http_invalid_and_terminal_requests_return_4xx(self) -> None:
        app = FastAPI()
        app.include_router(portfolio_ai_module.router, prefix="/api/portfolio")

        async def run_flow() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                malformed = await client.post("/api/portfolio/ai-preview", json={"message": 123})
                self.assertEqual(malformed.status_code, 422)
                missing = await client.get("/api/portfolio/pending/not-a-real-pending-id")
                self.assertEqual(missing.status_code, 404)

                card = (await client.post("/api/portfolio/ai-preview", json={
                    "message": "银河买入10股比亚迪，均价100元",
                    "input_type": "text",
                })).json()
                cancel = await client.post("/api/portfolio/ai-cancel", json={"pending_id": card["pending_id"]})
                self.assertEqual(cancel.status_code, 200)
                confirm = await client.post("/api/portfolio/ai-confirm", json={"pending_id": card["pending_id"]})
                self.assertEqual(confirm.status_code, 409)

        asyncio.run(run_flow())
        self.assertEqual(self.service.load_portfolio()["positions"], [])


class AtomicFailureInjectionTest(TempLedgerCase):
    def test_second_operation_log_failure_restores_exact_ledger_and_removes_partial_logs(self) -> None:
        before = deepcopy(self.service.load_portfolio())
        original_create = self.service.create_operation
        call_count = 0

        def fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("injected operation log failure")
            return original_create(*args, **kwargs)

        with patch.object(self.service, "create_operation", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                self.service.safe_add_items_atomic([
                    {"item_id": "first", "changes": [self.position(10, 100)]},
                    {"item_id": "second", "changes": [
                        self.position(2, 200, account="IBKR", code="AAPL", name="Apple", currency="USD")
                    ]},
                ], pending_id="pending-injected-failure", summary="failure injection")

        after = self.service.load_portfolio()
        self.assertEqual(after.get("positions"), before.get("positions"))
        self.assertEqual(after.get("cash_accounts"), before.get("cash_accounts"))
        self.assertEqual(list(Path("data/operations").glob("*.json")), [])


class FrontendStateContractTest(unittest.TestCase):
    def test_terminal_partial_statuses_are_not_treated_as_active_cards(self) -> None:
        source = Path("frontend/src/components/ChatPanel.tsx").read_text(encoding="utf-8")
        for status in ("partially_rolled_back", "partially_expired", "partially_cancelled", "partially_closed"):
            self.assertIn(status, source)
        self.assertIn("cancelled: '已取消'", source)


if __name__ == "__main__":
    unittest.main()
