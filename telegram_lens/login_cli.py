"""대화형 로그인 CLI — `telegramlens-login`.

전화번호 인증은 코드 입력이 필요해 MCP 툴 안에서 처리하기 어렵다.
이 스크립트로 한 번 로그인하면 세션 파일이 생기고, 이후 MCP 서버는
그 세션을 재사용한다.

설계 메모:
  - API_ID/HASH 는 각 사용자가 my.telegram.org 에서 직접 발급한다(공유 api_id 금지 —
    텔레그램이 다수 계정 공유 패턴을 어뷰징으로 보고 api_id 자체를 정지시킬 수 있다).
  - 발급이 온보딩 최대 마찰점이라, 브라우저 자동 오픈 + 단계 안내 + 안심 블록으로
    손을 잡아준다.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import webbrowser

from telegram_lens.config import (
    data_dir,
    get_credentials,
    save_credentials,
    session_path,
)
from telegram_lens.stocks import refresh_stocks
from telegram_lens.db import init_db

_MY_TELEGRAM = "https://my.telegram.org"


def _print_safety_note() -> None:
    """계정 안전 안심 블록 — '내 텔레그램 로그인 = 털리나?' 공포를 낮춘다."""
    home = data_dir()
    print("─" * 56)
    print("  안전 안내")
    print(f"  · 로그인 세션과 자격증명은 이 PC `{home}` 에만 저장됩니다.")
    print("  · 외부 서버로 전송하지 않습니다. 데이터는 전적으로 당신 것입니다.")
    print("  · 자동매매·송금 기능은 없습니다. 읽기(채널 메시지 수집)만 합니다.")
    print("─" * 56)
    print()


def _prompt_api_credentials() -> tuple[int, str]:
    """API_ID/HASH 발급을 손잡고 안내한 뒤 입력받아 저장한다."""
    print("=== Telegram API 자격증명 발급 ===")
    print()
    print(f"브라우저로 {_MY_TELEGRAM} 을 엽니다.")
    print("(자동으로 안 열리면 주소를 직접 입력하세요.)")
    print()
    print("발급 순서:")
    print("  1) 본인 전화번호로 로그인 (텔레그램으로 코드가 옵니다)")
    print("  2) 'API development tools' 클릭")
    print("  3) App title / Short name 에 아무 이름이나 입력 후 Create")
    print("     (URL·Platform 칸은 비워도 됩니다)")
    print("  4) 화면에 나온 api_id(숫자)와 api_hash(영숫자)를 아래에 붙여넣기")
    print()
    print("  ※ 처음 Create 시 'ERROR' 가 떠도 당황 말고 한 번 더 시도하면 됩니다.")
    print()

    try:
        webbrowser.open(_MY_TELEGRAM)
    except Exception:
        pass  # 브라우저 못 열어도 안내문은 이미 출력됨

    while True:
        raw_id = input("API_ID (숫자): ").strip()
        try:
            api_id = int(raw_id)
        except ValueError:
            print("  api_id 는 숫자만 들어갑니다. 다시 확인해 주세요.\n")
            continue
        api_hash = input("API_HASH (영숫자): ").strip()
        if not api_hash:
            print("  api_hash 가 비어 있습니다. 다시 입력해 주세요.\n")
            continue
        break

    save_credentials(api_id, api_hash)
    print("\n자격증명 저장 완료.\n")
    return api_id, api_hash


async def _sign_in_with_code(client, phone: str) -> None:
    """인증 코드 입력 — 오타로 인한 실패는 재시도하게 한다."""
    from telethon.errors import (
        PhoneCodeInvalidError,
        SessionPasswordNeededError,
    )

    while True:
        code = input("받은 인증 코드: ").strip()
        try:
            await client.sign_in(phone, code)
            return
        except PhoneCodeInvalidError:
            print("  코드가 올바르지 않습니다. 다시 입력해 주세요.\n")
            continue
        except SessionPasswordNeededError:
            # 2단계 인증(클라우드 비밀번호)이 걸린 계정
            pw = getpass.getpass("2단계 인증 비밀번호: ")
            await client.sign_in(password=pw)
            return


async def _login() -> None:
    from telethon import TelegramClient

    from telegram_lens.client import connect_with_timeout, disconnect_safely

    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        api_id, api_hash = _prompt_api_credentials()

    _print_safety_note()

    client = TelegramClient(str(session_path()), api_id, api_hash)

    try:
        await connect_with_timeout(client)
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"이미 로그인됨: {me.first_name} (@{me.username})")
            return

        print("이제 텔레그램 계정으로 로그인합니다.")
        phone = input("전화번호 (예: +821012345678): ").strip()
        await client.send_code_request(phone)
        print("텔레그램 앱으로 인증 코드를 보냈습니다.\n")

        await _sign_in_with_code(client, phone)

        me = await client.get_me()
        print(f"\n로그인 성공: {me.first_name} (@{me.username})")
    finally:
        await disconnect_safely(client)


def _emit(payload: dict) -> None:
    """JSON-Lines 프로토콜의 한 줄 — LeetKit Manager가 stdout에서 한 줄씩 읽는다."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


