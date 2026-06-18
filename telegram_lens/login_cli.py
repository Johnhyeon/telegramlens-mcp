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

import asyncio
import getpass
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

    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        api_id, api_hash = _prompt_api_credentials()

    _print_safety_note()

    client = TelegramClient(str(session_path()), api_id, api_hash)
    await client.connect()

    try:
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
        await client.disconnect()


def main() -> None:
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
