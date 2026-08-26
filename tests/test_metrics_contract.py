"""도구 호출 기록의 필드 계약 — 세 Lens 가 같은 모양이어야 한다.

지원 번들은 StockLens·DartLens·TelegramLens 로그를 한 zip 에 담고, 받는 쪽은 그걸
나란히 놓고 훑는다. 그래서 필드 이름이 셋 다 같아야 한다. 하나가 조용히 이름을
바꾸거나 빠뜨려도 아무 일도 안 일어나는데, 그게 필요해지는 순간은 고객이 "안 된다"고
한 뒤다 — 그때 가서야 없는 걸 알게 된다.

실제로 어긋나 있었다(2026-08-16 확인): StockLens 만 output_tokens 를 더 남기고 있었고
아무도 몰랐다. 그건 get_metrics_summary 도구가 쓰는 정당한 확장이라 계약을 '최소
집합'으로 정의했다 — 더 넣는 건 되고, 빠지는 건 안 된다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from telegram_lens import _metrics

# ── 세 Lens 공통 계약 ────────────────────────────────────────────────────
# 이 목록을 고치면 세 저장소를 함께 고쳐야 한다.
# tools/check_metrics_contract.py 로 네 저장소가 같은 내용인지 확인할 수 있다.
METRICS_CONTRACT_FIELDS = {
    "timestamp",     # 호출 시각 (로컬 ISO, 앞 10자가 날짜)
    "tool",          # 도구 이름
    "kwargs",        # 인자 (민감값은 각 Lens 가 가림)
    "duration_ms",   # 소요 시간
    "output_chars",  # 응답 길이
    "cache_hit",     # 캐시 추정
    "error",         # 예외 타입 이름 (정상이면 None)
    "error_detail",  # 예외 메시지 (URL 쿼리·개인정보는 가린 뒤)
}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))
    monkeypatch.setattr(_metrics, "data_dir", lambda: tmp_path)
    return tmp_path


def _last_record() -> dict:
    lines = _metrics.get_metrics_file().read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_success_record_has_every_contract_field():
    @_metrics.track_metrics("probe")
    async def tool(hours=24):
        return "결과"

    asyncio.run(tool())
    assert METRICS_CONTRACT_FIELDS <= set(_last_record())


def test_failure_record_has_every_contract_field():
    """실패했을 때도 같은 모양이어야 한다 — 정작 들여다보는 건 실패한 줄이다."""

    @_metrics.track_metrics("probe")
    async def tool():
        raise ConnectionError("연결 거부")

    with pytest.raises(ConnectionError):
        asyncio.run(tool())
    rec = _last_record()
    assert METRICS_CONTRACT_FIELDS <= set(rec)
    assert rec["error"] == "ConnectionError"


def test_timestamp_starts_with_a_date():
    """앞 10자를 날짜로 잘라 쓰는 곳이 여럿이다(매니저의 체험 사용량 집계 등)."""
    import re

    @_metrics.track_metrics("probe")
    async def tool():
        return "x"

    asyncio.run(tool())
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", _last_record()["timestamp"])


def test_error_is_none_when_the_call_succeeds():
    """실패를 성공으로 기록하면 로그가 있으나 마나다."""

    @_metrics.track_metrics("probe")
    async def tool():
        return "x"

    asyncio.run(tool())
    assert _last_record()["error"] is None


# ── 커버리지 한계 카운터 (v3) ────────────────────────────────────────────
# 도구가 요청보다 적게 돌려주는 일이 얼마나 자주 일어나는지 세지 않으면, 계약을
# 만들어 놓고도 "그래서 실제로 얼마나 잘리나"에 답할 수 없다. 라벨은 고정 집합만
# 받는다 - 종목코드·티커·검색어가 라벨로 들어가면 시계열 카디널리티가 터지고,
# 지원 번들로 나가는 로그에 고객의 질의가 그대로 남는다.

COUNTER_CONTRACT = {
    "lens_coverage_truncated_total": {"lens", "tool", "reason"},
    "lens_incomplete_bar_total": {"tool", "timeframe"},
    "lens_mixed_period_total": {"tool"},
    "lens_unknown_adjustment_total": {"tool"},
}


def _last_counter() -> dict:
    lines = _metrics.get_counters_file().read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_counter_names_and_labels_are_exactly_the_contract():
    assert set(_metrics.COUNTER_LABELS) == set(COUNTER_CONTRACT)
    for name, labels in COUNTER_CONTRACT.items():
        assert set(_metrics.COUNTER_LABELS[name]) == labels


def test_counting_writes_a_record_with_only_allowed_labels():
    _metrics.count_limitation(
        "lens_incomplete_bar_total", tool="probe", timeframe="week"
    )
    rec = _last_counter()
    assert rec["metric"] == "lens_incomplete_bar_total"
    assert rec["labels"] == {"tool": "probe", "timeframe": "week"}
    assert rec["value"] == 1


def test_user_input_cannot_become_a_label():
    """종목코드·티커·검색어가 라벨에 남으면 카디널리티가 터지고 질의가 로그에 남는다."""
    for bad in (
        {"code": "005930"},
        {"ticker": "AAPL"},
        {"query": "삼성전자 배당"},
        {"corp_code": "00126380"},
        {"keyword": "조기상환"},
    ):
        with pytest.raises(ValueError):
            _metrics.count_limitation("lens_mixed_period_total", tool="probe", **bad)


def test_missing_required_label_is_rejected():
    with pytest.raises(ValueError):
        _metrics.count_limitation("lens_coverage_truncated_total", tool="probe")


def test_unknown_counter_name_is_rejected():
    with pytest.raises(ValueError):
        _metrics.count_limitation("lens_something_total", tool="probe")


def test_non_string_label_value_is_rejected():
    with pytest.raises(ValueError):
        _metrics.count_limitation("lens_mixed_period_total", tool=object())


def test_counters_do_not_pollute_the_call_record_file():
    """도구 호출 기록 형식을 건드리면 지원 번들 파서가 조용히 깨진다."""
    assert _metrics.get_counters_file() != _metrics.get_metrics_file()

    @_metrics.track_metrics("probe")
    async def tool():
        return "x"

    asyncio.run(tool())
    _metrics.count_limitation("lens_mixed_period_total", tool="probe")
    call_rec = json.loads(
        _metrics.get_metrics_file().read_text(encoding="utf-8").splitlines()[-1]
    )
    assert METRICS_CONTRACT_FIELDS <= set(call_rec)
    assert "metric" not in call_rec


def test_running_tool_name_is_available_to_counters():
    """라벨을 호출부까지 인자로 실어 나르지 않기 위해 실행 중 도구 이름을 노출한다."""
    seen = {}

    @_metrics.track_metrics("probe")
    async def tool():
        seen["tool"] = _metrics.current_tool()
        return "x"

    assert _metrics.current_tool() is None
    asyncio.run(tool())
    assert seen["tool"] == "probe"
    assert _metrics.current_tool() is None
