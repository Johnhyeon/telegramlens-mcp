"""Telethon 래퍼 — 로그인된 사용자 세션으로 채널 메시지를 읽는다.

로그인(전화번호 인증)은 대화형이라 별도 CLI(login_cli.py)에서 처리하고,
여기서는 이미 만들어진 세션 파일을 재사용한다. 읽기는 비대화형이므로
MCP 툴 안에서도 안전하게 호출할 수 있다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Channel,
    Chat,
    MessageMediaWebPage,
    PeerChannel,
    PeerChat,
    PeerUser,
)

from telegram_lens import db
from telegram_lens.config import get_credentials, session_path

_LOG = logging.getLogger("telegramlens.client")

# 채널 하나가 응답 없이 멎어도(죽은 소켓 등) 전체 사이클을 붙잡지 않도록 채널별 상한.
_CHANNEL_TIMEOUT_SEC = 90
# 연속 타임아웃이 이 횟수를 넘으면 client 상태 자체가 오염됐을 가능성 — 남은 채널은
# 갈아넣지 않고 이번 사이클을 조기 종료(다음 사이클에 재시도).
_MAX_CONSECUTIVE_CHANNEL_TIMEOUTS = 2


class NotLoggedInError(RuntimeError):
    pass


class NoCredentialsError(RuntimeError):
    pass


async def connect_with_timeout(client: TelegramClient, timeout: float = 30) -> None:
    """client.connect() 를 시간제한 안에서 시도. 실패 시 예외를 그대로 올린다 —

    호출자가 finally 에서 disconnect_safely 로 뒷정리하는 것을 전제로 한다(연결 도중
    타임아웃이 나도 소켓이 반쯤 열린 상태로 남을 수 있으므로).
    """
    await asyncio.wait_for(client.connect(), timeout=timeout)


async def disconnect_safely(client: TelegramClient, timeout: float = 10) -> None:
    """연결 종료를 시간제한 안에서 시도하고, 오래 걸리면 취소 후 포기한다.

    Telethon 은 연결 중 send/recv 루프를 백그라운드 task 로 돌리는데, 이 task 들이 끝나기
    전에 프로세스/이벤트루프가 먼저 닫히면 'Task was destroyed but it is pending' /
    GeneratorExit 로 남는다(실제 고객 로그에서 확인된 장애 시그니처). asyncio.shield 로
    감싸 disconnect() 자체는 timeout 만큼 계속 진행되게 하고, 그래도 안 끝나면 그때 취소해
    최소한 무한정 매달리지는 않게 한다.
    """
    try:
        if not client.is_connected():
            return
    except Exception:  # noqa: BLE001 — is_connected 자체가 이상해도 disconnect 는 시도
        pass
    task = asyncio.ensure_future(client.disconnect())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.TimeoutError:
        _LOG.warning("client.disconnect() %s초 초과 — 강제 취소", timeout)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except Exception as e:  # noqa: BLE001 — disconnect 실패가 호출자 종료 흐름을 막으면 안 됨
        _LOG.warning("client.disconnect() 실패: %s: %s", type(e).__name__, e)


def make_client() -> TelegramClient:
    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        raise NoCredentialsError(
            "Telegram API 자격증명이 없습니다. https://my.telegram.org 에서 "
            "API_ID / API_HASH 발급 후 `telegramlens-login` 으로 등록하세요."
        )
    client = TelegramClient(str(session_path()), api_id, api_hash)
    # 짧은 FloodWait(60초 이하)는 Telethon 이 알아서 잠깐 자고 재시도한다. 그보다 긴
    # 대기는 예외로 올라오고, fetch_recent 가 채널별로 잡아 '이 사이클만 스킵'한다
    # (다음 사이클 재시도) — 긴 대기로 데몬 전체를 막지 않기 위함.
    client.flood_sleep_threshold = 60
    return client


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
) -> tuple[list[dict], list[dict], dict]:
    """since 이후 메시지를 채널별로 수집.

    channel_ids 가 None 이면 가입된 모든 브로드캐스트 채널을 대상으로 한다.

    신규 채널 1회 백필: known_ids(이미 수집해 본 채널)에 없는 '처음 보는' 채널은
    new_since(보통 더 과거) 까지, new_limit 만큼 깊게 1회 소급 수집한다. 그래서 새로
    가입한 방은 최근 며칠치 맥락이 한 번에 들어오고, 그 다음 사이클부터는 평소의
    짧은 창(since)으로만 따라간다.

    반환: (messages, channels, stats)
      messages : [{channel_id, title, username, subscribers, msg_id, date, text}, ...]
      channels : 이번에 훑은 모든 대상 채널 메타 [{id, title, username, subscribers}, ...]
                 — 메시지가 0건이어도 포함(= '봤다'고 기록해 재백필을 막는다).
      stats    : {total, processed, succeeded, failed} — daemon_status.json 노출용.
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
    stats = {"total": len(targets), "processed": 0, "succeeded": 0, "failed": 0}
    consecutive_timeouts = 0
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
        stats["processed"] += 1
        # 채널별 격리: 한 채널이 실패(권한 상실·FloodWait·구조 변경·응답 없음)해도 그 채널만
        # 건너뛰고, 이미 수집한 정상 채널 결과는 보존한 채 다음 채널로 계속한다(P0: 내결함성).
        # asyncio.wait_for 로 감싸 '예외 없이 그냥 안 끝나는' 채널(죽은 소켓 등)도 상한을 둔다.
        try:
            msgs = await asyncio.wait_for(
                _collect_one_channel(client, ent, title, username, subs, eff_since, eff_limit),
                timeout=_CHANNEL_TIMEOUT_SEC,
            )
            results.extend(msgs)
            stats["succeeded"] += 1
            consecutive_timeouts = 0
        except asyncio.TimeoutError:
            stats["failed"] += 1
            consecutive_timeouts += 1
            _LOG.warning(
                "채널 %s 수집 타임아웃(%d초) — 스킵",
                getattr(ent, "id", "?"), _CHANNEL_TIMEOUT_SEC,
            )
            if consecutive_timeouts > _MAX_CONSECUTIVE_CHANNEL_TIMEOUTS:
                _LOG.error(
                    "연속 채널 타임아웃 %d회 — client 상태 오염 의심, 이번 사이클 조기 종료"
                    "(남은 채널은 다음 사이클에)",
                    consecutive_timeouts,
                )
                break
            continue
        except FloodWaitError as e:
            stats["failed"] += 1
            consecutive_timeouts = 0
            _LOG.warning(
                "채널 %s FloodWait %s초 — 이 사이클 스킵(다음 사이클 재시도)",
                getattr(ent, "id", "?"), getattr(e, "seconds", "?"),
            )
            continue
        except Exception as e:  # noqa: BLE001 — 한 채널 실패가 전체 사이클을 막으면 안 됨
            stats["failed"] += 1
            consecutive_timeouts = 0
            _LOG.warning(
                "채널 %s 수집 실패 — 스킵: %s: %s",
                getattr(ent, "id", "?"), type(e).__name__, e,
            )
            continue
    return results, channels, stats


