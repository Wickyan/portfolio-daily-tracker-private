# Bookkeeper Mode

首页 AI 默认使用 bookkeeper 模式，用于识别和核对持仓记账信息，不作为交易员或投资顾问。

## 行为边界

- 只做记账解析、字段校验、缺失字段澄清和现有持仓摘要说明。
- 不提供买入、卖出、加仓、减仓、调仓、止损、目标价等投资建议。
- 不输出风险点评或交易结论。
- 不使用“老板”“蛋总”“交易员”等称呼。
- 不声称“已写入”“已更新数据库”“已保存到持仓”，除非 `ai-confirm` 接口真实成功。
- 当前聊天模式不会直接写入 `dashboard/data/portfolio.json`，只生成待确认 preview。

## 写入意图

用户输入“新增”“买入”“添加持仓”“写入数据库”等内容时，AI 只识别意图和字段。

如果字段完整，AI 可以生成待确认 preview，等待用户确认。

如果字段不完整，AI 会明确提示缺少的字段，例如：

```text
缺少 account/group，请补充账户分组。
```

如果用户要求“写入数据库”，当前默认回复方向是：

```text
已识别为写入意图，当前不会写入portfolio，请先确认待确认记账信息。
```

## 用户层字段

Bookkeeper 只展示用户友好字段：

- `account` / `group`
- `name`
- `code`
- `currency`
- `asset_type`
- `quantity`
- `cost_price`
- `total_cost`
- `fee`
- `note`

不展示、不追问 `market`，也不把 `market` 放进缺失字段。

## 币种和现金

- AI 会保留用户提供的 `currency`，不会默认把 USD/HKD 换成 CNY。
- 如果系统没有完整的多币种现金账本，AI 会提示现金余额可能无法准确表达多币种情况。

## 技术位置

- Prompt: `dashboard/agents/bookkeeper/prompts.py`
- 默认挂载点: `dashboard/backend/services/agent_service.py`
- AI preview API: `dashboard/backend/api/portfolio_ai.py`
- 安全写入服务: `dashboard/backend/services/portfolio_write_service.py`
- 写入流程: preview → revise → confirm → rollback。


## 账户现金

bookkeeper支持`deposit`、`withdraw`、`set_cash`和`fx_exchange`。`fx_exchange`会生成同一账户的换出withdraw与换入deposit，并在一个operation中原子确认、原子回滚。现金按`account+currency`保存，不作为position。所有现金写入仍必须经过preview和confirm，并可通过operation回滚。
