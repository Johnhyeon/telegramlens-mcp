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
# 이름을 바꾼 적이 없어 정리할 옛 키가 없다. 다른 Lens와 코드 모양을 맞추려고 둔다.
LEGACY_KEYS: list[str] = []


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

    # uv tool bin 디렉토리를 **PATH 보다 먼저** 본다.
    #
    # 실기기에서 확인한 사고: 옛 `pip install` 잔재가 시스템 Python 의 Scripts\ 에 남아
    # 있으면 PATH 순서상 그게 먼저 잡혀서, 설정 파일에 옛 실행 파일 경로가 박힌다. 그러면
    # Manager 로 최신 버전을 올려도 호스트 앱(Claude·ChatGPT)은 계속 옛 버전을 띄운다 —
    # "업데이트했는데 그대로다" 가 되고, 원인이 설정 파일 안에 있어서 찾기도 어렵다.
    # uv 가 관리하는 쪽이 Manager 가 실제로 갱신하는 대상이므로 그쪽을 먼저 쓴다.
    # (uv 없이 pip 로만 설치한 환경은 이 디렉토리가 없어 아래 PATH 탐색으로 내려간다.)
    for bin_dir in _uv_tool_bin_dirs():
        for name in (f"{preferred_command}.exe", preferred_command):
            candidate = bin_dir / name
            if candidate.exists():
                return {"command": str(candidate)}

    found = shutil.which(preferred_command)
    if found:
        return {"command": found}

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


def get_codex_config_path() -> Path:
    """Codex CLI의 MCP 서버 설정 — `~/.codex/config.toml`, `[mcp_servers.<name>]` 섹션.

    Windows에서 실제 설치본으로 이 경로를 확인했다. macOS/Linux도 관례상 같은 경로일
    가능성이 높으나 이 환경에서 직접 검증하지는 못했다.
    """
    return Path.home() / ".codex" / "config.toml"


TARGETS: dict[str, tuple] = {
    "claude-desktop": (get_claude_desktop_config_path, "Claude Desktop"),
    "claude-code": (get_claude_code_config_path, "Claude Code CLI"),
    "codex": (get_codex_config_path, "Codex CLI"),
}


def _has_codex() -> bool:
    """codex 타겟을 쓸 수 있는 환경인지 — Codex CLI 또는 ChatGPT 앱.
    통합 이후 둘은 같은 `~/.codex/config.toml` 을 읽으므로 하나로 본다.

    ChatGPT 앱을 깔았지만 MCP 설정을 한 번도 안 건드린 사람은 이 폴더가 없을 수 있다 —
    그때는 아래 auto 판정이 결국 claude-desktop 으로 떨어진다(앱 자체를 찾아내는 일은
    OS별 설치 경로 탐색이 필요해서 LeetKit Manager 쪽이 담당한다)."""
    if shutil.which("codex"):
        return True
    return get_codex_config_path().parent.exists()


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
        if has_desktop:
            return ["claude-desktop"]
        # Claude 가 하나도 없으면 codex(ChatGPT 앱·Codex CLI)를 본다. 예전에는 무조건
        # claude-desktop 으로 떨어져서, ChatGPT 만 쓰는 사람에게 **없는 앱의 설정 파일**을
        # 만들고 "등록 완료"라고 말했다.
        if _has_codex():
            return ["codex"]
        return ["claude-desktop"]
    raise ValueError(f"Invalid target: {arg}")


def _configure_toml_target(config_path: Path, label: str, *, command: str, quiet: bool = False) -> dict:
    """Codex처럼 TOML(`[mcp_servers.<name>]`)로 MCP 서버를 등록하는 클라이언트용.

    tomlkit으로 파싱·재작성해서 다른 mcp_servers.* 항목·주석은 그대로 두고 우리
    섹션만 추가/갱신한다.
    """
    import tomlkit

    if not quiet:
        print()
        print(f"  -> {label}")

    config_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    doc = tomlkit.document()
    if config_path.exists():
        backup_path = config_path.with_suffix(".toml.backup")
        backup_path.write_bytes(config_path.read_bytes())
        if not quiet:
            print(f"  [OK] Backup saved: {backup_path}")
        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except Exception:
            if not quiet:
                print("  [WARN] Existing config is corrupted. Creating new one.")
            doc = tomlkit.document()

    if "mcp_servers" not in doc:
        doc["mcp_servers"] = tomlkit.table()

    entry = resolve_server_entry(command)
    server_table = tomlkit.table()
    server_table["command"] = entry["command"]
    if "args" in entry:
        server_table["args"] = entry["args"]
    doc["mcp_servers"][SERVER_KEY] = server_table

    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

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


