"""메시지 텍스트에서 종목 언급을 추출.

두 경로 모두 종목 사전으로 검증해 오탐을 줄인다:
  1. 6자리 종목코드(\\d{6}) → 사전에 존재하는 코드만 채택
  2. 종목명 부분일치 → 사전의 이름과 매칭

종목명 매칭은 한국어 특성상 공백 없이 붙는 경우가 많아 부분일치로 한다.
짧은 이름(1글자)·잡음이 큰 이름은 길이 필터로 거른다. 길이 내림차순으로
정렬해 가장 긴 이름이 먼저 잡히게 한다(예: "삼성전자" 우선, "삼성" 차순).
"""

from __future__ import annotations

import re
from functools import lru_cache

from telegram_lens.stocks import (
    load_aliases,
    load_ambiguous,
    load_source_firms,
    load_stocks,
)

# 단축코드: 6자리 숫자(전통) 또는 신형 영숫자(DDDDAD). 영숫자 코드가 더 큰 영숫자
# 토큰 속에 박혀 오탐 나는 걸 막으려 양옆 경계를 영숫자(ASCII)로 둔다. 사전 검증
# (code in by_code)이 뒤따르므로 과매칭은 걸러진다.
_CODE_RE = re.compile(r"(?<![0-9A-Za-z])(\d{4}[0-9A-Z]\d)(?![0-9A-Za-z])")

# 이름 매칭에서 제외할 너무 흔하거나 짧은 토큰
_MIN_NAME_LEN = 2

# R2a(한글 앞경계)를 적용할 짧은 한글 이름 길이 상한. 2글자 이름이 한글 음절 바로 뒤에
# 오면 단어 중간(바이오텍의 '오텍', 지도부의 '도부')으로 보고 거른다. 3글자 이상 distinctive
# 이름은 오히려 정상 매칭이 많아 제외.
_HANGUL_BOUNDARY_MAXLEN = 2


def _is_hangul(ch: str) -> bool:
    return bool(ch) and "가" <= ch <= "힣"


def _embedded_match(text: str, idx: int, term: str) -> bool:
    """이름이 '토큰'이 아니라 더 큰 단어 '속에 박혀' 매칭됐는지(=오탐) 판정.

    R1(영문 경계): ASCII 영숫자 이름이 ASCII 영숫자에 인접 → 영어 단어 일부(ROLLS←LS).
    R2a(한글 앞경계): 짧은 한글 이름 바로 앞이 한글 음절 → 한글 단어 중간(바이[오텍]).
      한글은 조사가 뒤에 붙어(삼성전자+가) '뒤경계'로는 못 거르므로 '앞'만 본다.
    """
    n = len(term)
    before = text[idx - 1] if idx > 0 else ""
    after = text[idx + n] if idx + n < len(text) else ""
    # R1: 영문 substring 박힘 (앞 또는 뒤가 ASCII 영숫자)
    if term[:1].isascii() and term[:1].isalnum() and before.isascii() and before.isalnum():
        return True
    if term[-1:].isascii() and term[-1:].isalnum() and after.isascii() and after.isalnum():
        return True
    # R2a: 짧은 한글 이름이 한글 음절 바로 뒤 (단어 중간)
    if n <= _HANGUL_BOUNDARY_MAXLEN and _is_hangul(term[0]) and _is_hangul(before):
        return True
    return False

# 증권사 등 '출처'가 인용 문맥으로 쓰일 때의 신호어(이 이름 직후 등장).
_CITATION_WORDS = (
    "리서치", "리포트", "레포트", "보고서", "자료", "코멘트", "데일리",
    "위클리", "모닝", "세미나", "컨콜", "발", "센터",
)


def _is_citation(text: str, idx: int, length: int) -> bool:
    """위치 idx 의 출처명이 '종목'이 아니라 '인용(출처)'으로 쓰였는지.

    데이터에서 관찰된 인용 패턴:
      [삼성증권] / 키움증권(2026.06.01) / 교보증권 리포트 / 부국증권 - 보고서 / 교보증권/공시
    """
    before = text[idx - 1] if idx > 0 else ""
    after = text[idx + length:]
    after_strip = after.lstrip(" 　")

    if before == "[":
        return True
    if after[:1] in ("(", "/"):
        return True
    if after_strip[:1] == "-":
        return True
    if after_strip.startswith(_CITATION_WORDS):
        return True
    return False


