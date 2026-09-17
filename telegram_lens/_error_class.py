"""조회 실패 원인 분류 — 세 Lens 동일 사본(StockLens·DartLens·TelegramLens).

`_result_meta.py`처럼 세 저장소에 같은 내용으로 둔다. 한 곳을 고치면 나머지 둘도
같이 고친다(파일 전체가 글자 그대로 같아야 한다). category 이름은 Manager 진단 화면과
error_code(예: `RECENT_TOOL_FAILURES_TLS`)의 계약이라 세 Lens가 문자 그대로 같아야 한다.
Lens 고유 예외 이름도 여기 한곳에 모은다 — 다른 Lens에서는 그 이름이 안 나올 뿐 해가 없다.

metrics JSONL 에 남는 것은 예외 객체가 아니라 `error`(예외 클래스 이름)와
`error_detail`(메시지) 문자열뿐이다. 그래서 분류도 그 두 문자열만 보고 한다 —
진단 중에 잡은 살아 있는 예외도(`classify_exception`) 같은 규칙을 타야 "최근 조회 실패"와
"지금 연결 확인"이 같은 원인을 같은 이름으로 부른다.
"""

from __future__ import annotations

import re

# 판정 순서이기도 하다. tls 가 connect 보다 앞인 이유: httpx 는 인증서 검증 실패를
# ConnectError 로 감싸서 올린다. 순서가 바뀌면 "보안 프로그램이 가로챈다"는 진짜
# 원인이 "연결 못 함"으로 뭉개진다(2026-08-13 문의가 정확히 이 경우였다).
CATEGORIES: tuple[str, ...] = (
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

_DNS_TYPES = {"gaierror"}
_DNS_TEXT = ("getaddrinfo", "name or service not known", "nodename nor servname", "no address associated")
# Windows 소켓 오류 번호. 종목코드·가격 안의 숫자와 헷갈리지 않게 앞뒤가 숫자가 아닐 때만.
_DNS_CODE = re.compile(r"(?<!\d)11001(?!\d)")

_TIMEOUT_TYPES = {"timeoutexception", "readtimeout", "connecttimeout", "pooltimeout", "writetimeout", "timeouterror"}
_TIMEOUT_TEXT = ("timed out",)

# TelegramLens(Telethon): FloodWaitError·SlowModeWaitError = 텔레그램이 일정 시간 요청을 막음.
_BLOCKED_TYPES = {"floodwaiterror", "slowmodewaiterror"}
# rate_limited: StockLens 증권사 클라이언트가 비밀 없이 올리는 상태값.
_BLOCKED_TEXT = re.compile(r"403 forbidden|429 too many|http[ =]?429|451 unavailable|http[ =]?451| 451 |rate_limited")

_AUTH_TYPES = {
    # TelegramLens(Telethon): 세션이 끊김·계정 비활성화. 할 일은 다시 로그인.
    "authkeyunregisterederror",
    "sessionrevokederror",
    "authkeyerror",
    "userdeactivatederror",
    # TelegramLens: 로그인 전·api_id/api_hash 없음. 할 일이 auth 와 같다([텔레그램 로그인]).
    "notloggedinerror",
    "nocredentialserror",
    # DartLens: DART 인증키가 아예 없을 때. 할 일이 auth 와 같다(인증키 넣기).
    "missingapikeyerror",
}
# credential_invalid 등: StockLens 증권사 클라이언트 상태값.
_AUTH_TEXT = re.compile(r"401 unauthorized|http[ =]?401|credential_invalid|authentication_failed|permission_denied")

# connectionerror: Telethon 은 텔레그램 서버 접속 실패를 내장 ConnectionError 로 올린다
# ("Connection to Telegram failed N time(s)").
_CONNECT_TYPES = {
    "connecterror",
    "connectionerror",
    "connectionrefusederror",
    "connectionreseterror",
    "connectionabortederror",
    "remoteprotocolerror",
}
_CONNECT_TEXT = ("connection refused", "connection reset")
_CONNECT_CODE = re.compile(r"(?<!\d)(10061|10054)(?!\d)")

_SCHEMA_TYPES = {"keyerror", "indexerror", "jsondecodeerror"}
_SCHEMA_TEXT = ("구조가 바뀌", "구조 변경", "expecting value", "source_parse_error")

# DART 는 오류를 HTTP 200 + status 코드로 준다. DartApiError 의 문자열은 "[012] 메시지".
_DART_STATUS = re.compile(r"^\s*\[(\d{3})\]")
_DART_BLOCKED = {"012", "020"}  # 012 IP 차단, 020 요청 제한
_DART_AUTH = {"010", "011"}  # 010 등록되지 않은 키, 011 사용할 수 없는 키


def classify_error(error_type: str | None, error_detail: str | None) -> str:
    """예외 클래스 이름과 메시지로 원인 분류 하나를 고른다. 모르면 "other"."""
    t = (error_type or "").strip().lower()
    d = (error_detail or "").strip()
    dl = d.lower()

    if "cancellederror" in t:
        return "cancelled"
    if t in _TLS_TYPES or t.startswith("ssl") or any(k in dl for k in _TLS_TEXT):
        return "tls"
    if t in _DNS_TYPES or any(k in dl for k in _DNS_TEXT) or _DNS_CODE.search(dl):
        return "dns"
    if t in _TIMEOUT_TYPES or "timeout" in t or any(k in dl for k in _TIMEOUT_TEXT):
        return "timeout"

    dart = _DART_STATUS.match(d) if t == "dartapierror" else None
    dart_status = dart.group(1) if dart else None

    if t in _BLOCKED_TYPES or dart_status in _DART_BLOCKED or _BLOCKED_TEXT.search(f" {dl} "):
        return "blocked"
    if t in _AUTH_TYPES or dart_status in _DART_AUTH or _AUTH_TEXT.search(dl):
        return "auth"
    if t in _CONNECT_TYPES or any(k in dl for k in _CONNECT_TEXT) or _CONNECT_CODE.search(dl):
        return "connect"
    # 각 Lens 의 파싱 실패 전용 예외(NaverParseError 등)는 이름이 ParseError 로 끝난다.
    if t in _SCHEMA_TYPES or t.endswith("parseerror") or any(k in dl for k in _SCHEMA_TEXT):
        return "schema"
    return "other"


def classify_exception(exc: BaseException) -> str:
    """살아 있는 예외를 분류한다. 감싸인 원인(__cause__/__context__)까지 본다.

    - tls 는 어느 겹에 있든 그것이 답이다. 보안 프로그램의 가로채기는 연결 오류나
      Lens 자체 예외 안에 싸여 오는 일이 많다.
    - 그 밖에는 바깥 겹부터 처음 분류가 나오는 것을 고른다. 증권사 클라이언트는 httpx
      오류를 비밀 없는 자체 예외로 바꿔 올리는데(`raise ... from None`), 바깥이 이미
      credential_invalid(auth)라고 말하면 안쪽 사정보다 그게 할 일에 맞다.
    """
    found: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(found) < 6:
        seen.add(id(cur))
        found.append(classify_error(type(cur).__name__, str(cur)))
        cur = cur.__cause__ or cur.__context__
    if "tls" in found:
        return "tls"
    return next((c for c in found if c != "other"), "other")


# ---------- "최근 조회 실패"에서 아직 실패로 남았다고 볼지 ----------
#
# 원인이 분명하고 저절로 풀리지 않는 분류는 한 번만 남아도 '주의'다.
# 나머지(원인 불명·시간 초과·연결 실패)는 같은 도구가 끝에서 연달아 실패했을 때만 본다.
# AI 앱이 인자를 잘못 넣은 호출이나 한 번 삐끗한 타임아웃 하나로 카드가 이틀 내내
# '주의'로 남으면 진짜 경고까지 안 믿게 된다. 입력 오류만 골라 빼려 해도 ValueError 는
# 입력 검증과 내부 결함이 같은 이름이라 못 가른다 — 되풀이 여부가 더 믿을 만한 신호다.
_STICKY = {"tls", "dns", "blocked", "auth", "schema"}
_REPEAT_TO_WARN = 2


def still_failing(trailing: list[str]) -> bool:
    """한 도구의 마지막 성공 뒤로 이어진 실패 분류들(오래된 → 최근, 취소 제외)."""
    if not trailing:
        return False
    return trailing[-1] in _STICKY or len(trailing) >= _REPEAT_TO_WARN


# ---------- LeetKit Manager 화면 문구 (진단 action) ----------
#
# 상황 한 문장 → 할 일 하나 → 그래도 같으면 [지원 문의]. 버튼 이름은 Manager 화면 글자
# 그대로다. Claude 답변 안에 넣을 때는 각 Lens가 "LeetKit Manager의 …" 앞말로 바꿔 쓴다.
_ACTIONS: dict[str, str] = {
    "tls": (
        "백신이나 회사 보안 프로그램이 연결을 가로채고 있을 수 있어요. "
        "{lens} 카드의 [업데이트]를 확인하고, 그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "timeout": (
        "연결이 느려서 시간 안에 답을 못 받았어요. 잠시 뒤 [진단]을 다시 눌러주세요. "
        "그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "dns": (
        "인터넷 주소를 찾지 못했어요. 인터넷 연결을 확인한 뒤 [진단]을 다시 눌러주세요. "
        "그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "connect": (
        "데이터 서버에 연결하지 못했어요. 잠시 뒤 [진단]을 다시 눌러주세요. "
        "그래도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "blocked": "데이터 제공처가 요청을 막았어요. 잠시 뒤 다시 해보고, 그래도 같으면 상단 [지원 문의]를 눌러주세요.",
    "schema": (
        "데이터 제공처 화면 구조가 바뀌었을 수 있어요. {lens} 카드의 [업데이트]를 확인하고, "
        "업데이트 후에도 같으면 상단 [지원 문의]를 눌러주세요."
    ),
    "other": "{lens} 카드의 [업데이트]를 확인해 주세요. 업데이트 후에도 같으면 상단 [지원 문의]를 눌러주세요.",
}

# 인증 실패는 Lens 마다 다시 넣을 것이 다르다.
_AUTH_ACTIONS: dict[str, str] = {
    "StockLens": "StockLens 카드의 [증권사 연결]에서 연결을 다시 확인해 주세요.",
    "DartLens": "DartLens 카드의 [활성화]에서 DART 인증키를 다시 넣어주세요.",
    "TelegramLens": "TelegramLens 카드의 [텔레그램 로그인]을 다시 눌러주세요.",
}


def action_for(category: str, lens: str) -> str | None:
    """분류별 Manager 화면 할 일 한 줄. cancelled 는 실패로 안 세므로 None, 모르는 분류는 other."""
    if category == "cancelled":
        return None
    if category == "auth":
        return _AUTH_ACTIONS.get(lens) or _ACTIONS["other"].format(lens=lens)
    return (_ACTIONS.get(category) or _ACTIONS["other"]).format(lens=lens)