def _configure_one_target(config_path: Path, label: str, *, command: str, quiet: bool = False) -> dict:
    """단일 config 파일에 mcpServers.telegramlens 등록. 변경 결과를 dict로 반환한다.

    quiet=True 면 사람용 진행 로그를 찍지 않는다(Manager의 --json/--non-interactive
    호출에서 stdout을 구조화 결과 하나로만 유지하기 위함).
    """
    if config_path.suffix == ".toml":
        return _configure_toml_target(config_path, label, command=command, quiet=quiet)

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


def _remove_one_target(config_path, label: str) -> dict:
    """한 설정 파일에서 우리 MCP 항목만 지운다. 다른 서버 항목·주석은 안 건드린다.

    파일이 없거나 항목이 없으면 "이미 없음"으로 성공 처리한다 — 해제를 두 번 눌러도
    실패로 보이면 안 된다. 지우기 전에 항상 백업을 남긴다(등록 때와 같은 규칙).
    """
    from pathlib import Path as _Path

    config_path = _Path(config_path)
    result = {"target_label": label, "config_path": str(config_path), "removed": False}
    if not config_path.exists():
        return result

    if config_path.suffix == ".toml":
        import tomlkit

        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        except Exception:
            return result
        config_path.with_suffix(".toml.backup").write_bytes(config_path.read_bytes())
        servers = doc.get("mcp_servers")
        if servers is not None:
            for key in [SERVER_KEY, *LEGACY_KEYS]:
                if servers.pop(key, None) is not None:
                    result["removed"] = True
        if result["removed"]:
            config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return result

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return result
    with open(config_path.with_suffix(".json.backup"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    servers = config.get("mcpServers") or {}
    for key in [SERVER_KEY, *LEGACY_KEYS]:
        if servers.pop(key, None) is not None:
            result["removed"] = True
    if result["removed"]:
        config["mcpServers"] = servers
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    return result


def remove(targets: list[str]) -> list[dict]:
    """선택한 타겟들에서 MCP 등록을 해제한다.

    Manager의 "MCP 등록" 모달에서 체크를 풀면 여기로 온다. 예전엔 해제 수단이 아예
    없어서, 체크박스가 토글처럼 보이는데 실제로는 "추가만" 됐다 — 체크를 풀고 등록을
    눌러도 그 설정이 그대로 남아 사용자 눈에는 먹통으로 보였다.
    """
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {unknown}. Valid: {list(TARGETS.keys())}")
    results = []
    for target in targets:
        path_func, label = TARGETS[target]
        entry = _remove_one_target(path_func(), label)
        entry["target"] = target
        results.append(entry)
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
        choices=["claude-desktop", "claude-code", "both", "auto", "codex"],
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
    p.add_argument(
        "--remove",
        action="store_true",
        help="등록을 해제한다(설정 파일에서 우리 항목만 삭제). Manager의 체크 해제가 이걸 쓴다.",
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

    # 해제는 등록과 완전히 다른 일이라 여기서 바로 갈라진다 — 키 검증·설치 확인 같은
    # 등록 절차를 탈 이유가 없다.
    if args.remove:
        try:
            removed = remove(targets)
        except Exception as e:  # noqa: BLE001 — 실패도 JSON 계약으로 알려야 한다
            if args.json:
                print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            else:
                print(f"  [ERROR] {e}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps({"ok": True, "removed": removed}, ensure_ascii=False))
        elif not quiet:
            for entry in removed:
                state = "해제됨" if entry["removed"] else "원래 없음"
                print(f"  [OK] {entry['target_label']}: {state}")
        sys.exit(0)

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
