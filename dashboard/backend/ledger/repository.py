"""Append-only SQLite repository for V3 transaction events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from .models import Transaction, TransactionType


def _decimal_text(value):
    return None if value is None else str(value)


def _transaction_json_payload(tx: Transaction) -> Dict:
    tx = tx.validated()
    return {
        "transaction_id": tx.transaction_id, "event_type": tx.event_type.value,
        "effective_at": str(tx.effective_at), "entered_at": str(tx.entered_at),
        "account": tx.account, "instrument_id": tx.instrument_id, "code": tx.code,
        "name": tx.name, "currency": tx.currency,
        "quantity": _decimal_text(tx.quantity), "price": _decimal_text(tx.price),
        "fee": _decimal_text(tx.fee), "tax": _decimal_text(tx.tax),
        "amount": _decimal_text(tx.amount), "counter_currency": tx.counter_currency,
        "counter_amount": _decimal_text(tx.counter_amount), "source": tx.source,
        "note": tx.note, "external_trade_id": tx.external_trade_id,
        "reverses_transaction_id": tx.reverses_transaction_id, "metadata": tx.metadata,
    }


def _transaction_from_json_payload(data: Dict) -> Transaction:
    return Transaction(
        transaction_id=data["transaction_id"], event_type=TransactionType(data["event_type"]),
        effective_at=data["effective_at"], entered_at=data["entered_at"], account=data["account"],
        instrument_id=data.get("instrument_id"), code=data.get("code"), name=data.get("name"),
        currency=data.get("currency"), quantity=data.get("quantity"), price=data.get("price"),
        fee=data.get("fee", "0"), tax=data.get("tax", "0"), amount=data.get("amount"),
        counter_currency=data.get("counter_currency"), counter_amount=data.get("counter_amount"),
        source=data.get("source", "manual"), note=data.get("note", ""),
        external_trade_id=data.get("external_trade_id"),
        reverses_transaction_id=data.get("reverses_transaction_id"),
        metadata=dict(data.get("metadata") or {}),
    ).validated()


DEFAULT_LEDGER_PATH = Path("data") / "ledger.sqlite3"


def _absolute_timestamp(value: str) -> float:
    raw = str(value or "").strip()
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(candidate)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).timestamp()


def _effective_in_range(value: str, lower: Optional[str], upper: Optional[str]) -> bool:
    reported_date = str(value)[:10]
    if lower:
        if len(lower) == 10:
            if reported_date < lower:
                return False
        elif _absolute_timestamp(value) < _absolute_timestamp(lower):
            return False
    if upper:
        if len(upper) == 10:
            if reported_date > upper:
                return False
        elif _absolute_timestamp(value) > _absolute_timestamp(upper):
            return False
    return True


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

                CREATE TABLE IF NOT EXISTS pending_batches (
                    pending_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    confirmed_at TEXT
                );
                """
            )

    @staticmethod
    def _insert_transaction(conn: sqlite3.Connection, tx: Transaction) -> None:
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

    def backup_database(self) -> Optional[Path]:
        """Create a consistent SQLite snapshot before a durable ledger write."""
        if str(self.db_path) == ":memory:":
            return None
        self.initialize()
        backup_dir = self.db_path.parent / "ledger_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
        backup_path = backup_dir / f"ledger.sqlite3.bak-{stamp}-{uuid4().hex[:8]}"
        try:
            with self._connect() as source, sqlite3.connect(str(backup_path)) as target:
                source.backup(target)
                check = target.execute("PRAGMA integrity_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise RuntimeError(f"ledger backup integrity check failed: {check}")
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise
        return backup_path

    def append(self, transaction: Transaction) -> Transaction:
        return self.append_many([transaction])[0]

    def append_many(self, transactions: List[Transaction]) -> List[Transaction]:
        return self.append_many_atomic(transactions)

    def append_many_atomic(
        self,
        transactions: List[Transaction],
        *,
        precommit_validator: Optional[Callable[[List[Transaction], List[Transaction]], None]] = None,
    ) -> List[Transaction]:
        """Validate against a locked current history and append as one transaction.

        ``precommit_validator`` executes after ``BEGIN IMMEDIATE`` and receives
        (existing_history, proposed_events). If it raises, SQLite rolls the
        whole write transaction back before any proposed event becomes visible.
        """
        validated = [transaction.validated() for transaction in transactions]
        if not validated:
            return []
        ids = [tx.transaction_id for tx in validated]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate transaction_id inside batch")
        external_keys = [
            (tx.account, tx.external_trade_id)
            for tx in validated
            if tx.external_trade_id
        ]
        if len(external_keys) != len(set(external_keys)):
            raise ValueError("duplicate external_trade_id inside batch/account")

        self.initialize()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if precommit_validator is not None:
                    rows = conn.execute(
                        "SELECT * FROM transactions ORDER BY effective_at, entered_at, transaction_id"
                    ).fetchall()
                    existing = [self._row_to_transaction(row) for row in rows]
                    precommit_validator(existing, validated)
                for tx in validated:
                    self._insert_transaction(conn, tx)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate transaction/external id: {exc}") from exc
        return validated

    def create_pending_batch(self, pending_id: str, transactions: List[Transaction], *, ttl_seconds: int = 1800) -> Dict:
        validated = [tx.validated() for tx in transactions]
        if not validated:
            raise ValueError("pending batch cannot be empty")
        self.initialize()
        now = datetime.now(timezone.utc)
        payload = json.dumps([_transaction_json_payload(tx) for tx in validated], ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO pending_batches (pending_id, created_at, expires_at, status, events_json) VALUES (?, ?, ?, 'pending', ?)",
                    (pending_id, now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat(), payload),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"duplicate pending_id: {pending_id}") from exc
        return self.get_pending_batch(pending_id)

    def get_pending_batch(self, pending_id: str) -> Optional[Dict]:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pending_batches WHERE pending_id = ?", (pending_id,)).fetchone()
        if row is None:
            return None
        return {
            "pending_id": row["pending_id"], "created_at": row["created_at"],
            "expires_at": row["expires_at"], "status": row["status"],
            "confirmed_at": row["confirmed_at"],
            "events": [_transaction_from_json_payload(item) for item in json.loads(row["events_json"] or "[]")],
        }

    def confirm_pending_batch(self, pending_id: str, validator: Callable[[List[Transaction], List[Transaction]], object]) -> Tuple[List[Transaction], object]:
        self.initialize()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM pending_batches WHERE pending_id = ?", (pending_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(pending_id)
            if row["status"] != "pending":
                raise ValueError(f"pending batch is already {row['status']}")
            now = datetime.now(timezone.utc)
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now > expires_at.astimezone(timezone.utc):
                conn.execute("UPDATE pending_batches SET status = 'expired' WHERE pending_id = ?", (pending_id,))
                conn.commit()
                raise ValueError("pending batch expired")
            proposed = [_transaction_from_json_payload(item) for item in json.loads(row["events_json"] or "[]")]
            existing_rows = conn.execute("SELECT * FROM transactions").fetchall()
            existing = [self._row_to_transaction(item) for item in existing_rows]
            validation_result = validator(existing, proposed)
            for tx in proposed:
                self._insert_transaction(conn, tx)
            confirmed_at = now.isoformat()
            conn.execute("UPDATE pending_batches SET status = 'confirmed', confirmed_at = ? WHERE pending_id = ?", (confirmed_at, pending_id))
            conn.commit()
            return proposed, validation_result
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError(f"duplicate transaction/external id: {exc}") from exc
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

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

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM transactions" + where, params).fetchall()
        transactions = [self._row_to_transaction(row) for row in rows]
        transactions = [
            tx for tx in transactions
            if _effective_in_range(str(tx.effective_at), effective_from, effective_to)
        ]
        transactions.sort(
            key=lambda tx: (
                _absolute_timestamp(str(tx.effective_at)),
                _absolute_timestamp(str(tx.entered_at)),
                tx.transaction_id,
            )
        )
        return transactions

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
