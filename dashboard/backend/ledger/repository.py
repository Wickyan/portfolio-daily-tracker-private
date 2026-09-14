"""Append-only SQLite repository for V3 transaction events."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import List, Optional

from .models import Transaction, TransactionType


def _decimal_text(value):
    return None if value is None else str(value)


DEFAULT_LEDGER_PATH = Path("data") / "ledger.sqlite3"


class TransactionRepository:
    def __init__(self, db_path: Path = DEFAULT_LEDGER_PATH):
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    transaction_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    entered_at TEXT NOT NULL,
                    account TEXT NOT NULL,
                    instrument_id TEXT,
                    code TEXT,
                    name TEXT,
                    currency TEXT,
                    quantity TEXT,
                    price TEXT,
                    fee TEXT NOT NULL DEFAULT 0,
                    tax TEXT NOT NULL DEFAULT 0,
                    amount TEXT,
                    counter_currency TEXT,
                    counter_amount TEXT,
                    source TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    external_trade_id TEXT,
                    reverses_transaction_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_transactions_effective_at
                    ON transactions(effective_at, entered_at, transaction_id);
                CREATE INDEX IF NOT EXISTS idx_transactions_account_code
                    ON transactions(account, code, effective_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_external_id
                    ON transactions(account, external_trade_id)
                    WHERE external_trade_id IS NOT NULL AND external_trade_id <> '';
                """
            )

    def append(self, transaction: Transaction) -> Transaction:
        tx = transaction.validated()
        self.initialize()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO transactions (
                        transaction_id, event_type, effective_at, entered_at,
                        account, instrument_id, code, name, currency, quantity,
                        price, fee, tax, amount, counter_currency, counter_amount,
                        source, note, external_trade_id, reverses_transaction_id,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tx.transaction_id, tx.event_type.value, tx.effective_at, tx.entered_at,
                        tx.account, tx.instrument_id, tx.code, tx.name, tx.currency, _decimal_text(tx.quantity),
                        _decimal_text(tx.price), _decimal_text(tx.fee), _decimal_text(tx.tax), _decimal_text(tx.amount),
                        tx.counter_currency, _decimal_text(tx.counter_amount),
                        tx.source, tx.note, tx.external_trade_id, tx.reverses_transaction_id,
                        json.dumps(tx.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate transaction/external id: {exc}") from exc
        return tx

    def get(self, transaction_id: str) -> Optional[Transaction]:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
        return self._row_to_transaction(row) if row else None

    def list_transactions(
        self,
        *,
        account: Optional[str] = None,
        code: Optional[str] = None,
        effective_from: Optional[str] = None,
        effective_to: Optional[str] = None,
    ) -> List[Transaction]:
        self.initialize()
        clauses = []
        params = []
        if account:
            clauses.append("account = ?")
            params.append(account)
        if code:
            clauses.append("code = ?")
            params.append(code.upper())
        if effective_from:
            clauses.append("effective_at >= ?")
            params.append(effective_from)
        if effective_to:
            clauses.append("effective_at <= ?")
            params.append(effective_to)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = (
            "SELECT * FROM transactions"
            + where
            + " ORDER BY effective_at, entered_at, transaction_id"
        )
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_transaction(row) for row in rows]

    @staticmethod
    def _row_to_transaction(row: sqlite3.Row) -> Transaction:
        return Transaction(
            transaction_id=row["transaction_id"],
            event_type=TransactionType(row["event_type"]),
            effective_at=row["effective_at"],
            entered_at=row["entered_at"],
            account=row["account"],
            instrument_id=row["instrument_id"],
            code=row["code"],
            name=row["name"],
            currency=row["currency"],
            quantity=row["quantity"],
            price=row["price"],
            fee=row["fee"],
            tax=row["tax"],
            amount=row["amount"],
            counter_currency=row["counter_currency"],
            counter_amount=row["counter_amount"],
            source=row["source"],
            note=row["note"],
            external_trade_id=row["external_trade_id"],
            reverses_transaction_id=row["reverses_transaction_id"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        ).validated()
