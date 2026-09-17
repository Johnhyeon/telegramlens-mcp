"""고객에게 보이는 문구에 터미널 명령·이메일 주소가 다시 들어가지 않게 막는다.

2026-09-17 토스 원칙 점검: 잠금 안내·진단 화면·로그인 안내에 `telegramlens-activate`,
`telegramlens-login` 같은 터미널 명령이 그대로 있었다. 주 고객층(40-50대)은 터미널을 열지
못해 거기서 멈추고 문의로 왔다. 안내는 LeetKit Manager 버튼 한 길이다.

여기서 모으는 문구는 전부 "Claude 답변 안"(도구 응답) 또는 "Manager 화면 안"(doctor --json
의 summary/action, 활성화 창 사유)에 뜨는 것이다. 터미널에서 직접 실행했을 때만 보이는
출력(doctor 텍스트 모드의 Fix: 줄, argparse 도움말)은 대상이 아니다.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telegram_lens import _metrics, client, doctor, licensing, procstate, server
from telegram_lens import config as tl_config

FORBIDDEN = [
    re.compile(r"\b(stocklens|dartlens|telegramlens)-(activate|doctor|setup|login|broker)\b", re.I),
    re.compile(r"\buv (tool|pip|run)\b", re.I),
    re.compile(r"터미널", re.I),
    re.compile(r"PowerShell", re.I),
    re.compile(r"@gmail\.com", re.I),
    re.compile(r"STOCKLENS_HOME|DARTLENS_HOME|TELEGRAMLENS_HOME", re.I),
]

# Manager 화면에 지금 있는 버튼 이름 그대로(이름 변경은 대표 결정 대기).
ALLOWED_BUTTONS = {
    "활성화", "구매", "업데이트", "MCP 등록", "진단", "복구", "텔레그램 로그인", "증권사 연결",
    "지원 문의", "문제 해결",
}
_BUTTON_RE = re.compile(r"\[([^\[\]]+)\]")


def assert_clean(text: str | None, where: str) -> None:
    if not text:
        return
    for rx in FORBIDDEN:
        assert not rx.search(text), f"{where}: 금지 패턴 {rx.pattern!r} → {text!r}"
    for name in _BUTTON_RE.findall(text):
        assert name in ALLOWED_BUTTONS, f"{where}: 없는 버튼 이름 [{name}] → {text!r}"


# ── 1. 도구 잠금 안내(Claude 답변 안) ──────────────────────────────────


@pytest.mark.parametrize(
    "reason, must_have",
    [
        ("missing", ["[활성화]", "[지원 문의]", "LeetKit Manager의 TelegramLens 카드에서"]),
        ("invalid", ["[활성화]", "[지원 문의]"]),
        ("expired", ["[구매]", "[활성화]"]),
        ("revoked", ["[지원 문의]"]),
        ("clock", ["날짜와 시간을 오늘로", "[지원 문의]"]),
    ],
)
def test_locked_messages(monkeypatch, reason, must_have):
    monkeypatch.setattr(licensing, "license_block_reason", lambda: reason)
    text = licensing.locked_message()
    assert_clean(text, f"locked_message({reason})")
    for piece in must_have:
        assert piece in text
    assert "습니다" not in text  # 해요체로 통일(합쇼체와 섞지 않는다)


def test_activation_reasons_shown_in_manager(monkeypatch):
    """활성화 창에 뜨는 사유 — 이메일 주소 대신 [지원 문의]."""
    monkeypatch.setattr(licensing, "verify_key", lambda k: {"valid": True, "license_id": "abc", "expires_on": None})

    monkeypatch.setattr(licensing, "_other_trial_used", lambda res: "zzz")
    res = licensing.save_key("X")
    assert_clean(res["reason"], "save_key(trial reused)")
    assert "[지원 문의]" in res["reason"]

    monkeypatch.setattr(licensing, "_other_trial_used", lambda res: None)
    monkeypatch.setattr(licensing, "effective_expiry", lambda res: None)
    monkeypatch.setattr(licensing, "_is_expired", lambda e: True)
    assert_clean(licensing.save_key("X")["reason"], "save_key(expired)")

    monkeypatch.setattr(licensing, "_is_expired", lambda e: False)
    monkeypatch.setattr(licensing, "is_revoked", lambda lid: True)
    res = licensing.save_key("X")
    assert_clean(res["reason"], "save_key(revoked)")
    assert "[지원 문의]" in res["reason"]


@pytest.mark.parametrize("key", ["!!!", "AAAA-BBBB", ""])
def test_activation_failure_message_hides_developer_reasons(key):
    """"서명 불일치(위조/변조)" 같은 원문은 활성화 창에 안 보낸다(터미널 출력·지원용에만)."""
    res = licensing.verify_key(key)
    assert res["valid"] is False
    msg = licensing.activation_failure_message(res)
    assert_clean(msg, f"activation_failure_message({key!r})")
    assert msg != res["reason"]
    assert "[지원 문의]" in msg
    for dev in ("서명 불일치", "형식 오류", "이 제품의 키가 아님", "공개키"):
        assert dev not in msg


# ── 2. 로그인 안내(Claude 답변 안) ────────────────────────────────────


def test_login_messages_constants():
    assert client.LOGIN_REQUIRED_MESSAGE == (
        "텔레그램 로그인이 필요해요. LeetKit Manager의 TelegramLens 카드에서 [텔레그램 로그인]을 눌러주세요."
    )
    assert client.NO_CREDENTIALS_MESSAGE == (
        "텔레그램 api_id와 api_hash가 아직 없어요. "
        "LeetKit Manager의 TelegramLens 카드에서 [텔레그램 로그인]을 눌러 넣어주세요."
    )
    assert_clean(client.LOGIN_REQUIRED_MESSAGE, "LOGIN_REQUIRED_MESSAGE")
    assert_clean(client.NO_CREDENTIALS_MESSAGE, "NO_CREDENTIALS_MESSAGE")


def test_make_client_without_credentials(monkeypatch):
    monkeypatch.setattr(client, "get_credentials", lambda: (None, None))
    with pytest.raises(client.NoCredentialsError) as exc:
        client.make_client()
    assert str(exc.value) == client.NO_CREDENTIALS_MESSAGE


def test_every_login_site_uses_the_shared_sentence():
    """로그인 필요 안내가 흩어져 다시 옛 문구로 돌아가지 않게 — 소스에서 직접 본다."""
    root = Path(server.__file__).parent
    for name in ("classify.py", "sync.py", "daemon.py", "collect_about.py", "client.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "`telegramlens-login` 을" not in src, name
    for name in ("classify.py", "sync.py", "daemon.py", "collect_about.py"):
        assert "LOGIN_REQUIRED_MESSAGE" in (root / name).read_text(encoding="utf-8"), name


def test_safe_tool_messages(monkeypatch):
    monkeypatch.setattr(server, "is_licensed", lambda: True)

    async def _no_update():
        return ""

    monkeypatch.setattr("telegram_lens._update_check.get_update_notice", _no_update)

    @server.safe_tool
    async def not_logged_in():
        raise client.NotLoggedInError(client.LOGIN_REQUIRED_MESSAGE)

    @server.safe_tool
    async def no_creds():
        raise client.NoCredentialsError(client.NO_CREDENTIALS_MESSAGE)

    @server.safe_tool
    async def boom():
        raise RuntimeError("뭔지 모를 오류")

    for fn in (not_logged_in, no_creds):
        out = asyncio.run(fn())
        assert_clean(out, fn.__name__)
        assert "[텔레그램 로그인]" in out

    out = asyncio.run(boom())
    assert_clean(out, "safe_tool(unknown)")
    assert "조회 중 문제가 생겼어요" in out
    assert "LeetKit Manager 상단 [지원 문의]" in out
    assert "RuntimeError" in out  # 원인 원문은 지원용으로 남긴다

    monkeypatch.setattr(server, "is_licensed", lambda: False)
    monkeypatch.setattr(licensing, "license_block_reason", lambda: "missing")
    monkeypatch.setattr(server, "locked_message", licensing.locked_message)
    out = asyncio.run(boom())
    assert_clean(out, "safe_tool(locked)")
    assert "[활성화]" in out


# ── 3. telegram_status(Claude 답변 안) ─────────────────────────────────


def _status(monkeypatch, *, logged_in: bool, held: bool = False, daemon_status: dict | None = None) -> dict:
    monkeypatch.setattr(server, "is_licensed", lambda: True)
    monkeypatch.setattr(server, "is_logged_in", lambda: logged_in)
    monkeypatch.setattr(procstate.DaemonLock, "is_held", lambda self: held)
    monkeypatch.setattr(
        procstate, "read_json", lambda p: daemon_status if Path(p).name == "daemon_status.json" else None
    )

    async def _no_update():
        return ""

    monkeypatch.setattr("telegram_lens._update_check.get_update_notice", _no_update)
    return json.loads(asyncio.run(server.telegram_status()))


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def test_status_not_logged_in(monkeypatch):
    out = _status(monkeypatch, logged_in=False)
    assert out["status"] == "failed"
    assert "command" not in out["recovery"]
    assert "[텔레그램 로그인]" in out["recovery"]["instruction"]
    assert "세션 파일" not in out["summary"] + out["last_error"]["message"]
    for text in _walk_strings(out):
        assert_clean(text, "telegram_status(not logged in)")


def test_status_daemon_problem_points_to_repair(monkeypatch):
    stalled = {"heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
    out = _status(monkeypatch, logged_in=True, held=True, daemon_status=stalled)
    assert out["last_error"]["code"] == "DAEMON_STALLED"
    assert "command" not in out["recovery"]
    assert "LeetKit Manager의 TelegramLens 카드에서 [복구]" in out["recovery"]["instruction"]
    for text in _walk_strings(out):
        assert_clean(text, "telegram_status(daemon)")


def test_status_docstring_does_not_send_to_terminal():
    """docstring 은 Claude 에게 주는 지시다. '터미널 명령은 안내하지 마세요' 금지문만 허용한다."""
    doc = server.telegram_status.__doc__ or ""
    assert "터미널 명령은 안내하지 마세요." in doc
    assert "[진단]" in doc and "[지원 문의]" in doc
    assert_clean(doc.replace("터미널 명령은 안내하지 마세요.", ""), "telegram_status docstring")


# ── 4. 진단 JSON(Manager 화면: summary / action) ───────────────────────

_CONTRACT_TOP = {
    "schema_version", "product", "package_name", "installed_version", "version_truth",
    "dist_metadata_scan", "latest_version", "update_available", "overall", "checked_at",
    "online", "license", "targets", "checks",
}
_OLD_IDS = {
    "UV_AVAILABLE", "PACKAGE_IMPORTABLE", "COMMAND_AVAILABLE", "MCP_CONFIG_DESKTOP",
    "MCP_CONFIG_CODE", "MCP_CONFIG_CODEX", "MCP_CONFIG_VALID", "LICENSE_ACTIVE",
    "TELEGRAM_LOGIN", "DAEMON_COLLECTOR", "DATA_SQLITE", "BACKFILL",
}


@pytest.fixture
def sim(tmp_path, monkeypatch, capsys):
    """doctor --json 을 진짜 main() 으로 돌리되, 이 PC 의 설정·세션·데몬은 안 본다."""
    state = {
        "uv": "C:/uv/uv.exe",
        "license": "active",
        "creds": (123, "hash"),
        "logged_in": True,
        "connect_exc": None,   # 가짜 텔레그램 연결이 던질 예외(없으면 연결 성공)
        "authorized": True,
        "held": False,
        "daemon_status": None,
        "records": [],
        "db_locked": False,
    }
    desktop = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(doctor, "_find_uv", lambda: state["uv"])
    monkeypatch.setattr(doctor, "get_claude_desktop_config_path", lambda: desktop)
    monkeypatch.setattr(doctor, "get_claude_code_config_path", lambda: tmp_path / "claude.json")
    monkeypatch.setattr(doctor, "get_codex_config_path", lambda: tmp_path / "codex.toml")
    monkeypatch.setattr(
        doctor, "license_summary", lambda: {"status": state["license"], "license_id_masked": None}
    )
    monkeypatch.setattr(tl_config, "get_credentials", lambda: state["creds"])
    monkeypatch.setattr(tl_config, "is_logged_in", lambda: state["logged_in"])

    class _FakeTelegram:
        """진짜 _check_real_connection 을 태우되 네트워크는 안 탄다."""

        async def connect(self):
            if state["connect_exc"] is not None:
                raise state["connect_exc"]

        async def is_user_authorized(self):
            return state["authorized"]

        def is_connected(self):
            return False

    monkeypatch.setattr(client, "make_client", lambda: _FakeTelegram())
    monkeypatch.setattr(procstate.DaemonLock, "is_held", lambda self: state["held"])
    monkeypatch.setattr(procstate, "read_json", lambda p: state["daemon_status"])
    monkeypatch.setattr(_metrics, "load_metrics", lambda **kw: state["records"])

    # 파일이 없으면 check_data 는 "아직 수집 전"으로 끝난다. 잠김 시나리오만 파일을 만든다.
    db_file = tmp_path / "telegramlens.db"
    monkeypatch.setattr(tl_config, "db_path", lambda: db_file)
    state["db_file"] = db_file
    real_connect = sqlite3.connect

    def _connect(*a, **kw):
        if state["db_locked"]:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", _connect)

    def register(command: str | None = sys.executable):
        desktop.write_text(
            json.dumps({"mcpServers": {doctor.SERVER_KEY: {"command": command, "args": []}}}),
            encoding="utf-8",
        )

    def run() -> dict:
        monkeypatch.setattr(sys, "argv", ["telegramlens-doctor", "--json"])
        capsys.readouterr()
        with pytest.raises(SystemExit):
            doctor.main()
        payload = json.loads(capsys.readouterr().out)
        for chk in payload["checks"]:
            assert_clean(chk["summary"], f"{chk['id']}.summary")
            assert_clean(chk["action"], f"{chk['id']}.action")
        return payload

    state["register"] = register
    state["run"] = run
    state["desktop"] = desktop
    return state


def _by_id(payload: dict) -> dict:
    return {c["id"]: c for c in payload["checks"]}


def test_contract_is_only_extended(sim):
    sim["register"]()
    payload = sim["run"]()
    assert set(payload) == _CONTRACT_TOP
    ids = [c["id"] for c in payload["checks"]]
    assert _OLD_IDS <= set(ids)
    assert "RECENT_TOOL_FAILURES" in ids
    for chk in payload["checks"]:
        assert {"id", "status", "summary", "details", "repairable", "repair_id", "action"} <= set(chk)
    assert _by_id(payload)["RECENT_TOOL_FAILURES"]["critical"] is False


@pytest.mark.parametrize(
    "status, summary, action",
    [
        ("missing", "라이선스 키가 아직 없어요.", "TelegramLens 카드의 [활성화]를 눌러 메일로 받은 키를 넣어주세요."),
        ("invalid", "저장된 라이선스 키를 확인할 수 없어요.", "TelegramLens 카드의 [활성화]를 눌러 메일로 받은 키를 다시 넣어주세요."),
        ("expired", "사용 기간이 끝났어요.", "TelegramLens 카드의 [구매]를 누르고, 받은 키를 [활성화]로 넣어주세요."),
        ("revoked", "이 라이선스 키는 사용이 중지돼 있어요.", "착오라면 상단 [지원 문의]를 눌러주세요."),
        ("clock", "이 컴퓨터의 날짜가 실제보다 과거로 되어 있어요.", "날짜와 시간을 오늘로 맞춘 뒤 [진단]을 다시 눌러주세요."),
    ],
)
def test_license_rows(sim, status, summary, action):
    sim["register"]()
    sim["license"] = status
    chk = _by_id(sim["run"]())["LICENSE_ACTIVE"]
    assert (chk["status"], chk["summary"], chk["action"]) == ("fail", summary, action)


def test_mcp_not_registered(sim):
    chk = _by_id(sim["run"]())["MCP_CONFIG_VALID"]
    assert chk["summary"] == "TelegramLens가 아직 AI 앱에 등록되지 않았어요."
    assert chk["action"] == "TelegramLens 카드의 [MCP 등록]을 눌러주세요."


def test_mcp_registered_but_exe_missing(sim, tmp_path):
    sim["register"](str(tmp_path / "gone" / "telegramlens.exe"))
    chk = _by_id(sim["run"]())["MCP_CONFIG_DESKTOP"]
    assert chk["status"] == "fail"
    assert chk["action"] == "TelegramLens 카드의 [MCP 등록]을 눌러주세요."
    assert any("Command file missing" in line for line in chk["details"]["lines"])  # 원문은 지원용 줄에


def test_mcp_config_broken_json(sim):
    sim["desktop"].write_text("{ not json", encoding="utf-8")
    chk = _by_id(sim["run"]())["MCP_CONFIG_DESKTOP"]
    assert chk["action"] == "TelegramLens 카드의 [MCP 등록]을 눌러주세요."


def test_uv_missing(sim):
    sim["register"]()
    sim["uv"] = None
    chk = _by_id(sim["run"]())["UV_AVAILABLE"]
    assert chk["status"] == "warn"
    assert "[업데이트]" in chk["action"]


def test_telegram_login_rows(sim):
    sim["register"]()
    sim["creds"] = (None, None)
    chk = _by_id(sim["run"]())["TELEGRAM_LOGIN"]
    assert chk["summary"] == "텔레그램 api_id와 api_hash가 아직 없어요."
    assert chk["action"] == "TelegramLens 카드의 [텔레그램 로그인]을 눌러 넣어주세요."

    sim["creds"] = (123, "hash")
    sim["logged_in"] = False
    chk = _by_id(sim["run"]())["TELEGRAM_LOGIN"]
    assert chk["summary"] == "텔레그램 로그인이 아직 안 돼 있어요."
    assert chk["action"] == "TelegramLens 카드의 [텔레그램 로그인]을 눌러주세요."

    sim["logged_in"] = True
    sim["authorized"] = False
    chk = _by_id(sim["run"]())["TELEGRAM_LOGIN"]
    assert chk["summary"] == "텔레그램 로그인이 풀려 있어요."
    assert chk["action"] == "TelegramLens 카드의 [텔레그램 로그인]을 다시 눌러주세요."
    assert any("SESSION_INVALID" in line for line in chk["details"]["lines"])


# Telethon 예외는 만들려면 요청 객체가 필요하다 — 분류는 클래스 이름만 보므로 같은 이름으로 흉내 낸다.
class FloodWaitError(Exception):
    pass


class AuthKeyUnregisteredError(Exception):
    pass


def _tls_wrapped():
    import ssl

    import httpx

    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as inner:
            raise httpx.ConnectError("connection failed") from inner
    except httpx.ConnectError as outer:
        return outer


@pytest.mark.parametrize(
    "make_exc, category",
    [
        (_tls_wrapped, "tls"),
        (lambda: TimeoutError(), "timeout"),
        (lambda: OSError("[Errno 11001] getaddrinfo failed"), "dns"),
        (lambda: ConnectionError("Connection to Telegram failed 5 time(s)"), "connect"),
        (lambda: FloodWaitError("A wait of 300 seconds is required"), "blocked"),
        (lambda: AuthKeyUnregisteredError("The key is not registered in the system"), "auth"),
        (lambda: KeyError("x"), "schema"),
        (lambda: RuntimeError("모르는 오류"), "other"),
    ],
)
def test_telegram_connection_failure_by_category(sim, make_exc, category):
    from telegram_lens._error_class import action_for

    sim["register"]()
    sim["connect_exc"] = make_exc()
    chk = _by_id(sim["run"]())["TELEGRAM_LOGIN"]
    assert chk["status"] == "fail"
    assert chk["action"] == action_for(category, "TelegramLens")
    assert not re.search(r"\w+Error", chk["summary"])  # 예외 이름은 summary 에 안 쓴다
    assert any(f"분류 {category}" in line for line in chk["details"]["lines"])


def test_telegram_connection_check_crashed(sim, monkeypatch):
    sim["register"]()

    async def _crash():
        raise TimeoutError("boom")

    monkeypatch.setattr(doctor, "_check_real_connection", _crash)
    chk = _by_id(sim["run"]())["TELEGRAM_LOGIN"]
    assert chk["status"] == "warn"
    assert chk["summary"] == "텔레그램 연결을 확인하지 못했어요."
    assert "TimeoutError" not in chk["summary"]


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.mark.parametrize(
    "held, make_status, expects_repair",
    [
        (True, lambda: {"heartbeat_at": _iso(10)}, True),                                # 하트비트 정지
        (True, lambda: {"heartbeat_at": _iso(0), "consecutive_failures": 3}, True),      # 연속 실패
        (True, lambda: {"heartbeat_at": _iso(0), "last_success_at": _iso(120)}, True),   # 수집 지연
        (True, lambda: {"heartbeat_at": _iso(0), "channels": {"failed": 2}}, True),      # 채널 실패
        (True, lambda: None, True),                                                      # 상태 파일 손상
        (False, lambda: {"consecutive_failures": 5}, False),                             # 꺼짐 + 과거 실패
    ],
)
def test_daemon_rows(sim, held, make_status, expects_repair):
    sim["register"]()
    sim["held"] = held
    sim["logged_in"] = True
    sim["daemon_status"] = make_status()  # 시각은 테스트가 도는 순간 기준으로 만든다
    chk = _by_id(sim["run"]())["DAEMON_COLLECTOR"]
    assert chk["status"] in ("warn", "fail")
    assert "습니다" not in chk["summary"]
    if expects_repair:
        assert chk["action"] == "TelegramLens 카드의 [복구]를 눌러주세요."
        assert chk["repairable"] is True
    else:
        assert chk["action"] is None


def test_backfill_rows(sim):
    sim["register"]()
    sim["daemon_status"] = {"backfill": {"state": "interrupted"}}
    chk = _by_id(sim["run"]())["BACKFILL"]
    assert chk["status"] == "warn"
    assert "telegram_collect_history" not in (chk["action"] or "")

    sim["held"] = True
    sim["daemon_status"] = {
        "heartbeat_at": _iso(0),
        "backfill": {"state": "running", "last_progress_at": _iso(30), "processed_channels": 1, "total_channels": 5},
    }
    chk = _by_id(sim["run"]())["BACKFILL"]
    assert chk["status"] == "fail"
    assert chk["action"] == "TelegramLens 카드의 [복구]를 눌러주세요."


def test_data_locked(sim):
    sim["register"]()
    sim["db_file"].touch()
    sim["db_locked"] = True
    chk = _by_id(sim["run"]())["DATA_SQLITE"]
    assert chk["status"] == "fail"
    assert "[지원 문의]" in chk["action"]


@pytest.mark.parametrize(
    "error, detail, category",
    [
        ("ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", "tls"),
        ("ReadTimeout", "", "timeout"),
        ("ConnectError", "[Errno 11001] getaddrinfo failed", "dns"),
        ("ConnectionRefusedError", "", "connect"),
        ("FloodWaitError", "A wait of 300 seconds", "blocked"),
        ("AuthKeyUnregisteredError", "", "auth"),
        ("KeyError", "'x'", "schema"),
        ("RuntimeError", "?", "other"),
    ],
)
def test_recent_failures_warn_rows(sim, error, detail, category):
    sim["register"]()
    now = datetime.now()
    sim["records"] = [
        {"timestamp": (now - timedelta(minutes=5)).isoformat(timespec="seconds"), "tool": "telegram_trending",
         "error": error, "error_detail": detail, "_ts": now - timedelta(minutes=5)},
    ]
    payload = sim["run"]()
    chk = _by_id(payload)["RECENT_TOOL_FAILURES"]
    assert chk["status"] == "warn"
    assert chk["details"]["error_code"] == f"RECENT_TOOL_FAILURES_{category.upper()}"
    assert chk["critical"] is False  # 이 한 줄로 카드가 "사용 불가"가 되면 안 된다


# ── 5. 트레이 메뉴 ────────────────────────────────────────────────────


def _fake_pystray():
    mod = types.ModuleType("pystray")

    class MenuItem:
        def __init__(self, text, action, default=False, enabled=True):
            self.text, self.action = text, action

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    mod.MenuItem, mod.Menu = MenuItem, Menu
    return mod


def test_tray_menu_opens_manager_not_a_console(monkeypatch):
    from telegram_lens import tray

    monkeypatch.setitem(sys.modules, "pystray", _fake_pystray())
    menu = tray._build_menu({"summary": "정상으로 돌고 있어요."})
    labels = [i.text for i in menu.items if hasattr(i, "text") and isinstance(i.text, str)]
    assert "LeetKit Manager 열기" in labels
    for label in labels:
        assert_clean(label, "tray menu")
        assert "doctor" not in label


def test_tray_finds_manager_shortcut(tmp_path, monkeypatch):
    from telegram_lens import tray

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert tray.find_manager_launcher() is None

    exe = tmp_path / ".local" / "bin" / "leetkit-manager.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    assert tray.find_manager_launcher() == ("command", str(exe))

    if sys.platform in ("win32", "darwin"):
        name = "LeetKit Manager.lnk" if sys.platform == "win32" else "LeetKit Manager.app"
        (tmp_path / "Desktop").mkdir()
        (tmp_path / "Desktop" / name).touch()
        assert tray.find_manager_launcher() == ("shortcut", str(tmp_path / "Desktop" / name))

        # 사용자가 고른 폴더가 바탕화면보다 먼저다(Manager 의 existing_shortcut 과 같은 순서)
        chosen = tmp_path / "Apps"
        chosen.mkdir()
        (chosen / name).touch()
        (tmp_path / ".leetkit-manager").mkdir()
        (tmp_path / ".leetkit-manager" / "shortcut_created").write_text(str(chosen), encoding="utf-8")
        assert tray.find_manager_launcher() == ("shortcut", str(chosen / name))


def test_tray_open_manager_without_manager_notifies_plainly(tmp_path, monkeypatch):
    from telegram_lens import tray

    monkeypatch.setattr(tray, "find_manager_launcher", lambda: None)
    notes = []

    class Icon:
        def notify(self, msg, title):
            notes.append(msg)

    tray._open_manager(Icon())
    assert notes and "LeetKit Manager" in notes[0]
    assert_clean(notes[0], "tray notify")


def test_tray_open_manager_launches_without_console(monkeypatch):
    from telegram_lens import tray

    calls = []
    monkeypatch.setattr(tray, "find_manager_launcher", lambda: ("command", "C:/x/leetkit-manager.exe"))
    monkeypatch.setattr(tray.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    tray._open_manager(object())
    assert calls[0][0] == ["C:/x/leetkit-manager.exe", "gui"]
    assert "cmd" not in calls[0][0]
