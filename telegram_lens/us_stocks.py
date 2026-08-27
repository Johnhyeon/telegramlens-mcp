"""미국 종목 사전 (TL-03).

실측(UAT): 고정 미국 15티커가 모두 종목 사전에 없어, 조회 결과 0건이
"실제 무언급"인지 "미국 미지원"인지 구분할 수 없었다.

사전은 두 겹이다:
1) 내장 시드 - 텔레그램에서 실제로 불리는 주요 티커와 한글 통용명.
   오프라인에서도 PLTR·RIVN·CRSP 같은 핵심이 항상 식별된다.
2) SEC company_tickers.json - 전체 티커·클래스주(주 1회 캐시, 실패해도
   시드로 동작). StockLens 의 SEC 연동과 같은 공개 엔드포인트다.

여기의 "지원"은 **식별**이 된다는 뜻이다. 언급 수집은 도입 시점 이후의
메시지부터 쌓이므로, 지원 종목의 0건은 무언급이지 미지원이 아니다.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

# 티커 -> (회사명, 한글 통용명들). 클래스주는 같은 회사명을 쓴다.
US_SEED: dict[str, tuple[str, tuple[str, ...]]] = {
    "AAPL": ("Apple Inc.", ("애플",)),
    "MSFT": ("Microsoft Corporation", ("마이크로소프트", "마소")),
    "GOOGL": ("Alphabet Inc.", ("알파벳", "구글")),
    "GOOG": ("Alphabet Inc.", ()),
    "AMZN": ("Amazon.com, Inc.", ("아마존",)),
    "NVDA": ("NVIDIA Corporation", ("엔비디아",)),
    "META": ("Meta Platforms, Inc.", ("메타",)),
    "TSLA": ("Tesla, Inc.", ("테슬라",)),
    "BRK.B": ("Berkshire Hathaway Inc.", ("버크셔", "버크셔해서웨이")),
    "AVGO": ("Broadcom Inc.", ("브로드컴",)),
    "AMD": ("Advanced Micro Devices, Inc.", ("에이엠디",)),
    "INTC": ("Intel Corporation", ("인텔",)),
    "TSM": ("Taiwan Semiconductor Manufacturing", ("티에스엠씨",)),
    "PLTR": ("Palantir Technologies Inc.", ("팔란티어",)),
    "RIVN": ("Rivian Automotive, Inc.", ("리비안",)),
    "LCID": ("Lucid Group, Inc.", ("루시드",)),
    "CRSP": ("CRISPR Therapeutics AG", ("크리스퍼", "크리스퍼테라퓨틱스")),
    "COIN": ("Coinbase Global, Inc.", ("코인베이스",)),
    "MSTR": ("MicroStrategy Incorporated", ("마이크로스트래티지",)),
    "NFLX": ("Netflix, Inc.", ("넷플릭스",)),
    "DIS": ("The Walt Disney Company", ("디즈니",)),
    "UBER": ("Uber Technologies, Inc.", ("우버",)),
    "ABNB": ("Airbnb, Inc.", ("에어비앤비",)),
    "SNOW": ("Snowflake Inc.", ("스노우플레이크",)),
    "ARM": ("Arm Holdings plc", ("에이알엠",)),
    "SMCI": ("Super Micro Computer, Inc.", ("슈퍼마이크로",)),
    "MU": ("Micron Technology, Inc.", ("마이크론",)),
    "QCOM": ("QUALCOMM Incorporated", ("퀄컴",)),
    "LLY": ("Eli Lilly and Company", ("일라이릴리",)),
    "NVO": ("Novo Nordisk A/S", ("노보노디스크",)),
    "JPM": ("JPMorgan Chase & Co.", ("제이피모건", "JP모건")),
    "XOM": ("Exxon Mobil Corporation", ("엑슨모빌",)),
    "IONQ": ("IonQ, Inc.", ("아이온큐",)),
    "RGTI": ("Rigetti Computing, Inc.", ("리게티",)),
    "SOFI": ("SoFi Technologies, Inc.", ("소파이",)),
    "HOOD": ("Robinhood Markets, Inc.", ("로빈후드",)),
    "SPY": ("SPDR S&P 500 ETF Trust", ()),
    "QQQ": ("Invesco QQQ Trust", ("큐큐큐",)),
    "ALL": ("The Allstate Corporation", ()),
}

# 일반 영어 단어와 겹치는 티커 - 문맥 가드 없이 bare 매칭하면 안 된다(요구 3).
COMMON_WORD_TICKERS = {
    "ALL", "CARS", "FAST", "GOOD", "IT", "ON", "SO", "ARE", "FOR", "NOW",
    "OPEN", "PLAY", "REAL", "RUN", "SEE", "TWO", "BIG", "COST", "EAT",
    "FUN", "HAS", "HE", "LOVE", "MAIN", "NICE", "OUT", "SAFE", "TEAM",
    "WELL", "YOU", "AN", "A", "ARM", "KEY", "META",
}

# 미국 bare 티커를 인정하는 시장 문맥어(요구 3).
MARKET_CONTEXT_WORDS = (
    "$", "나스닥", "nasdaq", "nyse", "미국", "미장", "주가", "실적", "티커",
    "프리장", "애프터", "pre-market", "earnings",
)

_FOREIGN_SUFFIX_RE = re.compile(
    r"^[A-Z0-9]{1,6}\.(T|HK|SS|SZ|L|PA|DE|TO|AX|KS|KQ)$")

_CACHE_TTL = 7 * 24 * 3600
_mem: dict = {"at": 0.0, "map": None}


def _cache_file() -> Path:
    from telegram_lens.config import data_dir

    return Path(data_dir()) / "us_tickers.json"


def _seed_map() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ticker, (name, aliases) in US_SEED.items():
        out[ticker] = {"ticker": ticker, "name": name, "aliases": list(aliases)}
    return out


def load_us_map(refresh: bool = False) -> dict[str, dict]:
    """티커 -> {ticker, name, aliases}. 시드 + (가능하면) SEC 전체 목록.

    SEC 조회가 안 되는 환경에서도 시드만으로 동작한다. 시드의 한글 별칭은
    SEC 데이터 위에 항상 덮어쓴다.
    """
    now = time.monotonic()
    if _mem["map"] is not None and not refresh and now - _mem["at"] < 3600:
        return _mem["map"]

    table = _seed_map()
    try:
        cache = _cache_file()
        data = None
        if cache.exists() and (time.time() - cache.stat().st_mtime) < _CACHE_TTL:
            data = json.loads(cache.read_text(encoding="utf-8"))
        if data is None:
            import httpx

            resp = httpx.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": "TelegramLens (whdqja216772@gmail.com)"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        for row in data.values():
            ticker = str(row.get("ticker") or "").upper()
            if ticker and ticker not in table:
                table[ticker] = {"ticker": ticker,
                                 "name": row.get("title") or "", "aliases": []}
    except Exception:
        pass   # 오프라인 - 시드로 동작

    _mem["map"] = table
    _mem["at"] = now
    return table


def is_unsupported_market(query: str) -> bool:
    """거래소 접미사가 붙은 외국(미국 외) 티커 표기 - 우리가 지원하지 않는 시장."""
    return bool(_FOREIGN_SUFFIX_RE.match((query or "").strip().upper()))


def resolve_us(query: str) -> dict | None:
    """질의 -> 미국 종목. 못 찾으면 None(조회 실패가 아니라 사전에 없음)."""
    q = (query or "").strip()
    if not q or is_unsupported_market(q):
        return None
    table = load_us_map()
    upper = q.upper()
    if upper in table:
        info = table[upper]
        return {"ticker": info["ticker"], "name": info["name"], "matched_by": "ticker"}
    low = q.lower()
    for info in table.values():
        if any(low == a.lower() for a in info["aliases"]):
            return {"ticker": info["ticker"], "name": info["name"],
                    "matched_by": "alias"}
    # 회사명 앞부분 일치(4자 이상만 - "The" 같은 접두로 오탐 방지)
    if len(low) >= 4:
        for info in table.values():
            name_low = (info["name"] or "").lower()
            if name_low.startswith(low) or f" {low}" in f" {name_low}":
                if low in name_low.split(",")[0]:
                    return {"ticker": info["ticker"], "name": info["name"],
                            "matched_by": "name"}
    return None


def alias_terms() -> list[tuple[str, str]]:
    """추출용 (매칭어, 티커) 목록 - 한글 통용명만(2자 이상)."""
    out = []
    for ticker, (_name, aliases) in US_SEED.items():
        for alias in aliases:
            if len(alias) >= 2:
                out.append((alias, ticker))
    return out
