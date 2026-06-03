"""Telethon 래퍼 — 로그인된 사용자 세션으로 채널 메시지를 읽는다.

로그인(전화번호 인증)은 대화형이라 별도 CLI(login_cli.py)에서 처리하고,
여기서는 이미 만들어진 세션 파일을 재사용한다. 읽기는 비대화형이므로
MCP 툴 안에서도 안전하게 호출할 수 있다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, PeerChannel, PeerChat, PeerUser

from telegram_lens import db
from telegram_lens.config import get_credentials, session_path

_LOG = logging.getLogger("telegramlens.client")


class NotLoggedInError(RuntimeError):
    pass


class NoCredentialsError(RuntimeError):
    pass


def make_client() -> TelegramClient:
    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        raise NoCredentialsError(
            "Telegram API 자격증명이 없습니다. https://my.telegram.org 에서 "
            "API_ID / API_HASH 발급 후 `telegramlens-login` 으로 등록하세요."
        )
    return TelegramClient(str(session_path()), api_id, api_hash)


async def list_dialogs(client: TelegramClient) -> list[dict]:
    """가입된 채널/그룹 목록."""
    out: list[dict] = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if isinstance(ent, (Channel, Chat)):
            out.append(
                {
                    "id": ent.id,
                    "title": getattr(ent, "title", None),
                    "username": getattr(ent, "username", None),
                    "is_broadcast": getattr(ent, "broadcast", False),
                    "participants": getattr(ent, "participants_count", None),
                }
            )
    return out


async def fetch_recent(
    client: TelegramClient,
    channel_ids: list[int] | None,
    since: datetime,
    per_channel_limit: int = 500,
    *,
    known_ids: set[int] | None = None,
    new_since: datetime | None = None,
    new_limit: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """since 이후 메시지를 채널별로 수집.

    channel_ids 가 None 이면 가입된 모든 브로드캐스트 채널을 대상으로 한다.

    신규 채널 1회 백필: known_ids(이미 수집해 본 채널)에 없는 '처음 보는' 채널은
    new_since(보통 더 과거) 까지, new_limit 만큼 깊게 1회 소급 수집한다. 그래서 새로
    가입한 방은 최근 며칠치 맥락이 한 번에 들어오고, 그 다음 사이클부터는 평소의
    짧은 창(since)으로만 따라간다.

    반환: (messages, channels)
      messages : [{channel_id, title, username, subscribers, msg_id, date, text}, ...]
      channels : 이번에 훑은 모든 대상 채널 메타 [{id, title, username, subscribers}, ...]
                 — 메시지가 0건이어도 포함(= '봤다'고 기록해 재백필을 막는다).
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if new_since is not None and new_since.tzinfo is None:
        new_since = new_since.replace(tzinfo=timezone.utc)

    targets: list = []
    async for dialog in client.iter_dialogs():
        ent = dialog.entity
        if not isinstance(ent, (Channel, Chat)):
            continue
        if channel_ids is not None and ent.id not in channel_ids:
            continue
        # 채널 ID만 지정한 경우가 아니면 브로드캐스트(공지형 채널) 우선
        if channel_ids is None and not getattr(ent, "broadcast", False):
            continue
        targets.append(ent)

    results: list[dict] = []
    channels: list[dict] = []
    for ent in targets:
        title = getattr(ent, "title", None)
        username = getattr(ent, "username", None)
        subs = getattr(ent, "participants_count", None)
        channels.append(
            {"id": ent.id, "title": title, "username": username, "subscribers": subs}
        )
        # 처음 보는 채널이면 더 과거(new_since)까지 깊게 1회 백필.
        is_new = (
            known_ids is not None
            and new_since is not None
            and ent.id not in known_ids
        )
        eff_since = new_since if is_new else since
        eff_limit = (new_limit or per_channel_limit) if is_new else per_channel_limit
        async for msg in client.iter_messages(ent, limit=eff_limit):
            if msg.date and msg.date < eff_since:
                break
            text = msg.message or ""
            if not text.strip():
                continue
            fwd = getattr(msg, "fwd_from", None)
            results.append(
                {
                    "channel_id": ent.id,
                    "title": title,
                    "username": username,
                    "subscribers": subs,
                    "msg_id": msg.id,
                    "date": msg.date.astimezone(timezone.utc).isoformat(),
                    "text": text,
                    "views": getattr(msg, "views", None),
                    "forwards": getattr(msg, "forwards", None),
                    "fwd_from_chat_id": _peer_id(getattr(fwd, "from_id", None)),
                    "fwd_from_chat_title": _fwd_title(msg, fwd),
                    "fwd_from_message_id": getattr(fwd, "channel_post", None),
                    "fwd_from_date": _iso(getattr(fwd, "date", None)),
                }
            )
    return results, channels


