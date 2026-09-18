import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backend.ledger import (
    LedgerWriteService,
    ReplayError,
    Transaction,
    TransactionRepository,
    TransactionType,
)


def buy(date, qty, price, *, entered_at="2026-09-18T01:30:00+08:00"):
    return Transaction(
        event_type=TransactionType.BUY,
        effective_at=date,
        entered_at=entered_at,
        account="IBKR",
        code="AAPL",
        currency="USD",
        quantity=qty,
        price=price,
    )


def sell(date, qty, price):
    return Transaction(
        event_type=TransactionType.SELL,
        effective_at=date,
        account="IBKR",
        code="AAPL",
        currency="USD",
        quantity=qty,
        price=price,
    )


class AtomicRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TransactionRepository(Path(self.tmp.name) / "ledger.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_many_is_atomic_on_duplicate_external_id(self):
        first = buy("2025-01-01", 1, 100)
        first = Transaction(**{**first.__dict__, "external_trade_id": "same-fill"})
        second = buy("2025-02-01", 1, 110)
        second = Transaction(**{**second.__dict__, "external_trade_id": "same-fill"})
        with self.assertRaises(ValueError):
            self.repo.append_many([first, second])
        self.assertEqual(self.repo.list_transactions(), [])

    def test_append_many_persists_whole_valid_batch(self):
        stored = self.repo.append_many(
            [buy("2025-01-01", 1, 100), buy("2025-02-01", 2, 120)]
        )
        self.assertEqual(len(stored), 2)
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_backup_database_is_consistent_snapshot(self):
        self.repo.append(buy("2025-01-01", 1, 100))
        backup = self.repo.backup_database()
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        with sqlite3.connect(str(backup)) as conn:
            self.assertEqual(conn.execute("select count(*) from transactions").fetchone()[0], 1)
            self.assertEqual(conn.execute("pragma integrity_check").fetchone()[0], "ok")

    def test_precommit_validator_failure_rolls_back_entire_batch(self):
        def reject(existing, proposed):
            self.assertEqual(existing, [])
            self.assertEqual(len(proposed), 2)
            raise ReplayError("reject batch")

        with self.assertRaises(ReplayError):
            self.repo.append_many_atomic(
                [buy("2025-01-01", 1, 100), buy("2025-02-01", 1, 110)],
                precommit_validator=reject,
            )
        self.assertEqual(self.repo.list_transactions(), [])


class LedgerWriteServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TransactionRepository(Path(self.tmp.name) / "ledger.sqlite3")
        self.service = LedgerWriteService(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_does_not_persist(self):
        preview = self.service.preview([buy("2025-01-01", 2, 100)])
        self.assertEqual(
            preview.projected_state.get_position("IBKR", "AAPL", "USD").quantity,
            Decimal("2"),
        )
        self.assertEqual(self.repo.list_transactions(), [])

    def test_backup_failure_prevents_confirm_write(self):
        original = self.repo.backup_database
        self.repo.backup_database = lambda: (_ for _ in ()).throw(RuntimeError("backup failed"))
        try:
            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                self.service.confirm([buy("2025-01-01", 2, 100)])
        finally:
            self.repo.backup_database = original
        self.assertEqual(self.repo.list_transactions(), [])

    def test_confirm_persists_only_after_full_history_is_valid(self):
        self.service.confirm([buy("2025-01-01", 2, 100)])
        with self.assertRaises(ReplayError):
            self.service.confirm([sell("2025-02-01", 3, 120)])
        rows = self.repo.list_transactions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_type, TransactionType.BUY)

    def test_invalid_historical_backfill_cannot_corrupt_later_valid_history(self):
        self.service.confirm([buy("2025-02-01", 2, 100)])
        with self.assertRaises(ReplayError):
            self.service.confirm([sell("2025-01-01", 1, 120)])
        rows = self.repo.list_transactions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_type, TransactionType.BUY)

    def test_valid_backfill_is_detected_and_recalculates_state(self):
        self.service.confirm([buy("2025-02-01", 1, 200)])
        preview = self.service.confirm([buy("2024-02-01", 1, 100)])
        self.assertTrue(preview.historical_backfill)
        pos = preview.projected_state.get_position("IBKR", "AAPL", "USD")
        self.assertEqual(pos.quantity, Decimal("2"))
        self.assertEqual(pos.average_cost, Decimal("150"))


class PendingLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TransactionRepository(Path(self.tmp.name) / "ledger.sqlite3")
        self.service = LedgerWriteService(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_preview_does_not_write_transactions(self):
        pending = self.service.create_pending([buy("2025-01-01", 2, 100)])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(self.repo.list_transactions(), [])
        self.assertEqual(len(pending["events"]), 1)

    def test_pending_confirm_creates_prewrite_backup(self):
        pending = self.service.create_pending([buy("2025-01-01", 2, 100)])
        self.service.confirm_pending(pending["pending_id"])
        backups = sorted((Path(self.tmp.name) / "ledger_backups").glob("ledger.sqlite3.bak-*"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(str(backups[0])) as conn:
            self.assertEqual(conn.execute("select count(*) from transactions").fetchone()[0], 0)

    def test_confirm_pending_writes_once_and_marks_confirmed(self):
        pending = self.service.create_pending([buy("2025-01-01", 2, 100)])
        preview = self.service.confirm_pending(pending["pending_id"])
        self.assertEqual(len(self.repo.list_transactions()), 1)
        self.assertEqual(
            preview.projected_state.get_position("IBKR", "AAPL", "USD").quantity,
            Decimal("2"),
        )
        stored = self.repo.get_pending_batch(pending["pending_id"])
        self.assertEqual(stored["status"], "confirmed")
        with self.assertRaises(ValueError):
            self.service.confirm_pending(pending["pending_id"])
        self.assertEqual(len(self.repo.list_transactions()), 1)

    def test_confirm_revalidates_after_history_changes(self):
        self.service.confirm([buy("2025-01-01", 2, 100)])
        pending = self.service.create_pending([sell("2025-03-01", 2, 120)])
        self.service.confirm([sell("2025-02-01", 1, 110)])
        with self.assertRaises(ReplayError):
            self.service.confirm_pending(pending["pending_id"])
        rows = self.repo.list_transactions()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            self.repo.get_pending_batch(pending["pending_id"])["status"],
            "pending",
        )

    def test_expired_pending_never_writes(self):
        pending = self.service.create_pending(
            [buy("2025-01-01", 2, 100)], ttl_seconds=-1
        )
        with self.assertRaisesRegex(ValueError, "expired"):
            self.service.confirm_pending(pending["pending_id"])
        self.assertEqual(self.repo.list_transactions(), [])
        self.assertEqual(
            self.repo.get_pending_batch(pending["pending_id"])["status"],
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
