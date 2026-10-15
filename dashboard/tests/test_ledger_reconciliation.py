import tempfile
import unittest
from pathlib import Path

from backend.ledger import Transaction, TransactionRepository, TransactionType
from backend.ledger.reconciliation import (
    _normalize_position,
    classify_ocr_text,
    compare_snapshot_to_ledger,
)


class ReconciliationClassificationTest(unittest.TestCase):
    def test_trade_history_is_classified_without_llm(self):
        text = "订单查询\n历史订单\n方向/状态\n已成交\n已撤单"
        self.assertEqual(classify_ocr_text(text), "trade_history")

    def test_holdings_snapshot_is_classified_without_llm(self):
        text = "总资产\n总市值\n当前持仓\n成本\n市值\n持仓盈亏"
        self.assertEqual(classify_ocr_text(text), "holdings_snapshot")

    def test_unknown_image_is_not_guessed(self):
        self.assertEqual(classify_ocr_text("欢迎回来\n消息中心"), "unknown")


class ReconciliationComparisonTest(unittest.TestCase):
    def setUp(self):
        self.transactions = [
            Transaction(
                event_type=TransactionType.OPENING_POSITION,
                effective_at="2026-10-01",
                account="长桥",
                code="NVDA",
                name="NVIDIA",
                currency="USD",
                quantity="5",
                price="200",
            ),
            Transaction(
                event_type=TransactionType.OPENING_CASH,
                effective_at="2026-10-01",
                account="长桥",
                currency="USD",
                amount="8.05",
            ),
        ]

    def snapshot(self, quantity="5", cost="200"):
        return {
            "captured_at": "2026-10-16T01:00:00+08:00",
            "account": "长桥",
            "positions": [{
                "code": "NVDA",
                "name": "NVIDIA",
                "currency": "USD",
                "quantity": quantity,
                "average_cost": cost,
            }],
            "cash": [{"currency": "USD", "amount": "8.05"}],
        }

    def test_exact_snapshot_matches_replay(self):
        result = compare_snapshot_to_ledger(self.snapshot(), self.transactions)
        self.assertEqual(result["status"], "match")
        self.assertEqual(result["summary"]["differences"], 0)
        self.assertEqual(result["positions"][0]["status"], "match")
        self.assertEqual(result["cash"][0]["status"], "match")

    def test_quantity_mismatch_is_reported_not_applied(self):
        result = compare_snapshot_to_ledger(self.snapshot(quantity="4"), self.transactions)
        row = result["positions"][0]
        self.assertEqual(result["status"], "differences")
        self.assertEqual(row["status"], "quantity_mismatch")
        self.assertEqual(row["snapshot_quantity"], "4")
        self.assertEqual(row["ledger_quantity"], "5")
        self.assertEqual(row["quantity_difference"], "-1")


class ReconciliationMathGuardTest(unittest.TestCase):
    def test_inconsistent_quantity_is_demoted_to_unknown(self):
        position, warnings = _normalize_position({
            "code": "AMZN",
            "name": "AMZN",
            "quantity": "7",
            "average_cost": "245.00",
            "current_price": "253.32",
            "market_value": None,
            "currency": "USD",
            "unrealized_pnl": "8.29",
            "confidence": 0.9,
        })
        self.assertIsNotNone(position)
        self.assertIsNone(position["quantity"])
        self.assertTrue(any("未实现盈亏不一致" in item for item in warnings))
        self.assertTrue(any("约为1.00" in item for item in warnings))


class ReconciliationRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TransactionRepository(Path(self.tmp.name) / "ledger.sqlite3")
        self.repo.append(
            Transaction(
                event_type=TransactionType.OPENING_POSITION,
                effective_at="2026-10-01",
                account="长桥",
                code="NVDA",
                currency="USD",
                quantity="5",
                price="200",
            )
        )

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self):
        return {
            "captured_at": "2026-10-16T01:00:00+08:00",
            "account": "长桥",
            "source": "screenshot_reconciliation",
            "source_hash": "a" * 64,
            "filename": "holdings.jpg",
            "model_label": "fake",
            "positions": [{
                "code": "NVDA",
                "currency": "USD",
                "quantity": "5",
                "average_cost": "200",
            }],
            "cash": [],
            "metadata": {},
        }

    def test_snapshot_is_stored_separately_from_transactions(self):
        before = len(self.repo.list_transactions())
        saved = self.repo.save_reconciliation_snapshot(self.payload())
        after = len(self.repo.list_transactions())
        self.assertEqual(before, after)
        self.assertEqual(saved["account"], "长桥")
        self.assertEqual(len(self.repo.list_reconciliation_snapshots()), 1)

    def test_duplicate_account_and_source_hash_is_rejected(self):
        self.repo.save_reconciliation_snapshot(self.payload())
        with self.assertRaisesRegex(ValueError, "duplicate reconciliation snapshot"):
            self.repo.save_reconciliation_snapshot(self.payload())


if __name__ == "__main__":
    unittest.main()