@lru_cache(maxsize=1)
def _name_index() -> list[tuple[str, str]]:
    """[(matchterm, code)] — 매칭어 길이 내림차순.

    - 모호 종목(ambiguous)은 이름 단독 매칭에서 제외(코드 경로로만 잡힘).
    - 별칭(aliases)을 매칭어로 추가(recall↑). 표시명은 정식명으로 해석.
    캐시는 사전/별칭 갱신 시 reset_index() 로 무효화.
    """
    by_code = load_stocks()
    ambiguous = load_ambiguous()
    items: list[tuple[str, str]] = [
        (name, code)
        for code, name in by_code.items()
        if len(name) >= _MIN_NAME_LEN and code not in ambiguous
    ]
    # 별칭 추가 — 모호 종목이라도 명시적 별칭은 허용(약어는 일반명사와 덜 충돌)
    for alias, code in load_aliases().items():
        if len(alias) >= _MIN_NAME_LEN:
            items.append((alias, code))
    items.sort(key=lambda x: len(x[0]), reverse=True)
    return items


def reset_index() -> None:
    """종목 사전·별칭 갱신 후 인덱스 캐시 무효화."""
    import telegram_lens.stocks as _s

    _s._aliases_cache = None
    _s._ambiguous_cache = None
    _s._source_firms_cache = None
    _name_index.cache_clear()


def extract_mentions(text: str) -> list[tuple[str, str]]:
    """텍스트에서 (code, name) 언급 목록을 중복 제거해 반환."""
    if not text:
        return []

    by_code = load_stocks()
    source_firms = load_source_firms()
    found: dict[str, str] = {}  # code -> name
    confirmed: set[str] = set()  # 6자리 코드로 확인된 종목(부모 억제에서 보호)

    # 1) 6자리 코드 — 사전 검증. 코드 동반은 출처가 아니라 실제 종목 언급.
    for m in _CODE_RE.finditer(text):
        code = m.group(1)
        if code in by_code:
            found[code] = by_code[code]
            confirmed.add(code)

    # 2) 종목명 부분일치 — 이미 코드로 잡힌 종목 위치는 그대로 두되,
    #    이름이 등장하면 추가. 긴 이름 우선으로 같은 영역 중복 카운트 방지.
    consumed = [False] * len(text)
    for term, code in _name_index():
        start = 0
        while True:
            idx = text.find(term, start)
            if idx == -1:
                break
            span = range(idx, idx + len(term))
            if not any(consumed[i] for i in span):
                # 경계 규칙: 이름이 더 큰 단어 속에 박힌 매칭(ROLLS←LS, 바이오텍←오텍)은 오탐.
                # code 동반(found 에 이미 코드로 있음)이면 보존.
                if code not in found and _embedded_match(text, idx, term):
                    pass  # 박힌 매칭 — 카운트 안 함(consumed 도 안 함: 다른 매칭 방해 X)
                # 증권사 등 출처성 종목: 인용 문맥이면 제외(코드 동반이면 1)에서 이미 채택됨)
                elif code in source_firms and code not in found and _is_citation(
                    text, idx, len(term)
                ):
                    pass  # 인용 → 종목으로 카운트하지 않음
                else:
                    found[code] = by_code.get(code, term)
                    for i in span:
                        consumed[i] = True
            start = idx + 1

    # 3) 모회사·자회사 이름 포함관계 억제: 자식 이름(예: '두산로보틱스')이 함께 잡혔으면,
    #    코드로 확인되지 않은 부모(예: '두산')는 제거한다. '두산로보틱스 … 두산 그룹'처럼
    #    자회사 글에 bare 모회사명이 묻어 중복 집계되는 것을 막는다(코드 동반 시는 보존).
    if len(found) > 1:
        names = list(found.values())
        for code, name in list(found.items()):
            if code in confirmed:
                continue
            if any(other != name and other.startswith(name) for other in names):
                del found[code]

    return list(found.items())
