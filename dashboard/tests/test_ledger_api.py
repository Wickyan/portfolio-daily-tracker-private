import tempfile
import json
from io import BytesIO
import unittest
from pathlib import Path

from fastapi import FastAPI
from PIL import Image
from fastapi.testclient import TestClient

from backend.api import ledger_v3
from backend.ledger import Transaction, TransactionRepository, TransactionType
from backend.ledger.screenshot import VisionProviderSpec
from core.llm.base import LLMResponse


class FakeScreenshotVisionProvider:
    async def chat(self, messages, **kwargs):
        return LLMResponse(
            content=json.dumps({
                "rows": [{
                    "event_type": "BUY",
                    "effective_at": "2025-03-12T10:30:00+08:00",
                    "account": "IBKR",
                    "code": "NVDA",
                    "name": "NVIDIA",
                    "currency": "USD",
                    "quantity": "2",
                    "price": "87.5",
                    "fee": "1",
                    "tax": "0",
                    "order_id": "SCREEN-ORDER-1",
                    "status": "已成交",
                    "confidence": 0.99,
                    "raw_text": "NVDA 2 @ 87.5"
                }],
                "warnings": [],
            }, ensure_ascii=False),
            model="fake",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            finish_reason="stop",
        )


def screenshot_bytes():
    image = Image.new("RGB", (800, 1000), "white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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
        self.original_create_vision = ledger_v3.create_configured_vision_providers
        ledger_v3.get_repository = lambda: self.repo
        ledger_v3.create_configured_vision_providers = lambda: [VisionProviderSpec(
            provider=FakeScreenshotVisionProvider(),
            family="openai",
            model="fake",
            label="fake vision",
        )]
        app = FastAPI()
        app.include_router(ledger_v3.router, prefix="/api/ledger-v3")
        self.client = TestClient(app)

    def tearDown(self):
        ledger_v3.get_repository = self.original_get_repository
        ledger_v3.create_configured_vision_providers = self.original_create_vision
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

    def test_preview_text_historical_buy_creates_pending_only(self):
        response = self.client.post("/api/ledger-v3/preview-text", json={
            "message": "去年3月12号在IBKR买了1股苹果，87.5美元一股，手续费1美元",
            "reference_time": "2026-09-18T10:00:00+08:00",
            "source": "voice",
        })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        event = body["events"][0]
        self.assertEqual(event["effective_at"], "2025-03-12T00:00:00+08:00")
        self.assertEqual(event["price"], "87.5")
        self.assertEqual(event["fee"], "1")
        self.assertEqual(event["source"], "voice")
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_preview_text_multi_clause_inherits_asset_and_year(self):
        response = self.client.post("/api/ledger-v3/preview-text", json={
            "message": "去年3月12号IBKR买1股苹果87.5美元；5月8号又买2股92美元",
            "reference_time": "2026-09-18T10:00:00+08:00",
        })
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual([e["code"] for e in events], ["AAPL", "AAPL"])
        self.assertEqual(events[1]["effective_at"], "2025-05-08T00:00:00+08:00")

    def test_preview_text_oversell_is_rejected_by_v3_replay(self):
        response = self.client.post("/api/ledger-v3/preview-text", json={
            "message": "2025年3月1日在IBKR卖2股苹果成交价160美元",
            "reference_time": "2026-09-18T10:00:00+08:00",
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_preview_text_requires_offset_when_reference_is_supplied(self):
        response = self.client.post("/api/ledger-v3/preview-text", json={
            "message": "昨天IBKR入金500美元",
            "reference_time": "2026-09-18T10:00:00",
        })
        self.assertEqual(response.status_code, 400)

    def test_screenshot_preview_creates_pending_without_writing(self):
        response = self.client.post(
            "/api/ledger-v3/preview-screenshots",
            files={"images": ("orders.png", screenshot_bytes(), "image/png")},
            data={"account_hint": "IBKR"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["code"], "NVDA")
        self.assertEqual(body["events"][0]["source"], "screenshot")
        self.assertEqual(body["extraction"]["duplicate_count"], 0)
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_same_screenshot_order_is_deduped_against_pending(self):
        first = self.client.post(
            "/api/ledger-v3/preview-screenshots",
            files={"images": ("orders.png", screenshot_bytes(), "image/png")},
            data={"account_hint": "IBKR"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(
            "/api/ledger-v3/preview-screenshots",
            files={"images": ("orders-again.png", screenshot_bytes(), "image/png")},
            data={"account_hint": "IBKR"},
        )
        self.assertEqual(second.status_code, 200, second.text)
        body = second.json()
        self.assertIsNone(body["pending_id"])
        self.assertEqual(body["status"], "no_new_events")
        self.assertEqual(body["extraction"]["duplicate_count"], 1)
        self.assertEqual(
            body["extraction"]["duplicates"][0]["duplicate_reason"],
            "already_in_ledger_or_pending",
        )

    def test_reverse_sell_preview_confirm_restores_position(self):
        rows = self.repo.list_transactions()
        sell_id = next(tx.transaction_id for tx in rows if tx.event_type == TransactionType.SELL)
        preview = self.client.post(
            f"/api/ledger-v3/reverse/{sell_id}/preview", json={"reason": "录错了卖出"}
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(len(self.repo.list_transactions()), 2)
        pending_id = preview.json()["pending_id"]

        confirmed = self.client.post(f"/api/ledger-v3/confirm/{pending_id}")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(len(self.repo.list_transactions()), 3)
        pos = confirmed.json()["projected_state"]["positions"][0]
        self.assertEqual(pos["quantity"], "2")
        self.assertEqual(pos["realized_pnl"], "0")

        history = self.client.get("/api/ledger-v3/transactions").json()["transactions"]
        target = next(item for item in history if item["transaction_id"] == sell_id)
        reversal = next(item for item in history if item["event_type"] == "REVERSAL")
        self.assertTrue(target["is_reversed"])
        self.assertEqual(target["reversed_by"], reversal["transaction_id"])

    def test_reverse_buy_is_rejected_if_later_sell_would_be_invalid(self):
        rows = self.repo.list_transactions()
        buy_id = next(tx.transaction_id for tx in rows if tx.event_type == TransactionType.BUY)
        response = self.client.post(
            f"/api/ledger-v3/reverse/{buy_id}/preview", json={"reason": "尝试冲销"}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(self.repo.list_transactions()), 2)

    def test_reverse_missing_transaction_returns_404(self):
        response = self.client.post(
            "/api/ledger-v3/reverse/not-found/preview", json={"reason": "test"}
        )
        self.assertEqual(response.status_code, 404)

    def test_reversal_cannot_be_reversed_again(self):
        rows = self.repo.list_transactions()
        sell_id = next(tx.transaction_id for tx in rows if tx.event_type == TransactionType.SELL)
        preview = self.client.post(f"/api/ledger-v3/reverse/{sell_id}/preview", json={})
        pending_id = preview.json()["pending_id"]
        self.client.post(f"/api/ledger-v3/confirm/{pending_id}")
        reversal = next(tx for tx in self.repo.list_transactions() if tx.event_type == TransactionType.REVERSAL)
        response = self.client.post(
            f"/api/ledger-v3/reverse/{reversal.transaction_id}/preview", json={}
        )
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
