"""집계 세그먼트(국내주식·미국주식·국내ETF·미국ETF)와 추출 오탐 가드.

실측 배경:
  - 48시간 버즈 1위가 "AI"(C3.ai)였다. 미국 bare 티커 가드가 시장 문맥어를
    메시지 전체에서 찾아, 증권 채널 글은 전부 통과했다.
  - "한화투자증권"을 인용으로 걸러낸 자리에서 접두어 "한화"(000880)가 다시 잡혔다.
  - 한 랭킹에 네 시장을 섞으면 언급이 많은 국내주식이 정원을 다 먹어
    국내ETF·미국ETF 가 통째로 사라졌다.
"""

from __future__ import annotations

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

from telegram_lens import extract, queries, us_stocks


class SegmentClassificationTests(unittest.TestCase):
    """코드 모양으로 시장을, ETF 목록으로 종류를 가른다."""

    def test_krx_short_codes_are_kr(self):
        for code in ("005930", "069500", "0126Z0"):
            self.assertEqual(queries.market_of(code), "KR", code)

    def test_tickers_are_us(self):
        for t in ("NVDA", "SPY", "AAPL"):
            self.assertEqual(queries.market_of(t), "US", t)

    def test_four_segments(self):
        with patch.object(queries, "load_etf_codes", lambda: {"069500"}):
            self.assertEqual(queries.segment_of("005930", "삼성전자"), "kr_stock")
            self.assertEqual(queries.segment_of("069500", "KODEX 200"), "kr_etf")
            self.assertEqual(queries.segment_of("NVDA", "NVIDIA Corporation"), "us_stock")
            self.assertEqual(
                queries.segment_of("SPY", "SPDR S&P 500 ETF Trust"), "us_etf"
            )

    def test_netflix_is_not_an_etf(self):
        """이름 부분일치로 ETF 를 보면 Netflix 의 'etf' 가 걸린다."""
        self.assertFalse(us_stocks.is_us_etf("NFLX", "Netflix, Inc."))
        self.assertTrue(us_stocks.is_us_etf("SPY", "SPDR S&P 500 ETF Trust"))


class PerSegmentCutoffTests(unittest.TestCase):
    """작은 세그먼트가 큰 세그먼트에 정원을 뺏기지 않는다."""

    def _rows(self):
        # 국내주식이 점수 상위를 독식하는 배치
        rows = [{"code": f"00{i:04d}", "name": f"국내{i}"} for i in range(1, 11)]
        rows += [{"code": "NVDA", "name": "NVIDIA Corporation"}]
        rows += [{"code": "069500", "name": "KODEX 200"}]
        rows += [{"code": "SPY", "name": "SPDR S&P 500 ETF Trust"}]
        return rows

    def test_small_segments_survive_the_cut(self):
        with patch.object(queries, "load_etf_codes", lambda: {"069500"}):
            out = queries._take_per_segment(
                self._rows(), top=3, name_key=lambda r: r["name"]
            )
        codes = [r["code"] for r in out]
        self.assertEqual(len([c for c in codes if c.startswith("00") and c != "069500"]), 3)
        self.assertIn("NVDA", codes)
        self.assertIn("069500", codes)
        self.assertIn("SPY", codes)

    def test_order_is_kr_stock_us_stock_kr_etf_us_etf(self):
        with patch.object(queries, "load_etf_codes", lambda: {"069500"}):
            out = queries._take_per_segment(
                self._rows(), top=1, name_key=lambda r: r["name"]
            )
        self.assertEqual([r["code"] for r in out], ["000001", "NVDA", "069500", "SPY"])

    def test_market_filter_narrows_to_one_side(self):
        with patch.object(queries, "load_etf_codes", lambda: {"069500"}):
            pred = queries._segment_predicate("all", "US")
            self.assertTrue(pred("NVDA", "NVIDIA Corporation"))
            self.assertFalse(pred("005930", "삼성전자"))
            pred_etf = queries._segment_predicate("etf", "KR")
            self.assertTrue(pred_etf("069500", "KODEX 200"))
            self.assertFalse(pred_etf("005930", "삼성전자"))


class CitationSuppressionTests(unittest.TestCase):
    """리포트 요약 글의 발행사 표기는 종목 언급이 아니다."""

    def _codes(self, text):
        return {c for c, _ in extract.extract_mentions(text)}

    def test_issuer_prefix_is_not_a_mention(self):
        # "한화투자증권"을 걸러낸 자리에서 "한화"가 다시 잡히면 안 된다
        self.assertNotIn("000880", self._codes("한화투자증권(2026-09-09) [링크]"))
        self.assertNotIn("034730", self._codes("SK증권(2026-09-09) [링크]"))

    def test_paren_issuer_at_line_start_is_not_a_mention(self):
        text = "HDC 24,700원(+5.78%)\n(한화) 나의 계절이 왔다"
        self.assertNotIn("000880", self._codes(text))

    def test_code_beside_the_name_still_counts(self):
        self.assertIn("000880", self._codes("한화 000880 주가가 올랐다"))

    def test_real_mention_survives(self):
        self.assertIn("042660", self._codes("한화오션(+2.32%) 태국 호위함 수주"))


