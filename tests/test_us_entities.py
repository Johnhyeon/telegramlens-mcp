"""TL-03: 미국 종목 사전과 미지원 상태 구분.

실측(UAT): 고정 미국 15티커가 모두 종목 사전에 없어, 0건이 "실제 무언급"인지
"미국 미지원"인지 결과만으로 구분할 수 없었다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from telegram_lens import extract, us_stocks


class UsResolveTests(unittest.TestCase):
    """수용 1: PLTR·RIVN·CRSP 를 미국 종목으로 식별한다."""

    def test_primary_tickers_resolve(self):
        for t, name_part in (("PLTR", "Palantir"), ("RIVN", "Rivian"),
                             ("CRSP", "CRISPR")):
            r = us_stocks.resolve_us(t)
            self.assertIsNotNone(r, t)
            self.assertEqual(r["ticker"], t)
            self.assertIn(name_part.lower(), r["name"].lower())

    def test_korean_aliases_resolve(self):
        self.assertEqual(us_stocks.resolve_us("팔란티어")["ticker"], "PLTR")
        self.assertEqual(us_stocks.resolve_us("리비안")["ticker"], "RIVN")
        self.assertEqual(us_stocks.resolve_us("엔비디아")["ticker"], "NVDA")

    def test_class_shares_share_an_issuer(self):
        a = us_stocks.resolve_us("GOOGL")
        b = us_stocks.resolve_us("GOOG")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a["name"], b["name"])

    def test_company_name_resolves(self):
        r = us_stocks.resolve_us("Palantir")
        self.assertEqual(r["ticker"], "PLTR")

    def test_unknown_is_none(self):
        self.assertIsNone(us_stocks.resolve_us("ZZZZZZ아님"))

    def test_foreign_exchange_suffix_is_unsupported_market(self):
        self.assertTrue(us_stocks.is_unsupported_market("7203.T"))
        self.assertTrue(us_stocks.is_unsupported_market("0700.HK"))
        self.assertFalse(us_stocks.is_unsupported_market("BRK.B"))
        self.assertFalse(us_stocks.is_unsupported_market("PLTR"))


class UsExtractionTests(unittest.TestCase):
    """요구 1·3: cashtag·별칭은 잡고, 일반 영어 단어 티커는 문맥 없이 안 잡는다."""

    def _mentions(self, text):
        with patch.object(extract, "load_stocks", lambda: {"005930": "삼성전자"}), \
             patch.object(extract, "load_source_firms", lambda: set()), \
             patch.object(extract, "load_aliases", lambda: {}), \
             patch.object(extract, "load_ambiguous", lambda: set()):
            extract._name_index.cache_clear()
            try:
                return dict(extract.extract_mentions(text))
            finally:
                extract._name_index.cache_clear()

    def test_cashtag_is_always_a_mention(self):
        out = self._mentions("$PLTR 실적 대박이네")
        self.assertIn("PLTR", out)

    def test_korean_alias_is_a_mention(self):
        out = self._mentions("팔란티어 어제 급등했다던데")
        self.assertIn("PLTR", out)

    def test_bare_uncommon_ticker_is_a_mention(self):
        out = self._mentions("RIVN 데이터 좋게 나옴")
        self.assertIn("RIVN", out)

    def test_common_word_ticker_needs_context(self):
        # ALL(Allstate)은 일반 영어 단어 - 문맥 없이 잡으면 안 된다
        self.assertNotIn("ALL", self._mentions("we are ALL in this together"))
        # 시장 문맥이 있으면 잡는다
        self.assertIn("ALL", self._mentions("나스닥 ALL 주가 반등"))

    def test_korean_stocks_still_work(self):
        out = self._mentions("삼성전자 오늘 강세, $PLTR 도 강세")
        self.assertIn("005930", out)
        self.assertIn("PLTR", out)


class BuzzStatusTests(unittest.IsolatedAsyncioTestCase):
    """수용 2·요구 2: 지원 0건 / 미지원 / 사전 없음을 다른 상태로 돌려준다."""

    async def _buzz(self, query):
        from telegram_lens import server

        async def run():
            return await server.telegram_stock_buzz(query=query, hours=24)

        # 라이선스 게이트·ETF 목록은 이 테스트의 대상이 아니다 - 깨끗한 환경
        # (라이선스 없음, 로컬 DB 없음)에서도 상태 구분 로직만 검증한다.
        with patch.object(server, "is_licensed", lambda: True), \
             patch.object(server, "_collecting_notice", lambda: None), \
             patch.object(server, "load_etf_codes", lambda: set()), \
             patch.object(server, "load_stocks", lambda: {"005930": "삼성전자"}), \
             patch.object(server.queries, "stock_buzz",
                          lambda code, name, hours, samples: {
                              "code": code, "name": name,
                              "independent": 0, "raw_messages": 0, "samples": []}):
            return json.loads(await run())

    async def test_supported_us_zero_mentions_is_distinct(self):
        out = await self._buzz("PLTR")
        self.assertEqual(out["entity_status"], "supported_but_zero_mentions")
        self.assertEqual(out["market"], "US")
        self.assertIn("무언급", out["status_note"])

    async def test_unknown_name_is_entity_not_found(self):
        from telegram_lens import server

        with patch.object(server, "is_licensed", lambda: True), \
             patch.object(server, "_collecting_notice", lambda: None), \
             patch.object(server, "load_stocks", lambda: {"005930": "삼성전자"}):
            out = json.loads(await server.telegram_stock_buzz(query="없는종목명임"))
        self.assertEqual(out["entity_status"], "entity_not_found")

    async def test_foreign_market_is_unsupported(self):
        from telegram_lens import server

        with patch.object(server, "is_licensed", lambda: True), \
             patch.object(server, "_collecting_notice", lambda: None), \
             patch.object(server, "load_stocks", lambda: {"005930": "삼성전자"}):
            out = json.loads(await server.telegram_stock_buzz(query="7203.T"))
        self.assertEqual(out["entity_status"], "unsupported_market")


if __name__ == "__main__":
    unittest.main(verbosity=2)
