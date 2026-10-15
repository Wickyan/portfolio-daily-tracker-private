"""Screenshot-to-ledger ingestion for V3.

This module is intentionally independent from the legacy chat/image path.
Images are never persisted here.  They are hashed, normalized, optionally
split into overlapping vertical tiles, sent to a configured vision model, and
converted into auditable Transaction candidates.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import json
import os
import re
import threading

import httpx
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

from config import get_config
from core.llm.base import LLMConfig, LLMProvider, LLMResponse
from providers.llm import LLMProviderType, create_llm_provider

from .models import Transaction, TransactionType, normalize_event_time


MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_PIXELS = 120_000_000
MAX_NORMALIZED_WIDTH = 1800
TILE_HEIGHT = 2200
TILE_OVERLAP = 280
MAX_TILES_PER_IMAGE = 64
Image.MAX_IMAGE_PIXELS = MAX_TOTAL_PIXELS


@dataclass(frozen=True)
class VisionProviderSpec:
    provider: Any
    family: str
    model: str
    label: str


@dataclass(frozen=True)
class ImageTile:
    image_sha256: str
    filename: str
    index: int
    y0: int
    y1: int
    image_width: int
    image_height: int
    jpeg_bytes: bytes

    @property
    def data_b64(self) -> str:
        return base64.b64encode(self.jpeg_bytes).decode("ascii")


@dataclass
class ScreenshotCandidate:
    event: Optional[Transaction]
    raw: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    duplicate_reason: Optional[str] = None

    def payload(self) -> Dict[str, Any]:
        return {
            "event": self.event,
            "raw": self.raw,
            "warnings": list(self.warnings),
            "missing_fields": list(self.missing_fields),
            "duplicate_reason": self.duplicate_reason,
        }


@dataclass
class ScreenshotExtractionResult:
    events: List[Transaction]
    duplicates: List[ScreenshotCandidate]
    unresolved: List[ScreenshotCandidate]
    warnings: List[str]
    image_summaries: List[Dict[str, Any]]
    model_label: str


def _provider_type(group: Dict[str, Any], model_id: str) -> Tuple[LLMProviderType, str]:
    explicit = str(group.get("provider_type") or "").lower()
    base_url = str(group.get("base_url") or "")
    if explicit in {"claude", "anthropic"} or "anthropic" in base_url.lower() or "claude" in model_id.lower():
        return LLMProviderType.CLAUDE, "claude"
    return LLMProviderType.OPENAI, "openai"


class ScreenshotVisionHTTPProvider:
    """Small proxy-aware client used only for screenshot vision extraction."""

    def __init__(
        self,
        *,
        family: str,
        config: LLMConfig,
        custom_headers: Optional[Dict[str, str]] = None,
        label: str,
    ):
        self.family = family
        self.config = config
        self.custom_headers = custom_headers or {}
        self.label = label
        self.proxy_url = os.getenv("VISION_PROXY_URL", "").strip() or None

    def _headers(self) -> Dict[str, str]:
        if self.custom_headers:
            return dict(self.custom_headers)
        if self.family == "claude":
            return {
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(self, messages: List[Dict[str, Any]], **kwargs) -> LLMResponse:
        if self.family == "claude":
            system = ""
            converted = []
            for message in messages:
                if message.get("role") == "system":
                    system = str(message.get("content") or "")
                else:
                    converted.append(message)
            payload: Dict[str, Any] = {
                "model": kwargs.get("model", self.config.model),
                "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
                "messages": converted,
                "temperature": kwargs.get("temperature", self.config.temperature),
            }
            if system:
                payload["system"] = system
            url = f"{self.config.base_url.rstrip('/')}/messages"
        else:
            payload = {
                "model": kwargs.get("model", self.config.model),
                "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
                "messages": messages,
                "temperature": kwargs.get("temperature", self.config.temperature),
            }
            url = f"{self.config.base_url.rstrip('/')}/chat/completions"

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout,
                proxy=self.proxy_url,
                trust_env=False,
            ) as client:
                response = await client.post(url, headers=self._headers(), json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise RuntimeError(f"{self.label} vision request failed: {type(exc).__name__}: {str(exc)[:300]}") from exc

        if self.family == "claude":
            content = "".join(
                str(item.get("text") or "")
                for item in data.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            )
            usage_data = data.get("usage") or {}
            usage = {
                "prompt_tokens": int(usage_data.get("input_tokens") or 0),
                "completion_tokens": int(usage_data.get("output_tokens") or 0),
            }
            finish_reason = str(data.get("stop_reason") or "")
            model = str(data.get("model") or self.config.model)
        else:
            choice = (data.get("choices") or [{}])[0]
            content = str((choice.get("message") or {}).get("content") or "")
            usage_data = data.get("usage") or {}
            usage = {
                "prompt_tokens": int(usage_data.get("prompt_tokens") or 0),
                "completion_tokens": int(usage_data.get("completion_tokens") or 0),
            }
            finish_reason = str(choice.get("finish_reason") or "")
            model = str(data.get("model") or self.config.model)

        return LLMResponse(
            content=content,
            model=model,
            usage=usage,
            finish_reason=finish_reason,
        )


def create_configured_vision_providers() -> List[VisionProviderSpec]:
    """Return configured vision models in preferred order without changing chat settings."""
    config = get_config()
    groups = config.get_all_api_groups()
    preferred = [
        "gpt-4o",
        "claude-sonnet-4-20250514",
        "gpt-4o-mini",
    ]
    candidates: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]] = []
    for group_id, group in groups.items():
        if not group.get("api_key") or not group.get("base_url"):
            continue
        for model in group.get("models", []):
            if not model.get("supports_vision"):
                continue
            model_id = str(model.get("id") or "")
            if not model_id:
                continue
            rank = preferred.index(model_id) if model_id in preferred else len(preferred) + 10
            candidates.append((rank, group_id, group, model))
    if not candidates:
        raise RuntimeError("没有配置可用的视觉模型；请先在设置中配置 supports_vision 的模型")

    candidates.sort(key=lambda item: (item[0], item[1]))
    specs: List[VisionProviderSpec] = []
    for _rank, group_id, group, model in candidates:
        model_id = str(model["id"])
        _ptype, family = _provider_type(group, model_id)
        llm_config = LLMConfig(
            api_key=group["api_key"],
            base_url=group["base_url"],
            model=model_id,
            temperature=0,
            max_tokens=5000,
            timeout=90,
        )
        label = f"{group.get('name', group_id)} / {model.get('name', model_id)}"
        provider = ScreenshotVisionHTTPProvider(
            family=family,
            config=llm_config,
            custom_headers=group.get("headers"),
            label=label,
        )
        specs.append(
            VisionProviderSpec(
                provider=provider,
                family=family,
                model=model_id,
                label=label,
            )
        )
    return specs


def create_configured_vision_provider() -> VisionProviderSpec:
    """Backward-compatible helper returning the first configured vision model."""
    return create_configured_vision_providers()[0]


_OCR_ENGINE = None
_OCR_ENGINE_LOCK = threading.Lock()


def _get_ocr_engine():
    global _OCR_ENGINE
    with _OCR_ENGINE_LOCK:
        if _OCR_ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise RuntimeError("本地OCR组件未安装") from exc
            _OCR_ENGINE = RapidOCR()
        return _OCR_ENGINE


def _ocr_tile_sync(tile: ImageTile) -> List[Dict[str, Any]]:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("本地OCR图像依赖未安装") from exc

    image_array = np.frombuffer(tile.jpeg_bytes, dtype=np.uint8)
    image = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("本地OCR无法解码图片切片")

    engine = _get_ocr_engine()
    with _OCR_ENGINE_LOCK:
        result, _elapsed = engine(image)

    rows: List[Dict[str, Any]] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        clean_text = str(text or "").strip()
        if not clean_text:
            continue
        try:
            points = [[float(p[0]), float(p[1])] for p in box]
            x = min(point[0] for point in points)
            y = min(point[1] for point in points)
        except Exception:
            points = []
            x = 0.0
            y = 0.0
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 0.0
        rows.append({
            "text": clean_text,
            "confidence": confidence,
            "x": x,
            "y": y,
            "box": points,
        })
    rows.sort(key=lambda item: (round(item["y"] / 10.0), item["x"]))
    return rows


def create_configured_text_provider() -> Tuple[LLMProvider, str]:
    """Create the currently selected text LLM for structured interpretation."""
    config = get_config()
    group = config.get_current_api_group()
    model_id = config.get_current_model()
    ptype, _family = _provider_type(group, model_id)
    llm_config = LLMConfig(
        api_key=group.get("api_key", ""),
        base_url=group.get("base_url"),
        model=model_id,
        temperature=0,
        max_tokens=5000,
        timeout=120,
    )
    provider = create_llm_provider(ptype, llm_config, group.get("headers"))
    label = f"{group.get('name', 'Text LLM')} / {model_id}"
    return provider, label


OCR_STRUCTURING_PROMPT = """You receive OCR text from one tile of a brokerage trade-history screenshot.
Convert only clearly executed/completed transactions into strict JSON using the same schema below.
Do not invent values that OCR did not contain. Do not interpret portfolio holdings, quotes,
watchlists, recommendations, cancelled orders, pending orders, or account summaries as trades.
OCR lines include approximate y/x coordinates and confidence to preserve reading order.
If a row is incomplete because it crosses a tile boundary, omit it; overlapping tiles are processed separately.

