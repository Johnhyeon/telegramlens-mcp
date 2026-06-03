"""경로·설정 관리.

모든 데이터(세션, DB, 종목사전, 추적채널)는 사용자 홈의
``~/.telegramlens/`` 아래에 저장된다. 데이터 주권은 사용자에게.

Telegram API 자격증명(API_ID / API_HASH)은 https://my.telegram.org 에서
발급받아 환경변수 또는 ``~/.telegramlens/credentials.json`` 에 저장한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def data_dir() -> Path:
    """TelegramLens 데이터 디렉토리. 없으면 생성."""
    override = os.environ.get("TELEGRAMLENS_HOME")
    base = Path(override) if override else (Path.home() / ".telegramlens")
    base.mkdir(parents=True, exist_ok=True)
    return base


def session_path() -> Path:
    """Telethon 세션 파일 경로(확장자 없이 — Telethon이 .session 부착)."""
    return data_dir() / "session"


def db_path() -> Path:
    return data_dir() / "telegramlens.db"


def stocks_path() -> Path:
    return data_dir() / "stocks.json"


def tracked_path() -> Path:
    return data_dir() / "tracked.json"


def _credentials_file() -> Path:
    return data_dir() / "credentials.json"


def get_credentials() -> tuple[int | None, str | None]:
    """(api_id, api_hash) 반환. 환경변수 우선, 없으면 credentials.json."""
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if api_id and api_hash:
        return int(api_id), api_hash

    f = _credentials_file()
    if f.exists():
        data = json.loads(f.read_text(encoding="utf-8"))
        aid = data.get("api_id")
        ah = data.get("api_hash")
        if aid and ah:
            return int(aid), str(ah)
    return None, None


def save_credentials(api_id: int, api_hash: str) -> None:
    f = _credentials_file()
    f.write_text(
        json.dumps({"api_id": int(api_id), "api_hash": api_hash}, indent=2),
        encoding="utf-8",
    )
    # 자격증명 파일 권한 최소화(가능한 플랫폼에서)
    try:
        f.chmod(0o600)
    except OSError:
        pass


def is_logged_in() -> bool:
    """세션 파일이 존재하는지(로그인 완료 여부의 약한 신호)."""
    return session_path().with_suffix(".session").exists()
