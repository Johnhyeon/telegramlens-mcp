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

        return wrapper

    return decorator
