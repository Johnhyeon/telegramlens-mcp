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


# 이름 주변에 이 말이 붙으면 종목이 아니라 출처(발행사) 문맥이다(요구 2).
_ISSUER_HINTS = ("리서치", "리포트", "레포트", "보고서", "작성자", "애널리스트", "출처")


def _fp_samples(conn, code: str, name: str, cut: str, limit: int = 3) -> list[dict]:
    """후보의 대표 표본 - 문맥·채널·시각·유형·링크(요구 1).

    문맥 없이 이름과 건수만 보면 안전한 차단 결정을 내릴 수 없다.
    코드 동반이 없는(=차단 시 사라질) 언급만 표본으로 뽑는다.
    """
    rows = conn.execute(
        """
        SELECT m.text, m.date, m.msg_id, m.msg_type,
               c.title AS ch_title, c.username AS ch_username
        FROM mentions men
        JOIN messages m ON m.id = men.message_id
        LEFT JOIN channels c ON c.id = men.channel_id
        WHERE men.code = ? AND men.date >= ?
          AND m.text NOT LIKE '%' || ? || '%'
        ORDER BY m.date DESC LIMIT ?
        """,
        (code, cut, code, limit),
    ).fetchall()
    samples = []
    for r in rows:
        text = r["text"] or ""
        idx = text.find(name)
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(text), idx + len(name) + 40)
            context = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
        else:
            context = text[:80]
        issuer_context = any(
            hint in text[max(0, idx - 12): idx + len(name) + 16]
            for hint in _ISSUER_HINTS
        ) if idx >= 0 else False
        samples.append({
            "context": context,
            "channel": r["ch_title"] or str(r["ch_username"] or ""),
            "date": r["date"],
            "msg_type": r["msg_type"],
            "link": (f"https://t.me/{r['ch_username']}/{r['msg_id']}"
                     if r["ch_username"] else None),
            "issuer_context": issuer_context,
        })
    return samples


def false_positive_candidates(
    days: float = 7, max_name_len: int = 3, min_count: int = 3, top: int = 40
) -> list[dict]:
    """코드 동반 없이 이름만으로 자주 잡힌 종목명 → 오탐 후보.

    의심도는 코드 동반 비율 하나로 정하지 않는다(TL-02 요구 3). 실측에서
    LG전자·에코프로비엠처럼 정상적인 이름 언급도 코드가 없다는 이유만으로
    1.0 이 됐다. 정식 상장명인지, 이름 길이, 채널 다양성, 같은 문장의 반복
    여부를 함께 본다 - 여러 채널에서 서로 다른 문장으로 불리는 이름은 은어
    충돌보다 실제 종목일 가능성이 높다.
    """
    cut = _cutoff(days)
    ambiguous = load_ambiguous()
    official_names = load_stocks()

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT men.code, men.name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN m.text LIKE '%' || men.code || '%' THEN 1 ELSE 0 END)
                       AS code_confirmed,
                   COUNT(DISTINCT men.channel_id) AS channels,
                   COUNT(DISTINCT m.text_sig) AS distinct_sigs,
                   COUNT(DISTINCT m.text) AS distinct_texts
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
            total = max(r["total"], 1)
            base = name_only / total
            distinct = max(r["distinct_sigs"] or 0, r["distinct_texts"] or 0)
            distinct_ratio = round(distinct / total, 2)
            official = official_names.get(r["code"]) == r["name"]

            samples = _fp_samples(conn, r["code"], r["name"], cut)
            issuer_hits = sum(1 for s in samples if s.get("issuer_context"))

            factor = 1.0
            if official and len(r["name"]) >= 3:
                factor *= 0.6          # 정식 상장명은 임의 은어보다 충돌 확률이 낮다
            if len(r["name"]) >= 4:
                factor *= 0.7          # 길수록 일반명사와 안 겹친다
            elif len(r["name"]) <= 2:
                factor *= 1.3          # 두 글자는 거의 항상 무언가와 겹친다
            if (r["channels"] or 0) >= 3:
                factor *= 0.75         # 여러 채널이 제각각 부르면 실제 종목 신호
            if distinct_ratio >= 0.5 and total >= 3:
                factor *= 0.85         # 복붙 반복이 아니라 서로 다른 문장
            suspicion = round(min(1.0, base * factor), 2)
            review = ("차단 검토" if suspicion >= 0.8
                      else "표본 확인" if suspicion >= 0.5 else "정상 가능성")

            out.append({
                "code": r["code"],
                "name": r["name"],
                "name_only_hits": name_only,
                "code_confirmed_hits": r["code_confirmed"] or 0,
                "code_absent_ratio": round(base, 2),
                "signals": {
                    "official_name": official,
                    "name_length": len(r["name"]),
                    "distinct_channels": r["channels"] or 0,
                    "distinct_text_ratio": distinct_ratio,
                    "issuer_context_in_samples": issuer_hits,
                },
                "suspicion": suspicion,
                "review": review,
                "samples": samples,
            })
    out.sort(key=lambda x: (x["suspicion"], x["name_only_hits"]), reverse=True)
    return out[:top]


def block_preview(code: str, days: float = 30) -> dict:
    """차단하면 최근 집계에서 빠질 언급 수와 표본(요구 4). 아무것도 쓰지 않는다."""
    cut = _cutoff(days)
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n, MAX(men.name) AS name
            FROM mentions men JOIN messages m ON m.id = men.message_id
            WHERE men.code = ? AND men.date >= ?
              AND m.text NOT LIKE '%' || ? || '%'
            """,
            (code, cut, code),
        ).fetchone()
        name = row["name"] or code
        samples = _fp_samples(conn, code, name, cut)
    return {"code": code, "name": name, "days": days,
            "would_exclude": row["n"] or 0, "samples": samples}


def purge_name_only_mentions(code: str, days: float = 30) -> int:
    """차단 적용 시 이름 단독 언급 행을 집계에서 제거한다.

    mentions 는 messages 에서 파생된 인덱스라 지워도 원문은 남고, 정책이
    바뀌면 재추출로 복원된다. block_preview 와 **같은 조건**을 쓰므로
    dry-run 과 실제 제외 건수가 일치한다(수용 3).
    """
    cut = _cutoff(days)
    with db.connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM mentions WHERE id IN (
                SELECT men.id FROM mentions men
                JOIN messages m ON m.id = men.message_id
                WHERE men.code = ? AND men.date >= ?
                  AND m.text NOT LIKE '%' || ? || '%'
            )
            """,
            (code, cut, code),
        )
        conn.commit()
        return cur.rowcount


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
