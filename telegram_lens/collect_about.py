"""채널 소개글(about) 1회 수집 — `telegramlens-collect-about`.

채널 export(공유 리스트)에 '이 채널이 어떤 방인지' 한 줄 설명을 붙이기 위해,
가입된 각 채널의 소개글을 Telethon GetFullChannel 로 받아 channels.about 에 저장한다.

설계:
  - export_channels 는 DB만 읽어 데몬과 세션 경합이 없다. about 은 라이브 접속이
    필요하므로, 평상시가 아니라 '필요할 때 1회' 이 CLI 로만 수집한다.
  - 채널마다 GetFullChannel 1콜 → FloodWait 가능. 호출 간 짧은 sleep 으로 완화하고,
    FloodWait 가 나면 안내 후 해당 채널은 건너뛴다(중단하지 않음).
  - about 이 없는 채널은 빈 문자열로 남겨 재시도 시 덮어쓰지 않게 한다(NULL 유지).
"""

from __future__ import annotations

import argparse
import asyncio

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel

from telegram_lens import db
from telegram_lens.client import make_client


def _clean(about: str | None, max_len: int) -> str:
    """소개글을 한 줄로 정리. 줄바꿈→공백, 과도한 길이는 자른다."""
    if not about:
        return ""
    text = " ".join(about.split())
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


async def _collect(max_len: int, only_missing: bool, delay: float) -> dict:
    db.init_db()

    # 대상: DB 에 있는 공개/추적 채널 중 username 보유분(공유 리스트에 나가는 것과 동일 기준).
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, title, username, about FROM channels"
        ).fetchall()
    targets = {r["id"]: dict(r) for r in rows}

    updated = 0
    skipped = 0
    flood = 0
    no_about = 0

    client = make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"error": "로그인이 필요합니다. 먼저 `telegramlens-login` 을 실행하세요."}

        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            if not isinstance(ent, Channel):
                continue
            meta = targets.get(ent.id)
            if meta is None:
                continue  # DB 에 없는 채널은 건너뜀(수집은 sync 의 몫)
            if only_missing and meta.get("about"):
                skipped += 1
                continue

            try:
                full = await client(GetFullChannelRequest(channel=ent))
                about = _clean(getattr(full.full_chat, "about", None), max_len)
            except FloodWaitError as e:
                flood += 1
                print(f"  [FloodWait {e.seconds}s] {meta.get('title')} — 건너뜀")
                continue
            except Exception as e:  # noqa: BLE001 — 한 채널 실패가 전체를 막지 않게
                print(f"  [skip] {meta.get('title')}: {type(e).__name__}")
                skipped += 1
                continue

            if not about:
                no_about += 1
                continue  # 소개글 없는 채널은 NULL 유지(빈값으로 덮지 않음)

            with db.connect() as conn:
                conn.execute(
                    "UPDATE channels SET about = ? WHERE id = ?", (about, ent.id)
                )
                conn.commit()
            updated += 1
            if delay:
                await asyncio.sleep(delay)
    finally:
        await client.disconnect()

    return {
        "updated": updated,
        "skipped": skipped,
        "no_about": no_about,
        "flood_wait": flood,
        "total_channels": len(targets),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        prog="telegramlens-collect-about",
        description="가입 채널의 소개글(about)을 1회 수집해 DB에 저장(공유 리스트 설명용).",
    )
    p.add_argument(
        "--max-len", type=int, default=60, help="소개글 최대 길이(자). 기본 60."
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="이미 about 이 있는 채널도 다시 수집(기본은 비어있는 채널만).",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="채널 간 호출 간격(초). FloodWait 완화. 기본 0.5.",
    )
    args = p.parse_args()

    print("채널 소개글 수집 중... (가입 채널 수에 따라 시간이 걸립니다)")
    result = asyncio.run(
        _collect(max_len=args.max_len, only_missing=not args.all, delay=args.delay)
    )
    if result.get("error"):
        print(result["error"])
        raise SystemExit(1)
    print(
        f"완료 — 갱신 {result['updated']}, 소개글 없음 {result['no_about']}, "
        f"건너뜀 {result['skipped']}, FloodWait {result['flood_wait']} "
        f"(전체 {result['total_channels']})"
    )
    print("이제 `telegramlens-export-channels` 로 소개가 포함된 리스트를 뽑을 수 있습니다.")


if __name__ == "__main__":
    main()
