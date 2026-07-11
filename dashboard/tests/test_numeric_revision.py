from __future__ import annotations

import unittest
from backend.api.portfolio_ai import (
    ReviseRequest,
    ai_revise,
    apply_bare_numeric_revision,
    apply_direct_code_revision,
    make_pending,
    pending_path,
    save_pending,
)


def base_pending() -> dict:
    return {
        "ok": True,
        "pending_id": "test-pending",
        "intent": "bookkeeping",
        "input_type": "text",
        "message": "银河 纳斯达克 1.243元 12700个\n[revise] 国泰",
        "status": "pending",
        "summary": "已结合原确认卡应用补充信息，请重新确认",
        "action_type": "add_or_update",
        "changes": [{
            "account": "银河",
            "code": "513100",
            "name": "纳指ETF国泰",
            "currency": "CNY",
            "asset_type": "stock",
            "quantity": 12700.0,
            "available_qty": 12700.0,
            "cost_price": 1.243,
            "current_price": 1.243,
            "total_cost": 15786.1,
            "note": "",
            "source": "ai",
            "side": "long",
            "action_type": "add_or_update",
        }],
        "missing_fields": [],
        "warnings": [
            "已结合原确认卡和补充“国泰”推测为“纳指ETF国泰”（513100）；请重新确认",
        ],
        "instrument_candidates": [{
            "code": "513100",
            "name": "纳指ETF国泰",
            "currency": "CNY",
            "asset_type": "stock",
            "classify": "Fund",
            "score": 210,
        }],
        "requires_confirmation": True,
    }


