"""오프라인 스모크 테스트 — 백필 이어받기(resume) 검증.

사용자 요청 백필이 도중에 끊기고 재시작해도, 이미 받아둔 채널은 '지금'부터 다시
훑지 않고 db.oldest_message_dates_by_channel() 이 알려주는 지점부터(Telethon
offset_date) 이어받는지 확인한다. DB 계층(채널별 최소 날짜 집계)과 client 계층
(offset_date 가 실제로 Telethon 호출에 전달되는지) 둘 다 검증한다.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="tglens_backfill_resume_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

from telethon.tl.types import Channel  # noqa: E402

from telegram_lens import cluster, db  # noqa: E402
import telegram_lens.client as client_mod  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def check_oldest_message_dates_by_channel() -> None:
    print("\n=== db.oldest_message_dates_by_channel ===")
    db.init_db()
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        db.upsert_channel(conn, 9001, "채널A", "cha", 100)
        db.upsert_channel(conn, 9002, "채널B", "chb", 100)
        # 채널A: 두 건(10일 전, 3일 전) — 가장 오래된 건 10일 전.
        for days_ago, mid in [(10, 1), (3, 2)]:
            d = (now - timedelta(days=days_ago)).isoformat()
            text = f"삼성전자 테스트 {mid}"
            db.insert_message(
                conn, 9001, mid, d, text,
                cluster_id=cluster.canonical_key(9001, mid, None, None),
                text_sig=cluster.text_signature(text),
            )
        # 채널B: 메시지 없음 — dict 에 아예 안 나와야 함(=처음부터).
        oldest = db.oldest_message_dates_by_channel(conn)

    _assert(9001 in oldest, "메시지 있는 채널은 dict 에 포함")
    _assert(9002 not in oldest, "메시지 없는 채널은 dict 에 없음(=처음부터 받으라는 신호)")
    got = datetime.fromisoformat(oldest[9001])
    if got.tzinfo is None:
        got = got.replace(tzinfo=timezone.utc)
    expected = now - timedelta(days=10)
    _assert(abs((got - expected).total_seconds()) < 5, f"가장 오래된 날짜=10일 전, got {got}")


def _make_channel(id_: int, title: str):
    ent = Channel.__new__(Channel)
    ent.id = id_
    ent.title = title
    ent.username = f"ch{id_}"
    ent.participants_count = 100
    ent.broadcast = True
    return ent


class _FakeMsg:
    def __init__(self, id_: int, date, text: str):
        self.id = id_
        self.date = date
        self.message = text
        self.views = None
        self.forwards = None
        self.fwd_from = None
        self.forward = None
        self.photo = None
        self.document = None
        self.media = None


class _FakeClient:
    """iter_messages 호출 시 넘어온 offset_date 를 기록해두는 가짜 client."""

    def __init__(self, ids: list[int]):
        self._entities = [_make_channel(i, f"ch{i}") for i in ids]
        self.offset_dates_seen: dict[int, object] = {}

    async def iter_dialogs(self):
        for ent in self._entities:
            yield SimpleNamespace(entity=ent)

    async def iter_messages(self, ent, limit=500, offset_date=None):
        self.offset_dates_seen[ent.id] = offset_date
        now = datetime.now(timezone.utc)
        yield _FakeMsg(ent.id * 100, now, f"삼성전자 005930 테스트 {ent.id}")


def check_offset_date_propagation() -> None:
    print("\n=== client.fetch_recent: offset_date 가 채널별로 정확히 전달됨 ===")
    fc = _FakeClient([1, 2, 3])
    since = datetime.now(timezone.utc) - timedelta(days=90)
    resume_point = datetime.now(timezone.utc) - timedelta(days=10)
    oldest_by_channel = {1: resume_point}  # 채널 1만 이어받기 지점 있음. 2·3은 없음.

    asyncio.run(
        client_mod.fetch_recent(
            fc, None, since, 500, oldest_by_channel=oldest_by_channel
        )
    )

    _assert(fc.offset_dates_seen[1] == resume_point, "이어받기 지점 있는 채널은 그 시각을 offset_date 로 전달")
    _assert(fc.offset_dates_seen[2] is None, "이어받기 지점 없는 채널은 offset_date=None(=지금부터)")
    _assert(fc.offset_dates_seen[3] is None, "마찬가지로 채널3도 offset_date=None")


def check_no_oldest_by_channel_means_all_none() -> None:
    print("\n=== client.fetch_recent: oldest_by_channel 자체를 안 주면 전부 None(기존 동작) ===")
    fc = _FakeClient([10, 11])
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    asyncio.run(client_mod.fetch_recent(fc, None, since, 500))
    _assert(all(v is None for v in fc.offset_dates_seen.values()), "oldest_by_channel 생략 시 전부 offset_date=None")


def main() -> None:
    print(f"임시 홈: {_TMP}")
    check_oldest_message_dates_by_channel()
    check_offset_date_propagation()
    check_no_oldest_by_channel_means_all_none()
    print("\nOK - 백필 이어받기(resume) 정상")


if __name__ == "__main__":
    main()
