"""대화형 로그인 CLI — `telegramlens-login`.

전화번호 인증은 코드 입력이 필요해 MCP 툴 안에서 처리하기 어렵다.
이 스크립트로 한 번 로그인하면 세션 파일이 생기고, 이후 MCP 서버는
그 세션을 재사용한다.
"""

from __future__ import annotations

import asyncio
import getpass

from telegram_lens.config import get_credentials, save_credentials, session_path
from telegram_lens.stocks import refresh_stocks
from telegram_lens.db import init_db


async def _login() -> None:
    from telethon import TelegramClient

    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        print("=== Telegram API 자격증명 등록 ===")
        print("https://my.telegram.org → API development tools 에서 발급")
        api_id = int(input("API_ID: ").strip())
        api_hash = input("API_HASH: ").strip()
        save_credentials(api_id, api_hash)
        print("자격증명 저장 완료.\n")

    client = TelegramClient(str(session_path()), api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"이미 로그인됨: {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    phone = input("전화번호 (예: +821012345678): ").strip()
    await client.send_code_request(phone)
    code = input("받은 인증 코드: ").strip()
    try:
        await client.sign_in(phone, code)
    except Exception as e:  # 2단계 인증 비밀번호 필요한 경우
        if "password" in str(e).lower() or "2FA" in str(e):
            pw = getpass.getpass("2단계 인증 비밀번호: ")
            await client.sign_in(password=pw)
        else:
            raise

    me = await client.get_me()
    print(f"로그인 성공: {me.first_name} (@{me.username})")
    await client.disconnect()


def main() -> None:
    print("TelegramLens 로그인\n")
    asyncio.run(_login())

    print("\nDB 초기화 중...")
    init_db()
    print("종목 사전 받는 중(KRX)...")
    stocks = refresh_stocks()
    print(f"종목 {len(stocks)}개 준비 완료.")
    print("\n준비 끝. 이제 MCP 서버(`telegramlens`)를 Claude에 등록하세요.")


if __name__ == "__main__":
    main()