class NumericRevisionTest(unittest.IsolatedAsyncioTestCase):
    def test_decimal_close_to_cost_is_tentatively_applied_as_cost(self) -> None:
        pending = base_pending()
        pending["warnings"].append("未找到与“1.244”匹配的场内证券")
        pending["changes"][0]["amount"] = 15786.1

        revised = apply_bare_numeric_revision(pending, "1.244")

        self.assertIsNotNone(revised)
        change = revised["changes"][0]
        self.assertEqual(change["code"], "513100")
        self.assertEqual(change["quantity"], 12700)
        self.assertEqual(change["cost_price"], 1.244)
        self.assertAlmostEqual(change["total_cost"], 15798.8)
        self.assertNotIn("amount", change)
        self.assertEqual(revised["revision_options"], [])
        self.assertTrue(any("猜你想把成本价（均价）" in warning for warning in revised["warnings"]))
        self.assertFalse(any("匹配的场内证券" in warning for warning in revised["warnings"]))

    def test_integer_close_to_quantity_is_tentatively_applied_as_quantity(self) -> None:
        pending = base_pending()
        pending["changes"][0]["cost_price"] = 1.244
        pending["changes"][0]["total_cost"] = 15798.8

        revised = apply_bare_numeric_revision(pending, "12710")

        self.assertIsNotNone(revised)
        change = revised["changes"][0]
        self.assertEqual(change["code"], "513100")
        self.assertEqual(change["quantity"], 12710)
        self.assertEqual(change["available_qty"], 12710)
        self.assertEqual(change["cost_price"], 1.244)
        self.assertAlmostEqual(change["total_cost"], 15811.24)
        self.assertTrue(any("猜你想把数量" in warning for warning in revised["warnings"]))

    def test_bare_number_never_replaces_existing_code(self) -> None:
        pending = base_pending()
        self.assertIsNone(apply_direct_code_revision(pending, "12710"))
        explicit = apply_direct_code_revision(pending, "代码12710")
        self.assertEqual(explicit["changes"][0]["code"], "12710")

    def test_ambiguous_number_produces_field_choice_buttons(self) -> None:
        pending = base_pending()
        pending["changes"][0]["quantity"] = 100
        pending["changes"][0]["available_qty"] = 100
        pending["changes"][0]["cost_price"] = 100
        pending["changes"][0]["total_cost"] = 10000

        revised = apply_bare_numeric_revision(pending, "100")

        self.assertEqual(revised["changes"][0]["quantity"], 100)
        self.assertEqual(revised["changes"][0]["cost_price"], 100)
        fields = {option["field"] for option in revised["revision_options"]}
        self.assertEqual(fields, {"quantity", "cost_price"})
        generated = make_pending(revised, "text", "100")
        self.assertFalse(generated["requires_confirmation"])

    async def test_sequential_numeric_followups_keep_code_and_other_fields(self) -> None:
        initial = make_pending(base_pending(), "text", "银河 纳斯达克 1.243元 12700个")
        created: list[str] = []
        try:
            save_pending(initial)
            created.append(initial["pending_id"])

            cost_card = await ai_revise(ReviseRequest(
                pending_id=initial["pending_id"],
                message="1.244",
            ))
            created.append(cost_card["pending_id"])
            self.assertEqual(cost_card["changes"][0]["code"], "513100")
            self.assertEqual(cost_card["changes"][0]["cost_price"], 1.244)
            self.assertTrue(cost_card["requires_confirmation"])

            quantity_card = await ai_revise(ReviseRequest(
                pending_id=cost_card["pending_id"],
                message="12710",
            ))
            created.append(quantity_card["pending_id"])
            change = quantity_card["changes"][0]
            self.assertEqual(change["code"], "513100")
            self.assertEqual(change["quantity"], 12710)
            self.assertEqual(change["cost_price"], 1.244)
            self.assertTrue(quantity_card["requires_confirmation"])
        finally:
            for pending_id in created:
                pending_path(pending_id).unlink(missing_ok=True)


    async def test_correction_reverts_wrong_previous_field_before_applying_new_field(self) -> None:
        initial_data = base_pending()
        initial_data["changes"][0]["quantity"] = 1270.0
        initial_data["changes"][0]["available_qty"] = 1270.0
        initial_data["changes"][0]["cost_price"] = 1.243
        initial_data["changes"][0]["total_cost"] = 1578.61
        initial = make_pending(initial_data, "text", "银河 纳斯达克 1.243元 1270个")
        created: list[str] = []
        try:
            save_pending(initial)
            created.append(initial["pending_id"])

            wrong = await ai_revise(ReviseRequest(
                pending_id=initial["pending_id"],
                message="1.240",
            ))
            created.append(wrong["pending_id"])
            self.assertEqual(wrong["changes"][0]["quantity"], 1270)
            self.assertEqual(wrong["changes"][0]["cost_price"], 1.24)

            corrected = await ai_revise(ReviseRequest(
                pending_id=wrong["pending_id"],
                message="不对 是数量修改成1.240",
            ))
            created.append(corrected["pending_id"])
            change = corrected["changes"][0]
            self.assertEqual(change["code"], "513100")
            self.assertEqual(change["quantity"], 1.24)
            self.assertEqual(change["available_qty"], 1.24)
            self.assertEqual(change["cost_price"], 1.243)
            self.assertAlmostEqual(change["total_cost"], 1.54132)
            self.assertNotIn("amount", change)
            self.assertTrue(corrected["requires_confirmation"])
            self.assertIsNotNone(corrected["correction_context"])

            reverted = [item for item in corrected["revision_diffs"] if item["kind"] == "reverted"]
            applied = [item for item in corrected["revision_diffs"] if item["kind"] == "applied"]
            self.assertTrue(any(
                item["field"] == "cost_price" and item["before"] == 1.24 and item["after"] == 1.243
                for item in reverted
            ))
            self.assertTrue(any(
                item["field"] == "quantity" and item["before"] == 1270 and item["after"] == 1.24
                for item in applied
            ))
            self.assertFalse(any("猜你想把成本价" in warning for warning in corrected["warnings"]))
            self.assertTrue(any("先撤销上一张卡" in warning for warning in corrected["warnings"]))
        finally:
            for pending_id in created:
                pending_path(pending_id).unlink(missing_ok=True)

    async def test_bare_correction_restores_previous_card_without_guessing(self) -> None:
        initial_data = base_pending()
        initial_data["changes"][0]["quantity"] = 1270.0
        initial_data["changes"][0]["available_qty"] = 1270.0
        initial_data["changes"][0]["cost_price"] = 1.243
        initial_data["changes"][0]["total_cost"] = 1578.61
        initial = make_pending(initial_data, "text", "银河 纳斯达克 1.243元 1270个")
        created: list[str] = []
        try:
            save_pending(initial)
            created.append(initial["pending_id"])
            wrong = await ai_revise(ReviseRequest(
                pending_id=initial["pending_id"],
                message="1.240",
            ))
            created.append(wrong["pending_id"])

            restored = await ai_revise(ReviseRequest(
                pending_id=wrong["pending_id"],
                message="不对，你改的不是这个",
            ))
            created.append(restored["pending_id"])
            change = restored["changes"][0]
            self.assertEqual(change["quantity"], 1270)
            self.assertEqual(change["cost_price"], 1.243)
            self.assertTrue(restored["requires_confirmation"])
            self.assertEqual(restored["summary"], "已撤销上一步修改，请继续补充或确认")
            self.assertTrue(any(item["kind"] == "reverted" for item in restored["revision_diffs"]))
            self.assertFalse(any(item["kind"] == "applied" for item in restored["revision_diffs"]))
        finally:
            for pending_id in created:
                pending_path(pending_id).unlink(missing_ok=True)

    async def test_correction_can_replace_wrong_quantity_with_cost_price(self) -> None:
        initial = make_pending(base_pending(), "text", "银河 纳斯达克 1.243元 12700个")
        created: list[str] = []
        try:
            save_pending(initial)
            created.append(initial["pending_id"])
            wrong = await ai_revise(ReviseRequest(
                pending_id=initial["pending_id"],
                message="数量12710",
            ))
            created.append(wrong["pending_id"])

            corrected = await ai_revise(ReviseRequest(
                pending_id=wrong["pending_id"],
                message="不是改数量，是成本价1.244",
            ))
            created.append(corrected["pending_id"])
            change = corrected["changes"][0]
            self.assertEqual(change["quantity"], 12700)
            self.assertEqual(change["cost_price"], 1.244)
        finally:
            for pending_id in created:
                pending_path(pending_id).unlink(missing_ok=True)


    async def test_plain_not_x_but_y_edits_current_card_without_undoing_unrelated_revision(self) -> None:
        initial = make_pending(base_pending(), "text", "银河 纳斯达克 1.243元 12700个")
        created: list[str] = []
        try:
            save_pending(initial)
            created.append(initial["pending_id"])
            quantity_card = await ai_revise(ReviseRequest(
                pending_id=initial["pending_id"],
                message="数量12710",
            ))
            created.append(quantity_card["pending_id"])

            currency_card = await ai_revise(ReviseRequest(
                pending_id=quantity_card["pending_id"],
                message="不是人民币，而是美元",
            ))
            created.append(currency_card["pending_id"])
            change = currency_card["changes"][0]
            self.assertEqual(change["quantity"], 12710)
            self.assertEqual(change["currency"], "USD")
            self.assertIsNone(currency_card.get("correction_context"))
            self.assertFalse(any(item["kind"] == "reverted" for item in currency_card["revision_diffs"]))
        finally:
            for pending_id in created:
                pending_path(pending_id).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
