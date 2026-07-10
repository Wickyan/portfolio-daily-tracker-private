"""手动持仓Provider。

portfolio.json使用规范账本字段。group/symbol只作为旧数据读取兼容，保存时
统一写为account/code；market仅在内存中用于行情Provider，不写入账本。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.data.base import MarketDataProvider, PortfolioProvider
from core.models import Market, Portfolio, Position, PositionSide, Stock


class ManualPortfolioProvider(PortfolioProvider):
    def __init__(
        self,
        data_file: str = "portfolio.json",
        market_provider: Optional[MarketDataProvider] = None,
    ):
        self.data_file = Path(data_file)
        self.market_provider = market_provider
        self._portfolio = Portfolio()
        self._last_update: Optional[datetime] = None
        self._load()

    @property
    def name(self) -> str:
        return "手动管理"

    @property
    def last_update(self) -> Optional[datetime]:
        return self._last_update

    @staticmethod
    def _infer_internal_market(symbol: str, currency: str = "") -> str:
        symbol = str(symbol or "").upper()
        currency = str(currency or "").upper()
        if currency == "HKD" or (symbol.isdigit() and len(symbol) == 4):
            return "hk_stock"
        if currency == "USD" or (symbol.isascii() and symbol and not symbol.isdigit()):
            return "us_stock"
        return "a_share"

    @staticmethod
    def _infer_currency(symbol: str) -> str:
        symbol = str(symbol or "").upper()
        if symbol.isdigit() and len(symbol) == 4:
            return "HKD"
        if symbol.isascii() and symbol and not symbol.isdigit():
            return "USD"
        return "CNY"

    @staticmethod
    def _infer_asset_type(symbol: str) -> str:
        return "stock" if str(symbol or "").strip() else "custom"

    @staticmethod
    def _identity(account: str, code: str, currency: str) -> Tuple[str, str, str]:
        return str(account or "").strip(), str(code or "").strip(), str(currency or "").upper().strip()

    @staticmethod
    def _position_identity(position: Position) -> Tuple[str, str, str]:
        return ManualPortfolioProvider._identity(
            getattr(position, "account", ""),
            position.stock.symbol,
            getattr(position, "currency", ""),
        )

    def _load(self) -> None:
        if not self.data_file.exists():
            return

        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            positions: List[Position] = []
            for raw in data.get("positions", []):
                code = str(raw.get("code") or raw.get("symbol") or "").strip()
                if not code:
                    continue

                account = str(raw.get("account") or raw.get("group") or "").strip()
                currency = str(raw.get("currency") or self._infer_currency(code)).upper()
                asset_type = "stock" if code else "custom"
                internal_market = self._infer_internal_market(code, currency)
                stock = Stock(
                    symbol=code,
                    name=str(raw.get("name") or code),
                    market=Market(internal_market),
                )
                position = Position(
                    stock=stock,
                    quantity=raw.get("quantity", 0),
                    available_qty=raw.get("available_qty", raw.get("quantity", 0)),
                    cost_price=raw.get("cost_price", 0),
                    current_price=raw.get("current_price", raw.get("cost_price", 0)),
                    side=PositionSide(raw.get("side", "long")),
                )
                # Position dataclass has no slots, so attach bookkeeping metadata
                # without changing the market-domain model yet.
                position.account = account
                position.currency = currency
                position.asset_type = asset_type
                position.total_cost = raw.get("total_cost")
                position.fee = raw.get("fee")
                position.note = raw.get("note", "")
                position.source = raw.get("source", "manual")
                position.created_at = raw.get("created_at")
                position.updated_at = raw.get("updated_at")
                positions.append(position)

            self._portfolio = Portfolio(
                positions=positions,
                cash=data.get("cash", 0.0),
            )
            self._last_update = datetime.now()
        except Exception as exc:
            print(f"加载持仓文件失败: {exc}")

    def _position_to_record(self, position: Position) -> Dict[str, Any]:
        quantity = position.quantity
        cost_price = position.cost_price
        return {
            "account": str(getattr(position, "account", "") or ""),
            "code": position.stock.symbol,
            "name": position.stock.name,
            "currency": str(getattr(position, "currency", "") or self._infer_currency(position.stock.symbol)).upper(),
            "asset_type": self._infer_asset_type(position.stock.symbol),
            "quantity": quantity,
            "available_qty": position.available_qty,
            "cost_price": cost_price,
            "total_cost": getattr(position, "total_cost", None) or quantity * cost_price,
            "fee": getattr(position, "fee", None),
            "note": str(getattr(position, "note", "") or ""),
            "source": str(getattr(position, "source", "manual") or "manual"),
            "created_at": getattr(position, "created_at", None) or datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "current_price": position.current_price,
            "side": position.side.value,
        }

    def _save(self) -> None:
        data = {
            "positions": [self._position_to_record(p) for p in self._portfolio.positions],
            "cash": self._portfolio.cash,
            "updated_at": datetime.now().isoformat(),
        }
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.data_file.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp_path.replace(self.data_file)

    async def get_portfolio(self) -> Portfolio:
        return self._portfolio

    async def get_positions(self) -> List[Position]:
        return self._portfolio.positions

    async def refresh(self) -> None:
        if not self.market_provider or not self._portfolio.positions:
            return

        positions_by_market: Dict[Market, List[Position]] = {}
        for position in self._portfolio.positions:
            positions_by_market.setdefault(position.stock.market, []).append(position)

        async def refresh_market_positions(market: Market, positions: List[Position]) -> None:
            symbols = list(dict.fromkeys(position.stock.symbol for position in positions))
            quote_map = {}

            try:
                quotes = await self.market_provider.get_quotes(symbols, market)
                quote_map = {quote.stock.symbol: quote for quote in quotes}
            except Exception as exc:
                print(f"批量刷新 {market.value} 失败: {exc}")

            for position in positions:
                quote = quote_map.get(position.stock.symbol)
                if quote is None:
                    try:
                        quote = await self.market_provider.get_quote(
                            position.stock.symbol,
                            position.stock.market,
                        )
                    except Exception as exc:
                        print(f"刷新 {position.stock.symbol} 价格失败: {exc}")
                        quote = None
                if quote:
                    position.current_price = quote.price

        await asyncio.gather(
            *(refresh_market_positions(market, positions) for market, positions in positions_by_market.items())
        )
        self._last_update = datetime.now()
        self._save()

    # Legacy provider methods remain for compatibility. New user-visible writes
    # go through PortfolioWriteService and identify positions by account+code+currency.
    def add_position(
        self,
        symbol: str,
        name: str,
        quantity: float,
        cost_price: float,
        market: Market = Market.A_SHARE,
        available_qty: Optional[float] = None,
        current_price: Optional[float] = None,
        account: str = "",
        currency: str = "",
        asset_type: str = "",
    ) -> None:
        code = str(symbol).strip()
        resolved_currency = str(currency or self._infer_currency(code)).upper()
        identity = self._identity(account, code, resolved_currency)
        matches = [p for p in self._portfolio.positions if self._position_identity(p) == identity]
        if len(matches) > 1:
            raise ValueError(f"检测到重复持仓身份: {identity}")

        position = Position(
            stock=Stock(symbol=code, name=name, market=market),
            quantity=quantity,
            available_qty=available_qty if available_qty is not None else quantity,
            cost_price=cost_price,
            current_price=current_price if current_price is not None else cost_price,
        )
        position.account = account
        position.currency = resolved_currency
        position.asset_type = asset_type or self._infer_asset_type(code)
        position.total_cost = quantity * cost_price
        position.fee = None
        position.note = ""
        position.source = "manual"
        position.created_at = datetime.now().isoformat()
        position.updated_at = datetime.now().isoformat()

        if matches:
            index = self._portfolio.positions.index(matches[0])
            self._portfolio.positions[index] = position
        else:
            self._portfolio.positions.append(position)
        self._save()

    def remove_position(self, symbol: str, account: str = "", currency: str = "") -> None:
        identity = self._identity(account, symbol, currency or self._infer_currency(symbol))
        matches = [p for p in self._portfolio.positions if self._position_identity(p) == identity]
        if not matches:
            raise FileNotFoundError(identity)
        if len(matches) > 1:
            raise ValueError(f"检测到重复持仓身份: {identity}")
        self._portfolio.positions.remove(matches[0])
        self._save()

    def update_position(
        self,
        symbol: str,
        quantity: Optional[float] = None,
        cost_price: Optional[float] = None,
        available_qty: Optional[float] = None,
        account: str = "",
        currency: str = "",
    ) -> None:
        identity = self._identity(account, symbol, currency or self._infer_currency(symbol))
        matches = [p for p in self._portfolio.positions if self._position_identity(p) == identity]
        if not matches:
            raise FileNotFoundError(identity)
        if len(matches) > 1:
            raise ValueError(f"检测到重复持仓身份: {identity}")
        position = matches[0]
        if quantity is not None:
            position.quantity = quantity
        if cost_price is not None:
            position.cost_price = cost_price
        if available_qty is not None:
            position.available_qty = available_qty
        position.total_cost = position.quantity * position.cost_price
        position.updated_at = datetime.now().isoformat()
        self._save()

    def set_cash(self, cash: float) -> None:
        self._portfolio.cash = cash
        self._save()

    def import_from_csv(self, csv_file: str) -> None:
        import csv

        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.add_position(
                    symbol=row["代码"],
                    name=row["名称"],
                    quantity=float(row["数量"]),
                    cost_price=float(row["成本价"]),
                    available_qty=float(row.get("可卖数量", row["数量"])),
                    account=row.get("账户", ""),
                    currency=row.get("币种", ""),
                )
