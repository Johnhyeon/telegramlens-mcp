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
    """번들 data/ 파일을 읽고, 사용자 홈에 같은 이름이 있으면 병합(사용자 우선)."""
    out: dict = {}
    bundled = _DATA_DIR / name
    if bundled.exists():
        try:
            out.update(json.loads(bundled.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    override = data_dir() / name
    if override.exists():
        try:
            out.update(json.loads(override.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return out


_aliases_cache: dict[str, str] | None = None
_ambiguous_cache: set[str] | None = None
_source_firms_cache: set[str] | None = None


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


def resolve_code(query: str) -> tuple[str | None, str]:
    """종목명/6자리 코드 입력을 (code, name) 으로 해석. 못 찾으면 (None, query).

    코드 정확일치 → 종목명 정확일치 → 종목명 부분일치 순. server·api 공용.
    """
    by_code = load_stocks()
    if query in by_code:
        return query, by_code[query]
    for c, n in by_code.items():
        if n == query:
            return c, n
    for c, n in by_code.items():
        if query in n:
            return c, n
    return None, query


def _cli_refresh() -> None:
    """`telegramlens-refresh-stocks` 엔트리포인트."""
    data = refresh_stocks()
    print(f"종목 사전 갱신 완료: {len(data)}개 → {stocks_path()}")
