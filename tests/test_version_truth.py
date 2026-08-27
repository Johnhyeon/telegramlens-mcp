"""TL-01: 버전 진실원천과 잔존 배포판.

실측(UAT): 실행 기능은 0.5.4 소스인데 __version__ 은 importlib.metadata 를
읽어 0.4.2(옛 dist-info)를 보고했고, 상태 안내는 "새 버전 0.5.2"를 제안했다.
개발 venv 에서도 재현된다: 소스 0.5.4 + telegramlens_mcp-0.5.2.dist-info.
전역 환경에는 ~elegramlens_mcp 처럼 pip 임시 리네임이 깨진 채 남은 배포
메타도 있었다.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import telegram_lens
from telegram_lens import _version as tv


class CodeVersionTruthTests(unittest.TestCase):
    def test_running_code_declares_its_own_version(self):
        """실행 코드의 버전은 dist-info 가 아니라 코드 자신이 말한다."""
        self.assertRegex(tv.CODE_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(telegram_lens.__version__, tv.CODE_VERSION)

    def test_code_version_matches_pyproject(self):
        """코드 상수와 pyproject 가 어긋나면 릴리스 전에 여기서 잡는다."""
        py = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8")
        m = re.search(r'^version = "([^"]+)"', py, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(tv.CODE_VERSION, m.group(1))

    def test_dist_version_is_reported_separately(self):
        """dist-info 버전은 별도 함수로 - 코드 버전과 섞지 않는다."""
        dv = tv.dist_version()
        self.assertTrue(dv is None or isinstance(dv, str))


class VersionCompareFallbackTests(unittest.TestCase):
    """0.5.4 실행 환경에 0.5.2 업데이트 안내가 나오면 안 된다 (수용 2)."""

    def test_lower_version_is_never_newer(self):
        from telegram_lens import _update_check as uc

        self.assertFalse(uc._version_gt("0.5.2", "0.5.4"))
        self.assertTrue(uc._version_gt("0.5.5", "0.5.4"))
        self.assertFalse(uc._version_gt("0.5.4", "0.5.4"))

    def test_fallback_without_packaging_is_numeric_and_strict(self):
        import sys as _sys
        from unittest.mock import patch
        from telegram_lens import _update_check as uc

        with patch.dict(_sys.modules, {"packaging": None, "packaging.version": None}):
            self.assertFalse(uc._version_gt("0.5.2", "0.5.4"))
            self.assertFalse(uc._version_gt("0.5.4.0", "0.5.4"))
            self.assertFalse(uc._version_gt("0.5.5rc1", "0.5.5"))
            self.assertFalse(uc._version_gt("unknown", "0.5.4"))
            self.assertTrue(uc._version_gt("0.5.5", "0.5.4"))


class DistMetadataScanTests(unittest.TestCase):
    """요구 5: 동일 정규화 패키지의 복수·깨진 배포 메타를 탐지한다."""

    def _site(self, tmp, names):
        import pathlib
        root = pathlib.Path(tmp)
        for n in names:
            d = root / n
            d.mkdir(parents=True)
            if n.endswith(".dist-info") and not n.startswith("~"):
                (d / "METADATA").write_text("Name: x", encoding="utf-8")
        return root

    def test_duplicates_and_broken_names_are_found(self):
        import tempfile
        from telegram_lens.doctor import scan_dist_metadata

        with tempfile.TemporaryDirectory() as tmp:
            root = self._site(tmp, [
                "telegramlens_mcp-0.5.4.dist-info",
                "telegramlens_mcp-0.4.2.dist-info",       # 잔존 구버전
                "~elegramlens_mcp-0.4.2.dist-info",       # pip 임시 리네임 잔재
                "somethingelse-1.0.dist-info",
            ])
            report = scan_dist_metadata("telegramlens-mcp", site_packages=[root])
        self.assertEqual(len(report["valid"]), 2)
        self.assertEqual(len(report["broken"]), 1)
        self.assertTrue(report["duplicated"])
        versions = {v["version"] for v in report["valid"]}
        self.assertEqual(versions, {"0.5.4", "0.4.2"})

    def test_single_clean_install_is_ok(self):
        import tempfile
        from telegram_lens.doctor import scan_dist_metadata

        with tempfile.TemporaryDirectory() as tmp:
            root = self._site(tmp, ["telegramlens_mcp-0.5.4.dist-info"])
            report = scan_dist_metadata("telegramlens-mcp", site_packages=[root])
        self.assertFalse(report["duplicated"])
        self.assertEqual(report["broken"], [])

    def test_version_mismatch_report(self):
        from telegram_lens.doctor import version_report

        r = version_report(code_version="0.5.4", dist="0.4.2")
        self.assertTrue(r["version_mismatch"])
        self.assertEqual(r["code_version"], "0.5.4")
        self.assertEqual(r["dist_version"], "0.4.2")
        r2 = version_report(code_version="0.5.4", dist="0.5.4")
        self.assertFalse(r2["version_mismatch"])
        # dist 를 못 읽는 것(editable 등)은 불일치가 아니라 미확인이다
        r3 = version_report(code_version="0.5.4", dist=None)
        self.assertFalse(r3["version_mismatch"])
        self.assertIsNone(r3["dist_version"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
