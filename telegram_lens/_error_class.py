"""조회 실패 원인 분류 — 세 Lens 동일 사본(StockLens·DartLens·TelegramLens).

`_result_meta.py`처럼 세 저장소에 같은 내용으로 둔다. 한 곳을 고치면 나머지 둘도
같이 고친다. category 이름은 Manager 진단 화면과 error_code(예:
`RECENT_TOOL_FAILURES_TLS`)의 계약이라 세 Lens가 문자 그대로 같아야 한다.

metrics JSONL 에 남는 것은 예외 객체가 아니라 `error`(예외 클래스 이름)와
`error_detail`(메시지) 문자열뿐이다. 그래서 분류도 그 두 문자열만 보고 한다 —
살아 있는 예외를 분류할 때도(`classify_exception`) 같은 규칙을 탄다.
"""

from __future__ import annotations

import re

# 판정 순서이기도 하다. tls 가 connect 보다 앞인 이유: httpx 는 인증서 검증 실패를
# ConnectError 로 감싸서 올린다. 순서가 바뀌면 "보안 프로그램이 가로챈다"는 진짜
# 원인이 "연결 못 함"으로 뭉개진다(2026-08-13 문의가 정확히 이 경우였다).
CATEGORIES = (
    "cancelled",
    "tls",
    "dns",
    "timeout",
    "blocked",
    "auth",
    "connect",
    "schema",
    "other",
)

_TLS_TYPES = {"sslerror", "sslcertverificationerror"}
_TLS_TEXT = ("certificate_verify_failed", "certificate verify", "self signed", "self-signed")

_DNS_TEXT = ("getaddrinfo", "name or service not known", "nodename nor servname", "no address associated")
_DNS_RE = re.compile(r"\b11001\b")

_TIMEOUT_TEXT = ("timed out",)

# TelegramLens(Telethon): FloodWaitError·SlowModeWaitError = 텔레그램이 일정 시간 요청을 막음.
_BLOCKED_TYPES = {"floodwaiterror", "slowmodewaiterror"}
_BLOCKED_TEXT = ("403 forbidden", "429 too many", " 451 ", "451 unavailable")

_AUTH_TYPES = {
    "authkeyunregisterederror",
    "sessionrevokederror",
    "authkeyerror",
    # TelegramLens(Telethon): 계정이 비활성화됨. 할 일은 다시 로그인.
    "userdeactivatederror",
    # TelegramLens: 로그인 전·api_id/api_hash 없음. 할 일이 auth 와 같다([텔레그램 로그인]).
    "notloggedinerror",
    "nocredentialserror",
    # DartLens: DART 인증키가 아예 없을 때. 할 일이 auth 와 같다(인증키 넣기).
    "missingapikeyerror",
}
_AUTH_TEXT = ("401 unauthorized",)

# connectionerror: Telethon 은 텔레그램 서버 접속 실패를 내장 ConnectionError 로 올린다
# ("Connection to Telegram failed N time(s)").
_CONNECT_TYPES = {
    "connecterror",
    "connectionerror",
    "connectionrefusederror",
    "connectionreseterror",
    "remoteprotocolerror",
}
_CONNECT_TEXT = ("connection refused", "connection reset")
_CONNECT_RE = re.compile(r"\b(10061|10054)\b")

_SCHEMA_TYPES = {"keyerror", "indexerror", "jsondecodeerror"}
_SCHEMA_TEXT = ("구조가 바뀌", "expecting value")

# DART 는 오류를 HTTP 200 + status 코드로 준다. DartApiError 의 문자열은 "[012] 메시지".
_DART_STATUS_RE = re.compile(r"^\s*\[(\d{3})\]")
_DART_BLOCKED = {"012", "020"}  # 012 IP 차단, 020 요청 제한
_DART_AUTH = {"010", "011"}  # 010 등록되지 않은 키, 011 사용할 수 없는 키


