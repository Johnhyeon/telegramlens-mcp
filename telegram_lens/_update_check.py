"""업데이트 알림 — PyPI 최신 버전 + GitHub Release 노트 조회.

StockLens·DartLens는 새 버전이 나오면 Claude 응답 안에서 알려주는데 TelegramLens만
그 통로가 없었다. 사람들은 LeetKit Manager를 잘 안 열어서, Manager에만 표시하면
옛 버전을 계속 쓰게 된다 — 세 Lens 동작을 같은 모양으로 맞춘다.

동작 원리(StockLens `_update_check`와 동일):
- 프로세스당 1회만 체크 (도구를 수백 번 불러도 한 번)
- 하루 1회 캐시 (~/.telegramlens/update_check.json)
- 네트워크 실패 시 조용히 빈 문자열 (도구 동작을 방해하지 않는다)
- TELEGRAMLENS_FORCE_UPDATE_NOTICE=1 로 강제 테스트

안내는 LeetKit Manager 하나만 말한다. 터미널 명령을 적어두면 주 고객층은 거기서
막힌다 — Manager가 있는 이유가 그 명령을 안 치게 하려는 것이다.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from telegram_lens import __version__

PYPI_URL = "https://pypi.org/pypi/telegramlens-mcp/json"
GITHUB_URL = "https://api.github.com/repos/Johnhyeon/telegramlens-mcp/releases/latest"
CACHE_TTL = timedelta(hours=24)
TIMEOUT = 3.0
MAX_NOTE_LINES = 8

_notice_issued: bool = False


def _cache_file() -> Path:
    # 홈을 옮겨 쓰는 사람(TELEGRAMLENS_HOME)도 따라간다 — config.data_dir 규칙과 같다.
    from telegram_lens.config import data_dir

    return data_dir() / "update_check.json"


def _load_cache() -> dict | None:
    try:
        path = _cache_file()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(data.get("checked_at", ""))
        if datetime.now() - checked_at > CACHE_TTL:
            return None
        return data
    except Exception:
        return None


def _save_cache(latest_version: str, release_notes: str) -> None:
    try:
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "checked_at": datetime.now().isoformat(),
                    "latest_version": latest_version,
                    "release_notes": release_notes,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


async def _fetch_latest() -> tuple[str, str] | None:
    """PyPI + GitHub 병렬 호출. 네트워크 실패 시 None."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            pypi_resp, gh_resp = await asyncio.gather(
                client.get(PYPI_URL),
                client.get(GITHUB_URL),
                return_exceptions=True,
            )
        latest_version = ""
        release_notes = ""
        if not isinstance(pypi_resp, Exception) and pypi_resp.status_code == 200:
            latest_version = pypi_resp.json().get("info", {}).get("version", "") or ""
        if not isinstance(gh_resp, Exception) and gh_resp.status_code == 200:
            release_notes = gh_resp.json().get("body", "") or ""
        if latest_version:
            return latest_version, release_notes
    except Exception:
        pass
    return None


def _version_gt(latest: str, current: str) -> bool:
    """semver 비교. 실패 시 단순 비교로 물러난다."""
    try:
        from packaging.version import Version

        return Version(latest) > Version(current)
    except Exception:
        return latest != current and latest != ""


def _format_notice(latest: str, current: str, notes: str) -> str:
    lines = [ln for ln in notes.strip().split("\n") if ln.strip()][:MAX_NOTE_LINES]
    notes_text = "\n".join(lines) if lines else "(릴리즈 노트 없음)"
    return (
        f"\n\n---\n"
        f"ℹ️ TelegramLens 업데이트 정보\n"
        f"새 버전: v{latest} (현재 v{current})\n"
        f"업데이트: LeetKit Manager를 열고 [지금 업데이트]를 눌러주세요.\n\n"
        f"주요 변경:\n{notes_text}"
    )


async def get_update_notice() -> str:
    """업데이트 알림 문자열. 알릴 게 없거나 실패하면 빈 문자열."""
    global _notice_issued

    force = os.environ.get("TELEGRAMLENS_FORCE_UPDATE_NOTICE") == "1"
    if _notice_issued and not force:
        return ""

    cached = _load_cache()
    if cached:
        latest = cached.get("latest_version", "")
        notes = cached.get("release_notes", "")
    else:
        result = await _fetch_latest()
        if not result:
            return ""
        latest, notes = result
        _save_cache(latest, notes)

    current = __version__
    if not force and not _version_gt(latest, current):
        _notice_issued = True  # 확인은 했으니 다시 안 한다
        return ""

    _notice_issued = True
    return _format_notice(latest, current, notes)
