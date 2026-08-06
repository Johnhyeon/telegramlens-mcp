"""데스크톱 트레이 아이콘 — 수집 상태를 색으로 보여준다. `telegramlens-tray`.

데몬이 자동으로 띄우지 않는 별도 수동 실행 프로그램이다(v1: Windows 우선). 새 판정
로직은 하나도 없다 — daemon_status.json 과 procstate.compute_health() 를 그대로
재사용해, 이미 doctor/telegram_status 가 쓰는 것과 동일한 기준으로 색만 입힌다.

초록(healthy) / 노랑(degraded) / 빨강(failed) / 회색(확인 불가)을 몇 초마다 갱신하고,
우클릭 메뉴로 즉시 새로고침·`telegramlens-doctor` 실행·종료를 제공한다.
"""

from __future__ import annotations

import subprocess
import sys
import time

from PIL import Image, ImageDraw

from telegram_lens import procstate
from telegram_lens.config import data_dir
from telegram_lens.daemon import lock_path as daemon_lock_path, status_path as daemon_status_path

_POLL_SEC = 5

_COLORS: dict[str, tuple[int, int, int, int]] = {
    "healthy": (34, 197, 94, 255),
    "degraded": (234, 179, 8, 255),
    "failed": (239, 68, 68, 255),
    "unknown": (148, 163, 184, 255),
}

_ICON_CACHE: dict[str, "Image.Image"] = {}


def _make_dot(color: tuple[int, int, int, int]) -> "Image.Image":
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color)
    return img


def _icon_for(health: str) -> "Image.Image":
    if health not in _ICON_CACHE:
        _ICON_CACHE[health] = _make_dot(_COLORS.get(health, _COLORS["unknown"]))
    return _ICON_CACHE[health]


def current_health() -> dict:
    """procstate.compute_health() 를 그대로 호출 — doctor/telegram_status 와 동일 기준.

    이 함수 자체가 실패해도(예: 상태 파일 손상 이상의 예외) 트레이 앱이 죽으면 안 되므로
    흡수하고 'unknown' 으로 표시한다.
    """
    try:
        held = procstate.DaemonLock(daemon_lock_path()).is_held()
        status = procstate.read_json(daemon_status_path())
        return procstate.compute_health(status, held)
    except Exception as e:  # noqa: BLE001
        return {"health": "unknown", "problem_code": None, "message": f"상태 확인 실패: {e}"}


def _run_doctor(icon) -> None:
    """telegramlens-doctor 를 별도 콘솔에 띄운다(Windows). 그 외 OS는 백그라운드 실행."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/k", "telegramlens-doctor"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            subprocess.Popen(["telegramlens-doctor"])
    except Exception as e:  # noqa: BLE001 — 실행 실패가 트레이 앱을 죽이면 안 됨
        try:
            icon.notify(f"doctor 실행 실패: {e}", "TelegramLens")
        except Exception:  # noqa: BLE001 — notify 미지원 플랫폼일 수 있음
            pass


def _quit(icon) -> None:
    icon.visible = False
    icon.stop()


def _refresh(icon, state: dict) -> None:
    health = current_health()
    state["summary"] = health["message"]
    icon.icon = _icon_for(health["health"])
    icon.title = f"TelegramLens — {health['message']}"


def _build_menu(state: dict):
    import pystray

    return pystray.Menu(
        pystray.MenuItem(lambda item: state.get("summary", "상태 확인 중..."), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("지금 새로고침", lambda icon, item: _refresh(icon, state)),
        pystray.MenuItem("telegramlens-doctor 실행", lambda icon, item: _run_doctor(icon)),
        pystray.MenuItem("종료", lambda icon, item: _quit(icon)),
    )


_QUIT_CHECK_SEC = 0.5


def _poll_loop(icon, state: dict) -> None:
    """상태는 _POLL_SEC 마다 갱신하되, '종료' 클릭 반응은 훨씬 짧은 주기로 확인한다 —
    한 번에 _POLL_SEC(5초)씩 자면 종료 버튼을 눌러도 최대 5초 멎어있는 것처럼 보인다."""
    icon.visible = True
    _refresh(icon, state)
    elapsed = 0.0
    while icon.visible:
        time.sleep(_QUIT_CHECK_SEC)
        elapsed += _QUIT_CHECK_SEC
        if elapsed < _POLL_SEC:
            continue
        elapsed = 0.0
        try:
            _refresh(icon, state)
        except Exception:  # noqa: BLE001 — 폴링 실패로 트레이 앱 자체가 죽으면 안 됨
            pass


def lock_path():
    return data_dir() / "tray.pid"


_tray_lock = procstate.DaemonLock(lock_path())


def is_alive() -> bool:
    """트레이 앱이 이미 떠 있는지(수동 실행이든 자동 스폰이든) — server.py 감시 루프가
    중복 스폰을 막는 데 쓴다. daemon.is_alive() 와 동일한 락 기반 판정."""
    return procstate.DaemonLock(lock_path()).is_held()


def spawn():
    """트레이 앱을 '평범한 자식 프로세스'로 띄운다 — MCP 서버 감시 루프가 호출.

    이미 떠 있으면(수동 실행 포함) None. daemon.spawn_child() 와 동일한 패턴 —
    detach·자동시작 레지스트리 없는 단순 자식 프로세스, stdio 는 DEVNULL.
    CREATE_NO_WINDOW 는 콘솔 창만 숨긴다 — 트레이 아이콘 자체는 그대로 보인다.
    """
    if is_alive():
        return None

    import shutil

    exe = shutil.which("telegramlens-tray")
    args = [exe] if exe else [sys.executable, "-m", "telegram_lens.tray"]

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.Popen(args, **kwargs)
    except OSError:
        return None


def main() -> None:
    import pystray

    if not _tray_lock.acquire():
        # 이미 다른 트레이 인스턴스가 떠 있음(수동 실행 포함) — 아이콘 중복 방지, 조용히 종료.
        # stdout 이 DEVNULL 인 자동 스폰 경로에서는 어차피 안 보이지만, 사용자가 터미널에서
        # 직접 실행했을 때는 '왜 아무것도 안 뜨지'로 헷갈리지 않게 이유를 남긴다.
        print("TelegramLens 트레이 아이콘이 이미 실행 중입니다.")
        return
    try:
        state: dict = {"summary": "상태 확인 중..."}
        icon = pystray.Icon(
            "telegramlens",
            _icon_for("unknown"),
            "TelegramLens",
            menu=_build_menu(state),
        )
        icon.run(setup=lambda ic: _poll_loop(ic, state))
    finally:
        _tray_lock.release()


if __name__ == "__main__":
    main()
