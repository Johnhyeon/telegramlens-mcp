"""사전 후보 자동 발굴 — 오탐·별칭을 데이터가 알려주게 한다.

사전 관리를 수작업 사냥이 아니라 주간 리뷰로 바꾸는 게 목표.

- false_positive_candidates: 코드 없이 '이름만'으로 자주 잡힌 짧은 종목명.
  일반명사/은어 충돌 의심 → ambiguous_codes.json 후보.
- alias_candidates: 텍스트에 `이름(123456)` 형태로 나오는데 현재 사전으로는
  그 이름이 해당 코드로 매칭되지 않는 토큰. 코드가 정답을 알려주므로 고정밀
  → aliases.json 후보.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from telegram_lens import db
from telegram_lens.extract import extract_mentions
from telegram_lens.stocks import load_ambiguous, load_stocks

# 이름(123456) — 한국 증시 글에서 매우 흔한 표기. 고정밀 별칭 신호.
_NAME_CODE_RE = re.compile(r"([가-힣A-Za-z][가-힣A-Za-z0-9]{1,9})\s*\(\s*(\d{6})\s*\)")


def _cutoff(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def false_positive_candidates(
    days: float = 7, max_name_len: int = 3, min_count: int = 3, top: int = 40
) -> list[dict]:
    """코드 동반 없이 이름만으로 잡힌 짧은 종목명 → 오탐 후보."""
    cut = _cutoff(days)
    ambiguous = load_ambiguous()

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT men.code, men.name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN m.text LIKE '%' || men.code || '%' THEN 1 ELSE 0 END)
                       AS code_confirmed
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            WHERE men.date >= ?
            GROUP BY men.code
            """,
            (cut,),
        ).fetchall()

    out = []
    for r in rows:
        if r["code"] in ambiguous:
            continue
        if len(r["name"]) > max_name_len:
            continue
        name_only = r["total"] - (r["code_confirmed"] or 0)
        if name_only < min_count:
            continue
        # 코드와 한 번도 함께 안 나온 짧은 이름일수록 오탐 가능성↑
        out.append(
            {
                "code": r["code"],
                "name": r["name"],
                "name_only_hits": name_only,
                "code_confirmed_hits": r["code_confirmed"] or 0,
                "suspicion": round(name_only / max(r["total"], 1), 2),
            }
        )
    out.sort(key=lambda x: (x["suspicion"], x["name_only_hits"]), reverse=True)
    return out[:top]


def alias_candidates(days: float = 7, min_count: int = 2, top: int = 40) -> list[dict]:
    """`이름(코드)` 표기에서 현재 사전이 못 잡는 토큰 → 별칭 후보."""
    cut = _cutoff(days)
    by_code = load_stocks()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT text FROM messages WHERE date >= ?", (cut,)
        ).fetchall()

    counter: dict[tuple[str, str], int] = {}
    for r in rows:
        for token, code in _NAME_CODE_RE.findall(r["text"]):
            if code not in by_code:
                continue
            official = by_code[code]
            if token == official:
                continue
            # 이미 현재 로직(이름/별칭)으로 이 코드가 잡히면 후보 아님
            extracted = dict(extract_mentions(token))
            if code in extracted:
                continue
            counter[(token, code)] = counter.get((token, code), 0) + 1

    out = [
        {
            "alias": token,
            "code": code,
            "official_name": by_code[code],
            "count": cnt,
        }
        for (token, code), cnt in counter.items()
        if cnt >= min_count
    ]
    out.sort(key=lambda x: x["count"], reverse=True)
    return out[:top]
