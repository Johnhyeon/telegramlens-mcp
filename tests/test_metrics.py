"""도구 호출 기록 — 세 Lens 중 여기만 없어서 뒤늦게 붙였다.

2026-08-13 해외 고객 장애 때, StockLens·DartLens 는 로그의 `error_detail` 한 줄이
원인(TLS 가로채기)을 알려줬는데 TelegramLens 만 깜깜했다. "이 Lens는 되는 것 같다"는
인상이 맞는지 확인할 방법 자체가 없었다.

이 파일이 지키는 것은 두 가지다.
  1. 실패가 실패로 남는가 (safe_tool 이 예외를 문자열로 바꾸기 전에 기록되는가)
  2. 고객이 메일로 내보내는 파일에 비밀이 안 실리는가 (전화번호·API 키)
"""

from __future__ import annotations

import asyncio
import json

import pytest

from telegram_lens import _metrics


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """이 PC의 진짜 ~/.telegramlens 를 건드리지 않는다."""
    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))
    monkeypatch.setattr(_metrics, "data_dir", lambda: tmp_path)
    return tmp_path


def _records() -> list[dict]:
    path = _metrics.get_metrics_file()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(func, **kwargs):
    return asyncio.run(func(**kwargs))


class TestRecording:
    def test_successful_call_is_recorded(self):
        @_metrics.track_metrics("demo")
        async def tool():
            return "결과 10건"

        _run(tool)
        (r,) = _records()
        assert r["tool"] == "demo"
        assert r["error"] is None
        assert r["output_chars"] == len("결과 10건")

    def test_failure_is_recorded_and_reraised(self):
        """예외를 삼키면 안 된다 — 기록만 하고 그대로 올려야 safe_tool 이 평소대로 동작한다."""

        @_metrics.track_metrics("demo")
        async def tool():
            raise ConnectionError("연결 거부")

        with pytest.raises(ConnectionError):
            _run(tool)
        (r,) = _records()
        assert r["error"] == "ConnectionError"
        assert "연결 거부" in r["error_detail"]

    def test_cancellation_is_not_recorded_as_success(self):
        """수집·동기화는 오래 걸려 취소될 여지가 있다. CancelledError 는 Exception 이
        아니라 BaseException 이라, 안 잡으면 error=null 로 남아 성공처럼 보인다."""

        @_metrics.track_metrics("demo")
        async def tool():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            _run(tool)
        (r,) = _records()
        assert r["error"] == "CancelledError"

    def test_schema_matches_the_other_lenses(self):
        """지원 번들에 세 Lens 로그를 나란히 담아 한 번에 훑는다 — 필드가 어긋나면 그 대조가 깨진다."""

        @_metrics.track_metrics("demo")
        async def tool():
            return "x"

        _run(tool)
        assert set(_records()[0]) == {
            "timestamp",
            "tool",
            "kwargs",
            "duration_ms",
            "output_chars",
            "cache_hit",
            "error",
            "error_detail",
        }


class TestSecretsStayOut:
    """이 파일은 지원 번들에 담겨 고객이 메일로 내보낸다."""

    def test_phone_number_is_masked(self):
        @_metrics.track_metrics("demo")
        async def tool():
            raise RuntimeError("인증 실패 +82 10-1234-5678")

        with pytest.raises(RuntimeError):
            _run(tool)
        detail = _records()[0]["error_detail"]
        assert "1234" not in detail and "<번호>" in detail

    def test_query_string_is_stripped(self):
        @_metrics.track_metrics("demo")
        async def tool():
            raise RuntimeError("실패: https://api.example/x?api_hash=deadbeefcafe")

        with pytest.raises(RuntimeError):
            _run(tool)
        detail = _records()[0]["error_detail"]
        assert "deadbeefcafe" not in detail

    def test_sensitive_kwargs_are_not_stored(self):
        @_metrics.track_metrics("demo")
        async def tool(phone=None, code=None, api_hash=None, hours=24):
            return "ok"

        _run(tool, phone="+821012345678", code="54321", api_hash="deadbeef", hours=24)
        kwargs = _records()[0]["kwargs"]
        assert kwargs["phone"] == "<가림>"
        assert kwargs["code"] == "<가림>"
        assert kwargs["api_hash"] == "<가림>"
        assert kwargs["hours"] == "24"  # 평범한 인자는 남아야 원인 추적에 쓸모가 있다

    def test_long_values_are_truncated(self):
        @_metrics.track_metrics("demo")
        async def tool(note=None):
            return "ok"

        _run(tool, note="가" * 500)
        assert len(_records()[0]["kwargs"]["note"]) <= 81


class TestNeverBreaksTheTool:
    def test_unwritable_log_folder_does_not_fail_the_call(self, tmp_path, monkeypatch):
        """기록 실패가 도구를 막으면 안 된다."""
        blocked = tmp_path / "blocked"
        blocked.write_text("나는 폴더가 아니다", encoding="utf-8")
        monkeypatch.setattr(_metrics, "data_dir", lambda: blocked)

        @_metrics.track_metrics("demo")
        async def tool():
            return "정상 결과"

        assert _run(tool) == "정상 결과"
