"""결함 2·출시 전 정리: telegram_status 의 버전 표시와 신규 설치 안전.

실측(UAT): 버전 불일치가 doctor 에서만 보였다 - 사용자는 status 를 먼저 본다.
실측(정리): 스키마 없는 빈 DB(신규 설치)에서 첫 조회가
"no such table: messages" 로 죽어 "처리 중 오류"로 보였다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_lens import server


class StatusVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_carries_version_report(self):
        """TL-01 요구 위치: doctor 만이 아니라 telegram_status 에도 버전이 보인다."""
        with patch.object(server, "is_licensed", lambda: True), \
             patch.object(server, "is_logged_in", lambda: False):
            out = json.loads(await server.telegram_status())
        v = out["version"]
        self.assertEqual(v["code_version"], "0.5.4")
        self.assertIn("dist_version", v)
        self.assertIn("version_mismatch", v)

    async def test_status_version_mismatch_flag(self):
        from telegram_lens.doctor import version_report

        self.assertTrue(version_report("0.5.4", "0.4.2")["version_mismatch"])
        self.assertFalse(version_report("0.5.4", "0.5.4")["version_mismatch"])
        # 메타를 못 읽은 것(editable 등)은 불일치가 아니다
        self.assertFalse(version_report("0.5.4", None)["version_mismatch"])


class CleanInstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_query_on_schemaless_db_is_notice_not_error(self):
        """신규 설치(스키마 없는 빈 DB): 오류가 아니라 '수집 중' 안내."""
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "telegram.db"
            empty.touch()   # 파일은 있으나 테이블이 없다 - db.connect 가 만드는 상태
            with patch.object(server.db, "db_path", lambda: empty):
                notice = server._collecting_notice()
        self.assertIsNotNone(notice)
        self.assertIn("처음 수집", notice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
