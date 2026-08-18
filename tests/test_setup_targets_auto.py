"""`--target auto` 가 ChatGPT 만 쓰는 PC에서 codex 를 고르는지 — TelegramLens.

예전에는 Claude 를 하나도 못 찾으면 무조건 claude-desktop 으로 떨어져서, ChatGPT 만
쓰는 사람에게 없는 앱의 설정 파일을 만들고 "등록 완료"라고 말했다.
"""

from __future__ import annotations

from unittest.mock import patch

from telegram_lens import setup_claude


def test_auto_picks_codex_when_no_claude_present(tmp_path):
    def which(name):
        return None if name == "claude" else str(tmp_path / "codex.exe")

    with patch.object(setup_claude.shutil, "which", side_effect=which), \
         patch.object(
             setup_claude, "get_claude_desktop_config_path",
             return_value=tmp_path / "nope" / "claude_desktop_config.json",
         ):
        assert setup_claude._resolve_targets("auto") == ["codex"]


def test_auto_keeps_claude_desktop_fallback_when_nothing_found(tmp_path):
    with patch.object(setup_claude.shutil, "which", return_value=None), \
         patch.object(
             setup_claude, "get_claude_desktop_config_path",
             return_value=tmp_path / "nope" / "claude_desktop_config.json",
         ), \
         patch.object(
             setup_claude, "get_codex_config_path",
             return_value=tmp_path / "nope" / ".codex" / "config.toml",
         ):
        assert setup_claude._resolve_targets("auto") == ["claude-desktop"]


def test_registered_command_prefers_uv_managed_binary(tmp_path):
    """설정 파일에 적히는 실행 파일은 uv 관리본이어야 한다 — 옛 pip 잔재가 PATH 앞에
    있으면 Manager 로 최신을 올려도 호스트 앱이 옛 버전을 띄운다(실기기 확인)."""
    uv_bin = tmp_path / "uv" / "bin"
    uv_bin.mkdir(parents=True)
    uv_exe = uv_bin / "telegramlens.exe"
    uv_exe.write_text("", encoding="utf-8")
    pip_exe = tmp_path / "Scripts" / "telegramlens.exe"
    pip_exe.parent.mkdir(parents=True)
    pip_exe.write_text("", encoding="utf-8")

    with patch.object(setup_claude, "_uv_tool_bin_dirs", return_value=[uv_bin]), \
         patch.object(setup_claude.shutil, "which", return_value=str(pip_exe)):
        assert setup_claude.resolve_server_entry("telegramlens")["command"] == str(uv_exe)
