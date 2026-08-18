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
