"""장 종료 후 일별 등락률 — 서버포맷 버즈 종목 옆에 붙일 종가 등락률(%).

TL 은 텔레그램 전용이지만, 서버포맷 버즈(_ready)는 Claude 를 안 거치므로 등락률도 '서버에서'
붙여야 한다(SL 은 별도 프로세스라 직접 호출 불가, Claude 가 중계하면 또 깨짐). 네이버 시세
polling API 한 번(배치)으로 받는다. 장중 호출 여부는 server.py 의 시간 게이트가 결정한다.
"""

from __future__ import annotations

import httpx

_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/"


def daily_change(codes: list[str]) -> dict[str, float]:
    """종목코드 리스트 → {code: 등락률(%)}. 실패/빈 입력이면 {} (버즈는 등락률 없이 그대로 출력).

    네이버 batch(콤마구분)로 받는다. fluctuationsRatioRaw = 전일 대비 등락률(부호 포함).
    시세 실패가 브리핑을 막으면 안 되므로 모든 예외를 삼키고 가능한 것만 반환한다.
    """
    uniq = [c for c in dict.fromkeys(codes) if c]  # 순서 유지 + 중복 제거
    if not uniq:
        return {}
    out: dict[str, float] = {}
    for i in range(0, len(uniq), 30):  # 과도한 URL 길이 방지 — 30개씩
        chunk = uniq[i : i + 30]
        try:
            resp = httpx.get(
                _URL + ",".join(chunk),
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10.0,
            )
            for it in resp.json().get("datas", []):
                code = str(it.get("itemCode") or "")
                rt = it.get("fluctuationsRatioRaw")
                if not code or rt is None:
                    continue
                try:
                    out[code] = float(rt)
                except (TypeError, ValueError):
                    continue
        except Exception:  # noqa: BLE001 — 시세 실패가 브리핑을 막으면 안 됨
            continue
    return out
