"""Claude(Desktop/Code)에 TelegramLens MCP 서버를 자동 등록.

설치 후 `telegramlens-setup` 실행. PATH 환경변수와 무관하게 실행 가능한
절대 경로를 찾아 config 에 기록한다.
"""

import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path

SERVER_KEY = "telegramlens"


def _uv_tool_bin_dirs() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("UV_TOOL_BIN_DIR")
    if env:
        candidates.append(Path(env))
    xdg = os.environ.get("XDG_BIN_HOME")
    if xdg:
        candidates.append(Path(xdg))
    candidates.append(Path.home() / ".local" / "bin")
    return [p for p in candidates if p.exists()]


def resolve_server_entry(preferred_command: str = "telegramlens") -> dict:
    """PATH 의존 없이 확실히 실행되는 MCP server config entry 생성."""
    if os.path.isabs(preferred_command) and Path(preferred_command).exists():
        return {"command": preferred_command}

    found = shutil.which(preferred_command)
    if found:
        return {"command": found}

    for bin_dir in _uv_tool_bin_dirs():
        for name in (f"{preferred_command}.exe", preferred_command):
            candidate = bin_dir / name
            if candidate.exists():
                return {"command": str(candidate)}

    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for name in (f"{preferred_command}.exe", preferred_command):
            candidate = scripts_dir / name
            if candidate.exists():
                return {"command": str(candidate)}
    except Exception:
        pass

    return {"command": sys.executable, "args": ["-m", "telegram_lens.server"]}


def _find_store_config_path() -> Path | None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    packages_dir = Path(local_appdata) / "Packages"
    if not packages_dir.exists():
        return None
    for pattern in ("Claude_*", "*Claude*"):
        for pkg in packages_dir.glob(pattern):
            candidate = (
                pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
            )
            if candidate.parent.exists():
                return candidate
    return None


def get_claude_desktop_config_path() -> Path:
    if sys.platform == "win32":
        store = _find_store_config_path()
        if store is not None:
            return store
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA environment variable not found.")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def get_claude_code_config_path() -> Path:
    return Path.home() / ".claude.json"


TARGETS: dict[str, tuple] = {
    "claude-desktop": (get_claude_desktop_config_path, "Claude Desktop"),
    "claude-code": (get_claude_code_config_path, "Claude Code CLI"),
}


def _resolve_targets(arg: str) -> list[str]:
    if arg == "both":
        return ["claude-desktop", "claude-code"]
    if arg in TARGETS:
        return [arg]
    if arg == "auto":
        env_target = (os.environ.get("TELEGRAMLENS_TARGET") or "").strip().lower()
        if env_target and env_target != "auto":
            return _resolve_targets(env_target)
        has_code = shutil.which("claude") is not None
        has_desktop = get_claude_desktop_config_path().parent.exists()
        if has_code and has_desktop:
            return ["claude-desktop", "claude-code"]
        if has_code:
            return ["claude-code"]
        return ["claude-desktop"]
    raise ValueError(f"Invalid target: {arg}")


def _configure_one_target(config_path: Path, label: str, *, command: str, quiet: bool = False) -> dict:
    """단일 config 파일에 mcpServers.telegramlens 등록. 변경 결과를 dict로 반환한다.

    quiet=True 면 사람용 진행 로그를 찍지 않는다(Manager의 --json/--non-interactive
    호출에서 stdout을 구조화 결과 하나로만 유지하기 위함).
    """
    if not quiet:
        print()
        print(f"  -> {label}")

    config_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            backup_path = config_path.with_suffix(".json.backup")
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            if not quiet:
                print(f"  [OK] Backup saved: {backup_path}")
        except json.JSONDecodeError:
            if not quiet:
                print("  [WARN] Existing config is corrupted. Creating new one.")
            config = {}
    else:
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    entry = resolve_server_entry(command)
    config["mcpServers"][SERVER_KEY] = entry

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    if not quiet:
        print(f"  [OK] Config updated (key: {SERVER_KEY})")
        print(f"  Path:    {config_path}")
        print(f"  Command: {entry['command']}")
        if "args" in entry:
            print(f"  Args:    {' '.join(entry['args'])}")

    return {
        "target_label": label,
        "config_path": str(config_path),
        "backup_path": str(backup_path) if backup_path else None,
        "command": entry["command"],
        "args": entry.get("args"),
    }


def configure(
    command: str = "telegramlens", *, targets: list[str] | None = None, quiet: bool = False
) -> list[dict]:
    """선택된 모든 타겟에 telegramlens MCP 등록. 타겟별 변경 결과 리스트를 반환한다."""
    targets = targets or ["claude-desktop"]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {unknown}. Valid: {list(TARGETS.keys())}")
    results = []
    for target in targets:
        path_func, label = TARGETS[target]
        results.append(_configure_one_target(path_func(), label, command=command, quiet=quiet))
    return results


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="telegramlens-setup",
        description="Register telegramlens in Claude config (Desktop and/or Code CLI).",
    )
    p.add_argument("command", nargs="?", default="telegramlens")
    p.add_argument(
        "--target",
        choices=["claude-desktop", "claude-code", "both", "auto"],
        default="auto",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="결과를 JSON으로 stdout에 출력 (Manager 연동용). 자동으로 --non-interactive를 겸한다.",
    )
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="배너/진행 로그를 찍지 않는다 (원래 프롬프트가 없으므로 동작 자체는 동일).",
    )
    return p


def main():
    args = _build_parser().parse_args()
    targets = _resolve_targets(args.target)
    quiet = args.json or args.non_interactive

    if not quiet:
        target_labels = ", ".join(TARGETS[t][1] for t in targets)
        print("==============================================")
        print("  TelegramLens - MCP Setup")
        print("==============================================")
        print(f"  Targets: {target_labels}")

    try:
        results = configure(args.command, targets=targets, quiet=quiet)
    except Exception as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"  [ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"ok": True, "targets": results}, ensure_ascii=False))
        sys.exit(0)

    if not quiet:
        print()
        if "claude-desktop" in targets:
            print("Done! Claude Desktop 을 완전히 종료 후 다시 실행하세요.")
        if "claude-code" in targets:
            print("Done! Claude Code 새 세션부터 telegramlens 도구 사용 가능.")


if __name__ == "__main__":
    main()
