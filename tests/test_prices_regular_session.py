"""브리핑 등락률은 정규장 기준이어야 한다 (KRX 애프터마켓 2026-09-14~)."""

import unittest
from unittest.mock import MagicMock, patch

from telegram_lens import prices


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


# 2026-09-15 16:04 실측 모양: 036930 은 애프터마켓 193,800(기준가 206,000),
# 정규장 종가는 15:30 봉 192,800. 069500(ETF)은 애프터마켓이 없어 regularMarket.
POLLING = {"datas": [
    {"itemCode": "036930", "marketSessionType": "afterMarket", "closePriceRaw": "193800",
     "compareToPreviousClosePriceRaw": "-12200", "fluctuationsRatioRaw": "-5.92",
     "localTradedAt": "2026-09-15T16:04:52+09:00"},
    {"itemCode": "069500", "marketSessionType": "regularMarket", "closePriceRaw": "104275",
     "compareToPreviousClosePriceRaw": "-1135", "fluctuationsRatioRaw": "-1.08",
     "localTradedAt": "2026-09-15T15:30:00+09:00"},
    {"itemCode": "277810", "marketSessionType": "afterMarket", "closePriceRaw": "430000",
     "compareToPreviousClosePriceRaw": "9500", "fluctuationsRatioRaw": "2.26",
     "localTradedAt": "2026-09-15T16:04:52+09:00"},
]}


def fake_get(url, params=None, headers=None, timeout=None):
    if "polling" in url:
        return _resp(POLLING)
    if "/036930/" in url:
        assert params == {"startDateTime": "202609151520", "endDateTime": "202609151530"}
        return _resp([{"localDateTime": "20260915153000", "currentPrice": 192800.0}])
    return _resp([])  # 277810: 15:30 봉 없음


class RegularSessionChangeTests(unittest.TestCase):
    def test_after_market_rows_use_the_1530_close(self) -> None:
        with patch.object(prices.httpx, "get", side_effect=fake_get):
            out = prices.daily_change(["036930", "069500", "277810"])

        self.assertEqual(out["036930"], -6.41)   # 애프터마켓 -5.92 가 아니라 정규장 -6.41
        self.assertEqual(out["069500"], -1.08)   # 정규장 시세는 원천 등락률 그대로
        self.assertNotIn("277810", out)          # 15:30 봉이 없으면 붙이지 않는다

    def test_source_failure_is_empty_not_error(self) -> None:
        with patch.object(prices.httpx, "get", side_effect=RuntimeError("down")):
            self.assertEqual(prices.daily_change(["036930"]), {})


if __name__ == "__main__":
    unittest.main()
