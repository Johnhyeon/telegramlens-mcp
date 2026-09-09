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
    load_firm_abbrs,
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

# R2b(한글 뒤경계)를 적용할 이름 길이 상한. 앞경계보다 넉넉하게 두는 이유는
# 뒤쪽 오탐이 '이름 + 다른 명사'(한화투자증권·카카오헬스케어·HD현대건설기계)
# 형태라 3~4글자 이름에서 특히 자주 나기 때문이다.
_HANGUL_TAIL_MAXLEN = 4

# 이름 뒤에 붙어도 '그 종목을 가리키는 말'인 한국어 조사·어미. 이 목록에 없는
# 한글이 뒤에 붙으면 다른 단어의 앞부분을 문 것으로 본다.
_TAIL_PARTICLES = (
    "은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "과", "와",
    "로", "으로", "에서", "에게", "한테", "께", "부터", "까지", "보다", "처럼",
    "마저", "조차", "이나", "이란", "이든", "이며", "이고", "입니다", "이다",
    "였", "이었", "인", "랑", "이랑", "고", "며", "서", "임", "밖",
)
# 회사를 가리키는 흔한 접미(현대차그룹·삼성전자우·테마주).
_TAIL_COMPANY = ("주", "사", "측", "우", "그룹", "그룹주", "홀딩스")
_TAIL_OK = tuple(
    sorted(set(_TAIL_PARTICLES + _TAIL_COMPANY), key=len, reverse=True)
)


def _is_hangul(ch: str) -> bool:
    return bool(ch) and "가" <= ch <= "힣"


def _is_wordchar(ch: str) -> bool:
    """영문 토큰 경계 판정용. URL·핸들에 쓰이는 '_' 도 단어 문자로 본다
    (t.me/HI_GS 의 GS 가 종목으로 잡히던 문제)."""
    return bool(ch) and ch.isascii() and (ch.isalnum() or ch == "_")


def _embedded_match(text: str, idx: int, term: str) -> bool:
    """이름이 '토큰'이 아니라 더 큰 단어 '속에 박혀' 매칭됐는지(=오탐) 판정.

    R1(영문 경계): ASCII 영숫자 이름이 ASCII 영숫자·'_' 에 인접 → 영어 단어 일부(ROLLS←LS).
    R2a(한글 앞경계): 짧은 한글 이름 바로 앞이 한글 음절 → 한글 단어 중간(바이[오텍]).
    R2b(한글 뒤경계): 이름 뒤가 한글인데 그 한글이 조사·회사접미로 시작하지 않으면
      다른 단어의 앞부분을 문 것(한화[투자증권], 하이브[리드], 기아[나]). 조사가
      뒤에 붙는 한국어 특성 때문에 '뒤경계는 못 쓴다'고 봤으나, 조사·접미는 유한한
      집합이라 화이트리스트로 두면 쓸 수 있다.
    """
    n = len(term)
    before = text[idx - 1] if idx > 0 else ""
    after = text[idx + n] if idx + n < len(text) else ""
    # R1: 영문 substring 박힘 (앞 또는 뒤가 ASCII 영숫자·밑줄)
    if term[:1].isascii() and term[:1].isalnum() and _is_wordchar(before):
        return True
    if term[-1:].isascii() and term[-1:].isalnum() and _is_wordchar(after):
        return True
    # R2a: 짧은 한글 이름이 한글 음절 바로 뒤 (단어 중간)
    if n <= _HANGUL_BOUNDARY_MAXLEN and _is_hangul(term[0]) and _is_hangul(before):
        return True
    # R2b: 이름 뒤 한글이 조사·회사접미가 아니면 더 긴 단어의 앞부분
    if n <= _HANGUL_TAIL_MAXLEN and _is_hangul(after):
        if not text[idx + n:].startswith(_TAIL_OK):
            return True
    return False

# 증권사 등 '출처'가 인용 문맥으로 쓰일 때의 신호어(이 이름 직후 등장).
_CITATION_WORDS = (
    "리서치", "리포트", "레포트", "보고서", "자료", "코멘트", "데일리",
    "위클리", "모닝", "세미나", "컨콜", "발", "센터",
)


# 이름 **앞**에 오는 인용 신호어. "작성자: 현대차증권" 처럼 출처를 앞에서
# 밝히는 형태가 리포트 요약 채널에서 가장 흔하다.
_CITATION_LEAD = (
    "작성자", "작성", "애널리스트", "출처", "제공", "자료출처", "by", "By", "BY",
)

# 이름 뒤 가까운 범위에 나오면 인용으로 보는 말. 이름과 신호어 사이에
# 의견·목표가가 끼어드는 형태가 흔하다 — "한화투자증권 Buy(유지) 보고서 발행".
_CITATION_NEAR = ("리서치", "리포트", "레포트", "보고서", "코멘트", "발간", "발행")
_CITATION_NEAR_WINDOW = 24


