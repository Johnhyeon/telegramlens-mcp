"""트레이 메뉴 [종료]를 눌렀을 때 되살아나지 않는지.

MCP 서버 감시 루프가 트레이를 되살린다(호스트 앱이 꺼졌다 켜지는 동안 아이콘이
사라지지 않게 하려는 것). 그런데 **사용자가 직접 껐을 때도** 되살려서, 껐는데 또
뜨는 상태였다 — 우리가 사용자 의사를 무시하는 것으로 읽힌다. 트레이는 상태를
보여주는 UI일 뿐이라 꺼도 수집은 그대로 돈다.

새 세션(호스트 앱을 새로 켬 = MCP 서버가 새로 뜸)에서는 다시 보여야 한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from telegram_lens import tray


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))


def test_flag_roundtrip():
    assert tray.is_dismissed() is False
    tray.mark_dismissed()
    assert tray.is_dismissed() is True
    tray.clear_dismissed()
    assert tray.is_dismissed() is False


def test_clear_is_safe_when_nothing_to_clear():
    """새로 깐 사람은 이 파일이 아예 없다 — 서버 기동 때마다 부르므로 조용해야 한다."""
    tray.clear_dismissed()
    tray.clear_dismissed()
    assert tray.is_dismissed() is False


def test_quit_marks_dismissed():
    """메뉴 [종료] 경로가 실제로 표시를 남기는지 — 이게 빠지면 다시 되살아난다."""
    icon = MagicMock()
    tray._quit(icon)
    assert tray.is_dismissed() is True
    icon.stop.assert_called_once()


def test_server_clears_the_flag_on_startup_and_honours_it_in_the_loop():
    """서버 코드가 두 곳을 실제로 부르는지 — 이름이 어긋나면 조용히 예전 동작으로 돌아간다."""
    from pathlib import Path

    src = Path(tray.__file__).with_name("server.py").read_text(encoding="utf-8")
    assert "clear_dismissed()" in src          # 새 세션에서 초기화
    assert "not tray.is_dismissed()" in src    # 감시 루프가 존중


def test_tray_is_checked_more_often_than_the_daemon():
    """호스트 앱을 바꿔 켜거나 [다시 시작]을 누르면 그 앱이 소유하던 트레이가 같이 죽는다.
    감시가 60초 주기였을 때는 그 사이 아이콘이 사라져 있어 "꺼졌나?"로 보였다.

    데몬 재기동 주기는 60초 그대로 둔다 — 크래시 루프에 빠진 데몬을 더 빨리 되살리면
    텔레그램 재접속을 그만큼 더 두드린다(flood wait). 수집 공백 자체는 데몬이 다음
    사이클에 캐치업하므로 서둘러서 얻을 게 없다."""
    from pathlib import Path

    src = Path(tray.__file__).with_name("server.py").read_text(encoding="utf-8")
    assert "_TRAY_CHECK_SEC = 15" in src
    assert "_DAEMON_CHECK_SEC = 60" in src
    assert "await asyncio.sleep(_TRAY_CHECK_SEC)" in src
    assert 'now - state["daemon_checked_at"] >= _DAEMON_CHECK_SEC' in src
