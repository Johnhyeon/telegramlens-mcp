"""도구 입력 스키마를 중계 클라이언트가 망가뜨리지 않는 모양으로 내보낸다.
StockLens · DartLens · TelegramLens 공통.

세 Lens는 별도 PyPI 패키지라 서로 import하지 않는다. 이 파일은 세 곳에
**같은 내용으로 복사**된다. 고칠 때는 세 곳을 함께 고친다.

## 무엇이 깨졌나 (2026-09-16 실측)

Claude 데스크탑 2.110.0 / Claude Code 2.1.271 에서 데스크탑 앱이 로컬 MCP 서버를
Code 세션에 중계하면, 도구 입력 스키마가 JSON Schema → zod 로 한 번 바뀐 뒤
검증된다. 그 경로에서 두 가지가 샌다.

1. `default` 가 붙은 선택 인자를 생략하면 `expected nonoptional` 로 거부된다.
   타입·기본값과 무관하다(True, "auto", None 모두). 변환기는 default 를
   prefault 로 바꾸는데, 검증 쪽 zod 가 그 필드를 선택 인자로 보지 않는다.
   `default` 키가 없는 선택 인자는 통과한다.
2. `anyOf: [{type: X}, {type: null}]` 은 타입 없는 `{}` 로 바뀐다. 모델은 타입을
   못 보고 `bgn_de` 에 20260916(숫자)을, `corp_code` 에 "null"(문자열)을 보냈다.

같은 버전이라도 서버에 직접 붙은 세션은 멀쩡했고, 2.1.270 까지 기록된 호출에서는
생략 인자가 한 번도 막히지 않았다. 클라이언트 쪽 회귀지만 고객의 Claude 는 첫
호출에서 실패하고 재시도한다. 그래서 서버가 두 경로 모두에서 통과하는 모양으로
내보낸다.

## 무엇을 바꾸나

- 선택 인자(required 에 없는 것)에서 `default` 를 뺀다. JSON Schema 의 default 는
  주석일 뿐이고, 생략 시 값은 파이썬 시그니처가 채운다 — 동작은 같다.
  기본값 안내는 도구 설명(docstring)에 있다.
- `X | None` 의 anyOf 를 X 하나로 접는다. 명시적 null 은 여전히 받는다 —
  FastMCP 는 JSON Schema 가 아니라 파이썬 시그니처(pydantic)로 인자를 검증한다.
- 문자열 인자에 정수가 오면 문자열로 바꿔서 넘긴다(20260916 → "20260916").
  앞자리 0 은 채우지 않는다. 126380 을 corp_code "00126380" 으로 추측하면 다른
  회사를 가리킬 수 있다. 그런 값은 각 도구의 형식 검증이 그대로 거절한다.
"""

from __future__ import annotations

import copy
from typing import Any

from mcp.server.fastmcp import FastMCP


def _is_null_branch(branch: Any) -> bool:
    return isinstance(branch, dict) and branch.get("type") == "null"


def _collapse_nullable(prop: dict) -> dict:
    branches = prop.get("anyOf")
    if not isinstance(branches, list) or not any(_is_null_branch(b) for b in branches):
        return prop
    rest = [b for b in branches if not _is_null_branch(b)]
    outer = {k: v for k, v in prop.items() if k != "anyOf"}
    if len(rest) == 1 and isinstance(rest[0], dict):
        return {**rest[0], **outer}
    return {**outer, "anyOf": rest}


def client_safe_input_schema(schema: Any) -> Any:
    """목록에 내보낼 입력 스키마. 원본(도구 등록 정보)은 건드리지 않는다."""
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return schema
    out = copy.deepcopy(schema)
    required = set(out.get("required") or [])
    props = out["properties"]
    for name, prop in list(props.items()):
        if not isinstance(prop, dict):
            continue
        prop = _collapse_nullable(prop)
        if name not in required:
            prop.pop("default", None)
        props[name] = prop
    return out


