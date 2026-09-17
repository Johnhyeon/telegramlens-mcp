"""telegramlens 설치·런타임 진단 도구.

실행: `telegramlens-doctor` 또는 `python -m telegram_lens.doctor`

기본(인자 없음)은 항상 읽기 전용 — 아무것도 고치지 않는다.

    telegramlens-doctor                  읽기 전용 전체 진단
    telegramlens-doctor --json           구조화된 JSON 출력(LeetKit Manager 등이 파싱)
    telegramlens-doctor --repair         감지된 안전한 문제를 복구(확인 프롬프트 있음)
    telegramlens-doctor --repair daemon  데몬/PID/상태파일만 복구
    telegramlens-doctor --repair --yes   확인 없이 진행(비대화형 실행용)

설치 검사(uv/패키지/명령/Claude config/라이선스)에 더해 런타임 진단을 한다:
데몬이 실제로 가동 중인지(락 보유), 하트비트·마지막 성공 수집·연속 실패, DB 무결성·락,
백필 진행 상태, 그리고 (데몬이 세션을 안 쓰고 있을 때만) 실제 Telegram 연결·인증 확인.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import sysconfig
import time
from pathlib import Path

try:
    from telegram_lens.setup_claude import (
        get_claude_desktop_config_path,
        get_claude_code_config_path,
        get_codex_config_path,
        SERVER_KEY,
        _uv_tool_bin_dirs,
        _find_store_config_path,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from telegram_lens.setup_claude import (
        get_claude_desktop_config_path,
        get_claude_code_config_path,
        get_codex_config_path,
        SERVER_KEY,
        _uv_tool_bin_dirs,
        _find_store_config_path,
    )

from telegram_lens._error_class import (  # noqa: E402
    CATEGORIES,
    action_for,
    classify_error,
    classify_exception,
)

# Manager 공통 계약(LeetKit Manager Program Requirements 3.1) 최상위 필드 상수.
# StockLens/DartLens와 이름을 반드시 맞출 것 — 여기서 임의로 새 이름을 만들면
# Manager가 Lens별 파서를 따로 둬야 한다.
SCHEMA_VERSION = 1
PRODUCT = "telegramlens"
PACKAGE_NAME = "telegramlens-mcp"

# TelegramLens는 아직 PyPI 최신 버전 확인 모듈이 없다 — latest_version/update_available는
# 항상 null(다른 두 Lens는 --online일 때만 채움. 이쪽은 그 online 경로 자체가 없음).

# doctor.py의 Check 이름 -> Manager 공통 계약 checks[].id. StockLens/DartLens와 개념이
# 겹치는 항목(PACKAGE_IMPORTABLE/COMMAND_AVAILABLE/MCP_CONFIG_VALID/LICENSE_ACTIVE)은
# 이름을 맞추고, TelegramLens 고유 런타임 검사는 여기서만 쓰는 이름을 붙인다.
_CHECK_IDS = {
    "uv": "UV_AVAILABLE",
    "package": "PACKAGE_IMPORTABLE",
    "command": "COMMAND_AVAILABLE",
    "config_desktop": "MCP_CONFIG_DESKTOP",
    "config_code": "MCP_CONFIG_CODE",
    "config_codex": "MCP_CONFIG_CODEX",
    "registered_targets": "MCP_CONFIG_VALID",
    "license": "LICENSE_ACTIVE",
    "telegram_login": "TELEGRAM_LOGIN",
    "daemon": "DAEMON_COLLECTOR",
    "data": "DATA_SQLITE",
    "backfill": "BACKFILL",
    "recent_failures": "RECENT_TOOL_FAILURES",
}

# ── Manager 화면에 뜨는 고객 문구(checks[].action / summary) ──────────────
# 세 Lens 공통 사양(2026-09-17): 터미널 명령 0개, Manager 버튼 이름은 화면 글자 그대로,
# 상황 → 할 일 하나 → 그래도 같으면 [지원 문의], 해요체. Manager 화면 안이라 앞말은
# "TelegramLens 카드의 [버튼]", "상단 [지원 문의]". 명령어는 fix(터미널 출력)에만 남긴다.
LENS = "TelegramLens"
_ACT_REGISTER = "TelegramLens 카드의 [MCP 등록]을 눌러주세요."
_ACT_LOGIN = "TelegramLens 카드의 [텔레그램 로그인]을 눌러주세요."
_ACT_REPAIR = "TelegramLens 카드의 [복구]를 눌러주세요."


def _registered_targets(desktop_check: "Check", code_check: "Check", codex_check: "Check") -> list[str]:
    """실제로 등록된 MCP 타겟 slug 목록 — Manager 공통 계약 top-level `targets` 필드용."""
    targets: list[str] = []
    if desktop_check.status == "ok":
        targets.append("claude-desktop")
    if code_check.status == "ok":
        targets.append("claude-code")
    if codex_check.status == "ok":
        targets.append("codex")
    return targets


class Check:
    def __init__(self, name: str):
        self.name = name
        self.status = None  # "ok" / "active" / "warn" / "fail"
        self.lines: list[str] = []
        # fix 와 action 을 나눈 이유: fix 는 터미널에서 doctor 를 직접 돌린 사람에게 보이는
        # 명령어이고, action 은 --json 으로 Manager 화면에 뜨는 고객 문구다. 예전엔 fix 하나를
        # 둘 다에 썼더니 Manager 카드에 `telegramlens-activate <라이선스-키>` 가 그대로 떴다.
        # JSON action 은 action 만 쓴다 — fix 로 되돌아가면 명령어가 다시 새어 나간다.
        self.fix: str | None = None
        self.action: str | None = None
        # False 면 이 항목 하나로 카드가 "사용 불가"가 되면 안 된다(StockLens 계약과 같은 뜻).
        self.critical: bool = True
        self.error_code: str | None = None
        # ok/warn/fail 중 실제로 상태를 확정한 호출의 메시지만 담는다(.info()는 제외) —
        # Manager 계약 checks[].summary는 이 한 줄이고, 나머지 info 라인은 details.lines로 간다.
        self.summary: str = ""
        # 진행률(done/total/…)을 숫자 그대로 담는다. 있으면 details.progress로 나간다.
        self.progress: dict | None = None

    def ok(self, msg: str):
        self.status = "ok"
        self.summary = msg
        self.lines.append(msg)
        return self

    def active(self, msg: str):
        """정상이지만 지금 뭔가 진행 중(예: 백필)임을 알리는 상태 — Manager는 이걸
        "문제"로 세지 않지만 "정상"과도 구분해 별도 "진행중" 카테고리로 보여준다."""
        self.status = "active"
        self.summary = msg
        self.lines.append(msg)
        return self

    def warn(self, msg: str, fix: str | None = None, action: str | None = None):
        if self.status != "fail":
            self.status = "warn"
            self.summary = msg
        self.lines.append(msg)
        if fix:
            self.fix = fix
        if action:
            self.action = action
        return self

    def fail(self, msg: str, fix: str | None = None, action: str | None = None):
        self.status = "fail"
        self.summary = msg
        self.lines.append(msg)
        if fix:
            self.fix = fix
        if action:
            self.action = action
        return self

    def info(self, msg: str):
        self.lines.append(msg)
        return self

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "lines": self.lines,
            "fix": self.fix,
        }

    def to_contract_dict(self, check_id: str, *, repairable: bool = False, repair_id: str | None = None) -> dict:
        """Manager 공통 계약(LeetKit Manager Program Requirements 3.1) checks[] 항목 형태."""
        details: dict = {}
        if self.lines:
            details["lines"] = list(self.lines)
        # 진행률은 숫자로 따로 내보낸다. 예전엔 "Processed: 3/30 채널" 같은 텍스트 줄만
        # 나가서, Manager가 바를 그리려면 그 문장을 파싱해야 했다 — 문구가 조금만
        # 바뀌어도 깨지는 방식이라 아예 구조화된 값을 준다.
        if self.progress is not None:
            details["progress"] = self.progress
        if self.error_code:
            details["error_code"] = self.error_code
        return {
            "id": check_id,
            "status": self.status,
            "summary": self.summary or (self.lines[0] if self.lines else ""),
            "details": details,
            "repairable": repairable,
            "repair_id": repair_id,
            "action": self.action,
            "critical": self.critical,
        }


# ── 설치 검사 ────────────────────────────────────────────────────────


def _find_uv() -> str | None:
    """uv 실행 파일 경로. PATH뿐 아니라 설치 스크립트가 실제로 두는 위치까지 본다.

    PATH만 보면 실사용에서 오탐이 난다 — LeetKit Manager가 uv를 자동 설치해주면 uv는
    `~/.local/bin`에 생기지만 설치 스크립트는 *영구* PATH(레지스트리)만 갱신하므로,
    이미 실행 중인 프로세스에는 반영되지 않는다. 그 상태로 PATH만 확인하면 설치가
    멀쩡히 끝났는데도 계속 "uv 없음" 경고가 떠서 카드가 영영 "주의"로 남는다.
    """
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for bin_dir in (home / ".local" / "bin", home / ".cargo" / "bin"):
        for name in ("uv.exe", "uv"):
            candidate = bin_dir / name
            if candidate.exists():
                return str(candidate)
    return None


def check_uv() -> Check:
    c = Check("uv (Python runtime manager)")
    uv = _find_uv()
    if uv:
        c.ok("uv is installed")
        c.info(f"Path:       {uv}")
    else:
        c.warn(
            "실행 환경(uv)을 찾지 못했어요.",
            fix=(
                "Install uv (recommended):\n"
                "  Windows: irm https://astral.sh/uv/install.ps1 | iex\n"
                "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
            ),
            action=action_for("other", LENS),
        )
    return c


def version_report(code_version: str, dist: str | None) -> dict:
    """실행 코드 버전 vs 설치 메타 버전(TL-01 요구 2).

    dist=None 은 메타를 못 읽은 것(editable 등)이지 불일치가 아니다.
    """
    mismatch = dist is not None and dist != code_version
    return {
        "code_version": code_version,
        "dist_version": dist,
        "version_mismatch": mismatch,
    }


def scan_dist_metadata(package_name: str,
                       site_packages: list | None = None) -> dict:
    """site-packages 의 이 패키지 배포 메타를 훑는다(TL-01 요구 5).

    실측(UAT): pip 임시 리네임이 깨진 채 남은 "~elegramlens_mcp-*.dist-info"
    와 옛 버전 dist-info 가 정상 배포판 옆에 공존했다. importlib.metadata 는
    그중 아무거나 집을 수 있어 버전이 과거로 보인다.
    """
    normalized = package_name.replace("-", "_").lower()
    # pip 임시 리네임은 첫 글자를 "~" 로 바꾼다: telegramlens -> ~elegramlens
    broken_stem = "~" + normalized[1:]
    if site_packages is None:
        site_packages = [Path(pth) for pth in {
            sysconfig.get_paths().get("purelib") or "",
            sysconfig.get_paths().get("platlib") or "",
        } if pth]

    valid: list[dict] = []
    broken: list[str] = []
    for root in site_packages:
        root = Path(root)
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            name = entry.name
            low = name.lower()
            if low.startswith(broken_stem):
                broken.append(str(entry))
                continue
            if not low.endswith(".dist-info"):
                continue
            stem = low[:-len(".dist-info")]
            pkg, _, ver = stem.rpartition("-")
            if pkg == normalized:
                valid.append({"path": str(entry), "version": ver})
    return {
        "valid": valid,
        "broken": broken,
        "duplicated": len(valid) > 1,
    }


def check_package() -> Check:
    c = Check("Package (telegramlens-mcp)")
    try:
        import telegram_lens  # noqa: F401
        c.ok("telegramlens-mcp is importable")
        c.info(f"Location:   {Path(telegram_lens.__file__).parent}")
        from telegram_lens._version import dist_version

        vr = version_report(telegram_lens.__version__, dist_version())
        c.info(f"Version:    {vr['code_version']} (실행 코드 기준)")
        if vr["version_mismatch"]:
            c.warn(
                f"version_mismatch: 실행 코드 {vr['code_version']} vs 설치 메타 "
                f"{vr['dist_version']} - 업그레이드가 덜 끝났거나 옛 dist-info 가 "
                "남아 있습니다. 재설치를 권합니다."
            )
        scan = scan_dist_metadata(PACKAGE_NAME)
        if scan["broken"]:
            c.warn(
                f"깨진 배포 메타 {len(scan['broken'])}개 발견(~ 로 시작하는 pip "
                "임시 리네임 잔재): " + ", ".join(
                    Path(b).name for b in scan["broken"][:3])
                + " - 삭제해도 안전합니다."
            )
        if scan["duplicated"]:
            vers = ", ".join(v["version"] for v in scan["valid"])
            c.warn(
                f"같은 패키지의 dist-info 가 {len(scan['valid'])}개입니다({vers}). "
                "importlib.metadata 가 아무거나 집을 수 있어 버전이 과거로 보일 수 "
                "있습니다. 하나만 남기세요."
            )
        c.info(f"Python:     {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        c.info(f"Executable: {sys.executable}")
    except ImportError:
        c.fail(
            "TelegramLens 설치가 깨져 있어요.",
            fix="uv tool install --force telegramlens-mcp",
            action=action_for("other", LENS),
        )
    return c


def check_telegramlens_command() -> Check:
    c = Check("Command (telegramlens)")
    exe = shutil.which("telegramlens")
    if exe:
        c.ok("'telegramlens' found in PATH")
        c.info(f"Path:       {exe}")
        return c

    for bin_dir in _uv_tool_bin_dirs():
        for name in ("telegramlens.exe", "telegramlens"):
            candidate = bin_dir / name
            if candidate.exists():
                # PATH에 없는 것 자체는 문제가 아니다 — MCP 등록은 절대경로로 하므로
                # 그대로 동작한다. 예전엔 warn이라 카드가 영영 "주의"로 남았는데,
                # 정작 안내문에 "무시 가능"이라고 적혀 있는 경고였다.
                c.ok("'telegramlens' is installed")
                c.info(f"Path:       {candidate}")
                c.info("Not on PATH — MCP registration uses this absolute path, so no action is needed.")
                c.info(f'Add "{bin_dir}" to PATH only if you want to type the command in a terminal.')
                return c

    try:
        scripts_dir = Path(sysconfig.get_paths()["scripts"])
        for name in ("telegramlens.exe", "telegramlens"):
            candidate = scripts_dir / name
            if candidate.exists():
                c.warn(
                    "'telegramlens' exists in sysconfig scripts but not on PATH",
                    fix=f'Add to PATH: "{scripts_dir}"',
                    action=action_for("other", LENS),
                )
                c.info(f"Path:       {candidate}")
                return c
    except Exception:
        pass

    c.fail(
        "TelegramLens 실행 파일을 찾지 못했어요.",
        fix="uv tool install --force telegramlens-mcp",
        action=action_for("other", LENS),
    )
    return c


def label_to_target(label: str) -> str:
    return "claude-code" if "Code" in label else "claude-desktop"


def _check_config_file(label: str, config_path: Path, *, required: bool) -> Check:
    """단일 config 파일에 대한 점검. required=False면 부재 시 fail 대신 info."""
    c = Check(f"Config — {label}")

    # Store 버전은 앱이 격리된 공간에서 돌아, 우리가 띄우는 프로세스도 그 안에 갇힌다.
    # 그 자체가 고장은 아니고 실제로 잘 쓰는 사람도 있어서 막지는 않는다 — 다만 문제가
    # 생겼을 때 원인 후보가 하나 더 붙는 환경이라, 진단에서는 눈에 띄게 알린다.
    # info 로 찍으면 화면을 스쳐 지나가서, 정작 막힌 사람이 이 줄을 못 보고 넘어갔다.
    if "Packages" in str(config_path) and "LocalCache" in str(config_path):
        store_fix = (
            "문제가 있다면 일반 설치판을 권합니다: "
            "① 설정 > 설치된 앱에서 Claude 제거 "
            "② %LOCALAPPDATA%\\Packages 에서 Claude_* 폴더 삭제 "
            "③ https://claude.ai/download 에서 설치 파일로 재설치 "
            "(스토어 페이지 말고 이 주소에서 받으세요)"
        )
        c.warn(
            "Microsoft Store 버전 Claude Desktop 을 쓰고 계십니다 (격리된 경로). "
            "지금 잘 되신다면 그대로 쓰셔도 됩니다.",
            fix=store_fix,
            action=store_fix,  # 터미널 명령이 아니라 설정 화면 안내라 Manager 에도 그대로 둔다
        )
    c.info(f"Path:       {config_path}")

    if not config_path.exists():
        if required:
            c.fail(
                f"{LENS}가 아직 AI 앱에 등록되지 않았어요.",
                fix="telegramlens-setup",
                action=_ACT_REGISTER,
            )
        else:
            c.info("Config file does not exist (target not in use — OK)")
            c.status = "info-skip"
            c.summary = "Config file does not exist (target not in use — OK)"
        return c

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        # 등록([MCP 등록])이 깨진 파일을 새로 만들어 쓴다(setup_claude 참고) — 그래서 할 일은 등록.
        c.info(f"Config is not valid JSON: {e}")
        c.fail(
            "AI 앱 설정 파일이 깨져 있어요.",
            fix="Back up and re-run telegramlens-setup",
            action=_ACT_REGISTER,
        )
        return c
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    servers = cfg.get("mcpServers", {}) or {}
    entry = servers.get(SERVER_KEY)

    if not entry:
        if required:
            c.fail(
                f"{LENS}가 아직 AI 앱에 등록되지 않았어요.",
                fix=f"telegramlens-setup --target {label_to_target(label)}",
                action=_ACT_REGISTER,
            )
        else:
            c.info(f"'{SERVER_KEY}' entry not present (target not in use — OK)")
            c.status = "info-skip"
            c.summary = f"'{SERVER_KEY}' entry not present (target not in use — OK)"
        return c

    cmd = entry.get("command")
    args = entry.get("args", [])
    c.info(f"Command:    {cmd}")
    if args:
        c.info(f"Args:       {args}")

    if not cmd:
        c.fail("Entry has no 'command' field")
        return c

    if Path(cmd).is_absolute():
        if Path(cmd).exists():
            c.ok("Command points to existing file")
        else:
            c.info(f"Command file missing: {cmd}")
            c.fail(_REGISTERED_EXE_MISSING, fix="telegramlens-setup", action=_ACT_REGISTER)
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.info(f"Command '{cmd}' not in PATH — client will fail to launch the server")
            c.fail(_REGISTERED_EXE_MISSING, fix="telegramlens-setup", action=_ACT_REGISTER)

    return c


# 등록은 돼 있는데 가리키는 실행 파일이 없다 — 재설치·경로 변경 뒤에 생긴다. 원래 영어
# 원문(경로 포함)은 details 줄에 남기고, 화면 한 줄은 고객 말로.
_REGISTERED_EXE_MISSING = "AI 앱에 등록된 TelegramLens 실행 파일을 찾을 수 없어요."


def check_config_desktop() -> Check:
    return _check_config_file(
        "Claude Desktop", get_claude_desktop_config_path(), required=False
    )


def check_config_code() -> Check:
    return _check_config_file(
        "Claude Code CLI", get_claude_code_config_path(), required=False
    )


def check_config_codex() -> Check:
    return _check_config_toml_file(
        "Codex CLI", get_codex_config_path(), required=False
    )


def _check_config_toml_file(label: str, config_path: Path, *, required: bool) -> Check:
    """TOML 기반 클라이언트(Codex의 `~/.codex/config.toml`, `[mcp_servers.<key>]`)용
    config 점검 — _check_config_file(JSON)과 같은 계약을 TOML 구조에 맞춰 재구현.
    setup_claude._configure_toml_target()이 쓰는 것과 동일한 구조를 읽기만 한다."""
    c = Check(f"Config — {label}")
    c.info(f"Path:       {config_path}")

    if not config_path.exists():
        if required:
            c.fail(
                f"{LENS}가 아직 AI 앱에 등록되지 않았어요.",
                fix="telegramlens-setup --target codex",
                action=_ACT_REGISTER,
            )
        else:
            c.info("Config file does not exist (target not in use — OK)")
            c.status = "info-skip"
            c.summary = "Config file does not exist (target not in use — OK)"
        return c

    try:
        import tomlkit

        with open(config_path, "r", encoding="utf-8") as f:
            cfg = tomlkit.parse(f.read())
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    servers = cfg.get("mcp_servers", {}) or {}
    entry = servers.get(SERVER_KEY)

    if not entry:
        if required:
            c.fail(
                f"{LENS}가 아직 AI 앱에 등록되지 않았어요.",
                fix="telegramlens-setup --target codex",
                action=_ACT_REGISTER,
            )
        else:
            c.info(f"'{SERVER_KEY}' entry not present (target not in use — OK)")
            c.status = "info-skip"
            c.summary = f"'{SERVER_KEY}' entry not present (target not in use — OK)"
        return c

    cmd = entry.get("command")
    args = list(entry.get("args") or [])
    c.info(f"Command:    {cmd}")
    if args:
        c.info(f"Args:       {args}")

    if not cmd:
        c.fail("Entry has no 'command' field")
        return c

    if Path(cmd).is_absolute():
        if Path(cmd).exists():
            c.ok("Command points to existing file")
        else:
            c.info(f"Command file missing: {cmd}")
            c.fail(_REGISTERED_EXE_MISSING, fix="telegramlens-setup --target codex", action=_ACT_REGISTER)
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.info(f"Command '{cmd}' not in PATH — client will fail to launch the server")
            c.fail(_REGISTERED_EXE_MISSING, fix="telegramlens-setup --target codex", action=_ACT_REGISTER)

    return c


def check_at_least_one_config(*configs: Check) -> Check:
    """모든 config가 미등록이면 종합 fail. 하나라도 등록돼있으면 OK."""
    c = Check("Registered targets")
    registered = [cc for cc in configs if cc.status == "ok"]
    if registered:
        c.ok(f"{len(registered)} target(s) configured")
        return c
    c.fail(
        f"{LENS}가 아직 AI 앱에 등록되지 않았어요.",
        fix="telegramlens-setup --target {claude-desktop|claude-code|both|codex}",
        action=_ACT_REGISTER,
    )
    return c


def license_summary() -> dict:
    """status/license_id_masked — Manager 공통 계약 top-level `license` 필드.

    키 원문은 물론 전체 license_id도 절대 담지 않는다(마지막 4자리만 마스킹).
    """
    try:
        from telegram_lens import licensing
    except ImportError:
        return {"status": "missing", "license_id_masked": None}

    key = licensing.stored_key()
    if not key:
        return {"status": "missing", "license_id_masked": None}

    res = licensing.verify_key(key)
    if not res["valid"]:
        return {"status": "invalid", "license_id_masked": None}

    # 키는 진짜지만 지금 못 쓰는 경우(기간 종료·폐기·시계 되돌림)를 그대로 드러낸다 —
    # 예전엔 전부 "active"라 도구는 잠겼는데 Manager는 정상이라고 말했다.
    _blocked = licensing.license_block_reason()
    if _blocked in ("expired", "revoked", "clock"):
        # 키에 박힌 날짜가 아니라 실제로 끝나는 날 — 매니저 배지가 이 값을 읽는다.
        _expiry = licensing.effective_expiry(res)
        return {
            "status": _blocked,
            "license_id_masked": licensing.mask_tail((res.get("license_id") or "").upper()) or None,
            "expires_on": _expiry.isoformat() if _expiry else None,
        }

    license_id = res.get("license_id") or ""
    masked = licensing.mask_tail(license_id.upper()) if license_id else None
    return {"status": "active", "license_id_masked": masked}


# 라이선스 상태별 (summary, action). 세 Lens 공통 기준 문구(사양 2-2)를 Lens 이름만 바꿔 쓴다.
_LICENSE_COPY = {
    "missing": (
        "라이선스 키가 아직 없어요.",
        "TelegramLens 카드의 [활성화]를 눌러 메일로 받은 키를 넣어주세요.",
    ),
    "invalid": (
        "저장된 라이선스 키를 확인할 수 없어요.",
        "TelegramLens 카드의 [활성화]를 눌러 메일로 받은 키를 다시 넣어주세요.",
    ),
    "expired": (
        "사용 기간이 끝났어요.",
        "TelegramLens 카드의 [구매]를 누르고, 받은 키를 [활성화]로 넣어주세요.",
    ),
    "revoked": (
        "이 라이선스 키는 사용이 중지돼 있어요.",
        "착오라면 상단 [지원 문의]를 눌러주세요.",
    ),
    "clock": (
        "이 컴퓨터의 날짜가 실제보다 과거로 되어 있어요.",
        "날짜와 시간을 오늘로 맞춘 뒤 [진단]을 다시 눌러주세요.",
    ),
}


def check_license() -> Check:
    """라이선스 활성화 여부. 키 원문·전체 license_id는 노출하지 않는다."""
    c = Check("License")
    try:
        from telegram_lens import licensing  # noqa: F401
    except ImportError as e:
        c.fail(f"licensing module not importable: {e}")
        return c

    summary = license_summary()
    status = summary["status"]
    if status in _LICENSE_COPY:
        msg, action = _LICENSE_COPY[status]
        c.fail(
            msg,
            fix="telegramlens-activate <라이선스-키>" if status in ("expired", "missing", "invalid") else None,
            action=action,
        )
        return c

    c.ok(f"Activated (license_id: {summary['license_id_masked']})")
    c.info(f"Source:     {'env' if os.environ.get('TELEGRAMLENS_LICENSE_KEY') else 'license.key'}")
    return c


# ── 런타임 진단 ──────────────────────────────────────────────────────


async def _check_real_connection() -> tuple[bool, str, str | None]:
    """실제로 Telegram 에 연결해 is_user_authorized() 까지 확인.

    반환: (성공 여부, 원문, 원인 분류) — 원문은 details 줄용, 분류는 _error_class 이름
    (성공이면 None, 인증 안 됨이면 "auth").

    호출자(check_telegram_login)가 데몬이 세션을 쓰고 있지 않을 때만 부른다 — 같은
    .session 파일을 데몬과 동시에 열면 SQLite 락 경합이 날 수 있어서다.
    """
    from telegram_lens.client import (
        NoCredentialsError,
        connect_with_timeout,
        disconnect_safely,
        make_client,
    )

    try:
        client = make_client()
    except NoCredentialsError as e:
        return False, str(e), "auth"
    try:
        await connect_with_timeout(client, timeout=15)
        authorized = await client.is_user_authorized()
        if authorized:
            return True, "실제 연결 및 인증 확인 성공", None
        return False, "연결은 됐지만 인증되어 있지 않습니다(세션 만료 가능성) — SESSION_INVALID", "auth"
    except Exception as e:  # noqa: BLE001 — 진단 자체가 죽으면 안 됨
        return False, f"{type(e).__name__}: {e}", classify_exception(e)
    finally:
        await disconnect_safely(client)


# 한 번 로그인했다가 풀린 경우(세션 만료·다른 기기에서 해제). "아직"이 아니라 "풀려"다.
_LOGIN_EXPIRED = "텔레그램 로그인이 풀려 있어요."


def check_telegram_login(daemon_running: bool) -> Check:
    """Telegram API 자격증명 + 로그인 세션 상태. 데몬이 안 쓰고 있을 때만 실 연결까지 확인."""
    c = Check("Telegram Login")
    try:
        from telegram_lens import config as tl_config
    except ImportError as e:
        c.fail(f"config module not importable: {e}")
        return c

    api_id, api_hash = tl_config.get_credentials()
    if not api_id or not api_hash:
        c.info("No Telegram API credentials found (TELEGRAM_API_ID / TELEGRAM_API_HASH)")
        c.fail(
            "텔레그램 api_id와 api_hash가 아직 없어요.",
            fix="telegramlens-login",
            action="TelegramLens 카드의 [텔레그램 로그인]을 눌러 넣어주세요.",
        )
        return c
    c.info(f"API_ID:     {api_id}")

    if not tl_config.is_logged_in():
        c.info("Credentials present but no session file — not logged in yet")
        c.fail(
            "텔레그램 로그인이 아직 안 돼 있어요.",
            fix="telegramlens-login",
            action=_ACT_LOGIN,
        )
        return c
    c.info(f"Path:       {tl_config.session_path().with_suffix('.session')}")

    if daemon_running:
        c.ok(
            "Session file exists — 데몬이 이미 이 세션으로 연결 중이라 실 연결 확인은 건너뜀"
            "(데몬 상태가 healthy 면 인증도 정상)"
        )
        return c

    try:
        ok, detail, category = asyncio.run(_check_real_connection())
    except Exception as e:  # noqa: BLE001
        category = classify_exception(e)
        c.info(f"실 연결 확인 중 오류: {type(e).__name__}: {e} (분류 {category})")
        c.warn("텔레그램 연결을 확인하지 못했어요.", action=action_for(category, LENS))
        return c
    if ok:
        c.ok(detail)
        return c
    # 원문(예외 이름 포함)은 details 줄에만. 화면 한 줄은 원인 분류로 고른다.
    c.info(f"{detail} (분류 {category})")
    if category == "auth":
        c.fail(_LOGIN_EXPIRED, fix="telegramlens-login (세션 재발급)", action=action_for("auth", LENS))
    else:
        c.fail(
            "텔레그램 서버에 연결하지 못했어요.",
            fix="telegramlens-login (세션 재발급)",
            action=action_for(category or "other", LENS),
        )
    return c


def check_daemon() -> Check:
    """수집 데몬 — 락 보유 여부(procstate.DaemonLock)와 상태 파일(daemon_status.json)."""
    c = Check("Daemon (collector)")
    from telegram_lens import procstate
    from telegram_lens.daemon import lock_path, status_path

    held = procstate.DaemonLock(lock_path()).is_held()
    status = procstate.read_json(status_path())
    health = procstate.compute_health(status, held)

    c.info(f"Lock held:  {held}")
    if status:
        c.info(f"State:      {status.get('state')}")
        c.info(f"Heartbeat:  {status.get('heartbeat_at')}")
        c.info(f"Last OK:    {status.get('last_success_at')}")
        c.info(f"Newest msg: {status.get('newest_message_at')}")
        c.info(f"Consecutive failures: {status.get('consecutive_failures')}")
        le = status.get("last_error")
        if le:
            c.info(f"Last error: [{le.get('code')}] {le.get('message')}")
    elif status_path().exists():
        c.info("상태 파일이 있지만 파싱할 수 없습니다(손상).")
    else:
        c.info("상태 파일이 아직 없습니다.")

    # problem_code 는 데몬이 떠 있을 때(락 보유)만 나온다 — 그때 Manager 카드에 [복구]가 뜬다
    # (main 의 daemon_repairable 과 같은 조건).
    fix = "telegramlens-doctor --repair daemon" if health["problem_code"] else None
    action = _ACT_REPAIR if health["problem_code"] else None
    if health["health"] == "healthy":
        c.ok(health["message"])
    elif health["health"] == "degraded":
        c.warn(health["message"], fix=fix, action=action)
    else:
        c.fail(health["message"], fix=fix, action=action)
    return c


def check_data() -> Check:
    """DB 파일 무결성·락 상태. 짧은 타임아웃으로 프로브해 락이 걸려 있으면 즉시 보고한다
    (db.connect() 의 30초 busy_timeout 을 그대로 쓰면 진단 명령 자체가 오래 멎는다)."""
    c = Check("Data (SQLite DB)")
    import sqlite3

    from telegram_lens.config import db_path

    path = db_path()
    if not path.exists():
        # 첫 설치 직후엔 당연히 없다 — 수집은 Claude Desktop을 열어야 시작된다.
        # 이걸 경고로 잡으면 갓 설치한 사용자에게 "문제 1건"으로 보여서, 잘못한 게
        # 없는데도 뭔가 고장 난 줄 알게 된다(데몬 미가동 판정과 같은 부류의 오해).
        c.ok("아직 수집된 데이터가 없습니다 — Claude Desktop을 열면 텔레그램 채널 메시지 수집이 시작됩니다.")
        return c
    c.info(f"Path:       {path}")

    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as e:
        # DATA_SQLITE 는 repairable 이 아니라 카드에 [복구]가 안 뜰 수 있다 — Claude 를
        # 껐다 켜는 단계가 있는 [문제 해결]로 보낸다.
        c.info(f"DB 락으로 접속 실패(2초 대기 후 포기): {e}")
        c.fail(
            "데이터 파일이 다른 작업에 잠겨 있어요.",
            fix="telegramlens-doctor --repair daemon (멎은 데몬이 락을 쥐고 있을 수 있습니다)",
            action="상단 [문제 해결]을 눌러주세요. 그래도 같으면 상단 [지원 문의]를 눌러주세요.",
        )
        return c

    try:
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.OperationalError as e:
            c.fail(f"PRAGMA quick_check 실패(DB 락 의심): {e}")
            return c
        if not row or row[0] != "ok":
            c.fail(f"PRAGMA quick_check 이상: {row[0] if row else '?'}")
            return c
        ch_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        newest = conn.execute("SELECT MAX(date) FROM messages").fetchone()[0]
        c.ok("정상(quick_check ok, 락 없음)")
        c.info(f"Channels:   {ch_count}")
        c.info(f"Messages:   {msg_count}")
        c.info(f"Newest msg: {newest or '(없음)'}")
    finally:
        conn.close()
    return c


def check_backfill() -> Check:
    c = Check("Backfill")
    from telegram_lens import procstate
    from telegram_lens.daemon import status_path

    status = procstate.read_json(status_path())
    bf = (status or {}).get("backfill") or {}
    state = bf.get("state", "idle")
    c.info(f"State:      {state}")

    if state == "idle":
        c.ok("진행 중인 백필 없음")
        return c

    c.info(f"Requested:  {bf.get('requested_days')}일")
    c.info(f"Processed:  {bf.get('processed_channels')}/{bf.get('total_channels')} 채널")
    c.info(f"Fetched:    {bf.get('fetched_messages')}건")

    if state == "interrupted":
        c.warn(
            "이전에 지난 기록을 가져오다가 멈춘 채로 남아 있어요.",
            fix="telegram_collect_history(days=...) 로 다시 요청하세요.",
            action="Claude에게 지난 텔레그램 기록을 다시 가져와 달라고 말해주세요.",
        )
        return c

    age = procstate.heartbeat_age_sec(bf, "last_progress_at")
    if age is not None and age > 300:
        c.fail(
            f"지난 기록 가져오기가 {int(age)}초째 멈춰 있어요.",
            fix="telegramlens-doctor --repair daemon",
            action=_ACT_REPAIR,
        )
    else:
        c.active("지난 기록을 가져오는 중")
    c.progress = _backfill_progress(bf)
    return c


def _backfill_progress(bf: dict) -> dict | None:
    """백필 진행률을 숫자로. Manager가 이걸로 진행 바를 그린다.

    "수집 중"이라는 말만으로는 얼마나 남았는지 알 수 없어, 사용자는 멈춘 건지 도는
    건지 구분을 못 한다 — 그래서 '조치 필요'로 오해하기 쉽다. 숫자와 남은 시간을
    같이 주면 기다리면 되는 상태라는 게 그 자리에서 보인다.
    """
    done = bf.get("processed_channels")
    total = bf.get("total_channels")
    if not isinstance(done, int) or not isinstance(total, int) or total <= 0:
        return None

    progress: dict = {
        "done": done,
        "total": total,
        "unit": "채널",
        "fetched": bf.get("fetched_messages"),
    }

    # 남은 시간은 지금까지의 평균 속도로만 어림한다. 채널마다 메시지 양이 달라 정확할
    # 수 없으므로 화면에서도 "약"으로 말한다. 아직 한 채널도 못 끝냈으면 계산 불가.
    started = bf.get("started_at")
    if done > 0 and done < total and started:
        try:
            from datetime import datetime, timezone

            begun = datetime.fromisoformat(started)
            if begun.tzinfo is None:
                begun = begun.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - begun).total_seconds()
            if elapsed > 0:
                progress["eta_sec"] = int(elapsed / done * (total - done))
        except (ValueError, TypeError):
            pass
    return progress


_RECENT_HOURS = 48
_RECENT_DETAIL_MAX_LINES = 8
_RECENT_DETAIL_CHARS = 120
# 지원 번들로 나가는 줄이라 키·토큰처럼 보이는 긴 덩어리는 가린다. _metrics._error_detail 이
# 쿼리스트링·전화번호는 이미 지우지만, 예외 메시지에 세션 해시·api_hash 가 실릴 수 있다.
_SECRET_CHUNK_RE = re.compile(r"\b(?:[0-9a-fA-F]{16,}|[A-Z2-7]{16,})\b")


def check_recent_tool_failures(records: list[dict] | None = None, now=None) -> Check:
    """RECENT_TOOL_FAILURES — AI 앱이 최근 48시간 동안 도구를 부르다 실패했는지(세 Lens 공통).

    입력은 도구 호출 기록(metrics JSONL). 네트워크를 타지 않는다. 판정은 "지금 막혀 있나"
    기준이다 — 실패가 있었어도 같은 도구가 그 뒤에 성공했으면 풀린 것으로 본다.

    한계: 도구가 예외 없이 "⚠️ …" 문자열을 돌려준 실패는 metrics 에 에러로 안 남아서 못 본다.
    데몬의 백그라운드 수집 실패도 여기 안 남는다(DAEMON_COLLECTOR 가 본다).
    """
    c = Check("Recent tool failures")
    c.critical = False  # 이 항목 하나로 카드가 "사용 불가"가 되면 안 된다
    if records is None:
        from telegram_lens._metrics import load_metrics

        records = load_metrics(hours=_RECENT_HOURS, now=now)

    if not records:
        c.ok(f"최근 이틀 동안 AI 앱이 {LENS}를 쓴 기록이 없어요.")
        return c

    total = len(records)
    per_tool: dict[str, dict] = {}
    failures = 0
    for rec in records:  # 시간순
        tool = str(rec.get("tool") or "unknown")
        err = rec.get("error")
        slot = per_tool.setdefault(tool, {"fails": 0, "cancelled": 0, "last": None, "last_ok": None})
        if not err:
            slot["last_ok"] = True
            continue
        category = classify_error(err, rec.get("error_detail"))
        if category == "cancelled":
            # AI 앱이 기다리다 취소한 것 — 실패도 성공도 아니다. 마지막 결과를 바꾸지 않는다.
            slot["cancelled"] += 1
            slot["cancel_rec"] = rec
            continue
        failures += 1
        slot["fails"] += 1
        slot["last"] = (rec, category)
        slot["last_ok"] = False

    unresolved = {t: s for t, s in per_tool.items() if s["last_ok"] is False}

    # 지원용 줄: 아직 막힌 도구 먼저, 그 안에서는 최근 실패 먼저. 최대 8줄.
    rows = []
    for tool, s in per_tool.items():
        if s["fails"]:
            rec, category = s["last"]
            n = s["fails"]
        elif s["cancelled"]:
            rec, category = s["cancel_rec"], "cancelled"
            n = s["cancelled"]
        else:
            continue
        rows.append((tool not in unresolved, tool, n, rec, category))
    # 안정 정렬 두 번 — timestamp 는 같은 형식의 ISO 문자열이라 글자순이 곧 시간순이다.
    rows.sort(key=lambda r: str(r[3].get("timestamp") or ""), reverse=True)
    rows.sort(key=lambda r: r[0])
    for _, tool, n, rec, category in rows[:_RECENT_DETAIL_MAX_LINES]:
        c.info(_failure_line(tool, n, rec, category))

    if not failures:
        c.ok(f"최근 이틀 동안 조회 {total}번이 모두 정상이었어요.")
        return c
    if not unresolved:
        c.ok(f"최근 이틀 동안 조회 {total}번 중 {failures}번이 실패했지만, 그 뒤에는 정상이었어요.")
        return c

    # 대표 분류: 아직 막혀 있는 도구들의 마지막 실패 분류 중 가장 많은 것(같으면 판정 순서 앞쪽).
    counts: dict[str, int] = {}
    for s in unresolved.values():
        counts[s["last"][1]] = counts.get(s["last"][1], 0) + 1
    lead = max(counts, key=lambda cat: (counts[cat], -CATEGORIES.index(cat)))
    c.error_code = f"RECENT_TOOL_FAILURES_{lead.upper()}"
    c.warn(
        f"최근 조회 중 아직 실패로 남아 있는 것이 {len(unresolved)}가지 있어요.",
        action=action_for(lead, LENS),
    )
    return c


def _failure_line(tool: str, n: int, rec: dict, category: str) -> str:
    ts = rec.get("_ts")
    if ts is None:
        try:
            from datetime import datetime

            ts = datetime.fromisoformat(str(rec.get("timestamp")))
        except (TypeError, ValueError):
            ts = None
    when = ts.strftime("%H:%M") if ts is not None else "?"
    err = str(rec.get("error") or "")
    detail = str(rec.get("error_detail") or "")[:_RECENT_DETAIL_CHARS]
    raw = f"{err}: {detail}" if detail else err
    raw = _SECRET_CHUNK_RE.sub("<가림>", raw)
    return f"{tool}: 실패 {n}번, 마지막 {when}, 분류 {category}, {raw}"


# ── 복구 ────────────────────────────────────────────────────────────


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, OSError):
        return False
    return ans in ("y", "yes")


def run_repair(scope: str, assume_yes: bool) -> dict:
    """안전한 복구만 수행. 세션·DB·라이선스·채널데이터는 절대 건드리지 않는다.

    scope: "all" 또는 "daemon" — 현재 구현된 복구 항목은 전부 데몬/상태파일 범위라
    두 scope 의 동작은 동일하다(향후 다른 범위가 생기면 여기서 분기).
    """
    from telegram_lens import procstate
    from telegram_lens.daemon import lock_path, status_path

    actions: list[dict] = []
    lock = procstate.DaemonLock(lock_path())
    held = lock.is_held()
    status = procstate.read_json(status_path())

    # 1) 멎은(하트비트 정지) 데몬 정리 — 신원은 락 자체가 보장하므로 확인만 받으면 된다.
    if not held:
        actions.append({
            "action": "daemon_zombie_kill", "status": "skipped",
            "detail": "데몬이 가동 중이 아닙니다(복구 대상 없음).",
        })
    else:
        health = procstate.compute_health(status, held)
        if health["problem_code"] == "DAEMON_STALLED":
            if _confirm("멎은 데몬(락 보유 프로세스, 하트비트 정지)을 종료할까요?", assume_yes):
                ok = lock.acquire(status_path=status_path())
                if ok:
                    lock.release()  # 다음 정상 데몬이 새로 락을 잡을 수 있게 반납
                    actions.append({
                        "action": "daemon_zombie_kill", "status": "done",
                        "detail": "멎은 데몬을 종료했습니다. Claude 를 재시작하면 새 데몬이 뜹니다.",
                    })
                else:
                    actions.append({
                        "action": "daemon_zombie_kill", "status": "failed",
                        "detail": "종료를 시도했지만 락 회수에 실패했습니다.",
                    })
            else:
                actions.append({
                    "action": "daemon_zombie_kill", "status": "skipped",
                    "detail": "확인하지 않아 건너뜀(--yes 로 비대화형 실행 가능).",
                })
        else:
            actions.append({
                "action": "daemon_zombie_kill", "status": "skipped",
                "detail": f"좀비로 판정되지 않아 종료 대상 아님(health={health['health']}, "
                          f"{health['message']}).",
            })

    # 2) 손상된 상태 파일 백업 후 정리(재생성은 다음 데몬 사이클이 자동으로 함).
    sp = status_path()
    if sp.exists() and status is None:
        backup = sp.with_name(sp.name + f".corrupt-{int(time.time())}")
        try:
            sp.rename(backup)
            actions.append({
                "action": "status_file_backup", "status": "done",
                "detail": f"손상된 상태 파일을 {backup.name} 로 백업했습니다.",
            })
        except OSError as e:
            actions.append({
                "action": "status_file_backup", "status": "failed", "detail": str(e),
            })
    else:
        actions.append({
            "action": "status_file_backup", "status": "skipped",
            "detail": "상태 파일이 정상입니다.",
        })

    # 3) 정체된 백필을 interrupted 로 표시.
    bf = (status or {}).get("backfill") or {}
    if bf.get("state") == "running":
        age = procstate.heartbeat_age_sec(bf, "last_progress_at")
        if age is not None and age > 300:
            status["backfill"]["state"] = "interrupted"
            procstate.atomic_write_json(status_path(), status)
            actions.append({
                "action": "backfill_mark_interrupted", "status": "done",
                "detail": "정체된 백필을 interrupted 로 표시했습니다.",
            })
        else:
            actions.append({
                "action": "backfill_mark_interrupted", "status": "skipped",
                "detail": "백필이 정상 진행 중입니다.",
            })
    else:
        actions.append({
            "action": "backfill_mark_interrupted", "status": "skipped",
            "detail": "진행 중인 백필이 없습니다.",
        })

    return {"scope": scope, "actions": actions}


# ── 출력 ────────────────────────────────────────────────────────────

STATUS_ICON = {
    "ok": "[ OK ]",
    "warn": "[WARN]",
    "fail": "[FAIL]",
    "info-skip": "[SKIP]",
    None: "[ ?  ]",
}


def print_check(c: Check):
    icon = STATUS_ICON.get(c.status, "[ ?  ]")
    print(f"{icon} {c.name}")
    for line in c.lines:
        print(f"       {line}")
    if c.fix:
        print(f"       Fix: {c.fix}")
    print()


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="telegramlens-doctor",
        description="telegramlens 설치·런타임 진단 도구.",
    )
    p.add_argument("--json", action="store_true", help="구조화된 JSON으로 출력합니다.")
    p.add_argument(
        "--repair",
        nargs="?",
        const="all",
        default=None,
        metavar="SCOPE",
        help="감지된 문제를 안전하게 복구합니다(SCOPE=daemon 이면 데몬/PID/상태파일만).",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="복구 확인 프롬프트 없이 진행합니다(비대화형/관리자 앱 실행용).",
    )
    return p


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = _build_parser().parse_args()

    if args.repair is not None:
        result = run_repair(args.repair, args.yes)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("=" * 60)
            print("  telegramlens-doctor --repair")
            print("=" * 60)
            for a in result["actions"]:
                print(f"[{a['status'].upper():>18}] {a['action']}: {a['detail']}")
            print("=" * 60)
        return

    from telegram_lens import procstate
    from telegram_lens.daemon import lock_path

    daemon_running = procstate.DaemonLock(lock_path()).is_held()

    desktop_check = check_config_desktop()
    code_check = check_config_code()
    codex_check = check_config_codex()
    daemon_check = check_daemon()
    backfill_check = check_backfill()

    check_pairs: list[tuple[str, Check]] = [
        ("uv", check_uv()),
        ("package", check_package()),
        ("command", check_telegramlens_command()),
        ("config_desktop", desktop_check),
        ("config_code", code_check),
        ("config_codex", codex_check),
        ("registered_targets", check_at_least_one_config(desktop_check, code_check, codex_check)),
        ("license", check_license()),
        ("telegram_login", check_telegram_login(daemon_running)),
        ("daemon", daemon_check),
        ("data", check_data()),
        ("backfill", backfill_check),
        ("recent_failures", check_recent_tool_failures()),
    ]
    checks = [c for _, c in check_pairs]

    any_fail = any(c.status == "fail" for c in checks)
    any_warn = any(c.status == "warn" for c in checks)

    if args.json:
        from datetime import datetime, timezone

        from telegram_lens import __version__, config as tl_config

        # TelegramLens doctor에는 --online 플래그가 없다 — 데몬 실행 여부에 따라
        # check_telegram_login()이 실 Telegram 연결을 시도했는지가 곧 "online" 여부다.
        api_id, api_hash = tl_config.get_credentials()
        online = (
            not daemon_running
            and bool(api_id and api_hash)
            and tl_config.is_logged_in()
        )

        # daemon/backfill 문제는 `telegramlens-doctor --repair daemon --yes`로 고칠 수 있다
        # (run_repair는 scope와 무관하게 좀비 데몬 정리·상태파일 복구·정체 백필 표시를 모두 수행).
        # daemon 쪽 좀비 정리는 데몬이 지금 떠 있어야만 의미가 있다(안 떠 있으면
        # run_repair가 "복구 대상 없음"으로 스킵) — 그래서 daemon_running으로 한 번 더
        # 거른다. backfill 쪽(정체된 백필을 interrupted로 표시)은 상태 파일만 보고
        # 판단해 데몬이 죽어있어도 유효한 복구라 daemon_running 조건을 걸지 않는다.
        daemon_repairable = daemon_running and daemon_check.status in ("warn", "fail")
        backfill_repairable = backfill_check.status in ("warn", "fail")

        checks_list = []
        for key, c in check_pairs:
            if key == "daemon":
                checks_list.append(
                    c.to_contract_dict(_CHECK_IDS[key], repairable=daemon_repairable,
                                        repair_id="daemon" if daemon_repairable else None)
                )
            elif key == "backfill":
                checks_list.append(
                    c.to_contract_dict(_CHECK_IDS[key], repairable=backfill_repairable,
                                        repair_id="daemon" if backfill_repairable else None)
                )
            else:
                checks_list.append(c.to_contract_dict(_CHECK_IDS[key]))

        payload = {
            "schema_version": SCHEMA_VERSION,
            "product": PRODUCT,
            "package_name": PACKAGE_NAME,
            "installed_version": __version__,
            "version_truth": version_report(
                __version__,
                __import__("telegram_lens._version",
                           fromlist=["dist_version"]).dist_version()),
            "dist_metadata_scan": {
                key: value for key, value in
                scan_dist_metadata(PACKAGE_NAME).items() if key != "valid"
            },
            "latest_version": None,
            "update_available": None,
            "overall": "fail" if any_fail else ("degraded" if any_warn else "ok"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "online": online,
            "license": license_summary(),
            "targets": _registered_targets(desktop_check, code_check, codex_check),
            "checks": checks_list,
        }
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(1 if any_fail else 0)

    print("=" * 60)
    print("  telegramlens Doctor - Installation & Runtime Diagnosis")
    print("=" * 60)
    print()

    for c in checks:
        print_check(c)

    print("=" * 60)
    if any_fail:
        print("  [FAIL] One or more critical issues found.")
        print("  Apply the 'Fix:' commands above, then re-run telegramlens-doctor.")
        sys.exit(1)
    elif any_warn:
        print("  [WARN] Installation works but some warnings exist.")
        print("  If MCP appears in Claude Desktop, you're fine.")
    else:
        print("  [ OK ] All checks passed!")
        print("  If MCP still doesn't appear, FULLY QUIT Claude Desktop")
        print("  (tray icon -> Quit) and restart.")
    print("=" * 60)


if __name__ == "__main__":
    main()