def _is_citation(text: str, idx: int, length: int) -> bool:
    """위치 idx 의 출처명이 '종목'이 아니라 '인용(출처)'으로 쓰였는지.

    데이터에서 관찰된 인용 패턴:
      [삼성증권] / 키움증권(2026.06.01) / 교보증권 리포트 / 부국증권 - 보고서 / 교보증권/공시
      작성자: 현대차증권 (박현욱)            ← 이름 앞에서 출처를 밝히는 형태
      당일 한화투자증권 Buy(유지) 보고서 발행  ← 이름과 신호어 사이에 의견이 낌
    """
    before_text = text[:idx]
    before = before_text[-1:] if idx > 0 else ""
    after = text[idx + length:]
    after_strip = after.lstrip(" 　")

    if before == "[":
        return True
    # 공백을 사이에 두고 괄호가 오는 형태도 인용이다 — "현대차증권 (박현욱)".
    if after_strip[:1] in ("(", "/"):
        return True
    if after_strip[:1] == "-":
        return True
    if after_strip.startswith(_CITATION_WORDS):
        return True
    # 앞에서 출처를 밝히는 형태: "작성자: 현대차증권"
    lead = before_text.rstrip(" 　:：-–—·|>[(")
    if lead.endswith(_CITATION_LEAD):
        return True
    # 뒤쪽 가까운 곳에 인용 신호어: "한화투자증권 Buy(유지) 보고서 발행"
    window = after_strip[:_CITATION_NEAR_WINDOW]
    if any(w in window for w in _CITATION_NEAR):
        return True
    return False


# 줄머리 괄호 출처 표기 — 리포트 요약 채널의 지배적 포맷.
#   지엔씨에너지 48,300원(+7.81%)
#   (IBK) AI데이터센터 수주 1번 타자
#   (LS) 데이터센터로 제시하는 성장의 그림
# 괄호 안이 발행사 약칭일 때만 인용으로 본다. 줄머리로 한정해 본문 중간의
# 정상 괄호 언급("(한화) 계열사" 같은)까지 죽이지 않는다.
_PAREN_LEAD_RE = re.compile(r"(?:^|\n)[ \t　>·\-]*\(([^()\n]{1,12})\)")


def _paren_citation_spans(text: str) -> list[tuple[int, int]]:
    """줄머리 '(발행사약칭)' 의 괄호 **안** 구간 목록."""
    abbrs = load_firm_abbrs()
    spans: list[tuple[int, int]] = []
    for m in _PAREN_LEAD_RE.finditer(text):
        if m.group(1).strip() in abbrs:
            spans.append((m.start(1), m.end(1)))
    return spans


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
    _s._firm_abbr_cache = None
    _name_index.cache_clear()


_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5}(?:\.[AB])?)\b")
_BARE_US_RE = re.compile(r"(?<![A-Za-z$])([A-Z]{2,5})(?![A-Za-z])")


# 한국 증권 텍스트에서 '단어'로 쓰이는 약어. 미국 티커와 철자가 겹치지만
# (AI→C3.ai, IR→Ingersoll Rand, HBM→Hudbay Minerals, HD→Home Depot,
#  KB→KB Financial ADR, DB→Deutsche Bank) 한국 채널에서 이 철자가 종목을
# 가리키는 일은 사실상 없다. cashtag($AI)로 명시할 때만 인정한다.
_KR_CONTEXT_ABBRS = frozenset({
    "AI", "IR", "IT", "HD", "KB", "DB", "CS", "PC", "TV", "PR", "PT", "PS",
    "MS", "MA", "BW", "CB", "EV", "OS", "UX", "UI", "QC", "RD", "SI", "SW",
    "HBM", "DDR", "CPI", "PPI", "GDP", "ETF", "ETN", "IPO", "ROE", "ROA",
    "EPS", "PER", "PBR", "PSR", "OEM", "ODM", "ESS", "ESG", "SMR", "LNG",
    "LPG", "RNA", "DNA", "CEO", "CFO", "CTO", "IPS", "PCB", "OLED", "LCD",
})

# 시장 문맥어를 티커 주변에서만 찾는 창(글자). 메시지 전체를 보면 증권 채널
# 글은 어디엔가 '주가'·'실적'이 있어 가드가 사실상 꺼진다.
_US_CONTEXT_WINDOW = 40


