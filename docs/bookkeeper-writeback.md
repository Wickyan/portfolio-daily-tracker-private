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

## 用户表达与安全识别

记账解析会区分“已经发生的操作”和咨询、计划、否定表达：

```text
银河增加2.2w美元           → 生成待确认卡
明天给IBKR入金100美元      → 普通对话，不写账
怎么给IBKR入金100美元      → 普通对话，不写账
不要给IBKR入金100美元      → 普通对话，不写账
```

金额支持`k/K/千/w/W/万`、合法千分位、全角数字以及`$`、`HK$`、`¥/￥`。科学计数法、错误千分位、多个小数点、负入金或负数量不会被截断成另一个正数，而是阻止确认并要求修改。

一条消息中的多条现金变化可按原顺序组成同一个原子operation，例如：

```text
银河增加100元，然后长桥增加200美元
```

不同类型的交易混在同一条消息时会要求拆分，避免只解析其中一半。代码与币种也会交叉校验，例如`1810+USD`会阻止确认并提示该代码通常使用HKD。

## 标的搜索边界

本系统只记录可在证券交易所直接买卖的标的。在线搜索只保留A股、港股、美股以及场内ETF/LOF等上市证券；场外申购赎回的OTC基金份额（例如基金联接A/C类）不会作为候选，也不会自动写入。

## 标的错别字处理

标的名称不维护“某个错字→某个正确字”的专用规则。系统会并行搜索多个场内候选，并结合名称相似度、拼音相似度、简称展开和候选间差距判断：

- 唯一高置信候选：自动填入代码，同时明确提示“可能包含简称或错别字”，等待用户确认。
- 多个候选接近：不自动写入代码，展示候选按钮，由用户选择。
- 场外OTC基金始终排除，不参与候选和自动匹配。

## 确认卡上下文修改

确认卡内的“补充/修改信息”不是独立的新记账消息。后端会把当前确认卡、缺失字段、搜索候选和用户补充一起交给结构化修改解析器；解析器只能提出字段变更或场内标的搜索词，不能自行编造证券代码。随后仍需通过场内标的搜索、候选置信度和账本校验，再生成新的确认卡。

例如一张“纳斯达克、代码缺失”的卡片中输入`国泰`，系统会结合原卡主题理解为对发行方/管理人的限定，搜索`纳指ETF国泰`并生成`513100`的新卡；若候选不唯一，则继续显示候选按钮让用户选择。

卡片操作区只保留一个主按钮：输入了修改内容时显示“应用修改并生成新卡”，未输入修改且信息完整时显示“确认写入”。修改请求失败会直接在卡片内显示错误，不再静默无响应。
