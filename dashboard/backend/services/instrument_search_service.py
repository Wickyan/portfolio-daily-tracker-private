"""Lightweight online instrument-name search for bookkeeping resolution.

This service keeps provider-specific market/exchange details outside the
portfolio schema. It returns only user-facing instrument metadata.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List

import httpx
from pypinyin import lazy_pinyin


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

    @classmethod
    def _fuzzy_text(cls, value: str) -> str:
        text = cls._normalize_text(value)
        # Normalize common market abbreviations, not user-specific typo pairs.
        semantic_aliases = {
            "纳斯达克": "纳指",
            "标准普尔": "标普",
            "恒生科技": "恒科",
        }
        for full, short in semantic_aliases.items():
            text = text.replace(full, short)
        text = re.sub(r"(?:etf|lof|qdii|指数|基金|股票|联接)", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\d+", "", text)
        return text

    @staticmethod
    def _phonetic_text(value: str) -> str:
        return "".join(lazy_pinyin(str(value or ""))).lower()

    @classmethod
    def fuzzy_similarity(cls, keyword: str, candidate_name: str) -> float:
        query = cls._fuzzy_text(keyword)
        candidate = cls._fuzzy_text(candidate_name)
        if not query or not candidate:
            return 0.0
        if query == candidate:
            return 1.0
        ratio = SequenceMatcher(None, query, candidate).ratio()
        if query in candidate or candidate in query:
            ratio = max(ratio, min(len(query), len(candidate)) / max(len(query), len(candidate)))

        # Generic homophone tolerance handles input-method typos such as
        # “达成/大成” or “纳之/纳指” without maintaining typo-pair rules.
        query_pinyin = cls._phonetic_text(query)
        candidate_pinyin = cls._phonetic_text(candidate)
        if query_pinyin and candidate_pinyin:
            ratio = max(ratio, SequenceMatcher(None, query_pinyin, candidate_pinyin).ratio())
        return round(ratio, 6)

    @classmethod
    def rank_candidates(cls, keyword: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ranked: List[Dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            item["match_score"] = cls.fuzzy_similarity(keyword, str(item.get("name") or ""))
            ranked.append(item)
        ranked.sort(
            key=lambda row: (
                -float(row.get("match_score") or 0.0),
                -int(row.get("score") or 0),
                str(row.get("code") or ""),
            )
        )
        return ranked

    @staticmethod
    def _metadata(item: Dict[str, Any]) -> tuple[str, str]:
        classify = str(item.get("Classify") or "")
        market_type = str(item.get("MarketType") or "")

        if classify == "UsStock" or market_type == "7":
            return "USD", "stock"
        if classify in {"HKStock", "HK"} or market_type in {"3", "116"}:
            return "HKD", "stock"
        # A-share stocks, ETF, LOF and fund products are all handled by the
        # same quantity × price bookkeeping path, so expose one type.
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
        query_mentions_etf = "etf" in query
        candidate_is_etf = "etf" in name
        if candidate_is_etf and not query_mentions_etf:
            score -= 70
        if classify == "UsStock" and not candidate_is_etf:
            score += 25
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
        listed_classifies = {"Fund", "AStock", "UsStock", "HKStock", "HK"}
        for item in raw_items:
            classify = str(item.get("Classify") or "")
            # The bookkeeping system only accepts securities that can be traded
            # directly on an exchange. OTC mutual-fund shares are never candidates.
            if classify not in listed_classifies:
                continue
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
        if "match_score" in first:
            first_match = float(first.get("match_score") or 0.0)
            second_match = float(candidates[1].get("match_score") or 0.0) if len(candidates) > 1 else 0.0
            if first_match >= 0.72 and (len(candidates) == 1 or first_match - second_match >= 0.12):
                return first
            return None
        second_score = int(candidates[1]["score"]) if len(candidates) > 1 else -1
        first_score = int(first["score"])
        if first_score >= 180 and first_score - second_score >= 25:
            return first
        return None
