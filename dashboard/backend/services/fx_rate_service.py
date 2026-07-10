"""Current FX rates for converting portfolio totals to CNY.

Provider-specific symbols stay inside this adapter. Portfolio data stores only
ISO currency codes. Tencent's public forex feed is the primary source; the
last successful rates are cached briefly to avoid slowing every page request.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import re
from typing import Dict, Optional

import httpx


class FXRateService:
    _BASE_URL = "https://qt.gtimg.cn/q="
    _QUERY_BY_CURRENCY = {
        "USD": "whUSDCNY",
        "HKD": "whHKDCNY",
    }
    _cache: Dict[str, float] = {"CNY": 1.0}
    _cache_at: Optional[datetime] = None
    _cache_ttl = timedelta(minutes=5)

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
            "Accept": "text/plain,*/*",
        }

    @staticmethod
    def parse_tencent_rate(text: str) -> Optional[float]:
        match = re.search(r'=\"(.*)\";?\s*$', str(text or "").strip())
        if not match:
            return None
        fields = match.group(1).split("~")
        if len(fields) < 4:
            return None
        try:
            value = float(fields[3])
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def _fetch_one(self, currency: str) -> tuple[str, Optional[float]]:
        query = self._QUERY_BY_CURRENCY[currency]
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=False,
                follow_redirects=True,
                headers=self.headers,
            ) as client:
                response = await client.get(f"{self._BASE_URL}{query}")
                response.raise_for_status()
                text = response.content.decode("gbk", errors="ignore")
            return currency, self.parse_tencent_rate(text)
        except Exception as exc:
            print(f"[FX] 获取{currency}/CNY失败: {exc}")
            return currency, None

    async def get_rates(self, force: bool = False) -> Dict[str, float]:
        now = datetime.now()
        if (
            not force
            and self.__class__._cache_at is not None
            and now - self.__class__._cache_at < self.__class__._cache_ttl
            and all(code in self.__class__._cache for code in self._QUERY_BY_CURRENCY)
        ):
            return dict(self.__class__._cache)

        results = await asyncio.gather(
            *(self._fetch_one(currency) for currency in self._QUERY_BY_CURRENCY)
        )
        updated = dict(self.__class__._cache)
        updated["CNY"] = 1.0
        success = False
        for currency, rate in results:
            if rate is not None:
                updated[currency] = rate
                success = True

        # Do not erase a previous good rate when one source temporarily fails.
        self.__class__._cache = updated
        if success or self.__class__._cache_at is None:
            self.__class__._cache_at = now
        return dict(updated)

    @classmethod
    def cache_timestamp(cls) -> Optional[str]:
        return cls._cache_at.isoformat() if cls._cache_at else None
