"""장 종료 후 일별 등락률 — 서버포맷 버즈 종목 옆에 붙일 **정규장** 등락률(%).

TL 은 텔레그램 전용이지만, 서버포맷 버즈(_ready)는 Claude 를 안 거치므로 등락률도 '서버에서'
붙여야 한다(SL 은 별도 프로세스라 직접 호출 불가, Claude 가 중계하면 또 깨짐). 네이버 시세
polling API 한 번(배치)으로 받는다. 장중 호출 여부는 server.py 의 시간 게이트가 결정한다.

2026-09-14 KRX 애프터마켓(16:00~20:00) 이후 polling 시세는 16시부터 애프터마켓 체결로 바뀐다
(marketSessionType=afterMarket, 2026-09-15 16:04 실측). 그 등락률을 종가 등락률로 붙이면
틀리므로, 애프터마켓 종목은 네이버 분봉의 15:30 봉(정규장 종가)으로 기준가 대비 등락률을
다시 계산한다. 15:30 봉을 못 구하면 그 종목은 등락률을 붙이지 않는다(추정하지 않는다).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx

_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/"
_MINUTE_URL = "https://api.stock.naver.com/chart/domestic/item/{code}/minute"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def regular_close(code: str, day: str) -> float | None:
    """그 날(YYYYMMDD) 15:30 봉 체결가 = 정규장 종가. 봉이 없거나 조회가 실패하면 None."""
    try:
        resp = httpx.get(
            _MINUTE_URL.format(code=code),
            params={"startDateTime": f"{day}1520", "endDateTime": f"{day}1530"},
            headers=_HEADERS,
            timeout=10.0,
        )
        rows = resp.json()
    except Exception:  # noqa: BLE001 — 시세 실패가 브리핑을 막으면 안 됨
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("localDateTime") or "") == f"{day}153000":
            return _num(row.get("currentPrice"))
    return None


def daily_change(codes: list[str]) -> dict[str, float]:
    """종목코드 리스트 → {code: 정규장 등락률(%)}. 실패/빈 입력이면 {} (버즈는 등락률 없이 그대로 출력).

    정규장 세션 시세면 fluctuationsRatioRaw(기준가 대비, 부호 포함)를 그대로 쓴다. 애프터마켓
    시세면 기준가(= 현재가 - 전일대비)와 15:30 정규장 종가로 다시 계산한다.
    시세 실패가 브리핑을 막으면 안 되므로 모든 예외를 삼키고 가능한 것만 반환한다.
    """
    uniq = [c for c in dict.fromkeys(codes) if c]  # 순서 유지 + 중복 제거
    if not uniq:
        return {}
    out: dict[str, float] = {}
    pending: list[tuple[str, float, str]] = []  # (code, 기준가, YYYYMMDD)
    for i in range(0, len(uniq), 30):  # 과도한 URL 길이 방지 — 30개씩
        chunk = uniq[i : i + 30]
        try:
            resp = httpx.get(_URL + ",".join(chunk), headers=_HEADERS, timeout=10.0)
            rows = resp.json().get("datas", [])
        except Exception:  # noqa: BLE001 — 시세 실패가 브리핑을 막으면 안 됨
            continue
        for it in rows:
            code = str(it.get("itemCode") or "")
            if not code:
                continue
            session = it.get("marketSessionType")
            if session in (None, "regularMarket"):
                rt = _num(it.get("fluctuationsRatioRaw"))
                if rt is not None:
                    out[code] = rt
                continue
            close = _num(it.get("closePriceRaw"))
            diff = _num(it.get("compareToPreviousClosePriceRaw"))
            day = str(it.get("localTradedAt") or "")[:10].replace("-", "")
            if close is None or diff is None or len(day) != 8 or not day.isdigit():
                continue
            base = close - diff
            if base > 0:
                pending.append((code, base, day))
    if pending:
        with ThreadPoolExecutor(max_workers=8) as ex:
            closes = list(ex.map(lambda p: regular_close(p[0], p[2]), pending))
        for (code, base, _), reg in zip(pending, closes):
            if reg is not None:
                out[code] = round((reg - base) / base * 100, 2)
    return out
