import asyncio
import json
import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from core.llm.base import LLMResponse
from backend.ledger.screenshot import (
    ScreenshotLedgerExtractor,
    VisionProviderSpec,
    prepare_image_tiles,
)


def make_png(width=800, height=1200, text='sample'):
    img = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), text, fill='black')
    out = BytesIO()
    img.save(out, format='PNG')
    return out.getvalue()


class FailingVisionProvider:
    async def chat(self, messages, **kwargs):
        raise RuntimeError("primary unavailable")


class FakeOCRFallback:
    def __init__(self, rows):
        self.rows = rows
        self.label = "fake OCR + text"

    async def extract_tile(self, tile, account_hint):
        return self.rows, ["使用OCR兜底"]


class FakeVisionProvider:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        payload = {'rows': self.rows, 'warnings': []}
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            model='fake-vision',
            usage={'prompt_tokens': 0, 'completion_tokens': 0},
            finish_reason='stop',
        )


class ScreenshotTilingTest(unittest.TestCase):
    def test_short_image_is_single_tile(self):
        image_hash, size, tiles = prepare_image_tiles(make_png(800, 1000), 'short.png')
        self.assertEqual(size, (800, 1000))
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0].y0, 0)
        self.assertEqual(tiles[0].y1, 1000)
        self.assertEqual(len(image_hash), 64)

    def test_tall_image_is_split_with_overlap(self):
        _hash, size, tiles = prepare_image_tiles(make_png(800, 9000), 'long.png')
        self.assertEqual(size, (800, 9000))
        self.assertGreater(len(tiles), 3)
        for prev, nxt in zip(tiles, tiles[1:]):
            self.assertLess(nxt.y0, prev.y1)
            self.assertGreaterEqual(prev.y1 - nxt.y0, 200)
        self.assertEqual(tiles[-1].y1, 9000)

    def test_duplicate_image_in_same_upload_is_skipped(self):
        provider = FakeVisionProvider([])
        extractor = ScreenshotLedgerExtractor(
            VisionProviderSpec(provider=provider, family='openai', model='fake', label='fake')
        )
        content = make_png(800, 1000)
        result = asyncio.run(extractor.extract([('a.png', content), ('b.png', content)]))
        self.assertEqual(provider.calls, 1)
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(result.duplicates[0].duplicate_reason, 'duplicate_image')


class ScreenshotExtractionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row = {
            'event_type': 'BUY',
            'effective_at': '2025-03-12T10:30:00+08:00',
            'account': '长桥证券',
            'code': 'NVDA',
            'name': 'NVIDIA',
            'currency': 'USD',
            'quantity': '2',
            'price': '87.5',
            'fee': '1',
            'tax': '0',
            'order_id': 'ORDER-123',
            'status': '已成交',
            'confidence': 0.99,
            'raw_text': 'NVDA 买入 2 @ 87.5 已成交',
        }

    def extractor(self, rows=None):
        provider = FakeVisionProvider(rows if rows is not None else [self.row])
        return ScreenshotLedgerExtractor(
            VisionProviderSpec(provider=provider, family='openai', model='fake', label='fake')
        ), provider

    async def test_falls_back_to_second_vision_provider(self):
        good = FakeVisionProvider([self.row])
        extractor = ScreenshotLedgerExtractor([
            VisionProviderSpec(
                provider=FailingVisionProvider(),
                family='openai',
                model='bad',
                label='bad vision',
            ),
            VisionProviderSpec(
                provider=good,
                family='openai',
                model='good',
                label='good vision',
            ),
        ])
        result = await extractor.extract([('one.png', make_png())])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.model_label, 'good vision')

    async def test_falls_back_to_ocr_when_all_vision_models_fail(self):
        extractor = ScreenshotLedgerExtractor(
            [
                VisionProviderSpec(
                    provider=FailingVisionProvider(),
                    family='openai',
                    model='bad',
                    label='bad vision',
                )
            ],
            ocr_fallback=FakeOCRFallback([self.row]),
        )
        result = await extractor.extract([('one.png', make_png())])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.model_label, 'fake OCR + text')
        self.assertTrue(any('OCR' in warning for warning in result.warnings))

    async def test_overlap_rows_are_deduplicated(self):
        extractor, provider = self.extractor()
        result = await extractor.extract([('long.png', make_png(800, 7000))])
        self.assertGreater(provider.calls, 1)
        self.assertEqual(len(result.events), 1)
        self.assertGreaterEqual(len(result.duplicates), 1)
        self.assertTrue(all(d.duplicate_reason == 'overlap_or_multi_image' for d in result.duplicates))
        self.assertEqual(result.events[0].account, '长桥')
        self.assertEqual(str(result.events[0].price), '87.5')

    async def test_existing_ledger_key_is_deduplicated(self):
        extractor, _provider = self.extractor()
        first = await extractor.extract([('one.png', make_png())])
        event = first.events[0]
        result = await extractor.extract(
            [('again.png', make_png(text='different pixels'))],
            existing_external_keys={(event.account, event.external_trade_id)},
        )
        self.assertEqual(result.events, [])
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(result.duplicates[0].duplicate_reason, 'already_in_ledger_or_pending')

    async def test_missing_account_is_unresolved_without_hint(self):
        row = dict(self.row)
        row['account'] = None
        extractor, _provider = self.extractor([row])
        result = await extractor.extract([('one.png', make_png())])
        self.assertEqual(result.events, [])
        self.assertEqual(len(result.unresolved), 1)
        self.assertIn('account', result.unresolved[0].missing_fields)

    async def test_account_hint_fills_missing_account(self):
        row = dict(self.row)
        row['account'] = None
        extractor, _provider = self.extractor([row])
        result = await extractor.extract([('one.png', make_png())], account_hint='IBKR')
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].account, 'IBKR')

    async def test_total_amount_is_converted_to_unit_price(self):
        row = dict(self.row)
        row['price'] = None
        row['total_amount'] = '175'
        row['order_id'] = 'ORDER-TOTAL'
        extractor, _provider = self.extractor([row])
        result = await extractor.extract([('one.png', make_png())])
        self.assertEqual(str(result.events[0].price), '87.5')

    async def test_screenshot_dedupes_against_manual_ledger_fingerprint(self):
        extractor, _provider = self.extractor()
        first = await extractor.extract([('one.png', make_png())])
        event = first.events[0]
        fingerprint_key = f"screenshot:fp:{event.metadata['fingerprint']}"

        result = await extractor.extract(
            [('again.png', make_png(text='other pixels'))],
            existing_fingerprint_keys={(event.account, fingerprint_key)},
        )
        self.assertEqual(result.events, [])
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(
            result.duplicates[0].duplicate_reason,
            'already_in_ledger_by_fields',
        )

    async def test_missing_order_id_emits_weak_dedupe_warning(self):
        row = dict(self.row)
        row['order_id'] = None
        extractor, _provider = self.extractor([row])
        result = await extractor.extract([('one.png', make_png())])
        self.assertEqual(len(result.events), 1)
        self.assertTrue(
            any('未识别到券商订单号' in warning for warning in result.warnings)
        )
        self.assertEqual(
            result.events[0].metadata.get('dedupe_basis'),
            'canonical_fields',
        )

    async def test_date_only_time_is_normalized_before_fingerprint(self):
        row = dict(self.row)
        row['order_id'] = None
        row['effective_at'] = '2025-03-12'
        extractor, _provider = self.extractor([row])
        first = await extractor.extract([('one.png', make_png())])
        event = first.events[0]
        self.assertEqual(event.effective_at, '2025-03-12T00:00:00')
        second = await extractor.extract(
            [('other.png', make_png(text='different image'))],
            existing_external_keys={(event.account, event.external_trade_id)},
        )
        self.assertEqual(second.events, [])
        self.assertEqual(second.duplicates[0].duplicate_reason, 'already_in_ledger_or_pending')

    async def test_cancelled_order_is_not_imported(self):
        row = dict(self.row)
        row['status'] = '已撤单'
        extractor, _provider = self.extractor([row])
        result = await extractor.extract([('one.png', make_png())])
        self.assertEqual(result.events, [])
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(result.duplicates[0].duplicate_reason, 'not_executed')


if __name__ == '__main__':
    unittest.main()
