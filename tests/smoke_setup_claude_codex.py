"""오프라인 스모크 테스트 — Codex(TOML) MCP 등록 경로.

`_configure_toml_target`이 tomlkit으로 round-trip 편집하는지(다른 mcp_servers.* 항목·
주석 보존, 우리 섹션만 갱신, 백업 생성)를 검증한다. 실제 uv/PATH 탐색에 의존하지 않게
`resolve_server_entry`를 monkeypatch한다.
"""

import os
import tempfile
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="tglens_codex_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

import tomlkit  # noqa: E402

from telegram_lens import setup_claude  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def check_codex_config_path() -> None:
    print("\n=== get_codex_config_path(): ~/.codex/config.toml ===")
    _assert(
        setup_claude.get_codex_config_path().parts[-2:] == (".codex", "config.toml"),
        "경로가 ~/.codex/config.toml",
    )


def check_codex_in_targets() -> None:
    print("\n=== TARGETS에 codex 등록 ===")
    _assert("codex" in setup_claude.TARGETS, "codex 키 존재")
    path_func, label = setup_claude.TARGETS["codex"]
    _assert(label == "Codex CLI", f"라벨 'Codex CLI', got {label}")


def check_target_choices_include_codex() -> None:
    print("\n=== --target choices에 codex 포함 ===")
    parser = setup_claude._build_parser()
    target_action = next(a for a in parser._actions if a.dest == "target")
    _assert("codex" in target_action.choices, f"choices에 codex 포함, got {target_action.choices}")


def check_toml_round_trip_preserves_other_entries(tmp_path_str: str) -> None:
    print("\n=== _configure_toml_target: 다른 항목·주석 보존 ===")
    import pathlib

    config_path = pathlib.Path(tmp_path_str) / "config.toml"
    config_path.write_text(
        "# a comment that must survive\n"
        "[mcp_servers.github]\n"
        'command = "npx"\n'
        'args = ["-y", "@modelcontextprotocol/server-github"]\n',
        encoding="utf-8",
    )

    with patch.object(setup_claude, "resolve_server_entry", return_value={"command": "/fake/telegramlens"}):
        result = setup_claude._configure_toml_target(config_path, "Codex CLI", command="telegramlens")

    text = config_path.read_text(encoding="utf-8")
    _assert("# a comment that must survive" in text, "기존 주석 보존")
    doc = tomlkit.parse(text)
    _assert(doc["mcp_servers"]["github"]["command"] == "npx", "기존 github 항목 보존")
    _assert(doc["mcp_servers"]["telegramlens"]["command"] == "/fake/telegramlens", "telegramlens 항목 추가")
    _assert(result["backup_path"] == str(config_path.with_suffix(".toml.backup")), "백업 경로 반환")
    _assert((pathlib.Path(tmp_path_str) / "config.toml.backup").exists(), "백업 파일 생성됨")


def check_configure_one_target_dispatches_toml() -> None:
    print("\n=== _configure_one_target: .toml 확장자면 TOML 경로로 분기 ===")
    import pathlib

    config_path = pathlib.Path(_TMP) / "dispatch-check" / "config.toml"
    with patch.object(setup_claude, "_configure_toml_target", return_value={"ok": "toml"}) as mock_toml:
        result = setup_claude._configure_one_target(config_path, "Codex CLI", command="telegramlens")
    _assert(mock_toml.called, "_configure_toml_target 호출됨")
    _assert(result == {"ok": "toml"}, "반환값 그대로 전달")


def main() -> None:
    check_codex_config_path()
    check_codex_in_targets()
    check_target_choices_include_codex()
    check_toml_round_trip_preserves_other_entries(_TMP)
    check_configure_one_target_dispatches_toml()
    print("\nOK - Codex(TOML) 등록 경로 정상")


if __name__ == "__main__":
    main()
