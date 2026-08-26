"""MCP 도구 호출 메트릭 (JSONL).

저장 위치: ~/.telegramlens/logs/metrics_YYYYMMDD.jsonl  (config.data_dir 기준)

**왜 뒤늦게 붙였나.** 세 Lens 중 여기만 이 파일이 없었다. 2026-08-13 해외 고객 장애를
쫓을 때, StockLens·DartLens 는 로그의 `error_detail` 한 줄이 원인(TLS 가로채기)을
알려줬는데 TelegramLens 만 깜깜했다 — "이 Lens는 되는 것 같다"는 인상이 근거 없는
추측이었는지 확인할 방법 자체가 없었다. 다음 장애 때 같은 자리에서 멈추지 않으려고 만든다.

**스키마는 StockLens·DartLens 와 반드시 같아야 한다.** 지원 번들에 셋을 나란히 담고
한 번에 훑기 때문이다(timestamp/tool/kwargs/duration_ms/output_chars/cache_hit/
error/error_detail). 필드 이름을 여기서 임의로 바꾸면 그 대조가 깨진다.
"""

from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram_lens.config import data_dir

# stocklens/dartlens `_metrics.py` 와 같은 규칙이어야 한다 — 세 로그를 나란히 놓고 읽는다.
_QUERY_RE = re.compile(r"\?[^\s'\"]*")
_DETAIL_MAX = 200

# 이 Lens에서만 특히 조심할 것: 전화번호·인증코드·세션 문자열이 예외 메시지에 실릴 수
# 있다. 이 파일은 지원 번들에 담겨 고객이 메일로 내보내므로, 숫자 뭉치는 지운다.
_PHONE_RE = re.compile(r"\+?\d[\d\-\s]{7,}\d")


def _error_detail(exc: BaseException) -> str | None:
    """예외 메시지를 로그에 담을 수 있는 형태로. 메시지가 없으면 None."""
    msg = str(exc).strip()
    if not msg:
        return None
    msg = _QUERY_RE.sub("?…", msg)
    msg = _PHONE_RE.sub("<번호>", msg)
    return msg[:_DETAIL_MAX]


def get_metrics_dir() -> Path:
    folder = data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def get_metrics_file() -> Path:
    return get_metrics_dir() / f"metrics_{datetime.now():%Y%m%d}.jsonl"


# 값이 길거나 민감할 수 있는 인자는 이름만 남기고 값은 버린다.
_DROP_KEYS = {"phone", "code", "password", "session", "api_hash", "api_id", "token"}
_VALUE_MAX = 80


def _sanitize_kwargs(kwargs: dict) -> dict:
    out: dict = {}
    for k, v in kwargs.items():
        if k in _DROP_KEYS:
            out[k] = "<가림>"
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            s = str(v)
            out[k] = s if len(s) <= _VALUE_MAX else s[:_VALUE_MAX] + "…"
        elif isinstance(v, (list, tuple)):
            out[k] = f"<{type(v).__name__} {len(v)}개>"
        else:
            out[k] = f"<{type(v).__name__}>"
    return out


