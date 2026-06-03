"""룰베이스 전처리 태깅 — 수집 시점에 추가 네트워크 없이 메시지 성격을 분류.

순수 함수 모듈(I/O 없음, seed_channel_tiers 만 DB 접근). sync 파이프라인이 메시지마다
호출한다. AI 추론이 아니라 키워드/패턴 규칙이라 빠르고 결정적이며, 잘못 잡혀도
설명 가능하다(원문 키워드가 곧 근거).

  - tag_sentiment : 긍정/부정/중립(혼재) — 키워드 사전
  - tag_msg_type  : report/breaking/gossip/chat — 키워드·시간패턴·채널 tier
  - seed_channel_tiers : 채널 title 휴리스틱으로 tier 초기 시드(수동 분류는 보존)
"""

from __future__ import annotations

import re
import sqlite3

from telegram_lens import db
from telegram_lens.stocks import load_source_firms, load_stocks

# ── 감성 키워드 ─────────────────────────────────────────────────────
_POS = ("상향", "급등", "돌파", "신고가", "수혜", "매수", "강세", "호실적")
_NEG = ("하향", "급락", "우려", "매도", "실망", "리스크", "약세", "부진")


def tag_sentiment(text: str) -> str:
    """positive / negative / neutral. 둘 다 있거나 둘 다 없으면 neutral(혼재)."""
    if not text:
        return "neutral"
    pos = any(k in text for k in _POS)
    neg = any(k in text for k in _NEG)
    if pos == neg:  # 둘 다 True(혼재) 또는 둘 다 False(무신호)
        return "neutral"
    return "positive" if pos else "negative"


# ── 메시지 유형 ─────────────────────────────────────────────────────
# 속보 시간 패턴(예: "09:31 속보", "[14:05]"). 분 단위까지 붙은 시각.
_TIME_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_REPORT_KEYWORDS = ("TP", "목표주가", "투자의견")

# 본문이 너무 짧고 종목코드도 없으면 잡담으로 본다(자 단위).
_CHAT_MAX_LEN = 100


def _has_brokerage(text: str) -> bool:
    """본문에 증권사명(사전상 ...증권/...투자증권)이 등장하는지 — report 신호.

    stocks.load_source_firms() 가 산출한 '인용 억제 대상(증권사 등)' 코드의 종목명을
    재사용한다(증권사 채널이 아니어도 본문에 증권사 리포트를 인용하면 report).
    """
    by_code = load_stocks()
    firm_names = [by_code[c] for c in load_source_firms() if c in by_code]
    return any(name in text for name in firm_names)


def tag_msg_type(text: str, code_count: int, tier: str | None) -> str:
    """report / breaking / gossip / chat.

    우선순위: gossip(채널 tier) > breaking > report > chat(fallback).
      - gossip : 채널이 찌라시 tier 로 분류된 경우(성격이 채널에 종속).
      - breaking: [속보]·❗️·시각 패턴.
      - report : TP/목표주가/투자의견·증권사명 인용·애널리스트/리서치 채널.
      - chat   : 종목코드 없음 + 짧은 본문. (그 외 미매칭도 chat 으로 보수 기록)
    """
    text = text or ""
    if tier == "gossip":
        return "gossip"
    if "[속보]" in text or "❗️" in text or _TIME_RE.search(text):
        return "breaking"
    if (
        any(k in text for k in _REPORT_KEYWORDS)
        or tier in ("analyst", "research")
        or _has_brokerage(text)
    ):
        return "report"
    if code_count == 0 and len(text) < _CHAT_MAX_LEN:
        return "chat"
    return "chat"  # fallback — 스펙이 4종만 정의. 가장 보수적으로 chat.


# ── 채널 tier 휴리스틱 시드 ─────────────────────────────────────────
# 증권사명(=analyst 후보). 변형 표기 포함. title 부분일치로 검사한다.
_BROKERAGES = (
    "키움", "미래에셋", "신한", "한국투자", "한투", "메리츠", "하나증권", "하나금투",
    "DB금투", "DB증권", "대신증권", "NH투자", "NH증권", "KB증권", "삼성증권",
    "유진투자", "교보증권", "하이투자", "SK증권", "IBK투자", "유안타", "다올투자",
    "다올증권", "현대차증권", "상상인", "BNK투자", "한양증권", "흥국증권", "케이프",
    "신영증권", "이베스트", "유화증권", "부국증권", "DAOL", "DB Tech",
)
# 독립리서치 신호(증권사명 없을 때). title 부분일치.
_RESEARCH_HINTS = ("리서치", "리포트", "레포트", "research", "insight", "리뷰")

_TIER_WEIGHTS = {
    "analyst": 1.0,
    "research": 0.8,
    "info": 0.5,
    "gossip": 0.3,
}


def tier_weight(tier: str) -> float:
    return _TIER_WEIGHTS.get(tier, 0.5)


def classify_tier(title: str | None) -> str:
    """채널 title 휴리스틱 → tier. gossip 은 title 만으론 신뢰 어려워 수동에 맡긴다.

    analyst(증권사 운영) > research(독립리서치 신호) > info(기본값).
    """
    t = title or ""
    if any(b in t for b in _BROKERAGES):
        return "analyst"
    low = t.lower()
    if any(h.lower() in low for h in _RESEARCH_HINTS):
        return "research"
    return "info"


def seed_channel_tiers(conn: sqlite3.Connection, only_missing: bool = True) -> int:
    """DB channels 를 휴리스틱으로 분류해 channel_tier 에 시드.

    source='manual'(대표가 수동 지정)은 절대 덮어쓰지 않는다. only_missing=True 면
    tier 행이 아예 없는 채널만 새로 채우고(평상시), False 면 기존 'heuristic' 행도
    재분류한다(사전/규칙 갱신 후 재시드용).

    반환: 새로 시드/갱신된 채널 수.
    """
    existing = db.channel_tiers(conn)
    channels = conn.execute("SELECT id, title FROM channels").fetchall()
    n = 0
    for ch in channels:
        cur = existing.get(ch["id"])
        if cur is not None:
            if cur.get("source") == "manual":
                continue  # 수동 분류 보존
            if only_missing:
                continue  # 이미 heuristic 행 있음 — 평상시엔 건드리지 않음
        tier = classify_tier(ch["title"])
        db.upsert_channel_tier(
            conn, ch["id"], tier, tier_weight(tier), source="heuristic", note=""
        )
        n += 1
    return n
