"""오프라인 스모크 테스트 — doctor.py가 Codex(TOML) 등록 상태를 실제로 감지하는지.

실사용 중 발견된 문제 재현: setup_claude.py는 Phase C에서 --target codex를 지원하게
됐지만, doctor.py는 여전히 claude-desktop/claude-code(JSON)만 확인하고 있어서 실제로
Codex에 정상 등록해도 Manager 대시보드의 targets/체크박스에는 전혀 반영되지 않았다.
"""

import sys
import tempfile
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tomlkit  # noqa: E402

from telegram_lens import doctor  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def check_info_skip_when_file_missing() -> None:
    print("\n=== config.toml이 없으면 info-skip ===")
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "config.toml"
        result = doctor._check_config_toml_file("Codex CLI", missing, required=False)
        _assert(result.status == "info-skip", f"got {result.status}")


def check_ok_when_entry_present() -> None:
    print("\n=== 등록된 entry가 있으면 ok ===")
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.toml"
        fake_exe = Path(tmp) / "telegramlens.exe"
        fake_exe.write_text("", encoding="utf-8")
        doc = tomlkit.document()
        doc["mcp_servers"] = tomlkit.table()
        server = tomlkit.table()
        server["command"] = str(fake_exe)
        doc["mcp_servers"][doctor.SERVER_KEY] = server
        config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        result = doctor._check_config_toml_file("Codex CLI", config_path, required=False)
        _assert(result.status == "ok", f"got {result.status}")


def check_other_servers_do_not_count_as_registered() -> None:
    print("\n=== 다른 MCP 서버만 있고 telegramlens 항목은 없으면 info-skip ===")
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.toml"
        doc = tomlkit.document()
        doc["mcp_servers"] = tomlkit.table()
        other = tomlkit.table()
        other["command"] = "npx"
        doc["mcp_servers"]["github"] = other
        config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        result = doctor._check_config_toml_file("Codex CLI", config_path, required=False)
        _assert(result.status == "info-skip", f"got {result.status}")


def check_registered_targets_includes_codex() -> None:
    print("\n=== _registered_targets가 codex를 포함한다 ===")
    desktop_check = doctor.Check("d")
    desktop_check.status = "fail"
    code_check = doctor.Check("c")
    code_check.status = "fail"
    codex_check = doctor.Check("x")
    codex_check.status = "ok"

    targets = doctor._registered_targets(desktop_check, code_check, codex_check)
    _assert(targets == ["codex"], f"got {targets}")


def check_at_least_one_config_ok_when_only_codex() -> None:
    print("\n=== check_at_least_one_config: codex만 등록돼도 ok ===")
    desktop_check = doctor.Check("d")
    desktop_check.status = "fail"
    code_check = doctor.Check("c")
    code_check.status = "fail"
    codex_check = doctor.Check("x")
    codex_check.status = "ok"

    result = doctor.check_at_least_one_config(desktop_check, code_check, codex_check)
    _assert(result.status == "ok", f"got {result.status}")


def main() -> None:
    check_info_skip_when_file_missing()
    check_ok_when_entry_present()
    check_other_servers_do_not_count_as_registered()
    check_registered_targets_includes_codex()
    check_at_least_one_config_ok_when_only_codex()
    print("\nOK - doctor.py Codex(TOML) 등록 감지 정상")


if __name__ == "__main__":
    main()