async def _read_stdin_json() -> dict | None:
    """LeetKit Manager가 stdin에 써주는 한 줄(JSON)을 기다린다. EOF(상대가 프로세스를
    끝냈거나 파이프가 닫힘)면 None — 호출자는 즉시 정리하고 반환해야 한다."""
    loop = asyncio.get_event_loop()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _me_dict(me) -> dict:
    return {"first_name": me.first_name, "username": me.username}


# send_code_request()가 실제로 어떤 경로로 코드를 보냈는지 — "코드가 안 온다" 문의의
# 절반은 SMS만 보고 텔레그램 앱 자체로 온 걸 놓치는 경우다(계정이 이미 어딘가 로그인돼
# 있으면 텔레그램은 SMS보다 앱 내 메시지를 우선한다). 이걸 사용자에게 그대로 알려주면
# 어디를 봐야 할지 헷갈릴 일이 없다.
_SENT_CODE_TYPE_LABELS = {
    "SentCodeTypeApp": "텔레그램 앱으로 전송됨 — 앱 채팅 목록 맨 위(Telegram 계정)를 확인하세요",
    "SentCodeTypeSms": "SMS 문자로 전송됨",
    "SentCodeTypeCall": "전화(음성 안내)로 전송됨 — 곧 전화가 옵니다",
    "SentCodeTypeFlashCall": "발신자 번호 뒷자리가 곧 코드입니다 — 부재중 전화를 확인하세요",
    "SentCodeTypeMissedCall": "부재중 전화 뒷자리가 코드입니다",
    "SentCodeTypeFragmentSms": "Fragment 번호로 전송됨",
}


def _sent_code_channel(sent_code) -> str:
    type_name = type(sent_code.type).__name__
    return _SENT_CODE_TYPE_LABELS.get(type_name, f"전송됨({type_name})")


