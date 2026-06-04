"""기존 DB 1회 재색인 — `telegramlens-reindex`.

종목 사전·별칭·차단어·태깅 규칙·tier 휴리스틱을 바꾼 뒤, **이미 수집된 과거 메시지**에
그 변경을 소급 적용한다. 추출(mentions)은 수집 시점에 한 번 계산돼 저장되므로, 사전/규칙
변경은 재색인 없이는 과거 데이터에 반영되지 않는다(예: '하이닉스' 별칭 추가가 과거 '이닉스'
오탐을 자동으로 고치지 못함).

이 도구는 텔레그램 접속이 필요 없다(DB 만 다룬다). 한 트랜잭션으로 처리한다:
  1. 스키마 마이그레이션(init_db) + tier 휴리스틱 재시드
  2. mentions 전량 재추출(현재 사전/별칭/차단어 기준)
  3. 메시지별 sentiment / msg_type / text_sig / cluster_id 재계산
  4. 복붙 중복 휴리스틱 병합(전체 기간)
  5. 종목 베이스라인 재계산

데몬이 도는 중이어도 안전하다(WAL + busy_timeout). 다만 '옛 코드' 데몬이 계속 돌면
새로 수집되는 메시지는 태그가 비어 들어오므로, 코드 업그레이드 후 데몬을 재시작하는 것이
정석이다(재색인은 그 시점까지의 과거 데이터를 정리하는 용도).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3

from telegram_lens import cluster, db
from telegram_lens.config import data_dir
from telegram_lens.extract import extract_mentions, reset_index
from telegram_lens.tagging import seed_channel_tiers, tag_msg_type, tag_sentiment

_LOG = logging.getLogger("telegramlens.reindex")


def reindex(
    conn: sqlite3.Connection,
    baseline_days: int = 7,
    merge_window_min: int = 30,
) -> dict:
    """열린 커넥션에서 전체 재색인 수행. 반환: 요약 통계.

    호출측이 트랜잭션(commit)을 책임진다(db.connect 컨텍스트가 종료 시 commit).
    """
    reset_index()  # 사전/별칭/차단어 캐시 무효화 — 최신 규칙으로 추출

    # tier 강제 재분류(only_missing=False) — 재색인은 '현재 규칙을 과거 전체에 재적용'이
    # 목적이므로 기존 heuristic 분류도 갱신한다(수동 분류 source='manual' 은 보존).
    # 태깅의 msg_type 이 tier(gossip 등)를 쓰므로 재추출 전에 먼저 갱신해야 한다.
    seed_channel_tiers(conn, only_missing=False)
    tier_map = db.channel_tiers(conn)

    msgs = conn.execute(
        """
        SELECT id, channel_id, msg_id, date, text,
               fwd_from_chat_id, fwd_from_message_id
        FROM messages
        """
    ).fetchall()

    # mentions 전량 재추출 — 사전/별칭/차단어 변경을 과거에 소급.
    conn.execute("DELETE FROM mentions")
    n_mentions = 0
    for m in msgs:
        mentions = extract_mentions(m["text"])
        tier = (tier_map.get(m["channel_id"]) or {}).get("tier")
        conn.execute(
            "UPDATE messages SET sentiment=?, msg_type=?, text_sig=?, cluster_id=? WHERE id=?",
            (
                tag_sentiment(m["text"]),
                tag_msg_type(m["text"], len(mentions), tier),
                cluster.text_signature(m["text"]),
                cluster.canonical_key(
                    m["channel_id"], m["msg_id"],
                    m["fwd_from_chat_id"], m["fwd_from_message_id"],
                ),
                m["id"],
            ),
        )
        if mentions:
            db.insert_mentions(conn, m["id"], m["channel_id"], m["date"], mentions)
            n_mentions += len(mentions)

    merged = cluster.merge_heuristic_duplicates(
        conn, window_min=merge_window_min, since_iso="2000-01-01"
    )
    # 같은 채널의 동일 종목 반복 버스트도 묶는다(한 채널의 자기 반복 = 한 source).
    merged += cluster.merge_same_channel_bursts(
        conn, window_min=merge_window_min, since_iso="2000-01-01"
    )
    n_base = db.compute_baselines(conn, days=baseline_days)

    return {
        "messages": len(msgs),
        "mentions": n_mentions,
        "clusters_merged": merged,
        "baselines": n_base,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        prog="telegramlens-reindex",
        description="기존 DB를 현재 사전/규칙으로 1회 재색인(재추출·재태깅·재클러스터).",
    )
    p.add_argument("--baseline-days", type=int, default=7, help="베이스라인 윈도우(일).")
    p.add_argument(
        "--merge-window-min", type=int, default=30, help="복붙 병합 시간창(분)."
    )
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()  # 필요 시 스키마 마이그레이션
    _LOG.info("재색인 시작 (텔레그램 접속 없음, DB만 처리)…")
    with db.connect() as conn:
        result = reindex(
            conn,
            baseline_days=args.baseline_days,
            merge_window_min=args.merge_window_min,
        )
    # 데몬의 text_sig 1회 백필 마커를 세워 중복 작업 방지(reindex 가 이미 전량 채움).
    try:
        (data_dir() / "text_sig_backfilled").write_text("reindex", encoding="utf-8")
    except OSError:
        pass
    _LOG.info(
        "재색인 완료 — 메시지 %d, mentions %d, 병합 %d, 베이스라인 %d",
        result["messages"], result["mentions"],
        result["clusters_merged"], result["baselines"],
    )


if __name__ == "__main__":
    main()
