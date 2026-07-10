from backend.api.portfolio_ai import parse_bookkeeping_message


def test_structured_chinese_labels_are_recognized() -> None:
    parsed = parse_bookkeeping_message(
        "账户：IBKR 名称：Apple 代码：AAPL 币种：USD 类型：stock 数量：3 成本价：202.68"
    )
    assert parsed["intent"] == "bookkeeping"
    assert parsed["missing_fields"] == []
    change = parsed["changes"][0]
    assert change["account"] == "IBKR"
    assert change["name"] == "Apple"
    assert change["code"] == "AAPL"
    assert change["currency"] == "USD"
    assert change["asset_type"] == "stock"
    assert change["quantity"] == 3
    assert change["cost_price"] == 202.68


def test_confirmation_word_alone_is_not_a_new_bookkeeping_record() -> None:
    parsed = parse_bookkeeping_message("确认写入")
    assert parsed["intent"] == "chat_only"
