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

## 账户现金与总资产

账户现金按`account+currency`记录，支持：

- AI自然语言设置余额、入金、出金和换汇
- Settings页面手动设置指定账户/币种余额
- backup、operation和rollback
- USD/HKD按实时汇率折算为CNY

总资产口径为：

```text
Σ(持仓数量×实时价格×CNY汇率)+Σ(账户现金×CNY汇率)
```

持仓明细保留原币种价格和市值，顶部汇总统一显示CNY折算值。

### 自然语言现金示例

```text
IB港币减少4000        → IBKR/HKD withdraw 4000
IB港币变为20.32       → IBKR/HKD set_cash 20.32
长桥500港币换成20美元 → 同一操作内HKD withdraw 500 + USD deposit 20
银河提现了5k元        → 银河/CNY withdraw 5000
```

换汇记录保存实际成交的两端金额；资产汇总仍使用当前USD/CNY、HKD/CNY等实时汇率。减少现金、提现和换汇会在preview阶段检查当前对应币种余额；余额不足时禁用确认，后端confirm阶段还会再次校验。

## 测试工具

Settings中的reset工具只用于测试环境清理数据，不删除DeepSeek配置、Nginx密码和备份。

## 写入一致性与失败保护

- 同一个`pending_id`重复或并发confirm只会生成一次真实写入，重试返回原operation，不会重复加仓或重复入金。
- revise会生成新的确认卡；同一旧卡只能产生一个有效后继卡，相同修改请求重试会返回同一后继卡。
- pending过期、取消、替代、确认和回滚都是终态，终态卡不能再次写入；页面重新加载时会根据operation日志校准状态。
- `portfolio.json`、pending和operation均使用唯一临时文件原子替换；operation创建失败时会恢复写入前账本。
- 账本JSON损坏、身份重复、金额非法或operation日志损坏时采用fail-closed策略：拒绝继续写入，不会把异常账本当成空账本覆盖。
- 用户写入、回滚和行情价格更新共享互斥保护，防止并发读改写覆盖现金或持仓。

当前实时汇率和估值闭环支持`CNY`、`USD`、`HKD`。其他币种在补充对应FX数据源前会阻止确认，避免产生无法计入总资产的记录。

股票买卖记录当前只变更证券持仓，不自动增减账户现金；需要现金同步时应另行记录入金、出金或余额变更，避免系统擅自推断结算方式、费用或融资余额。
