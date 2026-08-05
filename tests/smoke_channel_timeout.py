"""오프라인 스모크 테스트 — client.fetch_recent 의 채널별 타임아웃 격리 검증.

핵심 질문(설계 리뷰에서 지적됨): 채널 하나가 iter_messages 도중 멎어도(응답 없는 소켓 등)
asyncio.wait_for 로 캔슬했을 때, 같은 client 로 진행하는 '다음' 채널이 정상 처리되는가 —
즉 캔슬이 client 의 연결 상태를 오염시켜 이후 채널까지 줄줄이 실패시키지 않는가?
그리고 연속 타임아웃이 임계치를 넘으면 실제로 사이클을 조기 종료하는가?
"""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="tglens_channel_timeout_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

from telethon.tl.types import Channel  # noqa: E402

import telegram_lens.client as client_mod  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def _make_channel(id_: int, title: str):
    # fetch_recent 는 isinstance(ent, (Channel, Chat)) 로 걸러내므로 진짜 telethon 타입이
    # 필요하다. __new__ 로 __init__ 인자 요구를 건너뛰고 필요한 속성만 채운다.
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
    """iter_dialogs/iter_messages 만 흉내낸 가짜 Telethon client. kind='hang' 인 채널은
    메시지를 한 건도 안 내놓고 영원히(테스트에선 999초) 멎는다."""

    def __init__(self, specs: list[tuple[int, str, str]]):
        self._entities = [_make_channel(id_, title) for id_, title, _kind in specs]
        self._kind = {id_: kind for id_, _title, kind in specs}

    async def iter_dialogs(self):
        for ent in self._entities:
            yield SimpleNamespace(entity=ent)

    async def iter_messages(self, ent, limit=500):
        if self._kind[ent.id] == "hang":
            await asyncio.sleep(999)
            return
        now = datetime.now(timezone.utc)
        for i in range(2):
            yield _FakeMsg(ent.id * 100 + i, now, f"삼성전자 005930 테스트 {ent.id}-{i}")


def check_hang_does_not_poison_next_channel() -> None:
    print("\n=== 채널 타임아웃: 멎은 채널이 다음 채널을 오염시키지 않음 ===")
    client_mod._CHANNEL_TIMEOUT_SEC = 1
    client_mod._MAX_CONSECUTIVE_CHANNEL_TIMEOUTS = 5
    fc = _FakeClient([(1, "정상1", "ok"), (2, "먹통", "hang"), (3, "정상2", "ok")])
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    rows, channels, stats = asyncio.run(client_mod.fetch_recent(fc, None, since, 500))

    _assert(
        stats == {"total": 3, "processed": 3, "succeeded": 2, "failed": 1},
        f"3채널 모두 처리·2성공·1실패, got {stats}",
    )
    ids = {r["channel_id"] for r in rows}
    _assert(ids == {1, 3}, f"멎은 채널(2) 제외, 정상 채널(1,3) 결과 포함, got {ids}")
    _assert(len(rows) == 4, f"채널당 2건씩 총 4건, got {len(rows)}")
    _assert(len(channels) == 3, "메타는 훑은 채널 전부(멎은 채널 포함) 기록됨")


def check_consecutive_timeouts_abort_cycle() -> None:
    print("\n=== 채널 타임아웃: 연속 타임아웃 초과 시 사이클 조기 종료 ===")
    client_mod._CHANNEL_TIMEOUT_SEC = 1
    client_mod._MAX_CONSECUTIVE_CHANNEL_TIMEOUTS = 1  # 2번 연속 타임아웃이면 중단
    fc = _FakeClient(
        [(1, "먹통1", "hang"), (2, "먹통2", "hang"), (3, "먹통3", "hang"), (4, "정상", "ok")]
    )
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    rows, channels, stats = asyncio.run(client_mod.fetch_recent(fc, None, since, 500))

    _assert(stats["processed"] == 2, f"연속 타임아웃 2회에서 조기 종료, processed={stats}")
    _assert(stats["succeeded"] == 0 and stats["failed"] == 2, f"stats={stats}")
    _assert(rows == [], "중단됐으므로 뒤쪽 정상 채널(4)엔 아예 도달 못함")
    _assert(len(channels) == 2, "조기 종료 전까지 훑은 채널만 메타에 기록(나머지는 다음 사이클)")


def main() -> None:
    print(f"임시 홈: {_TMP}")
    check_hang_does_not_poison_next_channel()
    check_consecutive_timeouts_abort_cycle()
    print("\nOK - 채널별 타임아웃 격리 정상")


if __name__ == "__main__":
    main()