class TailBoundaryTests(unittest.TestCase):
    """이름 뒤 한글이 조사·회사접미가 아니면 더 긴 단어의 앞부분이다."""

    def _codes(self, text):
        return {c for c, _ in extract.extract_mentions(text)}

    def test_longer_word_is_not_a_mention(self):
        self.assertNotIn("352820", self._codes("하이브리드 본딩 기술"))
        self.assertNotIn("000270", self._codes("프랑스령 기아나, 도미니카공화국"))
        self.assertNotIn("035720", self._codes("카카오헬스케어와의 공급 계약"))

    def test_particles_and_company_suffixes_keep_the_mention(self):
        self.assertIn("004370", self._codes("농심이 연말까지 편안하다"))
        self.assertIn("005930", self._codes("삼성전자를 담았다"))
        self.assertIn("005380", self._codes("현대차그룹 계열사"))


class UsTickerGuardTests(unittest.TestCase):
    """한국 채널에서 AI·IR·HBM 은 단어지 티커가 아니다."""

    def _codes(self, text):
        return {c for c, _ in extract.extract_mentions(text)}

    def test_korean_context_abbreviations_are_not_tickers(self):
        got = self._codes("괴물 AI 아스트라, AGI 시대. HBM 공급 부족, 주가 급등")
        for t in ("AI", "AGI", "HBM"):
            self.assertNotIn(t, got, t)

    def test_cashtag_still_wins(self):
        self.assertIn("AGI", self._codes("$AGI 알라모스골드 주가"))

    def test_hangul_adjacent_bare_ticker_is_skipped(self):
        # "HD현대"의 HD 가 Home Depot 으로 잡히던 문제
        self.assertNotIn("HD", self._codes("HD현대건설기계 실적 주가"))

    def test_seed_ticker_still_resolves(self):
        self.assertIn("NVDA", self._codes("엔비디아 NVDA 실적 서프라이즈"))


class BlockListIsDataTests(unittest.TestCase):
    """미국 차단 목록은 코드 상수가 아니라 데이터여야 한다 — 도구로 늘릴 수 있게."""

    def test_us_block_list_loads_from_data_file(self):
        from telegram_lens.stocks import load_us_blocked

        blocked = load_us_blocked()
        self.assertGreater(len(blocked), 50)
        for t in ("AI", "IR", "HBM", "ASIC", "DAC"):
            self.assertIn(t, blocked, t)

    def test_confirm_token_differs_by_market(self):
        from telegram_lens import discover

        self.assertEqual(discover.confirm_token_for("005930"), "005930")
        self.assertEqual(discover.confirm_token_for("NVDA"), "$NVDA")


class KoreanAbbreviationAliasTests(unittest.TestCase):
    """한국 회사 약어는 막을 게 아니라 국내 종목으로 돌려줘야 한다."""

    def _codes(self, text):
        return {c for c, _ in extract.extract_mentions(text)}

    def test_kai_resolves_to_korea_aerospace(self):
        self.assertIn("047810", self._codes("KAI 유무인 복합 전투기"))
        self.assertNotIn("KAI", self._codes("KAI 유무인 복합 전투기"))

    def test_kaist_is_not_a_mention(self):
        self.assertEqual(self._codes("KAIST 연구진 발표"), set())

    def test_longer_english_form_wins_over_short_alias(self):
        # 사전은 '케이티앤지'로만 갖고 있어, 영문형을 별칭으로 넣어야 KT 가 물지 않는다
        self.assertIn("033780", self._codes("KT&G 배당 확대"))
        self.assertNotIn("030200", self._codes("KT&G 배당 확대"))
        self.assertIn("344820", self._codes("KCC글라스 유리 흑자"))
        self.assertNotIn("002380", self._codes("KCC글라스 유리 흑자"))

    def test_short_alias_still_works_alone(self):
        self.assertIn("030200", self._codes("KT 실적 개선"))
        self.assertIn("002380", self._codes("KCC 실적 개선"))


class AliasCandidatePatternTests(unittest.TestCase):
    """`이름(코드)` 표기에서 이름을 통째로 떠와야 한다.

    실측: 옛 패턴은 10자에서 끊고 왼쪽 경계가 없어 '한국타이어앤테크놀로지' 를
    '국타이어앤테크놀로지' 로, 'S-Oil' 을 'Oil' 로 잘라 후보에 올렸다.
    """

    def _find(self, text):
        from telegram_lens.discover import _NAME_CODE_RE

        return _NAME_CODE_RE.findall(text)

    def test_long_korean_name_is_not_truncated(self):
        self.assertEqual(
            self._find("한국타이어앤테크놀로지(161390) 실적"),
            [("한국타이어앤테크놀로지", "161390")],
        )
        self.assertEqual(
            self._find("LIG디펜스앤에어로스페이스(079550)"),
            [("LIG디펜스앤에어로스페이스", "079550")],
        )

    def test_hyphen_and_ampersand_are_left_boundaries(self):
        self.assertEqual(self._find("S-Oil(010950) 정제마진"), [("S-Oil", "010950")])
        self.assertEqual(self._find("삼성E&A(028050) 수주"), [("삼성E&A", "028050")])

    def test_ascii_name_keeps_its_internal_space(self):
        self.assertEqual(self._find("LS ELECTRIC(010120)"), [("LS ELECTRIC", "010120")])
        self.assertEqual(self._find("NHN KCP(060250)"), [("NHN KCP", "060250")])

    def test_korean_name_does_not_swallow_the_previous_word(self):
        # 한글로 시작하는 이름에는 내부 구분자를 허용하지 않는다
        self.assertEqual(self._find("오늘의 리포트 삼성전자(005930)"), [("삼성전자", "005930")])

    def test_new_style_alphanumeric_code_is_matched(self):
        self.assertEqual(
            self._find("삼성에피스홀딩스(0126Z0) 상장"), [("삼성에피스홀딩스", "0126Z0")]
        )


if __name__ == "__main__":
    unittest.main()
