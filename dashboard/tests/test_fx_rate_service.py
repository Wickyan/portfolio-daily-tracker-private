from __future__ import annotations

import unittest

from backend.services.fx_rate_service import FXRateService


class FXRateServiceTest(unittest.TestCase):
    def test_parse_tencent_rate(self) -> None:
        text = 'v_whUSDCNY="310~美元人民币~USDCNY~6.7741~0~20260711011236";'
        self.assertEqual(FXRateService.parse_tencent_rate(text), 6.7741)

    def test_invalid_payload_returns_none(self) -> None:
        self.assertIsNone(FXRateService.parse_tencent_rate('v_pv_none_match="1";'))


if __name__ == "__main__":
    unittest.main()