async def _collect_one_channel(
    client: TelegramClient,
    ent,
    title: str | None,
    username: str | None,
    subs: int | None,
    since: datetime,
    limit: int,
) -> list[dict]:
    """단일 채널의 메시지를 모아 반환. fetch_recent 가 asyncio.wait_for 로 감싸 채널별
    타임아웃을 건다 — 이 함수 자체는 시간제한을 모른다(취소되면 그냥 중간에 멈출 뿐)."""
    out: list[dict] = []
    async for msg in client.iter_messages(ent, limit=limit):
        if msg.date and msg.date < since:
            break
        text = msg.message or ""
        if not text.strip():
            continue
        fwd = getattr(msg, "fwd_from", None)
        media_type, file_name = _media_meta(msg)
        out.append(
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
                "media_type": media_type,
                "file_name": file_name,
            }
        )
    return out


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


def _media_meta(msg) -> tuple[str | None, str | None]:
    """첨부 메타데이터만(다운로드 X). (media_type, file_name).

    media_type: photo | document | webpage | None. 문서면 file_name 도(리포트 PDF 등).
    '이 글에 파일/이미지 있음'을 알려 텔레그램 원문으로 유도하는 용도.
    """
    try:
        if getattr(msg, "photo", None) is not None:
            return "photo", None
        if getattr(msg, "document", None) is not None:
            return "document", getattr(getattr(msg, "file", None), "name", None)
        if isinstance(getattr(msg, "media", None), MessageMediaWebPage):
            return "webpage", None
    except Exception:  # noqa: BLE001 — 미디어 메타 파싱 실패는 무시(텍스트 수집이 우선)
        pass
    return None, None


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
