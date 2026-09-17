"""RECENT_TOOL_FAILURES — 최근 48시간 도구 호출 기록으로 "지금 막혀 있나"를 판정한다.

판정 기준(세 Lens 공통): 기록 없음 / 전부 정상 / 실패했지만 같은 도구가 그 뒤 성공 → ok,
어떤 도구의 마지막 호출이 실패 → warn. AI 앱이 취소한 호출(CancelledError)은 실패로 안 센다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from telegram_lens import _metrics, doctor

NOW = datetime(2026, 9, 17, 15, 0, 0)


def _rec(minutes_ago: int, tool: str, error: str | None = None, detail: str | None = None) -> dict:
    ts = NOW - timedelta(minutes=minutes_ago)
    return {
        "timestamp": ts.isoformat(timespec="seconds"),
        "tool": tool,
        "kwargs": {},
        "duration_ms": 12.0,
        "output_chars": 0 if error else 10,
        "cache_hit": False,
        "error": error,
        "error_detail": detail,
    }


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """진짜 ~/.telegramlens 를 건드리지 않게 metrics 폴더를 tmp 로 돌린다."""
    monkeypatch.setattr(_metrics, "data_dir", lambda: tmp_path)

    def write(records: list[dict]) -> None:
        folder = tmp_path / "logs"
        folder.mkdir(exist_ok=True)
        for rec in records:
            day = datetime.fromisoformat(rec["timestamp"]).strftime("%Y%m%d")
            with open(folder / f"metrics_{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return write


def _check(records=None):
    loaded = _metrics.load_metrics(hours=48, now=NOW) if records is None else records
    c = doctor.check_recent_tool_failures(records=loaded)
    return c, c.to_contract_dict("RECENT_TOOL_FAILURES")


def test_no_records(logs):
    c, d = _check()
    assert d["status"] == "ok"
    assert d["summary"] == "최근 이틀 동안 AI 앱이 TelegramLens를 쓴 기록이 없어요."
    assert d["critical"] is False
    assert d["action"] is None


def test_all_success(logs):
    logs([_rec(30, "telegram_trending"), _rec(10, "telegram_search"), _rec(5, "telegram_trending")])
    c, d = _check()
    assert d["status"] == "ok"
    assert d["summary"] == "최근 이틀 동안 조회 3번이 모두 정상이었어요."


def test_failure_then_same_tool_success_is_resolved(logs):
    logs([
        _rec(60, "telegram_trending", "ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
        _rec(50, "telegram_trending"),
        _rec(40, "telegram_search"),
    ])
    c, d = _check()
    assert d["status"] == "ok"
    assert d["summary"] == "최근 이틀 동안 조회 3번 중 1번이 실패했지만, 계속 실패하고 있지는 않아요."
    # 지원용 줄에는 남는다.
    assert any("telegram_trending: 실패 1번" in line and "분류 tls" in line for line in d["details"]["lines"])


def test_last_call_failed_is_warn(logs):
    logs([
        _rec(90, "telegram_trending"),
        _rec(30, "telegram_trending", "ReadTimeout", "The read operation timed out"),
        _rec(25, "telegram_trending", "ReadTimeout", "The read operation timed out"),
        _rec(20, "telegram_search", "FloodWaitError", "A wait of 300 seconds is required"),
        _rec(15, "telegram_messages", "ReadTimeout", ""),
        _rec(10, "telegram_messages", "ReadTimeout", ""),
    ])
    c, d = _check()
    assert d["status"] == "warn"
    assert d["summary"] == "최근 조회 중 아직 실패로 남아 있는 것이 3가지 있어요."
    # 대표 분류 = 막혀 있는 도구들 중 가장 많은 분류(timeout 2 > blocked 1)
    assert d["details"]["error_code"] == "RECENT_TOOL_FAILURES_TIMEOUT"
    assert d["action"].startswith("연결이 느려서")
    assert d["critical"] is False
    lines = d["details"]["lines"]
    assert any(line.startswith("telegram_trending: 실패 2번, 마지막 14:35, 분류 timeout, ReadTimeout:") for line in lines)
    assert len(lines) <= 8


def test_single_unclear_failure_is_not_warn_but_repeat_is(logs):
    """AI 앱이 인자를 한 번 잘못 넣은 호출로 카드가 이틀 내내 '주의'가 되면 안 된다.
    다시 불러도 또 실패하면 그때는 진짜 결함으로 본다."""
    logs([_rec(30, "telegram_timeline", "ValueError", "코드 999999 는 종목 사전에 없습니다.")])
    c, d = _check()
    assert d["status"] == "ok"

    logs([_rec(20, "telegram_timeline", "ValueError", "코드 999999 는 종목 사전에 없습니다.")])
    c, d = _check()
    assert d["status"] == "warn"
    assert d["details"]["error_code"] == "RECENT_TOOL_FAILURES_OTHER"


def test_cancelled_only_is_not_a_failure(logs):
    logs([_rec(30, "telegram_sync", "CancelledError", None), _rec(20, "telegram_sync", "CancelledError", None)])
    c, d = _check()
    assert d["status"] == "ok"
    assert d["summary"] == "최근 이틀 동안 조회 2번이 모두 정상이었어요."
    assert any("분류 cancelled" in line for line in d["details"]["lines"])


def test_cancel_after_failure_keeps_it_unresolved(logs):
    """취소는 성공이 아니다 — 앞선 실패를 풀린 것으로 바꾸지 않는다."""
    logs([
        _rec(30, "telegram_sync", "NotLoggedInError", "텔레그램 로그인이 필요해요."),
        _rec(20, "telegram_sync", "CancelledError", None),
    ])
    c, d = _check()
    assert d["status"] == "warn"


def test_records_older_than_48h_are_ignored(logs):
    logs([_rec(60 * 49, "telegram_trending", "ReadTimeout", ""), _rec(10, "telegram_trending")])
    c, d = _check()
    assert d["summary"] == "최근 이틀 동안 조회 1번이 모두 정상이었어요."


def test_detail_lines_are_capped_and_masked(logs):
    secret = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    # 원인 불명 실패는 되풀이돼야 '주의'다 — 도구마다 두 번씩 실패시킨다.
    logs([_rec(100 - i * 2 - j, f"tool_{i}", "RuntimeError", f"bad token {secret}") for i in range(12) for j in range(2)])
    c, d = _check()
    lines = d["details"]["lines"]
    tool_lines = [line for line in lines if ": 실패 " in line]
    assert len(tool_lines) == 8
    # 가장 최근 실패가 먼저(tool_11 이 1분 전 실패, 넷은 잘린다)
    assert tool_lines[0].startswith("tool_11:")
    assert all(secret not in line for line in lines)
    assert d["details"]["error_code"] == "RECENT_TOOL_FAILURES_OTHER"


def test_broken_lines_are_skipped(logs, tmp_path):
    logs([_rec(10, "telegram_trending")])
    with open(tmp_path / "logs" / f"metrics_{NOW:%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")
    c, d = _check()
    assert d["summary"] == "최근 이틀 동안 조회 1번이 모두 정상이었어요."


def test_load_metrics_does_not_create_folders(tmp_path, monkeypatch):
    """doctor 는 읽기 전용이다 — 기록이 없다고 폴더를 만들면 안 된다."""
    monkeypatch.setattr(_metrics, "data_dir", lambda: tmp_path / "home")
    assert _metrics.load_metrics(hours=48, now=NOW) == []
    assert not (tmp_path / "home").exists()
