"""원인 분류(_error_class) — 세 Lens 공통 사양 3-2 표의 근거마다 하나 이상.

category 이름은 Manager error_code(RECENT_TOOL_FAILURES_TLS 등)의 계약이라 세 Lens가
글자까지 같아야 한다. 판정 순서(tls 가 connect 보다 앞 등)도 여기서 고정한다.
"""

from __future__ import annotations

import ssl

import httpx
import pytest

from telegram_lens import _error_class as EC


def test_category_names_are_the_shared_contract():
    assert EC.CATEGORIES == (
        "cancelled", "tls", "dns", "timeout", "blocked", "auth", "connect", "schema", "other",
    )


@pytest.mark.parametrize(
    "etype, detail, expected",
    [
        # cancelled
        ("CancelledError", None, "cancelled"),
        # tls
        ("SSLError", "", "tls"),
        ("SSLCertVerificationError", "", "tls"),
        ("ConnectError", "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer", "tls"),
        ("ConnectError", "certificate verify failed", "tls"),
        ("ConnectError", "self signed certificate in certificate chain", "tls"),
        ("ConnectError", "self-signed certificate", "tls"),
        # dns
        ("ConnectError", "[Errno 11001] getaddrinfo failed", "dns"),
        ("ConnectError", "[Errno -2] Name or service not known", "dns"),
        ("ConnectError", "nodename nor servname provided, or not known", "dns"),
        ("OSError", "error 11001", "dns"),
        ("ConnectError", "[Errno -5] No address associated with hostname", "dns"),
        # timeout
        ("TimeoutException", "", "timeout"),
        ("ReadTimeout", "", "timeout"),
        ("ConnectTimeout", "", "timeout"),
        ("PoolTimeout", "", "timeout"),
        ("TimeoutError", "", "timeout"),
        ("RuntimeError", "The read operation timed out", "timeout"),
        # blocked
        ("HTTPStatusError", "Client error '403 Forbidden' for url 'x'", "blocked"),
        ("HTTPStatusError", "Client error '429 Too Many Requests' for url 'x'", "blocked"),
        ("HTTPStatusError", "HTTP 451 Unavailable For Legal Reasons", "blocked"),
        ("DartApiError", "[012] 접근할 수 없는 IP입니다", "blocked"),
        ("DartApiError", "[020] 요청 제한을 초과했습니다", "blocked"),
        ("FloodWaitError", "A wait of 30 seconds is required", "blocked"),
        # auth
        ("HTTPStatusError", "Client error '401 Unauthorized' for url 'x'", "auth"),
        ("AuthKeyUnregisteredError", "", "auth"),
        ("SessionRevokedError", "", "auth"),
        ("AuthKeyError", "", "auth"),
        ("DartApiError", "[010] 등록되지 않은 키입니다", "auth"),
        ("DartApiError", "[011] 사용할 수 없는 키입니다", "auth"),
        ("MissingApiKeyError", "DART 인증키가 아직 없어요.", "auth"),
        # connect
        ("ConnectError", "", "connect"),
        ("ConnectionRefusedError", "", "connect"),
        ("ConnectionResetError", "", "connect"),
        ("RemoteProtocolError", "Server disconnected", "connect"),
        ("OSError", "[WinError 10061] 대상 컴퓨터에서 연결을 거부했으므로", "connect"),
        ("OSError", "[WinError 10054] 현재 연결은 원격 호스트에 의해 강제로 끊겼습니다", "connect"),
        ("OSError", "Connection refused", "connect"),
        ("OSError", "Connection reset by peer", "connect"),
        # schema
        ("KeyError", "'list'", "schema"),
        ("IndexError", "list index out of range", "schema"),
        ("JSONDecodeError", "Expecting value: line 1 column 1 (char 0)", "schema"),
        ("ValueError", "Expecting value: line 1", "schema"),
        ("RuntimeError", "페이지 구조가 바뀌었습니다", "schema"),
        # other
        ("RuntimeError", "boom", "other"),
        (None, None, "other"),
        # DART 상태 코드는 DartApiError 일 때만 본다 — 다른 예외 글자에 [012] 가 섞여도 오판하지 않는다.
        ("RuntimeError", "[012] something", "other"),
        ("DartApiError", "[013] 조회된 데이터가 없습니다", "other"),
    ],
)
def test_classify_error_table(etype, detail, expected):
    assert EC.classify_error(etype, detail) == expected


def test_order_tls_beats_connect_and_timeout_beats_connect():
    assert EC.classify_error("ConnectError", "CERTIFICATE_VERIFY_FAILED") == "tls"
    assert EC.classify_error("ConnectTimeout", "") == "timeout"
    assert EC.classify_error("ConnectError", "getaddrinfo failed") == "dns"


def test_case_insensitive():
    assert EC.classify_error("sslerror", "") == "tls"
    assert EC.classify_error("CONNECTERROR", "CONNECTION REFUSED") == "connect"


def test_classify_exception_sees_wrapped_cause():
    inner = ssl.SSLCertVerificationError("verify failed")
    try:
        try:
            raise inner
        except ssl.SSLError as e:
            raise httpx.ConnectError("connection failed") from e
    except httpx.ConnectError as outer:
        assert EC.classify_exception(outer) == "tls"


def test_classify_exception_plain():
    assert EC.classify_exception(httpx.ReadTimeout("slow")) == "timeout"
    assert EC.classify_exception(httpx.ConnectError("[Errno 11001] getaddrinfo failed")) == "dns"


@pytest.mark.parametrize("category", [c for c in EC.CATEGORIES if c != "cancelled"])
def test_every_category_has_manager_action_with_button(category):
    action = EC.action_for(category, "DartLens")
    assert action and "[" in action


def test_cancelled_has_no_action():
    assert EC.action_for("cancelled", "DartLens") is None


def test_auth_action_is_lens_specific():
    assert "[활성화]" in EC.action_for("auth", "DartLens")
    assert "DART 인증키" in EC.action_for("auth", "DartLens")
    assert "[증권사 연결]" in EC.action_for("auth", "StockLens")
    assert "[텔레그램 로그인]" in EC.action_for("auth", "TelegramLens")


# ── TelegramLens 고유 예외(Telethon·로그인) ─────────────────────────────


@pytest.mark.parametrize(
    "etype, detail, expected",
    [
        ("SlowModeWaitError", "A wait of 60 seconds is required", "blocked"),
        ("UserDeactivatedError", "The user has been deleted/deactivated", "auth"),
        ("NotLoggedInError", "텔레그램 로그인이 필요해요.", "auth"),
        ("NoCredentialsError", "텔레그램 api_id와 api_hash가 아직 없어요.", "auth"),
        ("ConnectionError", "Connection to Telegram failed 5 time(s)", "connect"),
    ],
)
def test_telegramlens_specific(etype, detail, expected):
    assert EC.classify_error(etype, detail) == expected


def test_telegramlens_actions_point_to_its_card():
    assert EC.action_for("auth", "TelegramLens") == "TelegramLens 카드의 [텔레그램 로그인]을 다시 눌러주세요."
    assert "TelegramLens 카드의 [업데이트]" in EC.action_for("schema", "TelegramLens")
    assert "TelegramLens 카드의 [업데이트]" in EC.action_for("other", "TelegramLens")
