"""트레이가 macOS에서 프로세스를 죽이지 않는지 — 실제 크래시 리포트에서 나온 회귀.

구매자 맥에서 "파이썬 응용 프로그램이 예기치 않게 종료되었습니다"가 반복해서 떴다.
크래시 리포트가 가리킨 지점:

    _tkinter_create → Tk_InitOptions → Tk_GetColor → GetRGBA
    → doesNotRecognizeSelector → abort()          (SIGABRT, procRole=Background)

Homebrew Tk 8.6이 창 없는 백그라운드 프로세스에서 초기화되면 Objective-C 예외로
프로세스를 죽인다. Python 예외가 아니라 try/except로는 못 막으므로, macOS에서는
tkinter에 손을 대지 않는 것 말고는 방법이 없다.

실행: python tests/smoke_tray_macos.py
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from telegram_lens import server, tray  # noqa: E402


def _tkinter_attempted_on(platform: str) -> bool:
    """그 플랫폼에서 _run이 tkinter를 import하려 드는가."""
    attempted = []
    real_import = builtins.__import__

    def spy(name, *args, **kwargs):
        if name == "tkinter":
            attempted.append(name)
            raise ImportError("이 테스트에서는 tkinter를 막는다")
        return real_import(name, *args, **kwargs)

    fake_pystray = MagicMock()
    with patch.object(tray.sys, "platform", platform), \
         patch.object(builtins, "__import__", spy), \
         patch.object(tray, "_build_menu", return_value=None), \
         patch.object(tray, "_icon_for", return_value=None):
        tray._run(fake_pystray)
    return bool(attempted)


def main() -> int:
    failures = []

    if _tkinter_attempted_on("darwin"):
        failures.append("macOS에서 tkinter를 import했다 — Tk 초기화가 프로세스를 죽인다")
    else:
        print("[OK] macOS: tkinter에 손대지 않음")

    if not _tkinter_attempted_on("win32"):
        failures.append("Windows에서 tkinter를 안 썼다 — 진행상황 창이 사라진다")
    else:
        print("[OK] Windows: tkinter 그대로 사용")

    # 죽는 트레이를 무한히 되살리면 크래시 알림이 끝없이 뜬다 — 몇 번 만에 포기해야 한다.
    if not isinstance(getattr(server, "_TRAY_MAX_FAILURES", None), int):
        failures.append("_TRAY_MAX_FAILURES가 없다 — 감시 루프가 무한 재기동한다")
    elif server._TRAY_MAX_FAILURES < 1:
        failures.append(f"_TRAY_MAX_FAILURES가 {server._TRAY_MAX_FAILURES}라 아예 안 띄운다")
    else:
        print(f"[OK] 트레이 재기동 상한: {server._TRAY_MAX_FAILURES}회")

    for f in failures:
        print(f"[FAIL] {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
