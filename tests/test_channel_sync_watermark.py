"""채널별 수집 시각 - 실제로 받은 채널만 '받았다'고 적는다.

감사에서 나온 결함: 수집에 실패한 채널까지 channels.last_synced 를 '지금'으로 찍었고,
다음 사이클의 수집 창은 DB 전체의 최신 메시지로 정해졌다. 그래서 7일 캐치업 도중
한 채널만 타임아웃이 나면, 다른 채널이 최신을 끌어올려 다음 창이 30분으로 줄고
그 채널의 7일치는 영영 안 채워졌다. 목록에는 방금 수집한 것처럼 보였다.

여기서 확인하는 것:
- 실패한 채널은 last_synced 가 그대로다(신규 채널이면 비어 있어 백필을 다시 받는다).
- 다음 사이클은 그 채널만 자기 마지막 성공 지점부터 다시 읽는다.
- 따라잡기는 오래된 것부터 읽고, 상한·타임아웃에 걸려도 읽은 지점까지 전진한다
  (같은 구간을 되풀이 요청하는 재시도 루프가 생기지 않는다).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon.tl.types import Channel  # noqa: E402

import telegram_lens.client as client_mod  # noqa: E402
from telegram_lens import db, sync  # noqa: E402


def _channel(id_: int):
    ent = Channel.__new__(Channel)
    ent.id = id_
    ent.title = f"채널{id_}"
    ent.username = f"ch{id_}"
    ent.participants_count = 10
    ent.broadcast = True
    return ent


def _msg(id_: int, date: datetime, text: str = "시장 이야기"):
    return SimpleNamespace(
        id=id_, date=date, message=text, views=None, forwards=None, fwd_from=None,
        forward=None, photo=None, document=None, media=None,
    )


class FakeClient:
    """채널별 메시지 목록(최신→오래된 순)을 들고, Telethon iter_messages 흉내를 낸다.

    kind: ok | hang(한 건도 안 주고 멎음) | slow(두 건 주고 멎음) | flood(바로 FloodWait)
    """

    def __init__(self, channels: dict[int, tuple[str, list]]):
        self.channels = channels
        self.calls: list[dict] = []

    async def iter_dialogs(self):
        for cid in self.channels:
            yield SimpleNamespace(entity=_channel(cid))

    async def iter_messages(self, ent, limit=500, offset_date=None, reverse=False):
        kind, msgs = self.channels[ent.id]
        self.calls.append(
            {"id": ent.id, "limit": limit, "offset_date": offset_date, "reverse": reverse}
        )
        if kind == "hang":
            await asyncio.sleep(999)
        if kind == "flood":
            from telethon.errors import FloodWaitError

            raise FloodWaitError(request=None, capture=120)
        if reverse:
            seq = sorted([m for m in msgs if m.date > offset_date], key=lambda m: m.date)
        else:
            seq = msgs
        for n, m in enumerate(seq[:limit]):
            if kind == "slow" and n == 2:
                await asyncio.sleep(999)
            yield m


@pytest.fixture(autouse=True)
def fast_channel_timeout(monkeypatch):
    monkeypatch.setattr(client_mod, "_CHANNEL_TIMEOUT_SEC", 0.3)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))
    db.init_db()
    return tmp_path


def _now():
    return datetime.now(timezone.utc)


# ── plan_channel_catchup ───────────────────────────────────────────


def test_plan_skips_channels_that_are_up_to_date():
    now = _now()
    since = now - timedelta(minutes=30)
    plan = sync.plan_channel_catchup(
        {1: (now - timedelta(minutes=10)).isoformat()}, since, now, 500
    )
    assert plan == {}


def test_plan_extends_slightly_behind_channel_newest_first():
    now = _now()
    since = now - timedelta(minutes=30)
    plan = sync.plan_channel_catchup(
        {1: (now - timedelta(minutes=35)).isoformat()}, since, now, 500
    )
    start, limit, reverse = plan[1]
    assert reverse is False
    assert start <= now - timedelta(minutes=40)
    assert limit == 500


def test_plan_reads_far_behind_channel_oldest_first_with_cap():
    now = _now()
    since = now - timedelta(minutes=30)
    plan = sync.plan_channel_catchup(
        {1: (now - timedelta(days=30)).isoformat()}, since, now, 500, max_minutes=7 * 1440
    )
    start, limit, reverse = plan[1]
    assert reverse is True
    # 상한(7일)보다 오래된 구간은 전체 다운타임 정책처럼 포기한다.
    assert abs((start - (now - timedelta(days=7))).total_seconds()) < 5
    assert limit == sync._CATCHUP_LIMIT_CAP


# ── client.fetch_recent ────────────────────────────────────────────


def test_failed_channel_has_no_synced_through():
    now = _now()
    fc = FakeClient({1: ("ok", [_msg(1, now)]), 2: ("hang", [])})
    rows, chans, stats = asyncio.run(
        client_mod.fetch_recent(fc, None, now - timedelta(hours=1), 500)
    )
    by_id = {c["id"]: c for c in chans}
    assert by_id[1]["synced_through"] is not None
    assert by_id[2]["synced_through"] is None
    assert stats["failed"] == 1


def test_reverse_catchup_advances_to_last_read_message_on_limit():
    now = _now()
    old = [_msg(i, now - timedelta(hours=10 - i)) for i in range(10)]  # 10h 전 ~ 1h 전
    fc = FakeClient({1: ("ok", list(reversed(old)))})
    start = now - timedelta(hours=11)
    rows, chans, _ = asyncio.run(
        client_mod.fetch_recent(
            fc, None, now - timedelta(minutes=30), 500,
            catchup_by_channel={1: (start, 4, True)},
        )
    )
    call = fc.calls[0]
    assert call["reverse"] is True and call["offset_date"] == start
    assert len(rows) == 4  # 상한만큼, 오래된 것부터
    # 상한에 걸렸으니 '지금'이 아니라 읽은 마지막 글 시각까지만 받았다고 적는다.
    got = datetime.fromisoformat(chans[0]["synced_through"])
    assert got == old[3].date


def test_reverse_catchup_timeout_keeps_partial_rows_and_progress():
    now = _now()
    old = [_msg(i, now - timedelta(hours=10 - i)) for i in range(10)]
    fc = FakeClient({1: ("slow", list(reversed(old)))})
    rows, chans, stats = asyncio.run(
        client_mod.fetch_recent(
            fc, None, now - timedelta(minutes=30), 500,
            catchup_by_channel={1: (now - timedelta(hours=11), 100, True)},
        )
    )
    assert stats["failed"] == 1
    assert len(rows) == 2  # 멎기 전까지 읽은 두 건은 버리지 않는다
    assert datetime.fromisoformat(chans[0]["synced_through"]) == old[1].date


def test_flood_wait_is_not_retried_in_the_same_cycle():
    now = _now()
    fc = FakeClient({1: ("flood", [])})
    _rows, chans, stats = asyncio.run(
        client_mod.fetch_recent(fc, None, now - timedelta(hours=1), 500)
    )
    assert len(fc.calls) == 1
    assert stats["failed"] == 1
    assert chans[0]["synced_through"] is None


def test_catchup_over_cap_reads_recent_window_but_holds_sync_time():
    now = _now()
    msgs = [_msg(1, now - timedelta(minutes=1))]
    fc = FakeClient({1: ("ok", msgs), 2: ("ok", msgs)})
    far = now - timedelta(days=2)
    _rows, chans, _ = asyncio.run(
        client_mod.fetch_recent(
            fc, None, now - timedelta(minutes=30), 500,
            catchup_by_channel={1: (far, 1000, True), 2: (far, 1000, True)},
            max_catchup_channels=1,
        )
    )
    by_id = {c["id"]: c for c in chans}
    assert fc.calls[0]["reverse"] is True
    assert fc.calls[1]["reverse"] is False  # 정원 초과 - 이번엔 평소 창만
    assert by_id[1]["synced_through"] is not None
    assert by_id[2]["synced_through"] is None  # 빈 구간이 남았으니 시각을 올리지 않는다


# ── sync.run_sync: DB 에 남는 것 ───────────────────────────────────


def _run_sync_with(fc, monkeypatch, **kw):
    async def _noop(*a, **k):
        return None

    async def _no_views(*a, **k):
        return 0

    class _Authed:
        async def is_user_authorized(self):
            return True

    fake = fc
    fake.is_user_authorized = _Authed().is_user_authorized
    monkeypatch.setattr(sync, "make_client", lambda: fake)
    monkeypatch.setattr(sync, "connect_with_timeout", _noop)
    monkeypatch.setattr(sync, "disconnect_safely", _noop)
    monkeypatch.setattr(sync, "refresh_views", _no_views)
    # 종목 사전을 불러오다 네트워크로 새로 받는 일이 없게 - 여기서 보는 건 수집 시각뿐이다.
    monkeypatch.setattr(sync, "extract_mentions", lambda text: [])
    return asyncio.run(sync.run_sync(**kw))


def test_run_sync_keeps_last_synced_of_failed_channel(home, monkeypatch):
    now = _now()
    before = (now - timedelta(hours=3)).isoformat()
    with db.connect() as conn:
        db.upsert_channel(conn, 1, "채널1", "ch1", 10, synced_at=before)
        db.upsert_channel(conn, 2, "채널2", "ch2", 10, synced_at=before)

    fc = FakeClient({1: ("ok", [_msg(1, now - timedelta(minutes=5))]), 2: ("hang", [])})
    _run_sync_with(fc, monkeypatch, minutes=30, per_channel_limit=500)

    with db.connect() as conn:
        synced = db.last_synced_by_channel(conn)
    assert synced[1] > before
    assert synced[2] == before  # 실패 - 예전 값 그대로

    # 두 채널 다 3시간 뒤처져 있었다 → 이번 사이클에 자기 지점부터 따라잡기로 읽었다.
    assert all(c["reverse"] for c in fc.calls)


def test_run_sync_new_channel_that_failed_stays_new(home, monkeypatch):
    now = _now()
    fc = FakeClient({7: ("hang", [])})
    _run_sync_with(fc, monkeypatch, minutes=30, per_channel_limit=500)
    with db.connect() as conn:
        row = conn.execute("SELECT last_synced FROM channels WHERE id = 7").fetchone()
    assert row is not None and row["last_synced"] is None

    # 다음 사이클: 여전히 '처음 보는 채널'이라 신규 채널 백필 깊이로 읽는다.
    fc2 = FakeClient({7: ("ok", [_msg(1, now - timedelta(days=2))])})
    out = _run_sync_with(fc2, monkeypatch, minutes=30, per_channel_limit=500)
    assert out["new_channels"] == 1
    assert out["fetched"] == 1
