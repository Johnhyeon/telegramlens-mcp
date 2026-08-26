"""LeetKit 결과 메타 봉투 — 규약 v1. StockLens · DartLens · TelegramLens 공통.

세 Lens는 별도 PyPI 패키지라 서로 import하지 않는다. 이 파일은 세 곳에
**같은 내용으로 복사**되며 `META_VERSION`으로 규약을 맞춘다. 필드를 바꿀 때는
세 곳을 함께 고치고 버전을 올린다. 메타가 아예 없는 구버전 Lens와 섞여 돌 수
있으므로, 읽는 쪽은 봉투 부재를 에러가 아니라 "기준일 미상"으로 다뤄야 한다.

## 왜 as_of와 data_as_of를 나누는가

    as_of      = 조회 시각      ("내가 언제 물어봤나")
    data_as_of = 데이터 기준일  ("이 숫자가 언제 것인가")

토요일에 현재가를 조회하면 as_of는 토요일, data_as_of는 직전 거래일이다.
예전에는 as_of 하나뿐이라 금요일 종가가 토요일 시세로 읽혔다. 두 값이 붙어
있으면 그런 오독이 구조적으로 막힌다.

## data_basis — 이 숫자가 '무엇으로서' 확정됐는가

    realtime         장중 체결 스냅샷 (아직 종가 아님)
    last_close       최근 거래일 확정치
    in_progress_bar  장중 미완성 봉 — 지표·크로스 판정이 마감 때 뒤집힐 수 있음
    filing           공시 접수 기준
    aggregate        수집 구간 집계 (버즈 등)

## coverage - 요청한 범위와 실제로 돌려준 범위 (v3)

    requested  사용자가 요청한 범위      {"unit": "day", "value": 60}
    effective  도구가 실제로 적용한 범위  {"unit": "day", "value": 20}

도구는 상한·페이지·원천 한계 때문에 요청보다 적게 돌려주는 일이 잦은데, 지금까지
그 사실이 응답 어디에도 남지 않았다. 60일을 물어보고 20일치를 받은 사람은 그걸
60일 분석으로 읽는다. 두 값을 나란히 실으면 그 오독이 구조적으로 막힌다.

`coverage_complete=false`인데 `data_completeness=complete`인 조합은 금지다.
"잘라놓고 전부라고 말하기"가 정확히 이 계약이 없애려는 것이라, 값 검증에서
ValueError로 막는다. `reason`은 열거값이다 - 자유 문자열이면 집계도 대응도 못 한다.

범위 개념이 없는 도구는 coverage를 넘기지 않으면 되고, 그러면 키 자체가 생기지
않는다. 기존 응답은 그대로다.

## viewer_tz — 사용자가 어디서 보고 있는가 (v2)

해외 구매자가 있다. as_of·data_as_of는 한국 시장 기준(KST)이라 로스앤젤레스
사용자에게는 날짜가 하루 앞선다. 그 상태로 "오늘 시세"라고 말하면 사용자는
내일 날짜로 읽는다. 사용자 PC의 타임존을 같이 실어, 읽는 쪽이 '오늘/어제'를
현지 기준으로 환산할 수 있게 한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

META_VERSION = 3

KST = ZoneInfo("Asia/Seoul")

MARKER_START = "RESULT_META_JSON_START"
MARKER_END = "RESULT_META_JSON_END"

# data_basis
BASIS_REALTIME = "realtime"
BASIS_LAST_CLOSE = "last_close"
BASIS_IN_PROGRESS_BAR = "in_progress_bar"
BASIS_FILING = "filing"
BASIS_AGGREGATE = "aggregate"

_VALID_BASIS = {
    BASIS_REALTIME,
    BASIS_LAST_CLOSE,
    BASIS_IN_PROGRESS_BAR,
    BASIS_FILING,
    BASIS_AGGREGATE,
}

# data_completeness
COMPLETE = "complete"
PARTIAL = "partial"
NONE = "none"

_VALID_COMPLETENESS = {COMPLETE, PARTIAL, NONE}

# coverage.reason - 왜 요청보다 적게 돌려줬는가 (v3)
_VALID_COVERAGE_REASONS = {
    "server_cap",       # 도구가 스스로 건 상한 (예: 수급 배치 20일)
    "pagination",       # 원천이 페이지로 끊어 준다 (예: DART 공시 목록)
    "source_limit",     # 원천이 전체 건수를 아예 알려주지 않는다
    "incomplete_tail",  # 마지막 봉이 아직 진행 중이라 구간이 덜 찼다
    "mixed_periods",    # 종목마다 기준 기간이 달라 한 범위로 못 묶는다
    "unknown",          # 이유를 모른다. 모른다고 적는 편이 낫다
}

# 미완성 봉으로 계산된 값을 확정으로 읽지 않도록 붙이는 경고.
IN_PROGRESS_WARNING = (
    "장중 조회 — 마지막 봉이 아직 마감되지 않았습니다. "
    "이 봉으로 계산된 지표·크로스·신고가 판정은 장 마감 시 달라질 수 있습니다."
)


def validate_coverage(coverage: dict | None, data_completeness: str) -> dict | None:
    """coverage 값을 검증해 그대로 돌려준다. None이면 None(키를 만들지 않는다).

    막는 것은 딱 두 가지다. 정의되지 않은 reason과, 잘라놓고 complete라고
    주장하는 조합. 나머지 필드는 도구마다 담는 내용이 달라 형태를 강제하지 않는다.
    """
    if coverage is None:
        return None
    out = dict(coverage)
    reason = out.get("reason")
    if reason is not None and reason not in _VALID_COVERAGE_REASONS:
        raise ValueError(
            f"coverage reason 미정의 값: {reason!r} (허용: {sorted(_VALID_COVERAGE_REASONS)})"
        )
    if out.get("coverage_complete") is False and data_completeness == COMPLETE:
        raise ValueError(
            "coverage_complete=false이면 data_completeness=complete일 수 없습니다."
        )
    return out


def normalize_day(value) -> str | None:
    """YYYYMMDD / YYYY.MM.DD / YYYY-MM-DD → YYYY-MM-DD. 판별 불가면 None.

    Lens마다 날짜 표기가 달라(DART는 20260813, 네이버는 2026.08.14) 메타에서만은
    한 형태로 모은다. 이래야 StockLens event_date로 그대로 넘길 수 있다.
    """
    s = str(value or "").strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 8:
        return None
    raw = digits[:8]
    try:
        datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        return None
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def viewer_tz(local: datetime | None = None) -> dict | None:
    """사용자 PC의 타임존. KST와 같으면 None(국내 사용자에겐 노이즈).

    반환: {"tz": "PDT", "utc_offset": "-0700", "local_date": "2026-08-15"}
    local_date가 핵심이다 — 이게 data_as_of와 다르면 '오늘'이 서로 다른 날이다.

    local은 테스트에서 해외 사용자를 흉내 낼 때만 넘긴다(빈 인자면 실제 PC 시각).
    """
    try:
        local = local or datetime.now().astimezone()
    except Exception:
        return None
    if local.utcoffset() == datetime.now(KST).utcoffset():
        return None
    return {
        "tz": local.tzname() or "",
        "utc_offset": local.strftime("%z"),
        "local_date": local.date().isoformat(),
    }


def entity(
    *,
    stock_code: str | None = None,
    corp_code: str | None = None,
    name: str | None = None,
) -> dict | None:
    """Lens 간 이어달리기용 식별자 묶음.

    StockLens는 6자리 stock_code만, DartLens는 8자리 corp_code까지 안다.
    아는 쪽이 메타에 실어 보내면 받는 쪽이 재검색 없이 이어갈 수 있고,
    무엇보다 코드를 추측할 유인이 사라진다. 전부 비면 None(키 생략).
    """
    out = {}
    if stock_code:
        out["stock_code"] = str(stock_code).strip()
    if corp_code:
        out["corp_code"] = str(corp_code).strip()
    if name:
        out["name"] = str(name).strip()
    return out or None


def build_meta(
    *,
    lens: str,
    data_basis: str,
    data_as_of=None,
    data_period: str | None = None,
    market: str = "KR",
    session: str | None = None,
    is_delayed: bool = False,
    data_completeness: str = COMPLETE,
    coverage: dict | None = None,
    entity_info: dict | None = None,
    warnings: list[str] | None = None,
    now: datetime | None = None,
) -> dict:
    """결과 메타 봉투 생성.

    data_as_of는 **가능하면 실제 데이터에서** 뽑아 넘긴다(차트 마지막 봉 날짜,
    공시 접수일 등). 시장 캘린더로 역산한 값은 실제 데이터가 없을 때만 쓴다.
    """
    if data_basis not in _VALID_BASIS:
        raise ValueError(f"data_basis 미정의 값: {data_basis!r} (허용: {sorted(_VALID_BASIS)})")
    if data_completeness not in _VALID_COMPLETENESS:
        raise ValueError(f"data_completeness 미정의 값: {data_completeness!r}")
    coverage_out = validate_coverage(coverage, data_completeness)

    warns = list(warnings or [])
    if data_basis == BASIS_IN_PROGRESS_BAR and IN_PROGRESS_WARNING not in warns:
        warns.insert(0, IN_PROGRESS_WARNING)

    meta = {
        "meta_v": META_VERSION,
        "lens": lens,
        "as_of": (now or datetime.now(KST)).isoformat(timespec="seconds"),
        "data_as_of": normalize_day(data_as_of),
        "data_basis": data_basis,
        "market": market,
        "is_delayed": is_delayed,
        "data_completeness": data_completeness,
        "warnings": warns,
    }
    # 재무처럼 '날짜'가 아니라 '기간'이 기준인 데이터용. 날짜를 지어내지 않고
    # 원문 표기 그대로 넣는다 (예: "2026.03 확정", "2026 반기").
    if data_period:
        meta["data_period"] = data_period
    # 요청 범위와 실제 반환 범위가 다를 수 있는 도구만 싣는다 (v3).
    if coverage_out is not None:
        meta["coverage"] = coverage_out
    # 사용자 타임존이 KST와 다를 때만. 국내 사용자에겐 붙지 않는다.
    viewer = viewer_tz()
    if viewer:
        meta["viewer_tz"] = viewer
    if session:
        meta["session"] = session
    if entity_info:
        meta["entity"] = entity_info
    return meta


def append_meta(text: str, meta: dict) -> str:
    return (
        text
        + f"\n\n{MARKER_START}\n"
        + json.dumps(meta, ensure_ascii=False, sort_keys=True)
        + f"\n{MARKER_END}"
    )
