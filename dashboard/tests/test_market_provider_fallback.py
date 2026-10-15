from __future__ import annotations

import unittest
from datetime import datetime
from typing import List, Optional

from core.data.base import MarketDataProvider
from core.models import Market, Quote, Stock
from providers.market_data.multi_source_provider import MultiSourceProvider


class FakeMarketProvider(MarketDataProvider):
    def __init__(self, name: str, *, quote: Optional[Quote] = None, fail: bool = False):
        self._name = name
        self.quote = quote
        self.fail = fail
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_markets(self) -> List[Market]:
        return [Market.US_STOCK, Market.HK_STOCK, Market.A_SHARE]

    async def get_quote(self, symbol: str, market: Market) -> Optional[Quote]:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} unavailable")
        return self.quote

    async def get_quotes(self, symbols: List[str], market: Market) -> List[Quote]:
        if self.fail:
            raise RuntimeError(f"{self.name} unavailable")
        return [self.quote] if self.quote else []

    async def search_stock(self, keyword: str) -> List[Stock]:
        return []

    async def get_stock_info(self, symbol: str, market: Market) -> Optional[Stock]:
        return self.quote.stock if self.quote else None


class MultiSourceProviderFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_next_provider_takes_over_when_first_source_fails(self):
        quote = Quote(
            stock=Stock("NVDA", "NVIDIA", Market.US_STOCK),
            price=210.0,
            open=205.0,
            high=212.0,
            low=204.0,
            prev_close=206.0,
            volume=100,
            amount=21000.0,
            timestamp=datetime.now(),
        )
        primary = FakeMarketProvider("primary", fail=True)
        secondary = FakeMarketProvider("secondary", quote=quote)
        provider = MultiSourceProvider(
            [primary, secondary],
            provider_timeout=0.5,
            cache_ttl=0,
            stale_ttl=0,
        )

        result = await provider.get_quote("NVDA", Market.US_STOCK)

        self.assertIsNotNone(result)
        self.assertEqual(result.price, 210.0)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(secondary.calls, 1)

    async def test_last_successful_provider_is_reused(self):
        quote = Quote(
            stock=Stock("NVDA", "NVIDIA", Market.US_STOCK),
            price=211.0,
            open=205.0,
            high=212.0,
            low=204.0,
            prev_close=206.0,
            volume=100,
            amount=21100.0,
            timestamp=datetime.now(),
        )
        primary = FakeMarketProvider("primary", fail=True)
        secondary = FakeMarketProvider("secondary", quote=quote)
        provider = MultiSourceProvider(
            [primary, secondary],
            provider_timeout=0.5,
            cache_ttl=0,
            stale_ttl=0,
        )

        await provider.get_quote("NVDA", Market.US_STOCK)
        await provider.get_quote("AAPL", Market.US_STOCK)

        self.assertEqual(primary.calls, 1)
        self.assertEqual(secondary.calls, 2)


if __name__ == "__main__":
    unittest.main()
