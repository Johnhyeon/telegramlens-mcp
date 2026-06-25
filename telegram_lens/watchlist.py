"""보유/관심 종목 watchlist — '내 종목 관리' 데이터.

~/.telegramlens/watchlist.json 에 종목코드+이름을 저장한다. 명령(!보유)과 아침 브리핑의
'내 종목' 섹션이 이걸 읽어 종목별 텔레그램 언급·해석을 정리한다(종가는 Claude 가 SL 로).
종목은 등록 시점에 resolve_code 로 해석해 코드로 저장(이후 재해석·모호성 회피).
"""

from __future__ import annotations

import json

from telegram_lens.config import watchlist_path
from telegram_lens.stocks import resolve_code


def load() -> list[dict]:
    """[{code, name}] 리스트. 없거나 깨졌으면 빈 리스트."""
    p = watchlist_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    items = data.get("stocks") if isinstance(data, dict) else data
    return [s for s in (items or []) if isinstance(s, dict) and s.get("code")]


def _save(stocks: list[dict]) -> None:
    try:
        watchlist_path().write_text(
            json.dumps({"stocks": stocks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def codes() -> list[str]:
    return [s["code"] for s in load()]


def set_stocks(queries: list[str]) -> tuple[list[dict], list[str]]:
    """watchlist 를 입력 목록으로 '교체'. 반환: (등록된 [{code,name}], 못 찾은 입력)."""
    resolved, failed, seen = [], [], set()
    for q in queries:
        code, name = resolve_code(q)
        if not code:
            failed.append(q)
        elif code not in seen:
            seen.add(code)
            resolved.append({"code": code, "name": name})
    _save(resolved)
    return resolved, failed


def add(query: str) -> tuple[dict | None, bool]:
    """한 종목 추가. 반환: ({code,name}|None, 이미_있었나)."""
    code, name = resolve_code(query)
    if not code:
        return None, False
    cur = load()
    if any(s["code"] == code for s in cur):
        return {"code": code, "name": name}, True
    cur.append({"code": code, "name": name})
    _save(cur)
    return {"code": code, "name": name}, False


def remove(query: str) -> dict | None:
    """한 종목 제거. 코드 해석 우선, 안 되면 이름 부분일치. 반환: 제거된 종목 | None."""
    code, _ = resolve_code(query)
    cur = load()
    if code:
        kept = [s for s in cur if s["code"] != code]
        target = next((s for s in cur if s["code"] == code), None)
    else:
        target = next((s for s in cur if query in (s.get("name") or "")), None)
        kept = [s for s in cur if s is not target]
    if target is None:
        return None
    _save(kept)
    return target
