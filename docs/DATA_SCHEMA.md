# Portfolio账本数据结构

## 持仓唯一身份

每条持仓使用以下三元组唯一定位：

```text
account+code+currency
```

同一代码可以同时存在于不同券商账户，例如：

```text
IBKR+AAPL+USD
长桥+AAPL+USD
```

编辑、删除和合并持仓时，不能只使用`code`。

## 规范持仓字段

```json
{
  "account": "IBKR",
  "name": "Apple",
  "code": "AAPL",
  "currency": "USD",
  "asset_type": "stock",
  "quantity": 3,
  "cost_price": 202.68,
  "total_cost": 608.04,
  "fee": 0,
  "note": "",
  "source": "ai",
  "created_at": "2026-07-10T22:00:00",
  "updated_at": "2026-07-10T22:00:00"
}
```

当前行情兼容层还可能保存：

```text
available_qty
current_price
side
```

这些字段不是持仓身份的一部分。

## 不再写入的字段

```text
market
exchange
canonical_symbol
group
symbol
```

旧输入中的`group`和`symbol`只用于读取兼容：

```text
group→account
symbol→code
```

任何新写入都会转换成规范字段，`portfolio.json`不会再次保存旧别名。

## asset_type口径

- 所有有标准代码、按“数量×价格”估值的交易标的统一记为`stock`，包括普通股票、ETF、LOF和其他上市基金。
- 无标准代码的自定义资产记为`custom`。
- 现金不存入positions，按账户和币种存入`cash_accounts`；旧的标量`cash`只保留兼容。
- 旧数据中的`fund`、`etf`、`fund_or_custom`在读取和再次保存时会归一化为`stock`。


## 账户现金

```json
{
  "cash_accounts": [
    {
      "account": "IBKR",
      "currency": "USD",
      "amount": 1000,
      "updated_at": "2026-07-11T01:00:00"
    },
    {
      "account": "IBKR",
      "currency": "HKD",
      "amount": 2000,
      "updated_at": "2026-07-11T01:05:00"
    }
  ]
}
```

现金唯一身份为`account+currency`。因此同一券商可以同时存在多种币种，例如`IBKR+USD`和`IBKR+HKD`；它们分别设置、入金、出金和回滚，互不覆盖。出金后余额不得小于0。

## 总资产与汇率

- 每条持仓先按原币种计算`quantity×current_price`。
- USD和HKD使用实时汇率折算为CNY；CNY汇率恒为1。
- 顶部`total_market_value`、`cash`、`total_assets`、`total_profit`统一为CNY折算值。
- API同时返回原币种数值、`market_value_cny`、`profit_cny`、`fx_rate`、`fx_rates`和`fx_updated_at`。
- 每条持仓返回`asset_weight_pct=该持仓CNY市值/总资产CNY`，页面显示为“持仓比例”；另返回`holding_weight_pct=该持仓CNY市值/全部持仓CNY市值`。
- 每条账户现金返回`asset_weight_pct=该现金CNY折算值/总资产CNY`。同一券商的不同币种分别计算后再参与总资产汇总。
- 新增或确认持仓后，后端先刷新实时行情再返回，前端随后重新获取总资产。


## 换汇操作

换汇不新增独立余额类型，而是在同一个operation中保存两条现金变化：

```json
[
  {"action_type": "withdraw", "account": "长桥", "currency": "HKD", "amount": 500},
  {"action_type": "deposit", "account": "长桥", "currency": "USD", "amount": 20}
]
```

两条变化必须同时成功；preview和confirm都会检查换出币种余额，余额不足时整笔操作失败，不得只写入换入币种。回滚时两条变化也同时撤回。成交金额使用用户提供的实际换出/换入金额，总资产则按当前实时汇率重新估值。
