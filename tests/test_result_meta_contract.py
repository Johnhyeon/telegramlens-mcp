"""결과 메타 봉투 규약 v1 — TelegramLens 쪽 계약 테스트.

TelegramLens 고유의 함정은 **집계 창과 마지막 수집 시각이 다르다**는 점이다.
telegram_trending(hours=24)는 "최근 24시간"이라고 말하지만, 마지막 수집이 사흘
전이면 실제로는 사흘 전 시점의 24시간 창이다. 읽는 쪽은 "지금 뜨는 종목"으로
오해한다. 그래서 data_period(요청 창)와 data_as_of(마지막 메시지)를 나란히 싣고,
창보다 수집이 오래됐으면 partial + 경고로 드러낸다.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from telegram_lens import _result_meta as rmeta
from telegram_lens import server as tserver


def _iso_hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).replace(tzinfo=None).isoformat()


class ContractVersionTests(unittest.TestCase):
    def test_meta_version_matches_other_lenses(self):
        self.assertEqual(rmeta.META_VERSION, 1)

    def test_marker_matches_stocklens(self):
        self.assertEqual(rmeta.MARKER_START, "RESULT_META_JSON_START")


class StaleCollectionTests(unittest.TestCase):
    def _meta(self, newest_iso, hours):
        with patch.object(tserver.db, "newest_message_date", return_value=newest_iso):
            return tserver._tl_meta(hours=hours)

    def test_fresh_collection_is_complete(self):
        m = self._meta(_iso_hours_ago(1), 24)
        self.assertEqual(m["data_completeness"], "complete")
        self.assertEqual(m["warnings"], [])

    def test_collection_older_than_window_is_partial_with_warning(self):
        """마지막 수집이 창보다 오래됐다 = 이 결과는 '지금'이 아니다."""
        m = self._meta(_iso_hours_ago(72), 24)
        self.assertEqual(m["data_completeness"], "partial")
        self.assertTrue(any("telegram_sync" in w for w in m["warnings"]))
        self.assertTrue(any("지금" in w for w in m["warnings"]))

    def test_empty_db_reports_none_not_zero_buzz(self):
        """수집이 없는 것과 언급이 0인 것은 다르다."""
        m = self._meta(None, 24)
        self.assertEqual(m["data_completeness"], "none")
        self.assertIsNone(m["data_as_of"])
        self.assertTrue(m["warnings"])

    def test_window_is_a_period_label_not_a_date(self):
        m = self._meta(_iso_hours_ago(1), 24)
        self.assertEqual(m["data_period"], "최근 24시간")
        self.assertRegex(m["data_as_of"], r"^\d{4}-\d{2}-\d{2}$")

    def test_basis_is_aggregate_not_price(self):
        """언급 집계지 시세가 아니다 — 이 구분이 라벨로 드러나야 한다."""
        self.assertEqual(self._meta(_iso_hours_ago(1), 24)["data_basis"], "aggregate")

    def test_lens_is_tagged(self):
        self.assertEqual(self._meta(_iso_hours_ago(1), 24)["lens"], "telegramlens")


class PayloadShapeTests(unittest.TestCase):
    def test_stocks_payload_carries_meta_and_codes(self):
        """codes는 StockLens 배치 도구로 그대로 넘어가는 이어달리기 키다."""
        stocks = [{"code": "005930", "name": "삼성전자"}, {"code": "000660", "name": "SK하이닉스"}]
        with patch.object(tserver.db, "newest_message_date", return_value=_iso_hours_ago(1)), \
             patch.object(tserver, "load_etf_codes", return_value=set()):
            payload = tserver._stocks_payload(stocks, hours=24)
        self.assertEqual(payload["codes"], ["005930", "000660"])
        self.assertEqual(payload["_meta"]["data_period"], "최근 24시간")
        self.assertEqual(payload["_meta"]["meta_v"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
