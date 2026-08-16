"""관심종목 공용 저장 — 세 Lens가 같은 '내 종목'을 보게 하는 기반.

지금은 TelegramLens만 쓰지만, 같은 종목의 시세·공시를 보는 것도 결국 같은
'내 종목'이다. TelegramLens DB 안에 있으면 다른 Lens가 못 읽는다.
옮기면서 **쓰던 사람의 목록을 잃지 않는 것**이 이 테스트의 핵심이다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from telegram_lens import config


class SharedWatchlistPathTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self._env = {k: __import__("os").environ.get(k) for k in ("TELEGRAMLENS_HOME", "LEETKIT_HOME")}
        import os
        os.environ["TELEGRAMLENS_HOME"] = str(self.tmp / "tl")
        os.environ["LEETKIT_HOME"] = str(self.tmp / "lk")

    def tearDown(self):
        import os
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_legacy(self, stocks):
        p = config.data_dir() / "watchlist.json"
        p.write_text(json.dumps({"stocks": stocks}, ensure_ascii=False), encoding="utf-8")
        return p

    def test_path_is_in_shared_folder(self):
        self.assertIn("lk", str(config.watchlist_path()))
        self.assertNotIn("tl", str(config.watchlist_path()))

    def test_legacy_list_is_migrated_not_lost(self):
        """쓰던 사람이 업데이트 후 목록이 비면 그게 사고다."""
        self._write_legacy([{"code": "005930", "name": "삼성전자"}])
        data = json.loads(config.watchlist_path().read_text(encoding="utf-8"))
        self.assertEqual(data["stocks"][0]["code"], "005930")

    def test_legacy_file_is_kept_for_rollback(self):
        """구버전으로 되돌아가도 그대로 쓸 수 있어야 한다 — 원본을 지우지 않는다."""
        legacy = self._write_legacy([{"code": "005930", "name": "삼성전자"}])
        config.watchlist_path()
        self.assertTrue(legacy.exists())

    def test_migration_happens_once_and_does_not_overwrite(self):
        """이미 공용에 목록이 있으면 예전 것으로 덮어쓰면 안 된다."""
        self._write_legacy([{"code": "005930", "name": "삼성전자"}])
        shared = config.watchlist_path()
        shared.write_text(json.dumps({"stocks": [{"code": "000660", "name": "SK하이닉스"}]},
                                     ensure_ascii=False), encoding="utf-8")
        data = json.loads(config.watchlist_path().read_text(encoding="utf-8"))
        self.assertEqual(data["stocks"][0]["code"], "000660")

    def test_no_legacy_no_crash(self):
        """새 사용자는 옮길 게 없다."""
        p = config.watchlist_path()
        self.assertFalse(p.exists())
        self.assertIn("watchlist.json", p.name)

    def test_format_is_stable_for_other_lenses(self):
        """다른 Lens가 읽을 계약이다 — {"stocks":[{code,name}]} 를 유지한다."""
        from telegram_lens import watchlist
        config.watchlist_path().write_text(
            json.dumps({"stocks": [{"code": "039840", "name": "디오"}]}, ensure_ascii=False),
            encoding="utf-8")
        self.assertEqual(watchlist.codes(), ["039840"])
        self.assertEqual(watchlist.load()[0]["name"], "디오")


if __name__ == "__main__":
    unittest.main(verbosity=2)
