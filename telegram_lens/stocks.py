"""종목 사전 — 종목코드 ↔ 종목명 매핑.

추출(extract)에서 텍스트 속 종목명/코드를 검증하는 데 쓴다.
KRX 상장 전 종목을 한 번 받아 ``~/.telegramlens/stocks.json`` 에 캐시한다.
네트워크 실패 시 최소 시드 사전으로 폴백.

stocks.json 구조:
    {"updated": "ISO8601", "by_code": {"005930": "삼성전자", ...}}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from telegram_lens.config import data_dir, stocks_path

_DATA_DIR = Path(__file__).parent / "data"

# KRX 상장법인 목록 — EUC-KR HTML 테이블(method=download).
# https + 리다이렉트 추적 + UA 필요. 실패하면 시드로 폴백한다.
_KRX_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do"

# KRX 데이터시스템 ETF 전종목 finder(JSON, UTF-8). 상장법인 목록(searchType=13)에는
# ETF(펀드)가 없어 별도로 받아 병합한다. block1: [{full_code, short_code, codeName}].
# 실패해도 회사 사전은 유지(ETF만 건너뜀).
_KRX_ETF_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"

# KRX 단축코드 패턴 — 6자리 숫자(전통) 또는 신형 영숫자(DDDDAD: 4숫자+1대문자+1숫자).
# 숫자 코드 고갈로 신규 상장 종목·ETF에 영숫자 코드가 발급된다(회사 51개·ETF 269개 관측).
_SHORT_CODE = r"\d{4}[0-9A-Z]\d"

# 네트워크 불가 시 최소 동작용 시드(대형주 일부).
_SEED: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스",
    "005380": "현대차",
    "000270": "기아",
    "035420": "NAVER",
    "035720": "카카오",
    "051910": "LG화학",
    "006400": "삼성SDI",
    "068270": "셀트리온",
    "105560": "KB금융",
    "055550": "신한지주",
    "012330": "현대모비스",
    "028260": "삼성물산",
    "066570": "LG전자",
    "003670": "포스코퓨처엠",
    "096770": "SK이노베이션",
    "034730": "SK",
    "015760": "한국전력",
}

_cache: dict[str, str] | None = None
_etf_cache: set[str] | None = None


def _load_file() -> dict | None:
    """stocks.json 원본(dict)을 반환. by_code 가 비어있으면 None."""
    p = stocks_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        by_code = data.get("by_code")
        if isinstance(by_code, dict) and by_code:
            return data
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _save_file(by_code: dict[str, str], etf_codes: set[str]) -> None:
    stocks_path().write_text(
        json.dumps(
            {
                "updated": datetime.now(timezone.utc).isoformat(),
                "by_code": by_code,
                "etf_codes": sorted(etf_codes),
            },
            ensure_ascii=False,
            indent=0,
        ),
        encoding="utf-8",
    )


def _fetch_etfs() -> dict[str, str]:
    """KRX 데이터시스템에서 ETF 전종목(코드→약식명)을 받아 반환. 실패 시 {}."""
    try:
        resp = httpx.post(
            _KRX_ETF_URL,
            data={
                "bld": "dbms/comm/finder/finder_secuprodisu",
                "mktsel": "ETF",
                "typeNo": "0",
            },
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "http://data.krx.co.kr/"},
        )
        rows = resp.json().get("block1", [])
        out: dict[str, str] = {}
        for r in rows:
            code = str(r.get("short_code", "")).strip()
            name = str(r.get("codeName", "")).strip()
            if code and name:
                out[code] = name
        return out
    except Exception:
        return {}


def refresh_stocks() -> dict[str, str]:
    """KRX에서 상장종목 전체(회사 + ETF)를 받아 캐시. 실패 시 시드 반환."""
    by_code: dict[str, str] = {}
    try:
        import re

        resp = httpx.get(
            _KRX_URL,
            params={"method": "download", "searchType": "13"},
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.encoding = "euc-kr"
        html = resp.text

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
        for row in rows:
            cells = [
                re.sub(r"<[^>]+>", "", c).strip()
                for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            ]
            if len(cells) < 2:
                continue
            # 첫 셀이 회사명, 행 어딘가에 단축코드(숫자 또는 신형 영숫자)가 있다.
            name = cells[0]
            code_raw = next(
                (c for c in cells[1:] if re.fullmatch(_SHORT_CODE, c)), None
            )
            if code_raw and name:
                by_code[code_raw] = name
    except Exception:
        by_code = {}

    if not by_code:
        by_code = dict(_SEED)

    # ETF 병합 — 코드는 전 종목 유니크라 회사와 충돌 없음. 실패해도 회사 사전 유지.
    etfs = _fetch_etfs()
    by_code.update(etfs)
    etf_codes = set(etfs)

    _save_file(by_code, etf_codes)
    global _cache, _etf_cache
    _cache = by_code
    _etf_cache = etf_codes
    return by_code


def load_stocks(refresh: bool = False) -> dict[str, str]:
    """종목코드→종목명 매핑 반환(메모리 캐시). ETF 코드셋도 함께 채운다."""
    global _cache, _etf_cache
    if _cache is not None and not refresh:
        return _cache
    if not refresh:
        data = _load_file()
        if data:
            _cache = {str(k): str(v) for k, v in data["by_code"].items()}
            _etf_cache = {str(c) for c in data.get("etf_codes", [])}
            return _cache
    return refresh_stocks()


def load_etf_codes() -> set[str]:
    """ETF 단축코드 집합(주식과 구분용). 미로딩 시 사전 로드를 트리거한다."""
    global _etf_cache
    if _etf_cache is None:
        load_stocks()
    return _etf_cache or set()


def _load_json(name: str) -> dict:
    """번들 data/ 파일을 읽고, 사용자 홈에 같은 이름이 있으면 병합(사용자 우선).

    **중첩 dict 는 키 단위로 병합한다.** 통째로 덮어쓰면 사용자가 도구로 한 건만
    추가해도 번들 목록이 통째로 사라진다 — ambiguous_codes.json 의 codes /
    us_bare_blocked 가 그런 구조라, telegram_block_name 한 번에 기본 차단
    목록이 날아갔다(실측: 116종 → 1종).
    """
    out: dict = {}
    for path in (_DATA_DIR / name, data_dir() / name):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = {**out[key], **value}
            else:
                out[key] = value
    return out


_aliases_cache: dict[str, str] | None = None
_ambiguous_cache: set[str] | None = None
_source_firms_cache: set[str] | None = None
_firm_abbr_cache: frozenset[str] | None = None
_us_blocked_cache: dict[str, str] | None = None

# 리포트 요약 채널에서 발행사를 짧게 줄여 쓰는 표기. 사전에서 자동 산출되지 않는
# 것(비상장·자회사·독립리서치)만 손으로 적는다. 상장 증권사는 이름에서 접미사를
# 떼어 자동으로 합쳐진다(한화투자증권 → 한화). tagging(채널 tier 판정)과
# extract(인용 억제)가 같은 목록을 본다.
_MANUAL_FIRM_ABBRS = (
    "IBK", "KB", "하나", "하나금투", "신한", "한투", "한국투자", "DS", "흥국",
    "BNK", "iM", "IM", "메리츠", "리딩", "코넥트", "밸류파인더", "아리스",
    "한국IR", "IR큐더스", "하이", "하이투자", "케이프", "이베스트", "NH",
    "미래", "미래에셋", "키움", "대신", "교보", "유안타", "다올", "DAOL",
    "상상인", "신영", "유진투자", "DB금투", "DB Tech", "SK", "LS", "한화",
)

# 발행사 약칭에서 떼어낼 접미사(긴 것부터).
_FIRM_NAME_SUFFIXES = ("투자증권", "금융투자", "증권")


def load_aliases() -> dict[str, str]:
    """별칭(통용어/약어) → 코드. 코드가 실제 사전에 있는 것만 채택."""
    global _aliases_cache
    if _aliases_cache is not None:
        return _aliases_cache
    raw = _load_json("aliases.json")
    by_code = load_stocks()
    aliases = {
        str(k): str(v)
        for k, v in raw.items()
        if not k.startswith("_") and str(v) in by_code
    }
    _aliases_cache = aliases
    return aliases


def load_ambiguous() -> set[str]:
    """이름 단독 매칭을 완전히 막을 하드블록 코드 집합(명시 codes만).

    일반명사/증시은어 충돌(대상·TP·신흥 등). 코드 동반 시만 채택.
    증권사처럼 '유효 섹터지만 인용도 잦은' 종목은 여기가 아니라
    load_source_firms() 의 인용 억제로 다룬다.
    """
    global _ambiguous_cache
    if _ambiguous_cache is not None:
        return _ambiguous_cache
    raw = _load_json("ambiguous_codes.json")
    codes = raw.get("codes", {})
    _ambiguous_cache = {str(c) for c in codes} if isinstance(codes, dict) else set()
    return _ambiguous_cache


def load_source_firms() -> set[str]:
    """인용 억제 대상(증권사 등) 코드 집합.

    이름이 citation_suppress_suffixes 로 끝나는 종목을 사전에서 자동 산출.
    이름 매칭은 허용하되, 인용 문맥(extract 에서 판정)일 때만 제외한다.
    """
    global _source_firms_cache
    if _source_firms_cache is not None:
        return _source_firms_cache
    raw = _load_json("ambiguous_codes.json")
    suffixes = raw.get("citation_suppress_suffixes", [])
    result: set[str] = set()
    if isinstance(suffixes, list) and suffixes:
        by_code = load_stocks()
        sfx = tuple(str(s) for s in suffixes)
        result = {code for code, name in by_code.items() if name.endswith(sfx)}
    _source_firms_cache = result
    return _source_firms_cache


def load_us_blocked() -> dict[str, str]:
    """미국 bare 티커 차단 목록 {티커: 메모}. 패키지 기본 + 사용자 override 병합.

    한국 증권 텍스트에서 AI·IR·HBM 같은 철자는 단어지 티커가 아니다. cashtag($AI)
    로 쓰면 여전히 인정한다(extract 의 bare 경로에서만 막는다).
    """
    global _us_blocked_cache
    if _us_blocked_cache is not None:
        return _us_blocked_cache
    raw = _load_json("ambiguous_codes.json")
    blocked = raw.get("us_bare_blocked", {})
    _us_blocked_cache = (
        {str(t).upper(): str(n) for t, n in blocked.items()}
        if isinstance(blocked, dict)
        else {}
    )
    return _us_blocked_cache


def add_us_blocked(ticker: str, note: str = "") -> dict:
    """사용자 override(ambiguous_codes.json)에 미국 티커 차단 추가."""
    ticker = str(ticker).upper()
    data = _read_user_json("ambiguous_codes.json")
    blocked = data.get("us_bare_blocked")
    if not isinstance(blocked, dict):
        blocked = {}
    blocked[ticker] = note
    data["us_bare_blocked"] = blocked
    (data_dir() / "ambiguous_codes.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    global _us_blocked_cache
    _us_blocked_cache = None
    return {"ticker": ticker, "note": note}


def load_firm_abbrs() -> frozenset[str]:
    """발행사 약칭 집합 — 상장 증권사 이름에서 자동 산출 + 수동 목록.

    "한화투자증권" → "한화", "SK증권" → "SK" 처럼 접미사를 뗀 앞부분이 그대로
    리포트 요약 글의 출처 표기로 쓰인다. 이 앞부분이 다른 종목의 정식명과
    겹치기 때문에(한화 000880, SK 034730, LS 006260) 인용 판정에 필요하다.
    """
    global _firm_abbr_cache
    if _firm_abbr_cache is not None:
        return _firm_abbr_cache
    out = set(_MANUAL_FIRM_ABBRS)
    for name in load_stocks().values():
        for sfx in _FIRM_NAME_SUFFIXES:
            if name.endswith(sfx) and len(name) > len(sfx):
                out.add(name[: -len(sfx)])
                break
    _firm_abbr_cache = frozenset(out)
    return _firm_abbr_cache


def _read_user_json(name: str) -> dict:
    p = data_dir() / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def add_alias(alias: str, code: str) -> dict:
    """사용자 override(aliases.json)에 별칭 추가. 캐시 무효화는 호출측 책임."""
    by_code = load_stocks()
    if code not in by_code:
        raise ValueError(f"코드 {code} 는 종목 사전에 없습니다.")
    data = _read_user_json("aliases.json")
    data[alias] = code
    (data_dir() / "aliases.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    global _aliases_cache
    _aliases_cache = None
    return {"alias": alias, "code": code, "official_name": by_code[code]}


def add_ambiguous(code: str, note: str = "") -> dict:
    """사용자 override(ambiguous_codes.json)에 모호 종목 추가."""
    by_code = load_stocks()
    data = _read_user_json("ambiguous_codes.json")
    codes = data.get("codes")
    if not isinstance(codes, dict):
        codes = {}
    codes[code] = note or by_code.get(code, code)
    data["codes"] = codes
    (data_dir() / "ambiguous_codes.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    global _ambiguous_cache
    _ambiguous_cache = None
    return {"code": code, "name": by_code.get(code), "note": note}


def resolve_entity(query: str, by_code: dict[str, str] | None = None) -> dict:
    """질의 → {market, code, name, entity_status}. 모든 종목 해석의 단일 경로.

    순서: 국내 코드 정확 → 국내 종목명 정확 → 미지원 시장 표기 → 미국 한글 통용명
    정확 → 국내 종목명 부분 → 미국 티커·회사명.

    통용명 정확 일치가 국내 부분 일치보다 먼저인 이유(실측 2026-09-17): 부분 일치를
    먼저 보면 '엔비디아'가 'ACE 엔비디아밸류체인액티브', '메타'가 '노브메타파마',
    '인텔'이 '인텔리안테크'로 풀려 통용명 41개 중 13개가 엉뚱한 국내 종목이 됐다.
    엔비디아는 NVDA 48건인데 ETF 0건을 세어 '무언급'이라고 답했다.
    영문 티커 입력은 예전처럼 국내 부분 일치 뒤에 본다(HD·KB 같은 약어가 미국으로 가지 않게).

    by_code: 이미 불러온 국내 사전(생략 시 load_stocks()).
    """
    from telegram_lens import us_stocks

    if by_code is None:
        by_code = load_stocks()
    if query in by_code:
        return {"market": "KR", "code": query, "name": by_code[query],
                "entity_status": "supported"}
    for c, n in by_code.items():
        if n == query:
            return {"market": "KR", "code": c, "name": n,
                    "entity_status": "supported"}
    if us_stocks.is_unsupported_market(query):
        return {"market": "OTHER", "code": None, "name": query,
                "entity_status": "unsupported_market"}
    us = us_stocks.resolve_us_alias(query)
    if us:
        return {"market": "US", "code": us["ticker"], "name": us["name"],
                "entity_status": "supported"}
    for c, n in by_code.items():
        if query in n:
            return {"market": "KR", "code": c, "name": n,
                    "entity_status": "supported"}
    us = us_stocks.resolve_us(query)
    if us:
        return {"market": "US", "code": us["ticker"], "name": us["name"],
                "entity_status": "supported"}
    return {"market": None, "code": None, "name": query,
            "entity_status": "entity_not_found"}


def resolve_code(query: str) -> tuple[str | None, str]:
    """종목명/코드/티커 입력을 (code, name) 으로 해석. 못 찾으면 (None, query).

    resolve_entity 와 같은 순서다. 미국 종목이면 code 자리에 티커가 온다
    (mentions 도 티커로 저장된다). 봇 명령·내 종목·HTTP API 공용.
    """
    ent = resolve_entity(query)
    if ent["code"] is None:
        return None, query
    return ent["code"], ent["name"]


def _cli_refresh() -> None:
    """`telegramlens-refresh-stocks` 엔트리포인트."""
    data = refresh_stocks()
    print(f"종목 사전 갱신 완료: {len(data)}개 → {stocks_path()}")
