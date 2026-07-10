"""Lightweight online instrument-name search for bookkeeping resolution.

This service keeps provider-specific market/exchange details outside the
portfolio schema. It returns only user-facing instrument metadata.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import httpx


class InstrumentSearchService:
    _URL = "https://searchapi.eastmoney.com/api/suggest/get"
    _TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        }

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[\s()（）\-_/·]", "", str(value or "").lower())

    @classmethod
    def _base_name(cls, value: str) -> str:
        text = cls._normalize_text(value)
        # Remove common class/exchange suffixes only for ranking. Stored names
        # always use the provider's original name.
        text = re.sub(r"(?:qdii)?lof[ac]?$", "", text)
        text = re.sub(r"etf(?:联接)?[ac]?$", "", text)
        text = re.sub(r"[ac]$", "", text)
        return text

    @staticmethod
    def _metadata(item: Dict[str, Any]) -> tuple[str, str]:
        classify = str(item.get("Classify") or "")
        market_type = str(item.get("MarketType") or "")
        security_name = str(item.get("SecurityTypeName") or "")
        name = str(item.get("Name") or "")

        if classify == "UsStock" or market_type == "7":
            return "USD", "etf" if "ETF" in name.upper() else "stock"
        if classify in {"HKStock", "HK"} or market_type in {"3", "116"}:
            return "HKD", "etf" if "ETF" in name.upper() else "stock"
        if classify in {"Fund", "OTCFUND"} or security_name == "基金":
            return "CNY", "etf" if "ETF" in name.upper() else "fund"
        return "CNY", "stock"

    @classmethod
    def _score(cls, keyword: str, item: Dict[str, Any]) -> int:
        query = cls._normalize_text(keyword)
        query_base = cls._base_name(keyword)
        name = cls._normalize_text(str(item.get("Name") or ""))
        name_base = cls._base_name(str(item.get("Name") or ""))
        code = str(item.get("Code") or "").upper()
        classify = str(item.get("Classify") or "")

        score = 0
        if query and name == query:
            score += 180
        elif query_base and name_base == query_base:
            score += 150
        elif query and name.startswith(query):
            score += 120
        elif query and query in name:
            score += 80
        if keyword.strip().upper() == code:
            score += 200
        if classify in {"Fund", "AStock", "UsStock", "HKStock"}:
            score += 20
        if classify == "OTCFUND":
            score += 5
        # Prefer exchange-traded instruments for brokerage-style wording.
        if classify == "Fund":
            score += 10
        return score

    async def search(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        keyword = str(keyword or "").strip()
        if not keyword:
            return []

        params = {
            "input": keyword,
            "type": "14",
            "token": self._TOKEN,
            "count": str(max(1, min(limit, 20))),
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            trust_env=False,
            follow_redirects=True,
            headers=self.headers,
        ) as client:
            response = await client.get(self._URL, params=params)
            response.raise_for_status()
            payload = response.json()

        raw_items = payload.get("QuotationCodeTable", {}).get("Data", []) or []
        candidates: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_items:
            code = str(item.get("Code") or "").strip().upper()
            name = str(item.get("Name") or "").strip()
            if not code or not name:
                continue
            currency, asset_type = self._metadata(item)
            key = (code, currency)
            score = self._score(keyword, item)
            candidate = {
                "code": code,
                "name": name,
                "currency": currency,
                "asset_type": asset_type,
                "score": score,
                "classify": str(item.get("Classify") or ""),
                "source": "eastmoney_search",
            }
            # Deduplicate the exchange/OTC representations of the same code,
            # retaining the stronger match.
            existing_index = next((i for i, row in enumerate(candidates) if (row["code"], row["currency"]) == key), None)
            if existing_index is not None:
                if score > candidates[existing_index]["score"]:
                    candidates[existing_index] = candidate
                continue
            seen.add(key)
            candidates.append(candidate)

        candidates.sort(key=lambda row: (-int(row["score"]), row["code"]))
        return candidates[:limit]

    @staticmethod
    def choose_confident(candidates: List[Dict[str, Any]]) -> Dict[str, Any] | None:
        if not candidates:
            return None
        first = candidates[0]
        second_score = int(candidates[1]["score"]) if len(candidates) > 1 else -1
        first_score = int(first["score"])
        if first_score >= 140 and first_score - second_score >= 25:
            return first
        return None
