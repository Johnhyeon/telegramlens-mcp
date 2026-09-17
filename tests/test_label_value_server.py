"""도구 응답의 이름표 - 수집 신선도·브리핑 날짜·등락률 시점.

감사에서 나온 결함들:
- stock_buzz: 수집이 멎어 0건인데도 "조회는 정상, 무언급일 뿐"이라고 답했다.
- briefing: 머리말 날짜와 등락률 시점을 PC 현지 시각으로 정해, 해외에서는 날짜가 하루
  어긋나고 한국 장중 등락률이 붙었다. 12시간 창을 '오늘'이라 적었고, 평소보다 줄어든
  종목도 '갑자기 늘어난 종목' 아래 '최근 부쩍 늘어'로 나갔다. 수집이 오래돼도 말이 없었다.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_lens import server  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _iso_hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def _times(last_collection_hours_ago: float) -> dict:
    return {
        "newest_message": _iso_hours_ago(last_collection_hours_ago),
        "first_message": _iso_hours_ago(24 * 7),
        "last_collection": _iso_hours_ago(last_collection_hours_ago),
        "interval_minutes": 10,
    }


async def _no_notice():
    return ""


class StockBuzzZeroWhenStaleTests(unittest.IsolatedAsyncioTestCase):
    async def _buzz(self, times):
        zero = lambda code, name, hours, samples: {  # noqa: E731
            "code": code, "name": name, "window_hours": hours,
            "summary": {"independent": 0, "raw_messages": 0, "channels": 0},
            "samples": [],
        }
        with patch.object(server, "is_licensed", lambda: True), \
             patch.object(server, "_collecting_notice", lambda: None), \
             patch.object(server, "load_etf_codes", lambda: set()), \
             patch.object(server, "load_stocks", lambda: {"005930": "삼성전자"}), \
             patch.object(server, "_collection_times", lambda: times), \
             patch("telegram_lens._update_check.get_update_notice", _no_notice), \
             patch.object(server.queries, "stock_buzz", zero):
            return json.loads(await server.telegram_stock_buzz(query="005930", hours=24))

    async def test_zero_with_stale_collection_is_not_called_no_mentions(self):
        out = await self._buzz(_times(30))
        self.assertEqual(out["_meta"]["data_completeness"], "partial")
        self.assertNotIn("무언급일 뿐", out["status_note"])
        self.assertIn("단정할 수 없습니다", out["status_note"])
        self.assertIn("마지막 수집이 약 30시간 전", out["status_note"])
        self.assertEqual(out["_meta"]["entity"]["stock_code"], "005930")

    async def test_zero_with_fresh_collection_keeps_no_mentions_note(self):
        out = await self._buzz(_times(0.05))
        self.assertEqual(out["_meta"]["data_completeness"], "complete")
        self.assertIn("무언급일 뿐", out["status_note"])


class BriefingReadyTextTests(unittest.TestCase):
    def _text(self, momentum, **kw):
        return server._format_briefing_ready(
            [{"code": "005930", "name": "삼성전자", "independent": 5, "channels": 3}],
            momentum, [], [], None,
            hours=12, now_kst=datetime(2026, 9, 18, 7, 0, tzinfo=KST), **kw,
        )

    def test_header_uses_kst_date_and_window_not_today(self):
        text = self._text([
            {"code": "000660", "name": "SK하이닉스", "recent_channels": 4,
             "recent_mentions": 6, "is_new": True, "rising": True, "samples": []},
        ])
        self.assertIn("9월 18일 (최근 12시간)", text.splitlines()[0])
        self.assertNotIn("오늘", text)
        self.assertIn("평소 거의 없다가 최근 12시간 4개 채널에서 거론", text)

    def test_surge_section_skips_names_that_did_not_rise(self):
        text = self._text([
            {"code": "000660", "name": "SK하이닉스", "recent_channels": 4,
             "recent_mentions": 6, "is_new": False, "rising": False, "samples": []},
            {"code": "035420", "name": "NAVER", "recent_channels": 5,
             "recent_mentions": 7, "is_new": None, "rising": False, "samples": []},
        ])
        self.assertNotIn("갑자기 늘어난 종목", text)
        self.assertNotIn("부쩍", text)

    def test_stale_note_is_shown_under_header(self):
        text = self._text([], stale_note="마지막 수집이 약 9시간 전이라 그 뒤 글은 빠져 있습니다.")
        lines = text.splitlines()
        self.assertTrue(lines[1].startswith("⚠️ 마지막 수집이 약 9시간 전"))
        self.assertEqual(lines[2], "")


class BriefingPriceChangeGateTests(unittest.IsolatedAsyncioTestCase):
    """등락률은 KRX 거래일 정규장 마감 뒤(KST)에만 붙인다."""

    def _clock(self, status: str, is_trading_day: bool) -> dict:
        return {
            "now_kst": "2026-09-19T17:00:00+09:00",
            "krx": {"status": status, "is_trading_day": is_trading_day,
                    "last_trading_day": "2026-09-18", "next_trading_day": "2026-09-21"},
        }

    async def _run(self, clock):
        calls: list = []

        def fake_change(codes):
            calls.append(codes)
            return {}

        trending = [{"code": "005930", "name": "삼성전자", "independent": 3, "channels": 3}]
        with patch.object(server, "is_licensed", lambda: True), \
             patch.object(server, "_collecting_notice", lambda: None), \
             patch.object(server, "load_etf_codes", lambda: set()), \
             patch.object(server.queries, "trending", lambda **k: trending), \
             patch.object(server.queries, "momentum", lambda **k: []), \
             patch.object(server, "_list_noise_density", lambda h, c: {}), \
             patch.object(server, "_watchlist_buzz", lambda h: []), \
             patch.object(server, "_reading_list", lambda h: []), \
             patch.object(server, "_macro_buzz", lambda h: []), \
             patch.object(server, "_collection_times", lambda: _times(0.05)), \
             patch.object(server.market_clock, "get_market_clock", lambda: clock), \
             patch("telegram_lens._update_check.get_update_notice", _no_notice), \
             patch("telegram_lens.prices.daily_change", fake_change):
            out = json.loads(await server.telegram_briefing(hours=12))
        return out, calls

    async def test_weekend_evening_gets_no_price_change(self):
        out, calls = await self._run(self._clock("closed_weekend", False))
        self.assertEqual(calls, [])
        self.assertEqual(out["_meta"]["data_completeness"], "complete")

    async def test_trading_day_after_close_gets_price_change(self):
        _out, calls = await self._run(self._clock("after_hours", True))
        self.assertEqual(len(calls), 1)

    async def test_trading_day_regular_session_gets_no_price_change(self):
        _out, calls = await self._run(self._clock("regular", True))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
