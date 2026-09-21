"""Manual end-to-end smoke test for screenshot bookkeeping.

Generates synthetic brokerage screenshots and sends them through the real
FastAPI screenshot endpoint with a temporary ledger. Cloud vision is disabled
here on purpose so the test exercises RapidOCR + the configured text model.
No production ledger data is read or written.
"""

from __future__ import annotations

import tempfile
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from backend.api import ledger_v3
from backend.ledger import Transaction, TransactionRepository, TransactionType


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(size: int):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def draw_order(draw: ImageDraw.ImageDraw, y: int, order: dict, *, width: int = 1120) -> int:
    title = font(31)
    body = font(27)
    small = font(23)
    draw.rounded_rectangle((40, y, width - 40, y + 245), radius=18, outline="black", width=2)
    draw.text((65, y + 18), f"{order['side']}  {order['code']}  {order['name']}", fill="black", font=title)
    draw.text(
        (65, y + 68),
        f"Date: {order['date']}    Time: {order['time']}    Status: {order['status']}",
        fill="black",
        font=body,
    )
    draw.text(
        (65, y + 113),
        f"Quantity: {order['qty']}    Execution Price: {order['price']} {order['currency']}",
        fill="black",
        font=body,
    )
    draw.text(
        (65, y + 158),
        f"Fee: {order['fee']} {order['currency']}    Order ID: {order['order_id']}",
        fill="black",
        font=body,
    )
    draw.text((65, y + 203), f"Account: {order.get('account', 'IBKR')}", fill="black", font=small)
    return y + 275


def make_screenshot(orders: list[dict], *, height: int | None = None, positions: list[int] | None = None) -> bytes:
    width = 1120
    if positions is None:
        height = height or max(720, 150 + len(orders) * 285)
        positions = [130 + i * 285 for i in range(len(orders))]
    else:
        height = height or max(positions[-1] + 350, 900)

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 24), "BROKER TRADE HISTORY", fill="black", font=font(36))
    draw.text((40, 75), "Historical executed orders", fill="black", font=font(24))
    for y, order in zip(positions, orders):
        draw_order(draw, y, order, width=width)

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def order(
    side: str,
    code: str,
    name: str,
    date: str,
    time: str,
    qty: str,
    price: str,
    fee: str,
    order_id: str,
    *,
    status: str = "Filled",
    currency: str = "USD",
) -> dict:
    return {
        "side": side,
        "code": code,
        "name": name,
        "date": date,
        "time": time,
        "qty": qty,
        "price": price,
        "fee": fee,
        "order_id": order_id,
        "status": status,
        "currency": currency,
        "account": "IBKR",
    }


