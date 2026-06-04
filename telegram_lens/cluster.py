"""중복제거 + 원본추적 — 같은 글의 포워드/복붙 파생본을 한 클러스터로 묶는다.

trending/buzz 가 raw 메시지 단위로 세면 같은 찌라시가 30개 방에 퍼진 걸 30건으로
과대 집계한다. 클러스터 단위(COUNT DISTINCT cluster_id)로 세면 '독립 언급 수'가 되고,
복사본 수·포워드 수는 따로 '확산 강도'로 보존한다.

두 경로:
  1. 포워드 메타(fwd_from_chat_id, fwd_from_message_id) → 원본 키로 즉시 수렴. 정확.
     수집 시점에 canonical_key 로 cluster_id 를 박는다(db._migrate 가 기존분도 SQL 백필).
  2. 포워드 메타 없는 복붙(찌라시 복사) → 정규화 텍스트 서명(text_sig)이 같고 30분
     이내인 메시지를 가장 이른 메시지의 cluster_id 로 병합(merge_heuristic_duplicates).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram_lens import db

# 정규화에서 제거할 것들: URL, 이모지/기호, 공백. 한글·영숫자만 남겨 '본문 동일성'만 본다.
_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+")
# 한글·영문·숫자 외 전부 제거(이모지·구두점·공백 포함). 복붙 시 흔한 머리말/꼬리말 장식 무시.
_KEEP_RE = re.compile(r"[^0-9a-z가-힣]")

# 서명을 만들기에 너무 짧은 정규화 길이(짧은 인사·한 단어 글이 우연히 같아 오병합되는 것 방지).
_MIN_SIG_LEN = 20


def canonical_key(
    channel_id: int,
    msg_id: int,
    fwd_chat_id: int | None,
    fwd_msg_id: int | None,
) -> str:
    """클러스터 정규 키. 포워드면 원본 키, 아니면 자기 자신이 원본.

    원본(o:A:100)과 그 포워드(fwd=A:100 → o:A:100)가 같은 키로 수렴한다.
    """
    if fwd_chat_id and fwd_msg_id:
        return f"o:{fwd_chat_id}:{fwd_msg_id}"
    return f"o:{channel_id}:{msg_id}"


def _normalize(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = _URL_RE.sub("", t)
    return _KEEP_RE.sub("", t)


def text_signature(text: str) -> str | None:
    """정규화 텍스트의 짧은 해시. 너무 짧으면(<20자) None(오병합 방지).

    공백·URL·이모지·구두점만 다른 두 글은 같은 서명을 갖는다 → 복붙 중복 탐지.
    """
    norm = _normalize(text)
    if len(norm) < _MIN_SIG_LEN:
        return None
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def merge_heuristic_duplicates(
    conn: sqlite3.Connection,
    window_min: int = 30,
    since_iso: str | None = None,
) -> int:
    """포워드로 귀속되지 않은 복붙 중복을 텍스트 서명 + 시간창으로 병합.

    같은 text_sig 그룹 안에서 시간순으로 훑어, 30분 이내 연쇄로 이어지는 메시지들을
    가장 이른 메시지의 cluster_id 로 재할당한다. 이미 포워드로 다른 원본에 귀속된
    메시지(cluster_id != 자기 self 키)는 건드리지 않는다.

    동일 정규화 텍스트 → 추출 종목도 동일하므로 '동일 종목' 조건은 사실상 내포된다.
    text_sig 인덱스로 그룹을 끌어와 O(n²) 전체 비교를 피한다.

    Args:
        window_min: 연쇄 병합 허용 시간 간격(분).
        since_iso: 이 시각 이후 메시지만 대상(평상시 최근 구간만 처리해 저렴하게).

    반환: cluster_id 가 바뀐(병합된) 메시지 수.
    """
    cut = since_iso or (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()

    # 후보: 대상 구간에서 text_sig 가 2건 이상인 서명만(혼자면 병합할 게 없음).
    dup_sigs = [
        r["text_sig"]
        for r in conn.execute(
            """
            SELECT text_sig FROM messages
            WHERE text_sig IS NOT NULL AND date >= ?
            GROUP BY text_sig HAVING COUNT(*) > 1
            """,
            (cut,),
        )
    ]
    if not dup_sigs:
        return 0

    window = timedelta(minutes=window_min)
    merged = 0
    for sig in dup_sigs:
        rows = conn.execute(
            """
            SELECT id, channel_id, msg_id, date, cluster_id,
                   fwd_from_chat_id, fwd_from_message_id
            FROM messages
            WHERE text_sig = ? AND date >= ?
            ORDER BY date ASC
            """,
            (sig, cut),
        ).fetchall()

        anchor_cluster: str | None = None
        anchor_time: datetime | None = None
        for r in rows:
            # 이미 포워드로 다른 원본에 귀속된 메시지는 그 귀속을 존중(건너뜀).
            is_forward = r["fwd_from_chat_id"] and r["fwd_from_message_id"]
            try:
                t = datetime.fromisoformat(r["date"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if anchor_cluster is None or anchor_time is None:
                # 그룹의 첫 메시지 = 원본 앵커.
                anchor_cluster = r["cluster_id"]
                anchor_time = t
                continue

            if t - anchor_time > window:
                # 시간창을 벗어남 → 새 앵커로 리셋(다른 시점의 동일 텍스트는 별개 사건).
                anchor_cluster = r["cluster_id"]
                anchor_time = t
                continue

            # 시간창 안의 복붙 → 앵커 클러스터로 병합(포워드 귀속본은 제외).
            if not is_forward and r["cluster_id"] != anchor_cluster:
                db.update_cluster_id(conn, r["id"], anchor_cluster)
                merged += 1
            # 연쇄 병합: 마지막으로 본 시각을 앵커 시각으로 전진시켜 30분 연쇄를 잇는다.
            anchor_time = t

    return merged


def merge_same_channel_bursts(
    conn: sqlite3.Connection,
    window_min: int = 30,
    since_iso: str | None = None,
) -> int:
    """같은 채널이 같은 종목(집합)을 짧은 시간에 반복 게시한 '버스트'를 한 클러스터로 병합.

    text_sig 병합(exact 복붙)이 못 잡는 케이스: 한 채널이 같은 이슈를 *출처만 다른 헤드라인*
    으로 20분 새 7~8번 올리면 본문이 조금씩 달라(서명도 다) 독립 언급으로 뻥튀기된다.
    스펙 2-1의 '30분 이내 + 동일 종목' 경로 — 같은 채널 + 동일 mention 집합 + 시간창이면
    가장 이른 메시지의 cluster_id 로 묶는다. 한 채널의 한 burst = 한 source 로 본다.

    교차 채널 확산(여러 방이 같은 글)은 건드리지 않는다(그건 text_sig/forward 가 담당하고,
    오히려 '확산'은 보존해야 할 신호). 여기서 줄이는 건 '단일 채널의 자기 반복'뿐.

    반환: cluster_id 가 바뀐(병합된) 메시지 수.
    """
    cut = since_iso or (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    rows = conn.execute(
        """
        SELECT m.id, m.channel_id, m.date, m.cluster_id,
               (SELECT group_concat(men.code) FROM mentions men
                WHERE men.message_id = m.id) AS codes
        FROM messages m
        WHERE m.date >= ?
          AND EXISTS (SELECT 1 FROM mentions men WHERE men.message_id = m.id)
        ORDER BY m.channel_id, m.date ASC
        """,
        (cut,),
    ).fetchall()

    # (채널, 종목코드 집합) → 시간순 메시지. 같은 키 안에서 시간창 연쇄를 묶는다.
    from collections import defaultdict

    groups: dict = defaultdict(list)
    for r in rows:
        codeset = frozenset((r["codes"] or "").split(","))
        if not codeset:
            continue
        groups[(r["channel_id"], codeset)].append(r)

    window = timedelta(minutes=window_min)
    merged = 0
    for msgs in groups.values():
        if len(msgs) < 2:
            continue
        anchor_cluster: str | None = None
        anchor_time: datetime | None = None
        for r in msgs:
            try:
                t = datetime.fromisoformat(r["date"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if anchor_cluster is None or anchor_time is None or t - anchor_time > window:
                anchor_cluster = r["cluster_id"]
                anchor_time = t
                continue
            if r["cluster_id"] != anchor_cluster:
                db.update_cluster_id(conn, r["id"], anchor_cluster)
                merged += 1
            anchor_time = t  # 연쇄 — 마지막 본 시각으로 전진(30분 연쇄 유지)
    return merged


def backfill_text_sig(
    conn: sqlite3.Connection, since_iso: str, limit: int = 5000
) -> int:
    """업그레이드 이전 메시지(text_sig NULL)에 서명을 채운다. 반환: 채운 건수."""
    rows = db.messages_missing_text_sig(conn, since_iso, limit)
    n = 0
    for r in rows:
        sig = text_signature(r["text"])
        if sig is not None:
            db.set_text_sig(conn, r["id"], sig)
            n += 1
    return n
