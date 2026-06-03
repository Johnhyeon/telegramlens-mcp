"""수집 파이프라인 — Telethon fetch → 추출 → SQLite 저장.

수집 대상은 '가입된 모든 브로드캐스트 채널'이다. 매 사이클 dialogs 를 새로 훑으므로
새로 가입한 채널도 별도 등록 없이 자동으로 수집된다(allowlist 핀 없음). 종목과
무관한 채널이 섞여도 trending/momentum 은 종목 '언급' 기반이라 거의 영향이 없고,
오히려 종목코드가 안 붙는 거시·산업 글까지 모아 telegram_search(전문검색)의 사정권을
넓힌다. 채널별 종목 밀도는 telegram_classify_channels 로 따로 확인할 수 있다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from telegram_lens.client import fetch_recent, make_client, refresh_views
from telegram_lens import cluster, db
from telegram_lens.config import data_dir
from telegram_lens.extract import extract_mentions
from telegram_lens.tagging import seed_channel_tiers, tag_msg_type, tag_sentiment


# 처음 보는 채널을 1회 소급 백필할 깊이(분). 기본 3일.
_NEW_CHANNEL_BACKFILL_MIN = 3 * 1440

# 조회수 시계열 horizon → (min_age분, max_age분). max 는 '뒤늦게라도 한 번은 찍는' 슬랙.
# 게시 후 1h/6h/24h 시점의 views/forwards 를 한 번씩 snapshot 한다(확산 velocity 분석용).
_VIEW_HORIZONS: dict[str, tuple[float, float]] = {
    "1h": (60, 60 + 60),
    "6h": (360, 360 + 180),
    "24h": (1440, 1440 + 360),
}
# 정상 사이클(짧은 창)에서만 조회수 refresh 를 돌린다. 다운타임 캐치업/백필(큰 창)에선
# 메시지가 많아 FloodWait 위험이 커지므로 건너뛴다.
_VIEWS_REFRESH_MAX_WINDOW_MIN = 180
# 조회수 refresh 사이클당 메시지 상한. get_messages(ids) 는 채널당 100개 배치라 요청 수가
# 적어 FloodWait 위험이 낮다. 옛→새 데몬 전환 직후엔 collect-views 가 없는 백로그가 수천 건
# 생기는데, 너무 낮으면 6h 윈도우(폭 3h) 안에 다 못 돌아 일부가 24h 를 넘겨 영구 NULL 이
# 된다 → 호라이즌 안에 확실히 비우도록 상향.
_VIEWS_REFRESH_CAP = 400
# 베이스라인을 다시 계산할 주기(분). 이보다 오래됐으면 사이클 끝에 재계산.
_BASELINE_REFRESH_MIN = 360
# 복붙 중복 휴리스틱 병합을 적용할 최근 구간(시간). 교차 사이클 복사도 잡되 저렴하게.
_MERGE_WINDOW_HOURS = 6
# 업그레이드 이전 메시지 text_sig 1회 백필 깊이(일).
_SIG_BACKFILL_DAYS = 30


async def run_sync(
    minutes: int = 60,
    per_channel_limit: int = 500,
    new_channel_minutes: int = _NEW_CHANNEL_BACKFILL_MIN,
) -> dict:
    """최근 N분 메시지를 수집·저장. 요약 통계 반환.

    가입된 모든 브로드캐스트 채널을 대상으로 한다(새 채널 자동 포함). 그중 '처음 보는'
    채널(channels 테이블에 없는)은 new_channel_minutes(기본 3일)까지 1회 깊게 백필해
    가입 직후에도 최근 맥락이 들어오게 한다. 이후 사이클부터는 평소 창(minutes)만.

    수집 시점에 포워드 메타·조회수·룰베이스 태그(sentiment/msg_type)를 함께 저장하고,
    정상 사이클에선 게시 후 1h/6h/24h 조회수 snapshot 갱신과 종목 베이스라인 재계산도
    수행한다.
    """
    db.init_db()
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=minutes)
    # 신규 채널 백필 창은 평소 창보다 최소한 같거나 더 깊게.
    new_since = now - timedelta(minutes=max(new_channel_minutes, minutes))
    # 신규 채널은 더 깊게 긁으므로 채널당 상한도 넉넉히(날짜 경계가 먼저 멈춤).
    new_limit = max(per_channel_limit, 2000)

    # 이미 한 번이라도 수집해 본 채널 = '처음 보는' 채널 판별 기준.
    with db.connect() as conn:
        known_ids = {r["id"] for r in conn.execute("SELECT id FROM channels")}

    client = make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            from telegram_lens.client import NotLoggedInError

            raise NotLoggedInError(
                "로그인되어 있지 않습니다. `telegramlens-login` 을 먼저 실행하세요."
            )
        # channel_ids=None → 가입된 모든 브로드캐스트 채널(새 채널 자동 포함).
        rows, channels_meta = await fetch_recent(
            client,
            None,
            since,
            per_channel_limit,
            known_ids=known_ids,
            new_since=new_since,
            new_limit=new_limit,
        )

        # 정상 사이클(짧은 창)에서만 조회수 시계열 snapshot 갱신(큰 창은 FloodWait 위험).
        views_refreshed = 0
        if minutes <= _VIEWS_REFRESH_MAX_WINDOW_MIN:
            try:
                with db.connect() as vconn:
                    views_refreshed = await refresh_views(
                        client, vconn, _VIEW_HORIZONS, _VIEWS_REFRESH_CAP
                    )
            except Exception:  # noqa: BLE001 — 조회수 갱신 실패가 수집을 막으면 안 됨
                views_refreshed = 0
    finally:
        await client.disconnect()

    new_messages = 0
    new_mentions = 0
    new_channels = [c for c in channels_meta if c["id"] not in known_ids]

    with db.connect() as conn:
        # 훑은 모든 채널 메타 갱신(메시지 0건이어도 — '봤다'고 기록해 재백필 방지).
        for c in channels_meta:
            db.upsert_channel(
                conn, c["id"], c.get("title"), c.get("username"), c.get("subscribers")
            )
        # tier 행이 없는 채널을 휴리스틱 시드(수동 분류·기존 heuristic 행은 보존). 태깅이
        # tier 를 쓰므로 매 사이클 호출하되 only_missing 으로 신규 채널만 분류(저렴).
        seed_channel_tiers(conn, only_missing=True)
        tier_map = db.channel_tiers(conn)

        for r in rows:
            mentions = extract_mentions(r["text"])
            tier = (tier_map.get(r["channel_id"]) or {}).get("tier")
            sentiment = tag_sentiment(r["text"])
            msg_type = tag_msg_type(r["text"], len(mentions), tier)
            # 클러스터 키(포워드면 원본 키로 수렴) + 정규화 텍스트 서명(복붙 dedup용).
            cluster_id = cluster.canonical_key(
                r["channel_id"], r["msg_id"],
                r.get("fwd_from_chat_id"), r.get("fwd_from_message_id"),
            )
            text_sig = cluster.text_signature(r["text"])
            msg_rowid = db.insert_message(
                conn,
                r["channel_id"],
                r["msg_id"],
                r["date"],
                r["text"],
                views=r.get("views"),
                forwards=r.get("forwards"),
                fwd_from_chat_id=r.get("fwd_from_chat_id"),
                fwd_from_chat_title=r.get("fwd_from_chat_title"),
                fwd_from_message_id=r.get("fwd_from_message_id"),
                fwd_from_date=r.get("fwd_from_date"),
                sentiment=sentiment,
                msg_type=msg_type,
                cluster_id=cluster_id,
                text_sig=text_sig,
            )
            if msg_rowid is None:
                continue  # 이미 저장됨
            new_messages += 1
            # 수집 시점 조회수·확산 snapshot(horizon='collect').
            db.insert_views_log(
                conn, msg_rowid, r["channel_id"], "collect",
                r.get("views"), r.get("forwards"),
            )
            if mentions:
                db.insert_mentions(
                    conn, msg_rowid, r["channel_id"], r["date"], mentions
                )
                new_mentions += len(mentions)

        # 업그레이드 이전 메시지 text_sig 1회 백필 + 그 구간 휴리스틱 병합(마커로 1회 게이트).
        sig_backfilled = 0
        marker = data_dir() / "text_sig_backfilled"
        if not marker.exists():
            hist_since = (now - timedelta(days=_SIG_BACKFILL_DAYS)).isoformat()
            sig_backfilled = cluster.backfill_text_sig(conn, hist_since, limit=20000)
            cluster.merge_heuristic_duplicates(conn, window_min=30, since_iso=hist_since)
            try:
                marker.write_text(
                    datetime.now(timezone.utc).isoformat(), encoding="utf-8"
                )
            except OSError:
                pass

        # 복붙 중복 휴리스틱 병합 — 정상 사이클에서 최근 구간만(저렴). 큰 백필 사이클은 skip
        # (위 1회 백필이 과거를, 이후 정상 사이클이 최근을 덮는다).
        merged = 0
        if minutes <= _VIEWS_REFRESH_MAX_WINDOW_MIN:
            merge_since = (now - timedelta(hours=_MERGE_WINDOW_HOURS)).isoformat()
            merged = cluster.merge_heuristic_duplicates(
                conn, window_min=30, since_iso=merge_since
            )

        # 베이스라인이 없거나 오래됐으면(>6h) 재계산. 사이클당 가벼운 집계 1회.
        baselines_computed = 0
        age = db.baselines_age_minutes(conn)
        if age is None or age > _BASELINE_REFRESH_MIN:
            baselines_computed = db.compute_baselines(conn, days=7)

    return {
        "fetched": len(rows),
        "new_messages": new_messages,
        "new_mentions": new_mentions,
        "channels": len(channels_meta),
        "new_channels": len(new_channels),
        "new_channels_backfilled_days": round(new_channel_minutes / 1440, 1)
        if new_channels
        else 0,
        "views_refreshed": views_refreshed,
        "clusters_merged": merged,
        "text_sig_backfilled": sig_backfilled,
        "baselines_computed": baselines_computed,
        "since": since.isoformat(),
        "window_minutes": minutes,
    }
