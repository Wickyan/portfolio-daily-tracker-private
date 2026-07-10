"""Tencent quote provider for A-share, Hong Kong and US equities.

The public ``qt.gtimg.cn`` endpoint returns a compact tilde-separated quote.
This provider intentionally keeps exchange-specific formatting inside the
market-data adapter; portfolio.json continues to store only user-facing code.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
import asyncio
import re

import httpx

from core.data.base import MarketDataProvider
from core.models import Market, Quote, Stock


class TencentDirectProvider(MarketDataProvider):
    _BASE_URL = "https://qt.gtimg.cn/q="

    def __init__(self) -> None:
        self._headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
            "Accept": "text/plain,*/*",
        }

    @property
    def name(self) -> str:
        return "Tencent Direct"

    @property
    def supported_markets(self) -> List[Market]:
        return [Market.A_SHARE, Market.HK_STOCK, Market.US_STOCK]

    def _query_symbol(self, symbol: str, market: Market) -> str:
        code = str(symbol or "").strip().upper()
        if market == Market.US_STOCK:
            return f"us{code}"
        if market == Market.HK_STOCK:
            digits = re.sub(r"\D", "", code)
            return f"hk{digits.zfill(5)}"
        if code.startswith(("6", "5", "9")):
            return f"sh{code}"
        return f"sz{code}"

    @staticmethod
    def _number(value: str, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _parse_payload(self, requested_symbol: str, market: Market, text: str) -> Optional[Quote]:
        match = re.search(r'=\"(.*)\";?\s*$', text.strip())
        if not match:
            return None
        fields = match.group(1).split("~")
        if len(fields) < 35:
            return None

        price = self._number(fields[3])
        if price <= 0:
            return None

        name = fields[1].strip() or requested_symbol
        returned_code = fields[2].strip() or requested_symbol
        if market == Market.US_STOCK:
            returned_code = returned_code.split(".", 1)[0].upper()
        elif market == Market.HK_STOCK:
            returned_code = re.sub(r"\D", "", requested_symbol).zfill(4)
        else:
            returned_code = re.sub(r"\D", "", requested_symbol)

        volume = int(self._number(fields[6]))
        amount = 0.0
        if len(fields) > 37:
            amount = self._number(fields[37])

        return Quote(
            stock=Stock(symbol=returned_code, name=name, market=market),
            price=price,
            open=self._number(fields[5], price),
            high=self._number(fields[33], price),
            low=self._number(fields[34], price),
            prev_close=self._number(fields[4], price),
            volume=volume,
            amount=amount,
            timestamp=datetime.now(),
        )

    async def get_quote(self, symbol: str, market: Market) -> Optional[Quote]:
        query_symbol = self._query_symbol(symbol, market)
        try:
            async with httpx.AsyncClient(
                timeout=8.0,
                trust_env=False,
                follow_redirects=True,
                headers=self._headers,
            ) as client:
                response = await client.get(f"{self._BASE_URL}{query_symbol}")
                response.raise_for_status()
                text = response.content.decode("gbk", errors="ignore")
            return self._parse_payload(symbol, market, text)
        except Exception as exc:
            print(f"[Tencent Direct] 获取 {symbol} 失败: {exc}")
            return None

    async def get_quotes(self, symbols: List[str], market: Market) -> List[Quote]:
        results = await asyncio.gather(
            *(self.get_quote(symbol, market) for symbol in symbols),
            return_exceptions=True,
        )
        return [result for result in results if isinstance(result, Quote)]

    async def search_stock(self, keyword: str) -> List[Stock]:
        results: List[Stock] = []
        for market in self.supported_markets:
            quote = await self.get_quote(keyword, market)
            if quote:
                results.append(quote.stock)
        return results

    async def get_stock_info(self, symbol: str, market: Market) -> Optional[Stock]:
        quote = await self.get_quote(symbol, market)
        return quote.stock if quote else None