def post_image(client: TestClient, name: str, data: bytes):
    response = client.post(
        "/api/ledger-v3/preview-screenshots",
        files={"images": (name, data, "image/png")},
        data={"account_hint": "IBKR"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def event_summary(item: dict) -> str:
    return (
        f"{item['event_type']} {item.get('code')} "
        f"{item.get('quantity')} @ {item.get('price')} "
        f"fee={item.get('fee')} time={item.get('effective_at')} "
        f"order={item.get('metadata', {}).get('order_id')}"
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ledger-screenshot-e2e-") as tmp:
        tmp_path = Path(tmp)
        repo = TransactionRepository(tmp_path / "ledger.sqlite3")
        repo.append(
            Transaction(
                event_type=TransactionType.OPENING_POSITION,
                effective_at="2024-01-01",
                account="IBKR",
                code="AAPL",
                name="Apple",
                currency="USD",
                quantity="20",
                price="100",
                source="test-fixture",
            )
        )

        original_repo = ledger_v3.get_repository
        original_visions = ledger_v3.create_configured_vision_providers
        ledger_v3.get_repository = lambda: repo
        ledger_v3.create_configured_vision_providers = lambda: []

        try:
            app = FastAPI()
            app.include_router(ledger_v3.router, prefix="/api/ledger-v3")
            client = TestClient(app)

            first = order(
                "BUY", "NVDA", "NVIDIA", "2025-03-12", "10:30:15",
                "2", "87.50", "1.00", "HIST-1001",
            )
            short_img = make_screenshot([first])
            short_path = tmp_path / "01-short-buy.png"
            short_path.write_bytes(short_img)
            short = post_image(client, short_path.name, short_img)
            assert len(short["events"]) == 1, short
            e = short["events"][0]
            assert e["event_type"] == "BUY"
            assert e["code"] == "NVDA"
            assert e["quantity"] == "2"
            assert e["price"] in {"87.5", "87.50"}
            assert e["fee"] in {"1", "1.0", "1.00"}
            assert e["metadata"]["order_id"] == "HIST-1001"
            assert "2025-03-12" in e["effective_at"]
            print("SHORT", event_summary(e))

            confirmed = client.post(f"/api/ledger-v3/confirm/{short['pending_id']}")
            assert confirmed.status_code == 200, confirmed.text

            multi_orders = [
                order(
                    "BUY", "MSFT", "Microsoft", "2025-04-08", "09:45:21",
                    "3", "410.25", "0.50", "HIST-1002",
                ),
                order(
                    "SELL", "AAPL", "Apple", "2025-04-09", "14:22:08",
                    "2", "225.40", "0.35", "HIST-1003",
                ),
            ]
            multi_img = make_screenshot(multi_orders)
            multi_path = tmp_path / "02-multi-orders.png"
            multi_path.write_bytes(multi_img)
            multi = post_image(client, multi_path.name, multi_img)
            assert len(multi["events"]) == 2, multi
            assert {x["code"] for x in multi["events"]} == {"MSFT", "AAPL"}
            assert {x["event_type"] for x in multi["events"]} == {"BUY", "SELL"}
            print("MULTI", *[event_summary(x) for x in multi["events"]], sep="\n  ")

            duplicate_and_cancelled = [
                first,
                multi_orders[0],
                order(
                    "BUY", "TSLA", "Tesla", "2025-04-10", "11:18:44",
                    "1", "301.20", "0.20", "HIST-CANCEL-1", status="Cancelled",
                ),
            ]
            dupe_img = make_screenshot(duplicate_and_cancelled, height=1200)
            dupe_path = tmp_path / "03-duplicates-cancelled.png"
            dupe_path.write_bytes(dupe_img)
            dupe = post_image(client, dupe_path.name, dupe_img)
            assert dupe["events"] == [], dupe
            reasons = [x["duplicate_reason"] for x in dupe["extraction"]["duplicates"]]
            assert reasons.count("already_in_ledger_or_pending") >= 2, reasons
            assert all(x.get("code") != "TSLA" for x in dupe["events"]), dupe
            print(
                "DEDUPE",
                reasons,
                "cancelled_tsla=excluded",
                "explicit_not_executed=" + str("not_executed" in reasons),
            )

            long_orders = [
                order(
                    "BUY", "AMZN", "Amazon", "2025-05-01", "09:35:11",
                    "1", "188.10", "0.20", "LONG-2001",
                ),
                order(
                    "BUY", "GOOGL", "Alphabet", "2025-05-02", "10:12:33",
                    "4", "166.75", "0.40", "LONG-2002",
                ),
                order(
                    "BUY", "META", "Meta", "2025-05-03", "13:08:09",
                    "2", "592.30", "0.30", "LONG-2003",
                ),
                order(
                    "BUY", "AMD", "AMD", "2025-05-04", "15:26:51",
                    "5", "142.60", "0.45", "LONG-2004",
                ),
                order(
                    "SELL", "AAPL", "Apple", "2025-05-05", "15:58:41",
                    "1", "229.90", "0.25", "LONG-2005",
                ),
            ]
            # LONG-2002 and LONG-2004 sit entirely inside tile overlap bands,
            # so adjacent tiles should both see them and dedupe by Order ID.
            positions = [300, 1940, 4300, 5780, 8300]
            long_img = make_screenshot(long_orders, height=9000, positions=positions)
            long_path = tmp_path / "04-ultra-long-history.png"
            long_path.write_bytes(long_img)
            long_result = post_image(client, long_path.name, long_img)
            ids = [x["metadata"]["order_id"] for x in long_result["events"]]
            assert set(ids) == {x["order_id"] for x in long_orders}, (ids, long_result)
            assert len(ids) == len(set(ids)) == 5, ids
            assert long_result["extraction"]["images"][0]["tile_count"] >= 4
            assert long_result["extraction"]["duplicate_count"] >= 1
            print(
                "LONG",
                f"tiles={long_result['extraction']['images'][0]['tile_count']}",
                f"new={len(long_result['events'])}",
                f"deduped={long_result['extraction']['duplicate_count']}",
            )
            for item in long_result["events"]:
                print("  ", event_summary(item))

            # Screenshot previews must not write until explicitly confirmed.
            # Only the short screenshot was confirmed above.
            rows = repo.list_transactions()
            confirmed_ids = {
                tx.metadata.get("order_id")
                for tx in rows
                if tx.source == "screenshot"
            }
            assert confirmed_ids == {"HIST-1001"}, confirmed_ids
            print("LEDGER_SAFE", f"confirmed_screenshot_orders={sorted(confirmed_ids)}")
            print("FIXTURES", *(str(path) for path in sorted(tmp_path.glob("*.png"))), sep="\n  ")
            print("E2E_OK")
        finally:
            ledger_v3.get_repository = original_repo
            ledger_v3.create_configured_vision_providers = original_visions


if __name__ == "__main__":
    main()