Return only JSON:
{
  "rows": [
    {
      "event_type": "BUY|SELL|DEPOSIT|WITHDRAW|DIVIDEND|FX|null",
      "effective_at": "YYYY-MM-DD or ISO datetime or null",
      "account": "broker/account name if visible or null",
      "code": "ticker/code if visible or null",
      "name": "security name if visible or null",
      "currency": "USD|HKD|CNY|null",
      "quantity": "decimal or null",
      "price": "per-unit execution price or null",
      "total_amount": "explicit total trade amount or null",
      "amount": "cash-event amount or null",
      "fee": "decimal or null",
      "tax": "decimal or null",
      "order_id": "order/execution id if visible or null",
      "status": "exact status text if visible or null",
      "confidence": 0.0,
      "raw_text": "short combined OCR text supporting this row"
    }
  ],
  "warnings": []
}
"""


class LocalOCRTextFallback:
    def __init__(self, provider: LLMProvider, label: str):
        self.provider = provider
        self.label = label

    async def extract_tile(
        self,
        tile: ImageTile,
        account_hint: str,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        ocr_rows = await asyncio.to_thread(_ocr_tile_sync, tile)
        if not ocr_rows:
            return [], ["本地OCR未识别到文字"]

        compact_rows = [
            {
                "text": row["text"],
                "confidence": round(float(row["confidence"]), 4),
                "x": round(float(row["x"]), 1),
                "y": round(float(row["y"]), 1),
            }
            for row in ocr_rows
            if float(row["confidence"]) >= 0.25
        ]
        hint = f"\nUser account hint: {account_hint}" if account_hint else ""
        tile_context = (
            f"File={tile.filename}; tile={tile.index + 1}; "
            f"range={tile.y0}-{tile.y1}/{tile.image_height}.{hint}"
        )
        user_text = (
            OCR_STRUCTURING_PROMPT
            + "\n\n"
            + tile_context
            + "\nOCR lines:\n"
            + json.dumps(compact_rows, ensure_ascii=False)
        )
        response = await self.provider.chat(
            [
                {
                    "role": "system",
                    "content": "You are a conservative financial transaction parser. Output JSON only.",
                },
                {"role": "user", "content": user_text},
            ],
            temperature=0,
            max_tokens=5000,
        )
        parsed = _extract_json(response.content)
        rows = parsed.get("rows") or parsed.get("transactions") or []
        if not isinstance(rows, list):
            raise ValueError("OCR structuring response rows must be an array")
        warnings = [str(item) for item in (parsed.get("warnings") or [])]
        warnings.append(f"使用本地OCR兜底，共识别{len(compact_rows)}个文字片段")
        return [row for row in rows if isinstance(row, dict)], warnings


def create_local_ocr_fallback() -> LocalOCRTextFallback:
    provider, label = create_configured_text_provider()
    return LocalOCRTextFallback(provider, f"本地 RapidOCR + {label}")


def _load_normalized_image(content: bytes) -> Image.Image:
    if not content:
        raise ValueError("empty image")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit")
    try:
        image = Image.open(BytesIO(content))
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("invalid image dimensions")
        if width * height > MAX_TOTAL_PIXELS:
            raise ValueError(f"image has too many pixels: {width}x{height}")
        image.load()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("unsupported, corrupted, or unsafe image") from exc
    image = ImageOps.exif_transpose(image)
    width, height = image.size
    if width > MAX_NORMALIZED_WIDTH:
        scale = MAX_NORMALIZED_WIDTH / float(width)
        image = image.resize((MAX_NORMALIZED_WIDTH, max(1, int(height * scale))), Image.Resampling.LANCZOS)
    if image.mode != "RGB":
        background = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    return image


def prepare_image_tiles(content: bytes, filename: str = "screenshot") -> Tuple[str, Tuple[int, int], List[ImageTile]]:
    image_hash = hashlib.sha256(content).hexdigest()
    image = _load_normalized_image(content)
    width, height = image.size

    if height <= TILE_HEIGHT:
        ranges = [(0, height)]
    else:
        step = TILE_HEIGHT - TILE_OVERLAP
        ranges = []
        y0 = 0
        while y0 < height:
            y1 = min(height, y0 + TILE_HEIGHT)
            ranges.append((y0, y1))
            if y1 >= height:
                break
            y0 += step
        if len(ranges) > MAX_TILES_PER_IMAGE:
            raise ValueError(f"long screenshot requires {len(ranges)} tiles, exceeds {MAX_TILES_PER_IMAGE} tile limit")

    tiles: List[ImageTile] = []
    for index, (y0, y1) in enumerate(ranges):
        crop = image.crop((0, y0, width, y1))
        output = BytesIO()
        crop.save(output, format="JPEG", quality=90, optimize=True)
        tiles.append(
            ImageTile(
                image_sha256=image_hash,
                filename=filename,
                index=index,
                y0=y0,
                y1=y1,
                image_width=width,
                image_height=height,
                jpeg_bytes=output.getvalue(),
            )
        )
    return image_hash, (width, height), tiles


def _extract_json(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    raw = re.sub(r"^\s*\`\`\`(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*\`\`\`\s*$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("vision model did not return JSON")
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("vision model returned invalid JSON") from exc
    if isinstance(parsed, list):
        return {"rows": parsed}
    if not isinstance(parsed, dict):
        raise ValueError("vision JSON root must be object")
    return parsed


def _money(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    raw = str(value).strip().replace(",", "").replace("$", "").replace("HK$", "").replace("¥", "")
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _normalize_account(value: Any) -> str:
    raw = str(value or "").strip()
    compact = re.sub(r"\s+", "", raw)
    aliases = {
        "长桥证券": "长桥", "Longbridge": "长桥", "longbridge": "长桥",
        "盈透": "IBKR", "盈透证券": "IBKR", "InteractiveBrokers": "IBKR",
        "哈富证券": "哈富", "华盛证券": "华盛通", "银河证券": "银河",
    }
    return aliases.get(compact, raw)


def _normalize_security_code(
    code: Any,
    currency: Optional[str],
    raw_text: str = "",
) -> Optional[str]:
    """Normalize broker display codes without guessing the security identity.

    Longbridge prefixes US/HK market identifiers in the UI (for example
    USNVDA or HK01810). The LLM may return either the prefixed UI code or the
    plain ticker, so dedupe must collapse both representations.
    """
    raw = re.sub(r"\s+", "", str(code or "").upper())
    if not raw:
        return None

    text = re.sub(r"\s+", "", str(raw_text or "").upper())
    currency = str(currency or "").upper().strip()

    if currency == "USD":
        if raw.startswith("US") and len(raw) > 2:
            # If OCR visibly contains "US" + raw, raw itself is likely a real
            # ticker beginning with US (e.g. USO shown as USUSO). Otherwise
            # treat the first US as the broker market prefix.
            if f"US{raw}" not in text:
                candidate = raw[2:]
                if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,7}", candidate):
                    raw = candidate
        return raw

    if currency == "HKD":
        if raw.startswith("HK") and raw[2:].isdigit():
            return f"{raw[2:].zfill(5)}.HK"
        if raw.endswith(".HK") and raw[:-3].isdigit():
            return f"{raw[:-3].zfill(5)}.HK"
        if raw.isdigit() and len(raw) <= 5:
            return f"{raw.zfill(5)}.HK"
        return raw

    if currency == "CNY":
        match = re.fullmatch(r"(?:SH|SZ)(\d{6})", raw)
        if match:
            return match.group(1)

    return raw


def _normalize_reported_effective_at(
    value: Any,
    *,
    raw_text: str,
    currency: Optional[str],
    code: Optional[str],
) -> str:
    normalized = normalize_event_time(value)
    if _time_precision(value) != "datetime":
        return normalized

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        return normalized

    text = str(raw_text or "")
    zone_name: Optional[str] = None
    if "美东" in text or re.search(r"\b(?:ET|EST|EDT)\b", text, re.IGNORECASE):
        zone_name = "America/New_York"
    elif str(currency or "").upper() == "HKD" and str(code or "").upper().endswith(".HK"):
        zone_name = "Asia/Hong_Kong"
    elif str(currency or "").upper() == "CNY":
        zone_name = "Asia/Shanghai"

    if zone_name is None:
        return normalized
    return parsed.replace(tzinfo=ZoneInfo(zone_name)).isoformat()


def _time_precision(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "missing"
    # Date-only rows are common at screenshot/tile boundaries.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return "date"
    return "datetime"


def _event_day(value: Any) -> str:
    try:
        return normalize_event_time(value)[:10]
    except ValueError:
        return str(value or "").strip()[:10]


def _overlap_signature_from_event(tx: Transaction) -> Tuple[str, ...]:
    return (
        tx.account,
        tx.event_type.value,
        str(tx.code or "").upper(),
        str(tx.currency or "").upper(),
        _event_day(tx.effective_at),
        _canonical_decimal(tx.quantity),
        _canonical_decimal(tx.price),
        _canonical_decimal(tx.amount),
    )


def _unresolved_overlap_signature(
    candidate: ScreenshotCandidate,
    account_hint: str,
) -> Tuple[str, ...]:
    row = candidate.raw
    event_type = _normalize_event_type(row.get("event_type") or row.get("action"))
    account = _normalize_account(row.get("account") or account_hint)
    currency = str(row.get("currency") or "").strip().upper() or None
    raw_text = str(row.get("raw_text") or "")
    code = _normalize_security_code(
        row.get("code") or row.get("ticker"),
        currency,
        raw_text,
    )
    effective_at = str(
        row.get("effective_at") or row.get("trade_time") or row.get("date") or ""
    ).strip()
    quantity = _money(row.get("quantity"))
    price = _money(row.get("price"))
    amount = _money(row.get("amount") or row.get("total_amount"))
    return (
        account,
        event_type.value if event_type else "",
        str(code or ""),
        str(currency or ""),
        _event_day(effective_at),
        _canonical_decimal(quantity),
        _canonical_decimal(price),
        _canonical_decimal(amount),
        ",".join(sorted(candidate.missing_fields)),
    )


def _unresolved_matches_event(
    candidate: ScreenshotCandidate,
    event: Transaction,
    account_hint: str,
) -> bool:
    """Match an incomplete tile-boundary row to an already complete event."""
    row = candidate.raw
    event_type = _normalize_event_type(row.get("event_type") or row.get("action"))
    if event_type is None or event_type != event.event_type:
        return False

    account = _normalize_account(row.get("account") or account_hint)
    if account != event.account:
        return False

    currency = str(row.get("currency") or "").strip().upper() or None
    if currency and currency != event.currency:
        return False

    raw_text = str(row.get("raw_text") or "")
    code = _normalize_security_code(
        row.get("code") or row.get("ticker"),
        currency or event.currency,
        raw_text,
    )
    if code and code != event.code:
        return False

    raw_effective_at = str(
        row.get("effective_at") or row.get("trade_time") or row.get("date") or ""
    ).strip()
    if raw_effective_at:
        if _event_day(raw_effective_at) != _event_day(event.effective_at):
            return False
        row_precision = _time_precision(raw_effective_at)
        event_precision = str(event.metadata.get("time_precision") or "datetime")
        normalized_row_time = _normalize_reported_effective_at(
            raw_effective_at,
            raw_text=raw_text,
            currency=currency or event.currency,
            code=code or event.code,
        )
        if not _times_are_compatible_for_overlap(
            str(event.effective_at),
            event_precision,
            normalized_row_time,
            row_precision,
        ):
            return False

    quantity = _money(row.get("quantity"))
    if quantity is not None and quantity != event.quantity:
        return False
    price = _money(row.get("price"))
    if price is not None and price != event.price:
        return False
    amount = _money(row.get("amount") or row.get("total_amount"))
    if amount is not None and amount != event.amount:
        return False

    # Require at least one substantive trade field besides identity/date.
    return quantity is not None or price is not None or amount is not None


def _times_are_compatible_for_overlap(
    previous_time: str,
    previous_precision: str,
    current_time: str,
    current_precision: str,
) -> bool:
    """Return True when two same-day economic rows can be screenshot overlap.

    If both screenshots clearly report different execution times, they are
    separate trades even when quantity/price happen to match. If either side
    only contains a date (a common tile-boundary OCR loss), the richer row may
    safely replace the date-only candidate while still requiring confirmation.
    """
    if previous_precision != "datetime" or current_precision != "datetime":
        return True
    try:
        previous = datetime.fromisoformat(previous_time)
        current = datetime.fromisoformat(current_time)
    except ValueError:
        return previous_time == current_time

    if (previous.tzinfo is None) != (current.tzinfo is None):
        return previous.replace(tzinfo=None) == current.replace(tzinfo=None)
    return previous == current


def _normalize_event_type(value: Any) -> Optional[TransactionType]:
    token = str(value or "").strip().upper()
    aliases = {
        "BUY": TransactionType.BUY, "买": TransactionType.BUY, "买入": TransactionType.BUY,
        "SELL": TransactionType.SELL, "卖": TransactionType.SELL, "卖出": TransactionType.SELL,
        "DEPOSIT": TransactionType.DEPOSIT, "入金": TransactionType.DEPOSIT,
        "WITHDRAW": TransactionType.WITHDRAW, "出金": TransactionType.WITHDRAW, "提现": TransactionType.WITHDRAW,
        "DIVIDEND": TransactionType.DIVIDEND, "分红": TransactionType.DIVIDEND,
        "FX": TransactionType.FX, "换汇": TransactionType.FX,
    }
    return aliases.get(token)


def _status_disposition(value: Any) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return "unknown"
    rejected = ("cancel", "撤", "未成交", "失败", "rejected", "expired", "pending", "待成交", "submitted")
    if any(part in token for part in rejected):
        return "reject"
    accepted = ("filled", "executed", "completed", "成交", "已完成", "done")
    if any(part in token for part in accepted):
        return "accept"
    return "unknown"


def _canonical_decimal(value: Optional[Decimal]) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _fingerprint_fields(
    event_type: TransactionType,
    effective_at: str,
    code: Optional[str],
    currency: Optional[str],
    quantity: Optional[Decimal],
    price: Optional[Decimal],
    amount: Optional[Decimal],
    fee: Decimal,
    tax: Decimal,
) -> str:
    parts = [
        event_type.value,
        str(effective_at).strip(),
        str(code or "").upper(),
        str(currency or "").upper(),
        _canonical_decimal(quantity),
        _canonical_decimal(price),
        _canonical_decimal(amount),
        _canonical_decimal(fee),
        _canonical_decimal(tax),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _external_id(order_id: Any, fingerprint: str) -> Tuple[str, str]:
    raw = re.sub(r"\s+", "", str(order_id or "").strip())
    if raw:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        return f"screenshot:order:{digest}", "broker_order_id"
    return f"screenshot:fp:{fingerprint}", "canonical_fields"


def transaction_fingerprint_key(tx: Transaction) -> str:
    tx = tx.validated()
    fingerprint = _fingerprint_fields(
        tx.event_type,
        str(tx.effective_at),
        tx.code,
        tx.currency,
        tx.quantity,
        tx.price,
        tx.amount,
        tx.fee,
        tx.tax,
    )
    return f"screenshot:fp:{fingerprint}"


def _row_to_candidate(
    row: Dict[str, Any],
    *,
    account_hint: str,
    tile: ImageTile,
) -> ScreenshotCandidate:
    warnings: List[str] = []
    missing: List[str] = []
    event_type = _normalize_event_type(row.get("event_type") or row.get("action"))
    if event_type is None:
        missing.append("event_type")

    status_state = _status_disposition(row.get("status"))
    if status_state == "reject":
        return ScreenshotCandidate(event=None, raw=row, warnings=["非已成交订单，已排除"], duplicate_reason="not_executed")
    if status_state == "unknown":
        warnings.append("截图未明确显示已成交状态，请在确认卡核对")

    account = _normalize_account(row.get("account") or account_hint)
    if not account:
        missing.append("account")

    effective_at = str(row.get("effective_at") or row.get("trade_time") or row.get("date") or "").strip()
    if not effective_at:
        missing.append("effective_at")

    name = str(row.get("name") or "").strip() or None
    currency = str(row.get("currency") or "").strip().upper() or None
    raw_text = str(row.get("raw_text") or "")
    reported_code = str(row.get("code") or row.get("ticker") or "").strip().upper() or None
    code = _normalize_security_code(reported_code, currency, raw_text)
    quantity = _money(row.get("quantity"))
    price = _money(row.get("price"))
    total_amount = _money(row.get("total_amount"))
    fee_observed = row.get("fee") not in (None, "")
    tax_observed = row.get("tax") not in (None, "")
    fee = _money(row.get("fee")) or Decimal("0")
    tax = _money(row.get("tax")) or Decimal("0")
    amount = _money(row.get("amount"))

    if event_type in {TransactionType.BUY, TransactionType.SELL} and not fee_observed:
        warnings.append("截图未显示手续费，暂按0计；如需精确成本/收益请用交割单或后续补录")
    if event_type in {TransactionType.BUY, TransactionType.SELL} and not tax_observed:
        warnings.append("截图未显示税费，暂按0计；如需精确成本/收益请用交割单或后续补录")

    if price is None and total_amount is not None and quantity not in (None, Decimal("0")):
        price = total_amount / quantity
        warnings.append("截图只有总成交金额，已按数量折算单价；请核对")

    if event_type in {TransactionType.BUY, TransactionType.SELL}:
        if not code:
            missing.append("code")
        if not currency:
            missing.append("currency")
        if quantity is None or quantity <= 0:
            missing.append("quantity")
        if price is None or price < 0:
            missing.append("price")
    elif event_type in {TransactionType.DEPOSIT, TransactionType.WITHDRAW, TransactionType.DIVIDEND}:
        if not currency:
            missing.append("currency")
        if amount is None:
            amount = total_amount
        if amount is None or amount <= 0:
            missing.append("amount")

    if missing:
        return ScreenshotCandidate(event=None, raw=row, warnings=warnings, missing_fields=sorted(set(missing)))

    assert event_type is not None
    try:
        normalized_effective_at = _normalize_reported_effective_at(
            effective_at,
            raw_text=raw_text,
            currency=currency,
            code=code,
        )
    except ValueError:
        return ScreenshotCandidate(
            event=None,
            raw=row,
            warnings=warnings + ["成交时间格式无法标准化"],
            missing_fields=["effective_at"],
        )

    fingerprint = _fingerprint_fields(
        event_type, normalized_effective_at, code, currency, quantity, price, amount, fee, tax
    )
    external_id, dedupe_basis = _external_id(row.get("order_id") or row.get("execution_id"), fingerprint)
    if dedupe_basis == "canonical_fields":
        warnings.append(
            "未识别到券商订单号：将按日期/标的/数量/价格/金额等字段指纹去重；"
            "若确有两笔完全相同成交，请在确认前特别核对"
        )
    metadata = {
        "input": "screenshot",
        "image_sha256": tile.image_sha256,
        "filename": tile.filename,
        "tile_index": tile.index,
        "tile_y0": tile.y0,
        "tile_y1": tile.y1,
        "vision_confidence": row.get("confidence"),
        "raw_text": raw_text[:1000],
        "reported_code": reported_code or "",
        "order_id": str(row.get("order_id") or row.get("execution_id") or ""),
        "dedupe_basis": dedupe_basis,
        "fingerprint": fingerprint,
        "time_precision": _time_precision(effective_at),
        "fee_observed": fee_observed,
        "tax_observed": tax_observed,
    }

    try:
        event = Transaction(
            event_type=event_type,
            effective_at=normalized_effective_at,
            account=account,
            code=code,
            name=name,
            currency=currency,
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            amount=amount,
            source="screenshot",
            note=str(row.get("note") or ""),
            external_trade_id=external_id,
            metadata=metadata,
        ).validated()
    except ValueError as exc:
        return ScreenshotCandidate(event=None, raw=row, warnings=warnings + [str(exc)], missing_fields=["validation"])

    return ScreenshotCandidate(event=event, raw=row, warnings=warnings)


VISION_SYSTEM_PROMPT = """You extract executed brokerage transactions from screenshots.
Return strict JSON only. Never invent missing values.
Only extract actual executed/completed transactions. Do not turn portfolio holdings,
watchlists, quotes, cancelled orders, pending orders, or recommendations into trades.
Dates/times must be ISO-8601 when visible. Keep a date-only value as YYYY-MM-DD.
Numeric fields must be plain decimal strings without thousands separators or symbols.
If a field is not visible, use null.
"""

VISION_USER_PROMPT = """This is one tile of a brokerage order/trade-history screenshot.
Extract every visible transaction row, including rows partially repeated from neighboring
tiles; downstream logic will deduplicate them. If a row is cut off and key values cannot
be read reliably, omit it rather than guessing.

