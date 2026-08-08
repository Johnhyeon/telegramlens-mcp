"""폐기(거부) 목록 — 환불·취소된 키를 막는 장치.

**여기서 제일 중요한 건 "막히는가"가 아니라 "잘못 막지 않는가"다.**
목록을 못 받았다고 돈 낸 사람이 잠기면, 환불한 사람이 며칠 더 쓰는 것보다 훨씬
비싼 사고다. 그래서 fail open 케이스를 먼저 그리고 더 많이 검사한다.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from telegram_lens import licensing as L


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    """실제 라이선스·캐시를 절대 건드리지 않는다."""
    monkeypatch.setattr(L, "_revoked_cache_path", lambda: tmp_path / "revoked_cache.json")
    monkeypatch.setattr(L, "_revoked_fetched_this_process", False)
    monkeypatch.setattr(L, "_licensed_cache", False)
    return tmp_path


def _cache(path, ids, age_seconds=0.0):
    (path / "revoked_cache.json").write_text(
        json.dumps({"revoked": ids, "fetched_at": time.time() - age_seconds}), encoding="utf-8"
    )


class TestFailOpen:
    """모르면 통과. 이 그룹이 깨지면 유료 고객이 잠긴다."""

    def test_no_cache_and_network_down(self, _isolated):
        with patch("httpx.get", side_effect=OSError("offline")):
            assert L.is_revoked("abc123abc123") is False

    def test_server_returns_garbage(self, _isolated):
        response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: ["not", "a", "dict"]})()
        with patch("httpx.get", return_value=response):
            assert L.is_revoked("abc123abc123") is False

    def test_revoked_field_is_not_a_list(self, _isolated):
        response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"revoked": "abc"}})()
        with patch("httpx.get", return_value=response):
            assert L.is_revoked("abc123abc123") is False

    def test_broken_cache_file(self, _isolated):
        (_isolated / "revoked_cache.json").write_text("{깨진 파일", encoding="utf-8")
        with patch("httpx.get", side_effect=OSError("offline")):
            assert L.is_revoked("abc123abc123") is False

    def test_empty_license_id(self, _isolated):
        assert L.is_revoked("") is False
        assert L.is_revoked(None) is False


class TestBlocking:
    def test_id_in_list_is_revoked(self, _isolated):
        _cache(_isolated, ["abc123abc123"])
        assert L.is_revoked("abc123abc123") is True

    def test_case_and_space_insensitive(self, _isolated):
        _cache(_isolated, ["ABC123ABC123"])
        assert L.is_revoked("  abc123abc123  ") is True

    def test_other_ids_are_untouched(self, _isolated):
        _cache(_isolated, ["abc123abc123"])
        assert L.is_revoked("999999999999") is False

    def test_removing_from_the_list_unblocks(self, _isolated):
        """착오로 막았을 때 몇 분 안에 원복이 안 되면 그게 더 큰 사고다."""
        _cache(_isolated, ["abc123abc123"])
        assert L.is_revoked("abc123abc123") is True
        _cache(_isolated, [])
        assert L.is_revoked("abc123abc123") is False


class TestNetworkBudget:
    """is_licensed()는 도구 호출마다 불린다 — 매번 네트워크를 타면 안 된다."""

    def test_fresh_cache_does_not_hit_the_network(self, _isolated):
        _cache(_isolated, [], age_seconds=60)
        with patch("httpx.get", side_effect=AssertionError("네트워크를 타면 안 된다")) as get:
            L.is_revoked("abc123abc123")
            assert get.call_count == 0

    def test_stale_cache_refetches_once_per_process(self, _isolated):
        _cache(_isolated, [], age_seconds=L._REVOKED_TTL + 10)
        response = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: {"revoked": []}})()
        with patch("httpx.get", return_value=response) as get:
            for _ in range(5):
                L.is_revoked("abc123abc123")
            assert get.call_count == 1, "프로세스당 한 번만 받아야 한다"

    def test_timeout_is_short(self):
        """도구 호출을 오래 붙잡으면 사용자는 앱이 멈춘 줄 안다."""
        assert L._REVOKED_TIMEOUT <= 3.0


class TestMessages:
    def test_revoked_message_does_not_tell_them_to_re_enter_the_key(self):
        """이 사람은 키를 갖고 있다. 재입력하라고 하면 시간만 버린다."""
        assert "활성화하세요" not in L.REVOKED_MESSAGE
        assert "osy980315@gmail.com" in L.REVOKED_MESSAGE

    def test_locked_message_picks_by_reason(self, _isolated, monkeypatch):
        monkeypatch.setattr(L, "license_block_reason", lambda: "revoked")
        assert L.locked_message() == L.REVOKED_MESSAGE
        monkeypatch.setattr(L, "license_block_reason", lambda: "missing")
        assert L.locked_message() == L.LOCKED_MESSAGE