def _us_mentions(text: str) -> dict[str, str]:
    """미국 티커 언급(TL-03). cashtag 는 항상, bare 티커는 가드를 통과할 때만.

    "we are ALL in" 의 ALL(올스테이트)처럼 일반 영어 단어와 겹치는 티커를
    문맥 없이 잡으면 오탐 기계가 된다(요구 3). bare 매칭은 사전에 있는
    티커이면서, 아래를 모두 통과할 때만 인정한다:

      G1 한글 인접 금지 — "HD현대"·"KB금융"·"DB하이텍" 은 한국 종목명의 일부다.
      G2 한국 문맥 약어 금지 — AI·IR·HBM 등은 cashtag 로만 인정.
      G3 두 글자 bare 는 시드 티커만 — 두 글자 대문자는 약어와 충돌이 확실하다.
      G4 일반 영어 단어 티커·시드 밖 티커는 **티커 주변** 시장 문맥어를 요구.
    """
    from telegram_lens import us_stocks

    found: dict[str, str] = {}
    table = us_stocks.load_us_map()

    for m in _CASHTAG_RE.finditer(text):
        ticker = m.group(1)
        info = table.get(ticker)
        if info:
            found[ticker] = info["name"]

    for m in _BARE_US_RE.finditer(text):
        ticker = m.group(1)
        if ticker in found:
            continue
        info = table.get(ticker)
        if not info:
            continue
        start, end = m.start(1), m.end(1)
        # G1: 앞뒤가 한글이면 한국 종목명·합성어의 일부다.
        if _is_hangul(text[start - 1] if start else "") or _is_hangul(
            text[end] if end < len(text) else ""
        ):
            continue
        # G2: 한국 증권 문맥에서 단어로 쓰이는 약어는 cashtag 로만.
        if ticker in _KR_CONTEXT_ABBRS:
            continue
        # G3: 두 글자 bare 는 시드(주요 종목)만 허용.
        if len(ticker) <= 2 and ticker not in us_stocks.US_SEED:
            continue
        near = text[max(0, start - _US_CONTEXT_WINDOW):end + _US_CONTEXT_WINDOW].lower()
        has_context = any(w in near for w in us_stocks.MARKET_CONTEXT_WORDS)
        # G4: 일반 영어 단어 티커는 근처 문맥을 요구한다.
        if ticker in us_stocks.COMMON_WORD_TICKERS and not has_context:
            continue
        # 시드 밖(SEC 전체 목록)의 티커는 근처 문맥 없이는 인정하지 않는다 -
        # 영어 문장 대문자 단어 전부가 후보가 되기 때문이다.
        if ticker not in us_stocks.US_SEED and not has_context:
            continue
        found[ticker] = info["name"]
    return found


def extract_mentions(text: str) -> list[tuple[str, str]]:
    """텍스트에서 (code, name) 언급 목록을 중복 제거해 반환.

    한국 종목은 6자리 코드·이름·별칭으로, 미국 종목은 cashtag($PLTR)·한글
    통용명(팔란티어)·가드된 bare 티커로 잡는다(TL-03). code 자리에는 한국은
    6자리 코드, 미국은 티커가 들어간다.
    """
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
    paren_spans = _paren_citation_spans(text)
    consumed = [False] * len(text)
    for term, code in _name_index():
        start = 0
        while True:
            idx = text.find(term, start)
            if idx == -1:
                break
            end = idx + len(term)
            span = range(idx, end)
            if not any(consumed[i] for i in span):
                # 경계 규칙: 이름이 더 큰 단어 속에 박힌 매칭(ROLLS←LS, 바이오텍←오텍)은 오탐.
                # code 동반(found 에 이미 코드로 있음)이면 보존.
                suppressed = False
                if code not in found and _embedded_match(text, idx, term):
                    suppressed = True
                # 증권사 등 출처성 종목: 인용 문맥이면 제외(코드 동반이면 1)에서 이미 채택됨)
                elif code in source_firms and code not in found and _is_citation(
                    text, idx, len(term)
                ):
                    suppressed = True
                # 줄머리 괄호 출처: "(한화) 나의 계절이 왔다" 의 한화는 발행사다.
                elif code not in found and any(
                    a <= idx and end <= b for a, b in paren_spans
                ):
                    suppressed = True

                # 억제한 구간도 소비한다. 소비하지 않으면 더 긴 이름을 걸러낸 자리에서
                # 그 이름의 앞부분(한화투자증권 → 한화, SK증권 → SK)이 다시 잡힌다.
                for i in span:
                    consumed[i] = True
                if not suppressed:
                    found[code] = by_code.get(code, term)
            start = idx + 1

    # 2.5) 미국 종목(TL-03): cashtag·가드된 bare 티커. 한글 통용명은 아래
    #      name_index 경로가 아니라 여기서 함께 처리한다(사전이 분리되어 있다).
    try:
        from telegram_lens import us_stocks as _us

        for alias, ticker in _us.alias_terms():
            if alias in text and ticker not in found:
                found[ticker] = _us.load_us_map().get(
                    ticker, {}).get("name") or ticker
        for ticker, name in _us_mentions(text).items():
            found.setdefault(ticker, name)
    except Exception:
        pass   # 미국 사전 문제로 한국 추출까지 죽으면 안 된다

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
