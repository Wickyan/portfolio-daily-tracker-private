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