Return:
{
  "rows": [
    {
      "event_type": "BUY|SELL|DEPOSIT|WITHDRAW|DIVIDEND|FX|null",
      "effective_at": "YYYY-MM-DD or ISO datetime or null",
      "account": "broker/account name if visible or null",
      "code": "ticker/code if visible or null",
      "name": "security name if visible or null",
      "currency": "USD|HKD|CNY|null",
      "quantity": "decimal or null",
      "price": "per-unit execution price or null",
      "total_amount": "total trade amount if explicitly shown or null",
      "amount": "cash-event amount or null",
      "fee": "decimal or null",
      "tax": "decimal or null",
      "order_id": "broker order/execution id if visible or null",
      "status": "exact status text if visible or null",
      "confidence": 0.0,
      "raw_text": "short transcription of the row"
    }
  ],
  "warnings": []
}
"""


class ScreenshotLedgerExtractor:
    def __init__(
        self,
        vision: VisionProviderSpec | Sequence[VisionProviderSpec] | None,
        *,
        ocr_fallback: Optional[LocalOCRTextFallback] = None,
        max_concurrency: int = 4,
    ):
        if vision is None:
            self.visions = []
        elif isinstance(vision, VisionProviderSpec):
            self.visions = [vision]
        else:
            self.visions = list(vision)
        self.ocr_fallback = ocr_fallback
        if not self.visions and self.ocr_fallback is None:
            raise ValueError("at least one vision provider or OCR fallback is required")
        self.max_concurrency = max(1, min(int(max_concurrency), 8))
        self._used_model_labels: set[str] = set()
        self._disabled_vision_labels: set[str] = set()

    def _messages(
        self,
        vision: VisionProviderSpec,
        tile: ImageTile,
        account_hint: str,
    ) -> List[Dict[str, Any]]:
        hint = f"\nAccount hint from user: {account_hint}" if account_hint else ""
        tile_context = (
            f"\nFile: {tile.filename}; tile {tile.index + 1}; "
            f"vertical range {tile.y0}-{tile.y1} of {tile.image_height}.{hint}"
        )
        prompt = VISION_USER_PROMPT + tile_context
        if vision.family == "claude":
            user_content: List[Dict[str, Any]] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": tile.data_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ]
        else:
            user_content = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{tile.data_b64}",
                        "detail": "high",
                    },
                },
            ]
        return [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    async def _extract_tile(self, tile: ImageTile, account_hint: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        errors: List[str] = []
        for vision in self.visions:
            if vision.label in self._disabled_vision_labels:
                continue
            try:
                response = await vision.provider.chat(
                    self._messages(vision, tile, account_hint),
                    temperature=0,
                    max_tokens=5000,
                )
                parsed = _extract_json(response.content)
                rows = parsed.get("rows") or parsed.get("transactions") or []
                if not isinstance(rows, list):
                    raise ValueError("vision response rows must be an array")
                warnings = parsed.get("warnings") or []
                self._used_model_labels.add(vision.label)
                return [row for row in rows if isinstance(row, dict)], [str(x) for x in warnings]
            except (RuntimeError, ValueError) as exc:
                error_text = str(exc)
                errors.append(f"{vision.label}: {error_text[:180]}")
                if "401" in error_text or "403" in error_text:
                    self._disabled_vision_labels.add(vision.label)

        if self.ocr_fallback is not None:
            try:
                rows, warnings = await self.ocr_fallback.extract_tile(tile, account_hint)
                self._used_model_labels.add(self.ocr_fallback.label)
                if errors:
                    warnings = [
                        "云视觉模型不可用，已自动切换本地OCR",
                        *warnings,
                    ]
                return rows, warnings
            except (RuntimeError, ValueError) as exc:
                errors.append(f"{self.ocr_fallback.label}: {str(exc)[:180]}")

        raise RuntimeError("所有截图识别路径均失败；" + " | ".join(errors))

    async def extract(
        self,
        images: Sequence[Tuple[str, bytes]],
        *,
        account_hint: str = "",
        existing_external_keys: Optional[Iterable[Tuple[str, str]]] = None,
        existing_fingerprint_keys: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> ScreenshotExtractionResult:
        if not images:
            raise ValueError("at least one image is required")

        self._used_model_labels.clear()
        self._disabled_vision_labels.clear()
        existing = set(existing_external_keys or [])
        existing_fingerprints = set(existing_fingerprint_keys or [])
        seen_images: set[str] = set()
        seen_keys: set[Tuple[str, str]] = set()
        # Multiple legitimate same-day trades can share quantity/price, so
        # overlap signatures keep a list and only collapse rows whose reported
        # times are compatible.
        seen_overlap: Dict[Tuple[str, ...], List[Tuple[int, str, str]]] = {}
        seen_unresolved: Dict[Tuple[str, ...], List[Tuple[str, str]]] = {}
        events: List[Transaction] = []
        duplicates: List[ScreenshotCandidate] = []
        unresolved: List[ScreenshotCandidate] = []
        warnings: List[str] = []
        summaries: List[Dict[str, Any]] = []

        for filename, content in images:
            image_hash, dimensions, tiles = prepare_image_tiles(content, filename)
            if image_hash in seen_images:
                duplicates.append(
                    ScreenshotCandidate(
                        event=None,
                        raw={"filename": filename, "image_sha256": image_hash},
                        warnings=["与本次上传的另一张图片完全相同"],
                        duplicate_reason="duplicate_image",
                    )
                )
                continue
            seen_images.add(image_hash)

            semaphore = asyncio.Semaphore(self.max_concurrency)

            async def extract_one(tile: ImageTile):
                async with semaphore:
                    return tile, await self._extract_tile(tile, account_hint)

            tile_results = await asyncio.gather(*(extract_one(tile) for tile in tiles))
            tile_results.sort(key=lambda item: item[0].index)

            extracted_rows = 0
            for tile, (rows, tile_warnings) in tile_results:
                warnings.extend(f"{filename} 第{tile.index + 1}片: {item}" for item in tile_warnings)
                extracted_rows += len(rows)
                for row in rows:
                    candidate = _row_to_candidate(row, account_hint=account_hint, tile=tile)
                    if candidate.duplicate_reason == "not_executed":
                        duplicates.append(candidate)
                        continue
                    if candidate.event is None:
                        if any(
                            _unresolved_matches_event(candidate, event, account_hint)
                            for event in events
                        ):
                            candidate.duplicate_reason = "overlap_partial"
                            candidate.warnings.append(
                                "字段不完整的截图残片已与完整成交记录匹配并折叠"
                            )
                            duplicates.append(candidate)
                            continue
                        unresolved_signature = _unresolved_overlap_signature(candidate, account_hint)
                        raw_effective_at = str(
                            candidate.raw.get("effective_at")
                            or candidate.raw.get("trade_time")
                            or candidate.raw.get("date")
                            or ""
                        ).strip()
                        unresolved_precision = _time_precision(raw_effective_at)
                        unresolved_currency = str(
                            candidate.raw.get("currency") or ""
                        ).strip().upper() or None
                        unresolved_raw_text = str(candidate.raw.get("raw_text") or "")
                        unresolved_code = _normalize_security_code(
                            candidate.raw.get("code") or candidate.raw.get("ticker"),
                            unresolved_currency,
                            unresolved_raw_text,
                        )
                        unresolved_time = (
                            _normalize_reported_effective_at(
                                raw_effective_at,
                                raw_text=unresolved_raw_text,
                                currency=unresolved_currency,
                                code=unresolved_code,
                            )
                            if raw_effective_at
                            else ""
                        )
                        prior_unresolved = seen_unresolved.get(unresolved_signature, [])
                        compatible_unresolved = any(
                            _times_are_compatible_for_overlap(
                                previous_time,
                                previous_precision,
                                unresolved_time,
                                unresolved_precision,
                            )
                            for previous_precision, previous_time in prior_unresolved
                        )
                        if compatible_unresolved:
                            candidate.duplicate_reason = "overlap_unresolved"
                            candidate.warnings.append(
                                "与其他截图/切片中的同一条字段不完整订单重复，已折叠"
                            )
                            duplicates.append(candidate)
                            continue
                        prior_unresolved.append((unresolved_precision, unresolved_time))
                        seen_unresolved[unresolved_signature] = prior_unresolved
                        unresolved.append(candidate)
                        continue
                    key = (candidate.event.account, str(candidate.event.external_trade_id or ""))
                    if key in seen_keys:
                        candidate.duplicate_reason = "overlap_or_multi_image"
                        candidate.warnings.append("与本次上传已识别订单重复，已折叠")
                        duplicates.append(candidate)
                        continue
                    overlap_signature = _overlap_signature_from_event(candidate.event)
                    current_precision = str(
                        candidate.event.metadata.get("time_precision") or "datetime"
                    )
                    current_time = str(candidate.event.effective_at)
                    prior_overlap = seen_overlap.get(overlap_signature, [])
                    matching_prior = next(
                        (
                            (event_index, previous_precision, previous_time)
                            for event_index, previous_precision, previous_time in prior_overlap
                            if _times_are_compatible_for_overlap(
                                previous_time,
                                previous_precision,
                                current_time,
                                current_precision,
                            )
                        ),
                        None,
                    )
                    if matching_prior is not None:
                        event_index, previous_precision, previous_time = matching_prior
                        # Prefer the row that contains an actual execution time
                        # over a boundary OCR row that only contains the date.
                        if previous_precision != "datetime" and current_precision == "datetime":
                            events[event_index] = candidate.event
                            prior_overlap.remove(matching_prior)
                            prior_overlap.append((event_index, current_precision, current_time))
                        seen_keys.add(key)
                        candidate.duplicate_reason = "overlap_or_multi_image"
                        candidate.warnings.append(
                            "与相邻截图/切片中的同一成交重复，已折叠"
                        )
                        duplicates.append(candidate)
                        continue

                    if key in existing:
                        candidate.duplicate_reason = "already_in_ledger_or_pending"
                        candidate.warnings.append("账本或待确认批次中已存在相同订单，已去重")
                        duplicates.append(candidate)
                        continue
                    fingerprint_key = (
                        candidate.event.account,
                        f"screenshot:fp:{candidate.event.metadata.get('fingerprint', '')}",
                    )
                    if fingerprint_key in existing_fingerprints:
                        candidate.duplicate_reason = "already_in_ledger_by_fields"
                        candidate.warnings.append(
                            "已有语音/文字/手工记录与本截图的时间、标的、数量、价格等字段完全一致，"
                            "已按经济事实去重；请核对是否确为同一笔成交"
                        )
                        duplicates.append(candidate)
                        continue
                    resolved_fragment_indices = [
                        index
                        for index, unresolved_candidate in enumerate(unresolved)
                        if _unresolved_matches_event(
                            unresolved_candidate,
                            candidate.event,
                            account_hint,
                        )
                    ]
                    for index in reversed(resolved_fragment_indices):
                        fragment = unresolved.pop(index)
                        fragment.duplicate_reason = "overlap_partial"
                        fragment.warnings.append(
                            "字段不完整的截图残片已与完整成交记录匹配并折叠"
                        )
                        duplicates.append(fragment)

                    seen_keys.add(key)
                    seen_overlap.setdefault(overlap_signature, []).append(
                        (len(events), current_precision, current_time)
                    )
                    if candidate.warnings:
                        warnings.extend(
                            f"{filename} 第{tile.index + 1}片: {warning}"
                            for warning in candidate.warnings
                        )
                    events.append(candidate.event)
            summaries.append(
                {
                    "filename": filename,
                    "sha256": image_hash,
                    "width": dimensions[0],
                    "height": dimensions[1],
                    "tile_count": len(tiles),
                    "raw_rows": extracted_rows,
                }
            )

        return ScreenshotExtractionResult(
            events=events,
            duplicates=duplicates,
            unresolved=unresolved,
            warnings=warnings,
            image_summaries=summaries,
            model_label=", ".join(sorted(self._used_model_labels))
            or (self.visions[0].label if self.visions else self.ocr_fallback.label),
        )
