import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import ledger_v3
from backend.ledger import Transaction, TransactionRepository, TransactionType


class LedgerApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = TransactionRepository(Path(self.tmp.name) / "ledger.sqlite3")
        self.repo.append_many([
            Transaction(
                event_type=TransactionType.BUY,
                effective_at="2025-01-01T10:00:00+08:00",
                account="IBKR",
                code="AAPL",
                name="Apple",
                currency="USD",
                quantity="2",
                price="100",
            ),
            Transaction(
                event_type=TransactionType.SELL,
                effective_at="2025-02-01T10:00:00+08:00",
                account="IBKR",
                code="AAPL",
                name="Apple",
                currency="USD",
                quantity="1",
                price="150",
            ),
        ])
        self.original_get_repository = ledger_v3.get_repository
        ledger_v3.get_repository = lambda: self.repo
        app = FastAPI()
        app.include_router(ledger_v3.router, prefix="/api/ledger-v3")
        self.client = TestClient(app)

    def tearDown(self):
        ledger_v3.get_repository = self.original_get_repository
        self.tmp.cleanup()

    def test_transactions_return_exact_decimal_strings(self):
        response = self.client.get("/api/ledger-v3/transactions", params={"code": "AAPL"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["transactions"][0]["price"], "100")
        self.assertEqual(body["transactions"][1]["quantity"], "1")

    def test_state_returns_current_quantity_cost_and_realized_pnl(self):
        response = self.client.get("/api/ledger-v3/state")
        self.assertEqual(response.status_code, 200)
        pos = response.json()["positions"][0]
        self.assertEqual(pos["quantity"], "1")
        self.assertEqual(pos["average_cost"], "100")
        self.assertEqual(pos["realized_pnl"], "50")

    def test_invalid_as_of_returns_400(self):
        response = self.client.get("/api/ledger-v3/state", params={"as_of": "not-a-date"})
        self.assertEqual(response.status_code, 400)

    def test_transaction_date_filter_uses_reported_date(self):
        response = self.client.get(
            "/api/ledger-v3/transactions",
            params={"from": "2025-02-01", "to": "2025-02-01"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["transactions"][0]["event_type"], "SELL")

    def test_historical_state_respects_as_of(self):
        response = self.client.get("/api/ledger-v3/state", params={"as_of": "2025-01-15"})
        self.assertEqual(response.status_code, 200)
        pos = response.json()["positions"][0]
        self.assertEqual(pos["quantity"], "2")
        self.assertEqual(pos["realized_pnl"], "0")

    def test_preview_then_confirm_writes_exactly_once(self):
        payload = {
            "events": [{
                "event_type": "BUY",
                "effective_at": "2025-03-01T10:00:00+08:00",
                "account": "IBKR",
                "code": "AAPL",
                "name": "Apple",
                "currency": "USD",
                "quantity": "1",
                "price": "125.25",
                "fee": "0.01",
            }]
        }
        preview = self.client.post("/api/ledger-v3/preview", json=payload)
        self.assertEqual(preview.status_code, 200, preview.text)
        pending_id = preview.json()["pending_id"]
        self.assertEqual(len(self.repo.list_transactions()), 2)
        self.assertEqual(preview.json()["events"][0]["price"], "125.25")

        pending = self.client.get(f"/api/ledger-v3/pending/{pending_id}")
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json()["status"], "pending")

        confirmed = self.client.post(f"/api/ledger-v3/confirm/{pending_id}")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "confirmed")
        self.assertEqual(len(self.repo.list_transactions()), 3)

        repeated = self.client.post(f"/api/ledger-v3/confirm/{pending_id}")
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(len(self.repo.list_transactions()), 3)

    def test_preview_rejects_oversell_against_v3_history(self):
        response = self.client.post("/api/ledger-v3/preview", json={
            "events": [{
                "event_type": "SELL",
                "effective_at": "2025-03-01",
                "account": "IBKR",
                "code": "AAPL",
                "currency": "USD",
                "quantity": "2",
                "price": "160",
            }]
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_historical_preview_is_marked_as_backfill(self):
        response = self.client.post("/api/ledger-v3/preview", json={
            "events": [{
                "event_type": "BUY",
                "effective_at": "2024-01-01",
                "account": "IBKR",
                "code": "AAPL",
                "currency": "USD",
                "quantity": "1",
                "price": "80",
            }]
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["historical_backfill"])
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_missing_pending_returns_404(self):
        response = self.client.get("/api/ledger-v3/pending/does-not-exist")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
