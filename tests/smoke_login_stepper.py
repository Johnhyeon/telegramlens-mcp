"""오프라인 스모크 테스트 — login_cli.py의 --stepper(JSON-Lines) 프로토콜.

실제 Telethon/네트워크 없이 가짜 클라이언트로 각 분기(이미 로그인됨/정상 로그인/
잘못된 전화번호·코드·2단계 비밀번호 재시도)를 검증한다. `_read_stdin_json`을
monkeypatch해서 stdin을 흉내 낸 큐에서 한 번에 하나씩 소비한다.
"""

import asyncio
import os
import tempfile
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="tglens_login_stepper_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

from telegram_lens import login_cli  # noqa: E402
from telethon.errors import (  # noqa: E402
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


class _FakeMe:
    first_name = "홍길동"
    username = "honggd"


class _FakeClient:
    """실제 TelegramClient의 필요한 부분만 흉내 — 시나리오별로 어떤 예외를 던질지
    받는다."""

    def __init__(self, *, already_authorized=False, code_attempts=None, needs_2fa=False,
                 correct_password="right"):
        self._already_authorized = already_authorized
        self._code_attempts = list(code_attempts or ["000000"])  # 마지막이 정답
        self._needs_2fa = needs_2fa
        self._correct_password = correct_password
        self._connected = True

    async def connect(self):
        return None

    def is_connected(self):
        return self._connected

    async def disconnect(self):
        self._connected = False

    async def is_user_authorized(self):
        return self._already_authorized

    async def send_code_request(self, phone):
        if phone == "invalid":
            raise PhoneNumberInvalidError(request=None)
        return None

    async def sign_in(self, phone=None, code=None, *, password=None):
        if password is not None:
            if password != self._correct_password:
                raise PasswordHashInvalidError(request=None)
            return None
        expected_code = self._code_attempts.pop(0)
        if code != expected_code:
            if self._needs_2fa and not self._code_attempts:
                raise SessionPasswordNeededError(request=None)
            raise PhoneCodeInvalidError(request=None)
        if self._needs_2fa:
            raise SessionPasswordNeededError(request=None)
        return None

    async def get_me(self):
        return _FakeMe()


def _make_stdin_feeder(messages):
    queue = list(messages)

    async def _fake_read():
        if not queue:
            return None
        return queue.pop(0)

    return _fake_read


def _run_stepper_with(fake_client, stdin_messages, *, with_credentials=True):
    emitted = []

    def _fake_emit(payload):
        emitted.append(payload)

    with patch.object(login_cli, "_emit", side_effect=_fake_emit), \
         patch.object(login_cli, "_read_stdin_json", side_effect=_make_stdin_feeder(stdin_messages)), \
         patch("telethon.TelegramClient", return_value=fake_client), \
         patch.object(login_cli, "init_db", return_value=None), \
         patch.object(login_cli, "refresh_stocks", return_value=[]), \
         patch.object(
             login_cli, "get_credentials",
             return_value=(12345, "hash") if with_credentials else (None, None),
         ), \
         patch.object(login_cli, "save_credentials", return_value=None):
        asyncio.run(login_cli._run_stepper())
    return emitted


def check_already_logged_in_short_circuits() -> None:
    print("\n=== 이미 로그인된 세션이면 phone/code 단계 없이 바로 종료 ===")
    client = _FakeClient(already_authorized=True)
    emitted = _run_stepper_with(client, [])
    _assert(len(emitted) == 1, f"상태 한 줄만 emit, got {emitted}")
    _assert(emitted[0]["status"] == "already_logged_in", f"already_logged_in, got {emitted}")


def check_happy_path_phone_then_code() -> None:
    print("\n=== 정상 흐름: need_phone → code_sent → ok ===")
    client = _FakeClient(code_attempts=["999999"])
    emitted = _run_stepper_with(client, [{"phone": "+821012345678"}, {"code": "999999"}])
    statuses = [e["status"] for e in emitted]
    _assert(statuses == ["need_phone", "code_sent", "ok"], f"got {statuses}")
    _assert(emitted[-1]["me"]["username"] == "honggd", "me 정보 포함")


def check_wrong_code_retries_same_step() -> None:
    print("\n=== 잘못된 코드는 같은 code 단계를 재시도(프로세스 안 죽음) ===")
    client = _FakeClient(code_attempts=["111111", "999999"])
    emitted = _run_stepper_with(
        client, [{"phone": "+821012345678"}, {"code": "wrong"}, {"code": "999999"}]
    )
    statuses = [e["status"] for e in emitted]
    _assert(statuses == ["need_phone", "code_sent", "error", "ok"], f"got {statuses}")
    _assert(emitted[2]["code"] == "CODE_INVALID", "에러 코드 CODE_INVALID")


def check_invalid_phone_retries_same_step() -> None:
    print("\n=== 잘못된 전화번호는 need_phone을 재시도 ===")
    client = _FakeClient(code_attempts=["999999"])
    emitted = _run_stepper_with(
        client,
        [{"phone": "invalid"}, {"phone": "+821012345678"}, {"code": "999999"}],
    )
    statuses = [e["status"] for e in emitted]
    _assert(statuses == ["need_phone", "error", "need_phone", "code_sent", "ok"], f"got {statuses}")
    _assert(emitted[1]["code"] == "PHONE_INVALID", "에러 코드 PHONE_INVALID")


def check_2fa_flow_with_wrong_then_right_password() -> None:
    print("\n=== 2단계 인증: 잘못된 비밀번호 재시도 후 성공 ===")
    client = _FakeClient(needs_2fa=True, code_attempts=["999999"], correct_password="right")
    emitted = _run_stepper_with(
        client,
        [
            {"phone": "+821012345678"},
            {"code": "999999"},
            {"password": "wrong"},
            {"password": "right"},
        ],
    )
    statuses = [e["status"] for e in emitted]
    _assert(statuses == ["need_phone", "code_sent", "need_2fa", "error", "need_2fa", "ok"], f"got {statuses}")
    _assert(emitted[3]["code"] == "2FA_INVALID", "에러 코드 2FA_INVALID")


def check_missing_credentials_requests_them_first() -> None:
    print("\n=== API_ID/HASH 없으면 need_credentials부터 ===")
    client = _FakeClient(already_authorized=True)
    emitted = _run_stepper_with(
        client, [{"api_id": "12345", "api_hash": "somehash"}], with_credentials=False
    )
    statuses = [e["status"] for e in emitted]
    _assert(statuses == ["need_credentials", "already_logged_in"], f"got {statuses}")


def check_eof_on_stdin_exits_cleanly() -> None:
    print("\n=== stdin이 갑자기 닫히면(EOF) 무한 대기 없이 조용히 종료 ===")
    client = _FakeClient()
    emitted = _run_stepper_with(client, [])  # need_phone 이후 아무 것도 안 줌 → None
    statuses = [e["status"] for e in emitted]
    _assert(statuses == ["need_phone"], f"need_phone 한 번만 emit하고 종료, got {statuses}")


def check_daemon_active_blocks_before_connecting() -> None:
    """실제로 재현해서 발견한 버그: 데몬이 session.session을 쥐고 있는 동안 로그인을
    또 시도하면 SQLite가 'database is locked'로 죽는다 — 연결 시도 전에 먼저 걸러야 한다."""
    print("\n=== 데몬이 세션 파일을 쥐고 있으면 연결 시도 전에 DAEMON_ACTIVE로 막힘 ===")
    client = _FakeClient(already_authorized=True)
    with patch("telegram_lens.procstate.DaemonLock.is_held", return_value=True):
        emitted = _run_stepper_with(client, [])
    statuses = [e["status"] for e in emitted]
    _assert(statuses == ["error"], f"게이트에서 바로 막힘, got {statuses}")
    _assert(emitted[0]["code"] == "DAEMON_ACTIVE", f"에러 코드 DAEMON_ACTIVE, got {emitted[0]}")


def main() -> None:
    check_already_logged_in_short_circuits()
    check_happy_path_phone_then_code()
    check_wrong_code_retries_same_step()
    check_invalid_phone_retries_same_step()
    check_2fa_flow_with_wrong_then_right_password()
    check_missing_credentials_requests_them_first()
    check_eof_on_stdin_exits_cleanly()
    check_daemon_active_blocks_before_connecting()
    print("\nOK - login_cli --stepper 순수 로직 정상(실제 SMS 수신까지의 end-to-end는 실제 텔레그램 계정으로만 검증 가능)")


if __name__ == "__main__":
    main()
