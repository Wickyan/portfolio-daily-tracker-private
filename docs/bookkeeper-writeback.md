# Bookkeeper Writeback

真实数据源是 `dashboard/data/portfolio.json`。

## Position 字段

当前用户层字段为：

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
- `source`

`market` 已从用户层移除。新写入的 `portfolio.json` 不再包含 `market`、`exchange`、`canonical_symbol`。

## AI 写入流程

AI 不直接写入 portfolio，只生成 preview。

流程：

1. `POST /api/portfolio/ai-preview`
2. `POST /api/portfolio/ai-revise`
3. `POST /api/portfolio/ai-confirm`
4. `POST /api/portfolio/rollback/{operation_id}`

只有 confirm 成功后才会写入 `dashboard/data/portfolio.json`。

每次 confirm 都会：

- 备份旧 portfolio 到 `dashboard/data/backups/`
- 原子写入新的 `portfolio.json`
- 生成 operation 日志到 `dashboard/data/operations/`

## 图片输入

图片输入不会直接写入。图片聊天结果只用于生成待确认 preview，用户确认前 `/api/portfolio/live` 不应变化。

## 手动新增

Portfolio 页面“新增持仓”也走统一安全写入服务，会生成 backup 和 operation，可通过 operation rollback。

## 测试工具

Settings 中的 reset 工具只用于测试环境清理数据。联网查代码、用户别名、多币种 cash 账本是后续任务，本次不做。
