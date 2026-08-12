from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from telegram_lens import _update_check as U


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "_cache_file", lambda: tmp_path / "update_check.json")
    monkeypatch.setattr(U, "_notice_issued", False)
    monkeypatch.delenv("TELEGRAMLENS_FORCE_UPDATE_NOTICE", raising=False)
    return tmp_path / "update_check.json"


def _cache(path, version, *, age_hours=0):
    path.write_text(
        json.dumps(
            {
                "checked_at": (datetime.now() - timedelta(hours=age_hours)).isoformat(),
                "latest_version": version,
                "release_notes": "- 뭔가 개선",
            }
        ),
        encoding="utf-8",
    )


def _notice() -> str:
    # 이 리포엔 pytest-asyncio가 없다 — 코루틴을 직접 돌린다.
    return asyncio.run(U.get_update_notice())


class TestNoticeText:
    """세 Lens 안내를 같은 말로 맞춘다 — 터미널 명령을 적어두면 주 고객층은 거기서
    막힌다. LeetKit Manager가 있는 이유가 그 명령을 안 치게 하려는 것이다."""

    def test_points_at_the_manager(self):
        assert "LeetKit Manager" in U._format_notice("0.9.9", "0.4.20", "- 개선")

    def test_does_not_tell_a_terminal_command(self):
        assert "uv tool" not in U._format_notice("0.9.9", "0.4.20", "- 개선")

    def test_shows_both_versions(self):
        notice = U._format_notice("0.9.9", "0.4.20", "- 개선")
        assert "v0.9.9" in notice and "v0.4.20" in notice

    def test_caps_the_note_length(self):
        """릴리스 노트가 길면 도구 응답을 통째로 덮는다."""
        notes = "\n".join(f"- 항목 {i}" for i in range(50))
        body = U._format_notice("0.9.9", "0.4.20", notes).split("주요 변경:\n")[1]
        assert len(body.splitlines()) <= U.MAX_NOTE_LINES

    def test_empty_notes_do_not_leave_a_blank(self):
        assert "(릴리즈 노트 없음)" in U._format_notice("0.9.9", "0.4.20", "")


class TestGate:
    def test_silent_when_already_latest(self, _isolated):
        _cache(_isolated, U.__version__)
        assert _notice() == ""

    def test_announces_a_newer_version(self, _isolated):
        _cache(_isolated, "99.0.0")
        assert "99.0.0" in _notice()

    def test_only_once_per_process(self, _isolated):
        """도구를 수백 번 불러도 한 번만 — 매번 붙으면 데이터를 가린다."""
        _cache(_isolated, "99.0.0")
        assert _notice() != ""
        assert _notice() == ""

    def test_network_failure_is_silent(self, monkeypatch):
        """알림 하나 때문에 도구가 막히면 안 된다."""

        async def _fail():
            return None

        monkeypatch.setattr(U, "_fetch_latest", _fail)
        assert _notice() == ""

    def test_stale_cache_is_refetched(self, _isolated, monkeypatch):
        _cache(_isolated, "0.0.1", age_hours=48)
        called = {"n": 0}

        async def _fetch():
            called["n"] += 1
            return "99.0.0", "- 새 기능"

        monkeypatch.setattr(U, "_fetch_latest", _fetch)
        assert "99.0.0" in _notice()
        assert called["n"] == 1

    def test_corrupt_cache_does_not_crash(self, _isolated, monkeypatch):
        _isolated.write_text("깨진 파일", encoding="utf-8")

        async def _fail():
            return None

        monkeypatch.setattr(U, "_fetch_latest", _fail)
        assert _notice() == ""


class TestVersionCompare:
    def test_newer_is_greater(self):
        assert U._version_gt("0.5.0", "0.4.20") is True

    def test_same_is_not(self):
        assert U._version_gt("0.4.20", "0.4.20") is False

    def test_older_is_not(self):
        """되돌아가라고 권하면 안 된다."""
        assert U._version_gt("0.4.19", "0.4.20") is False
