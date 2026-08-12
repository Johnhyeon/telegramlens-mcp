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
}


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
        self.fix: str | None = None
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

    def warn(self, msg: str, fix: str | None = None):
        if self.status != "fail":
            self.status = "warn"
            self.summary = msg
        self.lines.append(msg)
        if fix:
            self.fix = fix
        return self

    def fail(self, msg: str, fix: str | None = None):
        self.status = "fail"
        self.summary = msg
        self.lines.append(msg)
        if fix:
            self.fix = fix
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
        return {
            "id": check_id,
            "status": self.status,
            "summary": self.summary or (self.lines[0] if self.lines else ""),
            "details": details,
            "repairable": repairable,
            "repair_id": repair_id,
            "action": self.fix,
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
            "uv not found",
            fix=(
                "Install uv (recommended):\n"
                "  Windows: irm https://astral.sh/uv/install.ps1 | iex\n"
                "  macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
            ),
        )
    return c


def check_package() -> Check:
    c = Check("Package (telegramlens-mcp)")
    try:
        import telegram_lens  # noqa: F401
        c.ok("telegramlens-mcp is importable")
        c.info(f"Location:   {Path(telegram_lens.__file__).parent}")
        c.info(f"Version:    {telegram_lens.__version__}")
        c.info(f"Python:     {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
        c.info(f"Executable: {sys.executable}")
    except ImportError:
        c.fail(
            "telegramlens-mcp NOT importable in current interpreter",
            fix="uv tool install --force telegramlens-mcp",
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
                )
                c.info(f"Path:       {candidate}")
                return c
    except Exception:
        pass

    c.fail(
        "'telegramlens' command NOT found anywhere",
        fix="uv tool install --force telegramlens-mcp",
    )
    return c


def label_to_target(label: str) -> str:
    return "claude-code" if "Code" in label else "claude-desktop"


def _check_config_file(label: str, config_path: Path, *, required: bool) -> Check:
    """단일 config 파일에 대한 점검. required=False면 부재 시 fail 대신 info."""
    c = Check(f"Config — {label}")

    if "Packages" in str(config_path) and "LocalCache" in str(config_path):
        c.info("Detected: Microsoft Store version (sandboxed path)")
    c.info(f"Path:       {config_path}")

    if not config_path.exists():
        if required:
            c.fail("Config file does not exist", fix="telegramlens-setup")
        else:
            c.info("Config file does not exist (target not in use — OK)")
            c.status = "info-skip"
            c.summary = "Config file does not exist (target not in use — OK)"
        return c

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        c.fail(f"Config is not valid JSON: {e}", fix="Back up and re-run telegramlens-setup")
        return c
    except Exception as e:
        c.fail(f"Cannot read config: {e}")
        return c

    servers = cfg.get("mcpServers", {}) or {}
    entry = servers.get(SERVER_KEY)

    if not entry:
        if required:
            c.fail(
                f"'{SERVER_KEY}' entry missing in mcpServers",
                fix=f"telegramlens-setup --target {label_to_target(label)}",
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
            c.fail(f"Command file missing: {cmd}", fix="telegramlens-setup")
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.fail(
                f"Command '{cmd}' not in PATH — client will fail to launch the server",
                fix="telegramlens-setup",
            )

    return c


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
            c.fail("Config file does not exist", fix="telegramlens-setup --target codex")
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
            c.fail(f"'{SERVER_KEY}' entry missing in mcp_servers", fix="telegramlens-setup --target codex")
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
            c.fail(f"Command file missing: {cmd}", fix="telegramlens-setup --target codex")
    else:
        resolved = shutil.which(cmd)
        if resolved:
            c.ok(f"Command resolvable via PATH: {resolved}")
        else:
            c.fail(
                f"Command '{cmd}' not in PATH — client will fail to launch the server",
                fix="telegramlens-setup --target codex",
            )

    return c


def check_at_least_one_config(*configs: Check) -> Check:
    """모든 config가 미등록이면 종합 fail. 하나라도 등록돼있으면 OK."""
    c = Check("Registered targets")
    registered = [cc for cc in configs if cc.status == "ok"]
    if registered:
        c.ok(f"{len(registered)} target(s) configured")
        return c
    c.fail(
        "telegramlens not registered in any MCP client (Claude Desktop / Code / Codex)",
        fix="telegramlens-setup --target {claude-desktop|claude-code|both|codex}",
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
        _expiry = res.get("expires_on")
        return {
            "status": _blocked,
            "license_id_masked": licensing.mask_tail((res.get("license_id") or "").upper()) or None,
            "expires_on": _expiry.isoformat() if _expiry else None,
        }

    license_id = res.get("license_id") or ""
    masked = licensing.mask_tail(license_id.upper()) if license_id else None
    return {"status": "active", "license_id_masked": masked}


def check_license() -> Check:
    """라이선스 활성화 여부. 키 원문·전체 license_id는 노출하지 않는다."""
    c = Check("License")
    try:
        from telegram_lens import licensing  # noqa: F401
    except ImportError as e:
        c.fail(f"licensing module not importable: {e}")
        return c

    summary = license_summary()
    if summary["status"] in ("expired", "revoked", "clock"):
        c.fail(
            {
                "expired": "License has expired",
                "revoked": "License has been revoked",
                "clock": "System clock is set in the past",
            }[summary["status"]],
            fix="telegramlens-activate <라이선스-키>" if summary["status"] == "expired" else None,
        )
        return c
    if summary["status"] == "missing":
        c.fail(
            "No license key found (env or license.key)",
            fix="telegramlens-activate <라이선스-키>",
        )
        return c
    if summary["status"] == "invalid":
        c.fail(
            "Stored key is invalid",
            fix="telegramlens-activate <라이선스-키>",
        )
        return c

    c.ok(f"Activated (license_id: {summary['license_id_masked']})")
    c.info(f"Source:     {'env' if os.environ.get('TELEGRAMLENS_LICENSE_KEY') else 'license.key'}")
    return c


# ── 런타임 진단 ──────────────────────────────────────────────────────


async def _check_real_connection() -> tuple[bool, str]:
    """실제로 Telegram 에 연결해 is_user_authorized() 까지 확인.

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
        return False, str(e)
    try:
        await connect_with_timeout(client, timeout=15)
        authorized = await client.is_user_authorized()
        if authorized:
            return True, "실제 연결 및 인증 확인 성공"
        return False, "연결은 됐지만 인증되어 있지 않습니다(세션 만료 가능성) — SESSION_INVALID"
    except Exception as e:  # noqa: BLE001 — 진단 자체가 죽으면 안 됨
        return False, f"{type(e).__name__}: {e}"
    finally:
        await disconnect_safely(client)


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
        c.fail(
            "No Telegram API credentials found (TELEGRAM_API_ID / TELEGRAM_API_HASH)",
            fix="telegramlens-login",
        )
        return c
    c.info(f"API_ID:     {api_id}")

    if not tl_config.is_logged_in():
        c.fail(
            "Credentials present but no session file — not logged in yet",
            fix="telegramlens-login",
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
        ok, detail = asyncio.run(_check_real_connection())
    except Exception as e:  # noqa: BLE001
        c.warn(f"실 연결 확인 중 오류: {type(e).__name__}: {e}")
        return c
    if ok:
        c.ok(detail)
    else:
        c.fail(detail, fix="telegramlens-login (세션 재발급)")
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

    fix = "telegramlens-doctor --repair daemon" if health["problem_code"] else None
    if health["health"] == "healthy":
        c.ok(health["message"])
    elif health["health"] == "degraded":
        c.warn(health["message"], fix=fix)
    else:
        c.fail(health["message"], fix=fix)
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
        c.fail(
            f"DB 락으로 접속 실패(2초 대기 후 포기): {e}",
            fix="telegramlens-doctor --repair daemon (멎은 데몬이 락을 쥐고 있을 수 있습니다)",
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
            "이전 백필이 중단된 채로 남아 있습니다.",
            fix="telegram_collect_history(days=...) 로 다시 요청하세요.",
        )
        return c

    age = procstate.heartbeat_age_sec(bf, "last_progress_at")
    if age is not None and age > 300:
        c.fail(
            f"백필 진행이 {int(age)}초째 멈춰 있습니다.",
            fix="telegramlens-doctor --repair daemon",
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
