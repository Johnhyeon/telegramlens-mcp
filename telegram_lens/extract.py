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

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 이름 매칭에서 제외할 너무 흔하거나 짧은 토큰
_MIN_NAME_LEN = 2

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

    # 1) 6자리 코드 — 사전 검증. 코드 동반은 출처가 아니라 실제 종목 언급.
    for m in _CODE_RE.finditer(text):
        code = m.group(1)
        if code in by_code:
            found[code] = by_code[code]

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
                # 증권사 등 출처성 종목: 인용 문맥이면 제외(코드 동반이면 1)에서 이미 채택됨)
                if code in source_firms and code not in found and _is_citation(
                    text, idx, len(term)
                ):
                    pass  # 인용 → 종목으로 카운트하지 않음
                else:
                    found[code] = by_code.get(code, term)
                for i in span:
                    consumed[i] = True
            start = idx + 1

    return list(found.items())