async def _run_stepper() -> None:
    """`--stepper` 모드 — GUI(LeetKit Manager)가 단계별로 대화하며 로그인을 대신 진행할
    수 있게 stdin/stdout으로 JSON 한 줄씩 주고받는다. 기본(대화형) 흐름과 실제 Telethon
    호출은 동일하다 — input()/getpass() 네 곳만 stdin 읽기로 바뀐 것.

    한 줄 = 상태 하나: need_credentials/need_phone/code_sent/need_2fa(요청) →
    already_logged_in/ok(완료) 또는 error(같은 단계 재시도 — 프로세스는 안 죽는다).
    """
    from telethon import TelegramClient
    from telethon.errors import (
        FloodWaitError,
        PasswordHashInvalidError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )

    from telegram_lens.client import connect_with_timeout, disconnect_safely
    from telegram_lens.daemon import lock_path
    from telegram_lens.procstate import DaemonLock

    if DaemonLock(lock_path()).is_held():
        # 데몬(Claude Desktop이 열려 있을 때 같이 뜨는 프로세스)이 이미 session.session
        # 파일을 쥐고 있으면, 여기서 또 열려는 순간 SQLite가 "database is locked"로
        # 죽는다(실제로 재현해서 확인함) — 그 cryptic한 에러 대신 바로 원인을 알려준다.
        _emit({
            "status": "error", "code": "DAEMON_ACTIVE",
            "message": "Claude Desktop이 열려 있어 세션 파일을 사용 중입니다. Claude Desktop을 완전히 종료한 뒤 다시 시도하세요.",
        })
        return

    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        _emit({"status": "need_credentials"})
        msg = await _read_stdin_json()
        if msg is None:
            return
        try:
            api_id = int(msg.get("api_id"))
        except (TypeError, ValueError):
            _emit({"status": "error", "code": "CREDENTIALS_INVALID", "message": "API_ID는 숫자여야 합니다."})
            return
        api_hash = str(msg.get("api_hash") or "").strip()
        if not api_hash:
            _emit({"status": "error", "code": "CREDENTIALS_INVALID", "message": "API_HASH가 비어 있습니다."})
            return
        save_credentials(api_id, api_hash)

    client = TelegramClient(str(session_path()), api_id, api_hash)
    logged_in = False
    try:
        await connect_with_timeout(client)

        if await client.is_user_authorized():
            me = await client.get_me()
            _emit({"status": "already_logged_in", "me": _me_dict(me)})
            logged_in = True
        else:
            phone: str | None = None
            while phone is None:
                _emit({"status": "need_phone"})
                msg = await _read_stdin_json()
                if msg is None:
                    return
                candidate = str(msg.get("phone") or "").strip()
                try:
                    sent = await client.send_code_request(candidate)
                except PhoneNumberInvalidError:
                    _emit({"status": "error", "code": "PHONE_INVALID", "message": "전화번호 형식이 올바르지 않습니다."})
                    continue
                except FloodWaitError as e:
                    # 같은 api_id/번호로 코드 요청을 너무 많이 반복하면(테스트 중 재시도 등)
                    # 텔레그램이 일정 시간 코드 발송 자체를 막는다 — send_code_request는
                    # 예외 없이 조용히 "성공"하지 않고 여기서 바로 알 수 있게 걸린다.
                    _emit({
                        "status": "error", "code": "FLOOD_WAIT",
                        "message": f"요청이 너무 잦아 텔레그램이 {e.seconds}초 동안 코드 발송을 제한했습니다. 잠시 후 다시 시도하세요.",
                    })
                    continue
                phone = candidate
                _emit({"status": "code_sent", "channel": _sent_code_channel(sent)})

            signed_in = False
            while not signed_in:
                msg = await _read_stdin_json()
                if msg is None:
                    return
                code = str(msg.get("code") or "").strip()
                try:
                    await client.sign_in(phone, code)
                    signed_in = True
                except PhoneCodeInvalidError:
                    _emit({"status": "error", "code": "CODE_INVALID", "message": "인증 코드가 올바르지 않습니다."})
                    continue
                except SessionPasswordNeededError:
                    while True:
                        _emit({"status": "need_2fa"})
                        msg = await _read_stdin_json()
                        if msg is None:
                            return
                        password = str(msg.get("password") or "")
                        try:
                            await client.sign_in(password=password)
                            signed_in = True
                            break
                        except PasswordHashInvalidError:
                            _emit({"status": "error", "code": "2FA_INVALID", "message": "2단계 인증 비밀번호가 올바르지 않습니다."})
                            continue

            me = await client.get_me()
            _emit({"status": "ok", "me": _me_dict(me)})
            logged_in = True
    except Exception as e:  # noqa: BLE001 — 예상 못 한 오류도 트레이스백 대신 상태 한 줄로
        _emit({"status": "error", "code": "UNEXPECTED", "message": str(e)})
        return
    finally:
        await disconnect_safely(client)

    if logged_in:
        init_db()
        refresh_stocks()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="telegramlens-login", description="TelegramLens 로그인.")
    p.add_argument(
        "--stepper", action="store_true",
        help="JSON-Lines 단계별 프로토콜로 동작(LeetKit Manager 등 GUI가 대신 진행할 때 사용).",
    )
    return p


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        # Windows 콘솔은 기본이 cp949라, --stepper의 stdout이 utf-8이라고 믿고 읽는
        # LeetKit Manager(InteractiveProcess)가 디코딩 에러를 EOF로 오인해 조용히
        # 죽는다(실제로 재현해서 확인함) — doctor.py --json과 동일하게 여기서도 맞춘다.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = _build_parser().parse_args()

    if args.stepper:
        asyncio.run(_run_stepper())
        return

    print("TelegramLens 로그인\n")
    asyncio.run(_login())

    print("\nDB 초기화 중...")
    init_db()
    print("종목 사전 받는 중(KRX)...")
    stocks = refresh_stocks()
    print(f"종목 {len(stocks)}개 준비 완료.")
    print("\n준비 끝. 이제 `telegramlens-setup` 으로 MCP 서버를 Claude에 등록하세요.")
    print("등록 후 Claude를 켜면 백그라운드 수집이 자동으로 시작됩니다.")
    print("(수집 상태는 Claude에서 telegram_status 로 확인할 수 있습니다.)")


if __name__ == "__main__":
    main()