def _peer_id(peer) -> int | None:
    """Peer(Channel/Chat/User) 에서 정수 id 추출. None 이면 None."""
    if peer is None:
        return None
    if isinstance(peer, PeerChannel):
        return peer.channel_id
    if isinstance(peer, PeerChat):
        return peer.chat_id
    if isinstance(peer, PeerUser):
        return peer.user_id
    return None


def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, AttributeError):
        return None


def _fwd_title(msg, fwd) -> str | None:
    """포워드 원본 채널/사람 표시명. Telethon 이 해석한 msg.forward.chat.title 우선,
    없으면 fwd_from.from_name(숨김 발신자 등)으로 폴백."""
    try:
        fwd_obj = getattr(msg, "forward", None)
        chat = getattr(fwd_obj, "chat", None) if fwd_obj is not None else None
        if chat is not None:
            title = getattr(chat, "title", None)
            if title:
                return title
    except Exception:  # noqa: BLE001 — forward 해석 실패는 무시(from_name 폴백)
        pass
    return getattr(fwd, "from_name", None)


async def refresh_views(
    client: TelegramClient,
    conn,
    horizons: dict[str, tuple[float, float]],
    per_cycle_cap: int = 200,
    batch: int = 100,
) -> int:
    """게시 후 horizon 시점의 조회수·확산을 Telegram 에서 재조회해 갱신.

    각 horizon(1h/6h/24h)에 대해 아직 그 snapshot 이 없고 나이가 구간에 든 메시지를
    골라(합산 per_cycle_cap 으로 제한) 채널별 batch get_messages 로 최신 views/forwards
    를 읽는다. messages 의 최신값을 갱신하고 message_views_log 에 horizon 행을 남긴다.
    FloodWait·접근불가 등은 채널 단위로 흡수(전체 사이클을 죽이지 않음).

    반환: 갱신(로그 기록)된 메시지 수.
    """
    # 1) 갱신 대상 수집 — horizon별로, 합산 상한까지.
    targets: list[tuple[int, int, int, str]] = []  # (msg_rowid, channel_id, msg_id, horizon)
    remaining = per_cycle_cap
    for horizon, (lo, hi) in horizons.items():
        if remaining <= 0:
            break
        rows = db.messages_needing_view_refresh(conn, horizon, lo, hi, remaining)
        for r in rows:
            targets.append((r["id"], r["channel_id"], r["msg_id"], horizon))
        remaining -= len(rows)

    if not targets:
        return 0

    # 2) 채널별로 묶어 batch 조회. (channel_id → [(msg_rowid, msg_id, horizon), ...])
    by_channel: dict[int, list[tuple[int, int, str]]] = {}
    for rowid, ch_id, msg_id, horizon in targets:
        by_channel.setdefault(ch_id, []).append((rowid, msg_id, horizon))

    updated = 0
    for ch_id, items in by_channel.items():
        try:
            ent = await client.get_entity(ch_id)
        except Exception as e:  # noqa: BLE001 — 엔티티 해석 실패 채널은 건너뜀
            _LOG.debug("refresh_views: get_entity(%s) 실패: %s", ch_id, e)
            continue
        # rowid 를 msg_id 로 역참조하기 위한 맵(같은 msg_id 가 여러 horizon 대상일 수 있음)
        wanted: dict[int, list[tuple[int, str]]] = {}
        for rowid, msg_id, horizon in items:
            wanted.setdefault(msg_id, []).append((rowid, horizon))
        ids = list(wanted.keys())
        for i in range(0, len(ids), batch):
            chunk = ids[i : i + batch]
            try:
                fetched = await client.get_messages(ent, ids=chunk)
            except Exception as e:  # noqa: BLE001 — FloodWait 등은 채널 단위로 흡수
                _LOG.debug("refresh_views: get_messages(%s) 실패: %s", ch_id, e)
                continue
            for m in fetched:
                if m is None:  # 삭제된 메시지 → snapshot 없음
                    continue
                views = getattr(m, "views", None)
                forwards = getattr(m, "forwards", None)
                for rowid, horizon in wanted.get(m.id, []):
                    db.update_message_views(conn, rowid, views, forwards)
                    db.insert_views_log(conn, rowid, ch_id, horizon, views, forwards)
                    updated += 1
    return updated
