"""채널 밀도 리포트 — 각 채널이 얼마나 '종목 위주'인지 측정한다.

각 브로드캐스트 채널의 최근 메시지를 샘플링해 "종목 언급이 있는 메시지 비율
(density)"을 잰다. 결과는 channel_scores 에 저장돼 어떤 채널이 종목방인지 파악하는
정보용이다.

NOTE: 수집은 더 이상 이 분류로 제한되지 않는다(2026-06 이후 '가입된 모든 브로드캐스트
채널 수집'으로 전환 — sync.py 참조). 그래서 tracked.json 자동 기록은 기본 OFF이며,
이 도구는 순수 진단/리포트 용도다. (옛 동작인 allowlist 가 필요하면 write_tracked=True.)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from telethon.tl.types import Channel, Chat

from telegram_lens import db
from telegram_lens.client import NotLoggedInError, make_client
from telegram_lens.config import tracked_path
from telegram_lens.extract import extract_mentions, reset_index


def _write_tracked(channel_ids: list[int], threshold: float) -> None:
    tracked_path().write_text(
        json.dumps(
            {
                "channel_ids": channel_ids,
                "threshold": threshold,
                "updated": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def run_classification(
    sample: int = 80,
    threshold: float = 0.05,
    min_mentions: int = 3,
    write_tracked: bool = False,
) -> dict:
    """전 채널 스캔 → 밀도 측정 → channel_scores 기록(진단/리포트).

    수집 자체는 가입된 모든 브로드캐스트 채널을 대상으로 하므로(sync.py), 이 분류는
    '어느 채널이 종목 위주인가'를 보여주는 정보용이다.

    Args:
        sample: 채널당 샘플링할 메시지 수.
        threshold: 주식채널 판정 밀도 임계값(0~1).
        min_mentions: 최소 누적 언급 수(저밀도 잡음 방지).
        write_tracked: True면 종목방 목록을 tracked.json 에 기록(옛 allowlist 호환용,
            기본 False — 수집은 이 파일을 더 이상 읽지 않는다).
    """
    db.init_db()
    reset_index()

    client = make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise NotLoggedInError(
                "로그인되어 있지 않습니다. `telegramlens-login` 을 먼저 실행하세요."
            )

        scored: list[dict] = []
        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            if not isinstance(ent, (Channel, Chat)):
                continue
            if not getattr(ent, "broadcast", False):
                continue

            with_text = 0
            with_mention = 0
            mentions = 0
            async for msg in client.iter_messages(ent, limit=sample):
                text = msg.message or ""
                if not text.strip():
                    continue
                with_text += 1
                m = extract_mentions(text)
                if m:
                    with_mention += 1
                    mentions += len(m)

            density = (with_mention / with_text) if with_text else 0.0
            is_stock = density >= threshold and mentions >= min_mentions
            scored.append(
                {
                    "channel_id": ent.id,
                    "title": getattr(ent, "title", None),
                    "username": getattr(ent, "username", None),
                    "subscribers": getattr(ent, "participants_count", None),
                    "sampled": with_text,
                    "with_mention": with_mention,
                    "mentions": mentions,
                    "density": round(density, 4),
                    "is_stock": 1 if is_stock else 0,
                    "classified_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    finally:
        await client.disconnect()

    with db.connect() as conn:
        for s in scored:
            db.upsert_channel_score(conn, s)

    stock_channels = [s for s in scored if s["is_stock"]]
    if write_tracked:
        _write_tracked([s["channel_id"] for s in stock_channels], threshold)

    scored.sort(key=lambda x: x["density"], reverse=True)
    return {
        "scanned": len(scored),
        "stock_channels": len(stock_channels),
        "filtered_out": len(scored) - len(stock_channels),
        "threshold": threshold,
        "sample": sample,
        "tracked_written": write_tracked,
        "channels": scored,
    }
