"""TL-02: 종목명 오탐 후보를 검토 가능하게.

실측(UAT):
- 코드 동반이 없다는 이유만으로 정상 종목(LG전자·에코프로비엠)도 의심도
  1.0 후보가 됐다.
- 후보에 원문 문맥·채널·시각이 없어 안전한 차단 결정을 내릴 수 없었다.
- 차단이 기존 집계에 몇 건 영향을 주는지 미리 볼 방법이 없었다.
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

from telegram_lens import db as tdb
from telegram_lens import discover
from telegram_lens import extract


def _now(offset_h=0):
    return (datetime.now(timezone.utc) - timedelta(hours=offset_h)) \
        .replace(tzinfo=None).isoformat()


STOCKS = {"066570": "LG전자", "247540": "에코프로비엠", "001680": "대상",
          "001500": "현대차증권", "005380": "현대차"}


class CitationFilterTests(unittest.TestCase):
    """수용 1: 리포트 작성사로만 등장한 증권사는 분석 대상 언급이 아니다."""

    def _mentions(self, text):
        with patch.object(extract, "load_stocks", lambda: STOCKS), \
             patch.object(extract, "load_source_firms", lambda: {"001500"}), \
             patch.object(extract, "load_aliases", lambda: {}), \
             patch.object(extract, "load_ambiguous", lambda: set()):
            extract._name_index.cache_clear()
            try:
                return extract.extract_mentions(text)
            finally:
                extract._name_index.cache_clear()

    def test_author_citation_is_not_a_mention(self):
        out = self._mentions("작성자: 현대차증권 (박현욱)")
        self.assertNotIn("001500", dict(out))

    def test_report_issuance_is_not_a_mention(self):
        out = self._mentions("당일 현대차증권 Buy(유지) 보고서 발행")
        self.assertNotIn("001500", dict(out))

    def test_code_confirmed_broker_is_still_a_mention(self):
        out = self._mentions("현대차증권(001500) 신사업 진출로 급등")
        self.assertIn("001500", dict(out))


def _seed(conn, rows):
    """rows: (code, name, channel_id, text, msg_type)"""
    conn.execute("INSERT OR IGNORE INTO channels(id, title, username) "
                 "VALUES (1,'리딩방A','lead_a'), (2,'리서치B','res_b'), "
                 "(3,'속보C','fast_c')")
    for i, (code, name, ch, text, mtype) in enumerate(rows):
        conn.execute(
            "INSERT INTO messages(channel_id, msg_id, date, text, msg_type) "
            "VALUES (?,?,?,?,?)", (ch, 1000 + i, _now(i), text, mtype))
        mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO mentions(message_id, channel_id, code, name, date) "
            "VALUES (?,?,?,?,?)", (mid, ch, code, name, _now(i)))
    conn.commit()


class SuspicionScoringTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        dbfile = Path(self._tmp.name) / "t.db"
        self._patch = patch.object(tdb, "db_path", lambda: dbfile)
        self._patch.start()
        tdb.init_db()
        with tdb.connect() as conn:
            rows = []
            # LG전자: 3개 채널, 서로 다른 문장 6건 - 코드는 한 번도 없다
            texts = ["LG전자 신형 가전 발표", "LG전자 실적 서프라이즈 얘기 도는 중",
                     "오늘 LG전자 흐름 좋네요", "LG전자 인도법인 상장 얘기",
                     "LG전자 전장 수주 확대", "LG전자 배당 늘린다는 말이"]
            for i, t in enumerate(texts):
                rows.append(("066570", "LG전자", (i % 3) + 1, t, "chat"))
            # 대상(2자): 한 채널에서 같은 문장 복붙 5건
            for _ in range(5):
                rows.append(("001680", "대상", 1, "오늘의 대상 종목은 여기", "gossip"))
            _seed(conn, rows)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _candidates(self, **kw):
        with patch.object(discover, "load_ambiguous", lambda: set()), \
             patch.object(discover, "load_stocks", lambda: STOCKS):
            opts = dict(days=7, max_name_len=6, min_count=3, top=40)
            opts.update(kw)
            return discover.false_positive_candidates(**opts)

    def test_legit_name_is_not_auto_block_grade(self):
        """수용 2: 정상 이름 언급은 코드가 없어도 의심도 1.0 이 아니다."""
        cands = {c["code"]: c for c in self._candidates()}
        lg = cands["066570"]
        self.assertLess(lg["suspicion"], 0.5)
        self.assertEqual(lg["review"], "정상 가능성")
        self.assertEqual(lg["code_absent_ratio"], 1.0)   # 원신호는 보존

    def test_short_spam_name_stays_suspicious(self):
        cands = {c["code"]: c for c in self._candidates()}
        daesang = cands["001680"]
        self.assertGreater(daesang["suspicion"],
                           cands["066570"]["suspicion"])
        self.assertNotEqual(daesang["review"], "정상 가능성")

    def test_signals_are_reported(self):
        """요구 3: 코드 동반 외 신호(정식명·길이·채널 다양성·반복)가 보인다."""
        lg = {c["code"]: c for c in self._candidates()}["066570"]
        sig = lg["signals"]
        self.assertTrue(sig["official_name"])
        self.assertEqual(sig["distinct_channels"], 3)
        self.assertGreaterEqual(sig["distinct_text_ratio"], 0.9)

    def test_samples_carry_context(self):
        """요구 1: 후보마다 문맥·채널·시각·유형·링크 표본이 붙는다."""
        lg = {c["code"]: c for c in self._candidates()}["066570"]
        self.assertGreaterEqual(len(lg["samples"]), 1)
        s = lg["samples"][0]
        self.assertIn("LG전자", s["context"])
        self.assertIn(s["channel"], ("리딩방A", "리서치B", "속보C"))
        self.assertTrue(s["date"])
        self.assertEqual(s["msg_type"], "chat")
        self.assertIn("t.me/", s["link"])


class BlockDryRunTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        dbfile = Path(self._tmp.name) / "t.db"
        self._patch = patch.object(tdb, "db_path", lambda: dbfile)
        self._patch.start()
        tdb.init_db()
        with tdb.connect() as conn:
            rows = [("001680", "대상", 1, "오늘의 대상 종목", "gossip")
                    for _ in range(4)]
            rows.append(("001680", "대상", 2, "대상(001680) 실적 발표", "report"))
            _seed(conn, rows)

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_dry_run_counts_only_name_only_mentions(self):
        """요구 4: 코드 동반 언급은 차단해도 남는다 - 제외될 것만 센다."""
        preview = discover.block_preview("001680", days=30)
        self.assertEqual(preview["would_exclude"], 4)
        self.assertGreaterEqual(len(preview["samples"]), 1)
        # dry-run 은 아무것도 지우지 않는다
        with tdb.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
        self.assertEqual(n, 5)

    def test_apply_matches_dry_run_count(self):
        """수용 3: dry-run 과 실제 적용의 제외 건수가 일치한다."""
        preview = discover.block_preview("001680", days=30)
        purged = discover.purge_name_only_mentions("001680", days=30)
        self.assertEqual(purged, preview["would_exclude"])
        with tdb.connect() as conn:
            left = conn.execute(
                "SELECT COUNT(*) FROM mentions WHERE code='001680'").fetchone()[0]
        self.assertEqual(left, 1)   # 코드 동반 언급은 남는다

    def test_blocked_code_disappears_from_candidates(self):
        """요구 5: 차단 후 fp_candidates 재실행으로 회귀를 확인할 수 있다."""
        with patch.object(discover, "load_ambiguous", lambda: {"001680"}), \
             patch.object(discover, "load_stocks", lambda: STOCKS):
            cands = discover.false_positive_candidates(
                days=7, max_name_len=6, min_count=1, top=40)
        self.assertNotIn("001680", {c["code"] for c in cands})


if __name__ == "__main__":
    unittest.main(verbosity=2)