def track_metrics(tool_name: str) -> Callable:
    def decorator(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            token = _current_tool.set(tool_name)
            start = time.monotonic()
            error_type: str | None = None
            error_detail: str | None = None
            result_text = ""
            try:
                result = await func(*args, **kwargs)
                if result is not None:
                    result_text = str(result)
                return result
            # BaseException 까지 잡는 이유: asyncio.CancelledError 는 Exception 이 아니라
            # BaseException 이다. 클라이언트가 느린 호출을 취소하면 error=null,
            # output_chars=0 으로 기록돼 **성공한 것처럼** 보인다. 수집·동기화는 오래
            # 걸릴 수 있어 실제로 취소될 여지가 있다. 기록만 하고 그대로 다시 올린다.
            except BaseException as e:
                error_type = type(e).__name__
                error_detail = _error_detail(e)
                raise
            finally:
                duration_ms = round((time.monotonic() - start) * 1000, 1)
                try:
                    record = {
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "tool": tool_name,
                        "kwargs": _sanitize_kwargs(kwargs),
                        "duration_ms": duration_ms,
                        "output_chars": len(result_text),
                        "cache_hit": duration_ms < 10.0,
                        "error": error_type,
                        "error_detail": error_detail,
                    }
                    with open(get_metrics_file(), "a", encoding="utf-8") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception:
                    pass  # 기록 실패가 도구를 막으면 안 된다
                # 도구 밖에서 current_tool() 이 남아 있지 않게 되돌린다.
                _current_tool.reset(token)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 커버리지 한계 카운터 (메타 규약 v3)
# ---------------------------------------------------------------------------
# 도구가 요청보다 적게 돌려주는 일이 얼마나 자주 일어나는지 세지 않으면, 계약을
# 만들어 놓고도 "그래서 실제로 얼마나 잘리나"에 답할 수 없다.
#
# 라벨은 고정 집합만 받는다. 종목코드·티커·검색어를 라벨로 받으면 두 가지가
# 동시에 깨진다: 시계열 카디널리티가 종목 수만큼 늘어나고, 지원 번들로 나가는
# 로그에 고객이 무엇을 조회했는지가 그대로 남는다.

COUNTER_LABELS: dict[str, tuple[str, ...]] = {
    "lens_coverage_truncated_total": ("lens", "tool", "reason"),
    "lens_incomplete_bar_total": ("tool", "timeframe"),
    "lens_mixed_period_total": ("tool",),
    "lens_unknown_adjustment_total": ("tool",),
}

_LABEL_VALUE_MAX = 40

# 실행 중인 도구 이름. 카운터 라벨 하나 때문에 도구 이름을 40곳 호출부까지
# 인자로 실어 나르지 않으려고 여기 둔다.
_current_tool: ContextVar = ContextVar("lens_current_tool", default=None)


def current_tool():
    """지금 실행 중인 MCP 도구 이름. 도구 밖에서는 None."""
    return _current_tool.get()


def get_counters_file() -> Path:
    """오늘 날짜의 카운터 파일. 도구 호출 기록과 **다른 파일**이다.

    한 파일에 섞으면 기존 파서(load_metrics 등)가 모양이 다른 줄을 만나 조용히
    어긋난다. 지원 번들에는 같은 폴더째로 담기므로 나눠도 함께 나간다.
    """
    date_str = datetime.now().strftime("%Y%m%d")
    return get_metrics_dir() / f"counters_{date_str}.jsonl"


def count_limitation(metric: str, **labels) -> None:
    """커버리지 한계를 1 센다. 허용 라벨 밖의 값은 받지 않는다.

    라벨이 틀리면 조용히 넘어가지 않고 ValueError 로 올린다. 라벨은 전부 코드에
    박힌 상수라, 틀렸다면 그건 버그이지 사용자 입력이 아니다. 파일 쓰기 실패만
    삼킨다 - 메트릭 때문에 도구 호출이 죽으면 안 된다.
    """
    allowed = COUNTER_LABELS.get(metric)
    if allowed is None:
        raise ValueError(f"정의되지 않은 카운터: {metric!r} (허용: {sorted(COUNTER_LABELS)})")
    if set(labels) != set(allowed):
        raise ValueError(
            f"{metric} 의 라벨은 정확히 {sorted(allowed)} 여야 합니다 (받음: {sorted(labels)})"
        )
    for key, value in labels.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"라벨 {key} 는 비어 있지 않은 문자열이어야 합니다 (받음: {value!r})")
        if len(value) > _LABEL_VALUE_MAX:
            raise ValueError(f"라벨 {key} 가 너무 깁니다({len(value)}자). 자유 문자열은 라벨이 아닙니다.")

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "metric": metric,
        "labels": {k: labels[k] for k in allowed},
        "value": 1,
    }
    try:
        with open(get_counters_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
