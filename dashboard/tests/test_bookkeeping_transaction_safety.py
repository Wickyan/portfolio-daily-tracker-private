from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.api.portfolio_ai import (
    CancelRequest,
    ConfirmRequest,
    ReviseRequest,
    ai_cancel,
    ai_confirm,
    ai_revise,
    get_pending,
    load_pending,
    make_pending,
    parse_bookkeeping_message,
    save_pending,
)
from backend.services.portfolio_write_service import PortfolioWriteService


class BookkeepingTransactionSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_cwd = Path.cwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        self.service = PortfolioWriteService(Path("data/portfolio.json"))

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temp_dir.cleanup()

    def create_pending(self, message: str) -> dict:
        parsed = parse_bookkeeping_message(message)
        pending = make_pending(parsed, "text", message)
        save_pending(pending)
        return pending

    def cash_amount(self, account: str, currency: str) -> float:
        for item in self.service.load_portfolio().get("cash_accounts", []):
            if item["account"] == account and item["currency"] == currency:
                return item["amount"]
        return 0.0

    def test_confirm_is_idempotent_and_does_not_double_deposit(self) -> None:
        pending = self.create_pending("银河增加100美元")
        first = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        second = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))

        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertTrue(second["already_confirmed"])
        self.assertEqual(self.cash_amount("银河", "USD"), 100)
        matching = [
            operation
            for operation in self.service._load_all_operations()
            if operation.get("type") == "ai_confirm"
            and operation.get("pending_action", {}).get("pending_id") == pending["pending_id"]
        ]
        self.assertEqual(len(matching), 1)

    def test_concurrent_confirm_requests_apply_once(self) -> None:
        pending = self.create_pending("银河增加2.2w港币")

        async def run_both():
            return await asyncio.gather(
                ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])),
                ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])),
            )

        first, second = asyncio.run(run_both())
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(self.cash_amount("银河", "HKD"), 22000)

    def test_retry_recovers_existing_operation_after_pending_status_interruption(self) -> None:
        pending = self.create_pending("银河增加500美元")
        operation = self.service.safe_add_positions(
            pending["changes"],
            summary=pending["summary"],
            pending_action=pending,
        )
        self.assertEqual(load_pending(pending["pending_id"])["status"], "pending")

        recovered = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertTrue(recovered["already_confirmed"])
        self.assertEqual(recovered["operation_id"], operation["operation_id"])
        self.assertEqual(self.cash_amount("银河", "USD"), 500)
        self.assertEqual(load_pending(pending["pending_id"])["status"], "confirmed")

    def test_confirmed_pending_cannot_be_cancelled(self) -> None:
        pending = self.create_pending("银河增加100元")
        asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_cancel(CancelRequest(pending_id=pending["pending_id"])))
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(load_pending(pending["pending_id"])["status"], "confirmed")

    def test_cancel_is_idempotent(self) -> None:
        pending = self.create_pending("银河增加100元")
        first = asyncio.run(ai_cancel(CancelRequest(pending_id=pending["pending_id"])))
        second = asyncio.run(ai_cancel(CancelRequest(pending_id=pending["pending_id"])))
        self.assertEqual(first["status"], "cancelled")
        self.assertTrue(second["already_cancelled"])

    def test_expired_pending_is_closed_on_read_and_cannot_confirm(self) -> None:
        pending = self.create_pending("银河增加100元")
        pending["expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
        save_pending(pending)

        public = asyncio.run(get_pending(pending["pending_id"]))
        self.assertEqual(public["status"], "expired")
        self.assertFalse(public["requires_confirmation"])
        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.cash_amount("银河", "CNY"), 0)

    def test_only_one_successor_can_be_created_for_same_revision(self) -> None:
        pending = self.create_pending("银河增加100元")

        async def revise_twice():
            return await asyncio.gather(
                ai_revise(ReviseRequest(pending_id=pending["pending_id"], message="港币")),
                ai_revise(ReviseRequest(pending_id=pending["pending_id"], message="美元")),
                return_exceptions=True,
            )

        results = asyncio.run(revise_twice())
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, HTTPException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].status_code, 409)
        original = load_pending(pending["pending_id"])
        self.assertEqual(original["status"], "superseded")
        self.assertEqual(original["revised_to_pending_id"], successes[0]["pending_id"])

    def test_currency_revision_rechecks_cash_availability(self) -> None:
        self.service.safe_set_cash_account("银河", "CNY", 5000)
        pending = self.create_pending("银河人民币减少1000")
        self.assertTrue(pending["requires_confirmation"])

        revised = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="港币",
        )))
        self.assertEqual(revised["changes"][0]["currency"], "HKD")
        self.assertIn("available_cash", revised["missing_fields"])
        self.assertFalse(revised["requires_confirmation"])

    def test_malformed_ledger_fails_closed_without_overwrite(self) -> None:
        path = Path("data/portfolio.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b'{"positions": ['
        path.write_bytes(original)

        with self.assertRaisesRegex(RuntimeError, "账本损坏"):
            self.service.load_portfolio()
        with self.assertRaisesRegex(RuntimeError, "账本损坏"):
            self.service.safe_set_cash_account("银河", "CNY", 100)
        self.assertEqual(path.read_bytes(), original)

    def test_duplicate_cash_identity_fails_closed(self) -> None:
        path = Path("data/portfolio.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "positions": [],
            "cash": 0,
            "cash_accounts": [
                {"account": "IBKR", "currency": "USD", "amount": 1},
                {"account": "IBKR", "currency": "USD", "amount": 2},
            ],
        }), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "重复现金账户"):
            self.service.load_portfolio()

    def test_operation_log_failure_restores_before_snapshot(self) -> None:
        before = self.service.load_portfolio()
        self.service.save_portfolio_atomic(before, preserve_updated_at=True)
        before = self.service.load_portfolio()
        with patch.object(self.service, "create_operation", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.service.safe_set_cash_account("银河", "CNY", 100)
        self.assertEqual(self.service.load_portfolio(), before)

    def test_concurrent_cash_writes_are_serialized(self) -> None:
        def deposit_once(_: int) -> None:
            PortfolioWriteService(Path("data/portfolio.json")).safe_add_positions([{
                "action_type": "deposit",
                "account": "IBKR",
                "currency": "USD",
                "amount": 1,
            }], summary="parallel deposit")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(deposit_once, range(20)))
        self.assertEqual(self.cash_amount("IBKR", "USD"), 20)

    def test_invalid_position_numbers_are_rejected_without_mutation(self) -> None:
        position = {
            "account": "IBKR",
            "name": "Apple",
            "code": "AAPL",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": 1,
            "cost_price": 100,
        }
        self.service.safe_add_positions([position], summary="seed")
        before = self.service.load_portfolio()

        with self.assertRaisesRegex(ValueError, "数量必须大于0"):
            self.service.safe_update_position("IBKR", "AAPL", "USD", {"quantity": -1})
        with self.assertRaisesRegex(ValueError, "有限数字"):
            self.service.safe_update_position("IBKR", "AAPL", "USD", {"cost_price": float("nan")})
        with self.assertRaisesRegex(ValueError, "positive_quantity"):
            self.service.safe_add_positions([{**position, "quantity": 0}], summary="invalid add")
        self.assertEqual(self.service.load_portfolio(), before)

    def test_operation_id_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.service.load_operation("../secret")



    def test_common_unsupported_currency_is_identified_not_treated_as_missing(self) -> None:
        parsed = parse_bookkeeping_message("IBKR入金100欧元")
        self.assertEqual(parsed["changes"][0]["currency"], "EUR")
        self.assertIn("unsupported_currency", parsed["missing_fields"])
        self.assertNotIn("currency", parsed["missing_fields"])

    def test_unsupported_currency_is_rejected_before_write(self) -> None:
        before = self.service.load_portfolio()
        with self.assertRaisesRegex(ValueError, "有效的account/currency/amount"):
            self.service.safe_add_positions([{
                "action_type": "deposit",
                "account": "IBKR",
                "currency": "EUR",
                "amount": 100,
            }], summary="unsupported cash currency")
        with self.assertRaisesRegex(ValueError, "unsupported_currency"):
            self.service.safe_add_positions([{
                "account": "IBKR",
                "name": "Test",
                "code": "TEST",
                "currency": "EUR",
                "asset_type": "stock",
                "quantity": 1,
                "cost_price": 10,
            }], summary="unsupported position currency")
        after = self.service.load_portfolio()
        self.assertEqual(after["positions"], before["positions"])
        self.assertEqual(after["cash_accounts"], before["cash_accounts"])
        self.assertEqual(after["cash"], before["cash"])

    def test_invalid_stored_cash_amount_fails_closed(self) -> None:
        path = Path("data/portfolio.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "positions": [],
            "cash": 0,
            "cash_accounts": [
                {"account": "IBKR", "currency": "USD", "amount": "nan"},
            ],
        }), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "无效amount"):
            self.service.load_portfolio()

    def test_corrupted_operation_log_fails_closed(self) -> None:
        operations = Path("data/operations")
        operations.mkdir(parents=True, exist_ok=True)
        (operations / "broken.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "operation日志损坏"):
            self.service.list_operations()

    def test_nonpositive_quote_does_not_overwrite_last_price(self) -> None:
        self.service.safe_add_positions([{
            "account": "IBKR",
            "name": "Apple",
            "code": "AAPL",
            "currency": "USD",
            "asset_type": "stock",
            "quantity": 1,
            "cost_price": 100,
            "current_price": 120,
        }], summary="seed quote")
        self.service.safe_update_prices({("IBKR", "AAPL", "USD"): 0})
        self.assertEqual(self.service.load_portfolio()["positions"][0]["current_price"], 120)

    def test_get_pending_reconciles_rolled_back_operation(self) -> None:
        pending = self.create_pending("银河增加100美元")
        confirmed = asyncio.run(ai_confirm(ConfirmRequest(pending_id=pending["pending_id"])))
        self.service.rollback_operation(confirmed["operation_id"])

        # Simulate a stale pending file left behind by an interrupted status update.
        stale = load_pending(pending["pending_id"])
        stale["status"] = "confirmed"
        stale["requires_confirmation"] = False
        save_pending(stale)

        public = asyncio.run(get_pending(pending["pending_id"]))
        self.assertEqual(public["status"], "rolled_back")
        self.assertFalse(public["requires_confirmation"])



    def test_revision_retry_returns_same_successor(self) -> None:
        pending = self.create_pending("银河增加100元")
        first = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="港币",
        )))
        retry = asyncio.run(ai_revise(ReviseRequest(
            pending_id=pending["pending_id"],
            message="港币",
        )))
        self.assertEqual(retry["pending_id"], first["pending_id"])
        self.assertEqual(retry["changes"][0]["currency"], "HKD")

        with self.assertRaises(HTTPException) as context:
            asyncio.run(ai_revise(ReviseRequest(
                pending_id=pending["pending_id"],
                message="美元",
            )))
        self.assertEqual(context.exception.status_code, 409)

    def test_duplicate_confirm_operations_fail_closed(self) -> None:
        pending = self.create_pending("银河增加100元")
        self.service.safe_add_positions(
            pending["changes"],
            summary="first duplicate",
            pending_action=pending,
        )
        self.service.create_operation(
            operation_type="ai_confirm",
            summary="second duplicate",
            before_snapshot=self.service.load_portfolio(),
            after_snapshot=self.service.load_portfolio(),
            pending_action=pending,
        )
        with self.assertRaisesRegex(RuntimeError, "重复确认operation"):
            self.service.find_confirm_operation_by_pending_id(pending["pending_id"])



if __name__ == "__main__":
    unittest.main()