def classify_error(error_type: str | None, error_detail: str | None) -> str:
    """예외 클래스 이름과 메시지로 원인 분류 하나를 고른다. 모르면 "other"."""
    etype = (error_type or "").strip().lower()
    detail = (error_detail or "").lower()

    if etype == "cancellederror":
        return "cancelled"
    if etype in _TLS_TYPES or any(t in detail for t in _TLS_TEXT):
        return "tls"
    if any(t in detail for t in _DNS_TEXT) or _DNS_RE.search(detail):
        return "dns"
    if "timeout" in etype or any(t in detail for t in _TIMEOUT_TEXT):
        return "timeout"

    dart_status = None
    if etype == "dartapierror":
        m = _DART_STATUS_RE.match(detail)
        dart_status = m.group(1) if m else None

    if etype in _BLOCKED_TYPES or dart_status in _DART_BLOCKED or any(t in detail for t in _BLOCKED_TEXT):
        return "blocked"
    if etype in _AUTH_TYPES or dart_status in _DART_AUTH or any(t in detail for t in _AUTH_TEXT):
        return "auth"
    if etype in _CONNECT_TYPES or any(t in detail for t in _CONNECT_TEXT) or _CONNECT_RE.search(detail):
        return "connect"
    if etype in _SCHEMA_TYPES or any(t in detail for t in _SCHEMA_TEXT):
        return "schema"
    return "other"


def classify_exception(exc: BaseException) -> str:
    """살아 있는 예외를 분류한다. 감싸인 원인(__cause__/__context__)까지 본다 —
    SSL 오류가 다른 예외에 싸여 올라와도 tls 로 잡혀야 한다."""
    names: list[str] = []
    texts: list[str] = []
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(names) < 5:
        seen.add(id(cur))
        names.append(type(cur).__name__)
        texts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    joined = " | ".join(texts)
    # 겉과 속 예외 이름을 각각 분류하고 판정 순서가 가장 앞선 것을 고른다 —
    # ConnectError 안에 SSL 오류가 들었으면 connect 가 아니라 tls 다.
    found = [classify_error(name, joined) for name in names]
    return min(found, key=CATEGORIES.index)


# Manager 화면(진단 action)에 쓰는 할 일. 앞말은 Manager 안 기준이다 — Claude 답변
# 안에 넣을 때는 각 Lens가 "LeetKit Manager의 …" 앞말로 바꿔 쓴다.
_AUTH_ACTION = {
    "StockLens": "StockLens 카드의 [증권사 연결]에서 연결을 다시 확인해 주세요.",
    "DartLens": "DartLens 카드의 [활성화]에서 DART 인증키를 다시 넣어주세요.",
    "TelegramLens": "TelegramLens 카드의 [텔레그램 로그인]을 다시 눌러주세요.",
}


def action_for(category: str, lens: str) -> str | None:
    """분류별 Manager 화면 할 일 한 줄. cancelled 는 실패로 안 세므로 None."""
    if category == "cancelled":
        return None
    if category == "tls":
        return (
            f"백신이나 회사 보안 프로그램이 연결을 가로채고 있을 수 있어요. {lens} 카드의 "
            "[업데이트]를 확인하고, 그래도 같으면 상단 [지원 문의]를 눌러주세요."
        )
    if category == "timeout":
        return (
            "연결이 느려서 시간 안에 답을 못 받았어요. 잠시 뒤 [진단]을 다시 눌러주세요. "
            "그래도 같으면 상단 [지원 문의]를 눌러주세요."
        )
    if category == "dns":
        return (
            "인터넷 주소를 찾지 못했어요. 인터넷 연결을 확인한 뒤 [진단]을 다시 눌러주세요. "
            "그래도 같으면 상단 [지원 문의]를 눌러주세요."
        )
    if category == "connect":
        return (
            "데이터 서버에 연결하지 못했어요. 잠시 뒤 [진단]을 다시 눌러주세요. "
            "그래도 같으면 상단 [지원 문의]를 눌러주세요."
        )
    if category == "blocked":
        return "데이터 제공처가 요청을 막았어요. 잠시 뒤 다시 해보고, 그래도 같으면 상단 [지원 문의]를 눌러주세요."
    if category == "auth":
        return _AUTH_ACTION.get(lens) or "상단 [지원 문의]를 눌러주세요."
    if category == "schema":
        return (
            f"데이터 제공처 화면 구조가 바뀌었을 수 있어요. {lens} 카드의 [업데이트]를 확인하고, "
            "업데이트 후에도 같으면 상단 [지원 문의]를 눌러주세요."
        )
    return f"{lens} 카드의 [업데이트]를 확인해 주세요. 업데이트 후에도 같으면 상단 [지원 문의]를 눌러주세요."
