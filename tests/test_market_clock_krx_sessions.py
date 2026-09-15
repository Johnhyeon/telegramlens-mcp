"""TelegramLens 시장 시계 - KRX 애프터마켓(2026-09-14~). StockLens 와 같은 규칙."""

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from telegram_lens import market_clock, server

KST = ZoneInfo("Asia/Seoul")


def krx(*args):
    return market_clock.get_market_clock(datetime(*args, tzinfo=KST))["krx"]


class KrxSessionTests(unittest.TestCase):
    def test_after_market_session(self) -> None:
        state = krx(2026, 9, 15, 17, 0)
        self.assertEqual(state["status"], "after_hours")
        self.assertEqual(state["current_session"], "after_market")
        self.assertFalse(state["is_open"])

    def test_gap_and_close(self) -> None:
        self.assertEqual(krx(2026, 9, 15, 15, 45)["status"], "closed_after_hours")
        self.assertIsNone(krx(2026, 9, 15, 15, 45)["current_session"])
        self.assertEqual(krx(2026, 9, 15, 20, 0)["reason"], "Regular and after-hours sessions ended")

    def test_no_after_market_before_start_and_no_pre_market(self) -> None:
        self.assertIsNone(krx(2026, 9, 11, 17, 0)["current_session"])
        self.assertEqual(krx(2026, 9, 16, 8, 40)["status"], "closed_before_open")
        with patch.object(market_clock, "KRX_PRE_MARKET_START", date(2026, 9, 16)):
            self.assertEqual(krx(2026, 9, 16, 7, 30)["status"], "pre_market")

    def test_text_names_sessions(self) -> None:
        text = market_clock.format_market_clock(
            market_clock.get_market_clock(datetime(2026, 9, 15, 17, 0, tzinfo=KST)))
        self.assertIn("한국장: 애프터마켓", text)
        self.assertIn("정규장 09:00~15:30 · 애프터마켓 16:00~20:00 · 프리마켓 미시행", text)


class BriefingViewTests(unittest.TestCase):
    def test_after_market_briefing_warns_against_mixing(self) -> None:
        view = server._briefing_session(krx(2026, 9, 15, 17, 0))
        self.assertIn("애프터마켓 진행 중", view)
        self.assertIn("정규장 종가·등락률처럼 쓰지 마세요", view)

    def test_after_20_stays_review(self) -> None:
        self.assertTrue(server._briefing_session(krx(2026, 9, 15, 20, 30)).startswith("장마감 후"))


if __name__ == "__main__":
    unittest.main()
