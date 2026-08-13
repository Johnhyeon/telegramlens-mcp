from __future__ import annotations

import base64
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from telegram_lens import daemon as D
from telegram_lens import licensing as L

_EPOCH = date(1970, 1, 1)


def _today() -> date:
    """오늘 날짜를 UTC 로 센다 — 제품 코드가 UTC 로 판단하기 때문이다.
    로컬 날짜를 쓰면 KST 00~09시에만 하루가 어긋나 아침에만 깨진다."""
    return datetime.now(timezone.utc).date()


@pytest.fixture
def key(tmp_path, monkeypatch):
    """임시 키쌍 + 임시 홈. 원하는 만료일의 키를 꽂아주는 함수를 돌려준다."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        L, "_PUBLIC_KEY_B64", base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    )
    monkeypatch.setattr(L, "data_dir", lambda: tmp_path)

    def use(days_left: int | None) -> None:
        payload = L.PRODUCT + secrets.token_bytes(6)
        if days_left is not None:
            expiry = _today() + timedelta(days=days_left)
            payload += ((expiry - _EPOCH).days).to_bytes(4, "big")
        raw = payload + priv.sign(payload)
        monkeypatch.setattr(L, "stored_key", lambda: base64.b32encode(raw).decode().rstrip("="))
        monkeypatch.setattr(L, "_licensed_cache", False)

    return use


class TestLicensedCheck:
    """체험이 끝났는데 수집만 계속 돌면, 쓸 수도 없는 데이터를 위해 남의 PC 자원과
    텔레그램 API를 계속 쓰는 셈이다."""

    def test_paid_key_keeps_collecting(self, key):
        key(None)
        assert D._licensed() is True

    def test_live_trial_keeps_collecting(self, key):
        key(3)
        assert D._licensed() is True

    def test_expired_trial_stops(self, key):
        key(-1)
        assert D._licensed() is False

    def test_unknown_state_keeps_collecting(self):
        """확인이 안 되는 상황에서 멈추면 돈 낸 사람의 수집이 조용히 죽는다 —
        체험자가 며칠 더 모으는 것보다 훨씬 나쁘다."""
        with patch.dict(sys.modules, {"telegram_lens.licensing": None}):
            assert D._licensed() is True


class TestSpawnGate:
    def test_expired_never_spawns(self, key):
        """데몬 자신도 스스로 서지만, 여기서 안 막으면 감시 루프가 60초마다 되살린다."""
        key(-1)
        with patch.object(D, "is_alive", return_value=False), patch("subprocess.Popen") as pop:
            assert D.spawn_child() is None
        assert not pop.called

    def test_live_key_spawns(self, key):
        key(3)
        with patch.object(D, "is_alive", return_value=False), patch("subprocess.Popen") as pop:
            D.spawn_child()
        assert pop.called