def _accepts(prop: Any, json_type: str) -> bool:
    if not isinstance(prop, dict):
        return False
    declared = prop.get("type")
    if declared == json_type or (isinstance(declared, list) and json_type in declared):
        return True
    return any(_accepts(b, json_type) for b in prop.get("anyOf") or [])


def coerce_integer_strings(schema: Any, arguments: dict) -> dict:
    """문자열만 받는 인자에 온 정수를 문자열로. 바꿀 게 없으면 원본을 그대로 돌려준다."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return arguments
    fixed = None
    for name, value in arguments.items():
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        prop = props.get(name)
        if not _accepts(prop, "string"):
            continue
        if _accepts(prop, "integer") or _accepts(prop, "number"):
            continue
        if fixed is None:
            fixed = dict(arguments)
        fixed[name] = str(value)
    return arguments if fixed is None else fixed


# ## 도구 주석(annotations) — Codex·ChatGPT 앱의 승인 프롬프트 (2026-09-17 실측)
#
# Codex CLI 0.147 / ChatGPT 앱은 같은 `~/.codex/config.toml` 을 읽고, MCP 도구를
# 부르기 전에 승인을 묻는다(`default_tools_approval_mode`). "writes" 모드는 **읽기
# 전용으로 표시되지 않은** 도구만 묻는데, 우리 도구는 주석이 하나도 없어 전부
# "쓰기일지도 모르는 도구"로 보였다. 그래서 첫 호출이 승인 대기에 걸리고, 비대화
# 실행(codex exec)에서는 곧바로 "user cancelled MCP tool call" 로 실패했다
# (search_stock·search·stocklens_status 네 번 연속). 고객이 말한 "GPT 에선 한 번은
# 꼭 실패"의 모양이 이것이다.
#
# 서버가 할 수 있는 몫: 읽기 도구에 readOnlyHint=True 를 붙인다. 쓰기 도구(파일
# 저장·관심종목 편집·수집)는 각 Lens 가 `write_tools` 로 넘긴다. 설치기는 별도로
# `default_tools_approval_mode` 를 config 에 적는다(setup_claude.py).
# openWorldHint=True 는 "외부(인터넷)와 통신한다"는 뜻이고 사실이다.

_READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False,
                          "idempotentHint": True, "openWorldHint": True}
_WRITE_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": False,
                      "idempotentHint": False, "openWorldHint": True}


class LensFastMCP(FastMCP):
    """목록 스키마와 호출 인자에 위 규칙을 적용하고, 도구마다 읽기/쓰기 주석을 붙인 FastMCP.

    write_tools: 파일을 만들거나 사용자 상태를 바꾸는 도구 이름들. 나머지는 읽기 전용으로
    표시한다. 이름이 등록된 도구에 없으면 등록 시점에 바로 실패한다 — 오타로 쓰기 도구가
    조용히 읽기 전용이 되면 안 된다(`check_write_tools`).
    """

    def __init__(self, *args, write_tools=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._lens_write_tools = frozenset(write_tools)

    def check_write_tools(self) -> None:
        """등록이 끝난 뒤(모든 @tool 다음) 한 번 부른다."""
        registered = {t.name for t in self._tool_manager.list_tools()}
        unknown = sorted(self._lens_write_tools - registered)
        if unknown:
            raise RuntimeError(f"write_tools 에 없는 도구 이름: {unknown}")

    async def list_tools(self):
        from mcp.types import ToolAnnotations

        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema = client_safe_input_schema(tool.inputSchema)
            if tool.annotations is None:
                hints = (_WRITE_ANNOTATIONS if tool.name in self._lens_write_tools
                         else _READ_ONLY_ANNOTATIONS)
                tool.annotations = ToolAnnotations(**hints)
        return tools

    async def call_tool(self, name, arguments, *args, **kwargs):
        tool = self._tool_manager.get_tool(name)
        if tool is not None and isinstance(arguments, dict):
            arguments = coerce_integer_strings(tool.parameters, arguments)
        return await super().call_tool(name, arguments, *args, **kwargs)
