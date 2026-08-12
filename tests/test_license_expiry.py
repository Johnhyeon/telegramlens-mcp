from __future__ import annotations

import base64
import secrets
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telegram_lens import licensing as L

_EPOCH = date(1970, 1, 1)


@pytest.fixture
def signer(monkeypatch):
    """운영 공개키 대신 임시 키쌍을 쓴다 — 실제 판매 키를 테스트에 끌어들이지 않는다."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        L, "_PUBLIC_KEY_B64", base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    )

    def mint(expires_on: date | None = None, product: bytes = L.PRODUCT) -> str:
        payload = product + secrets.token_bytes(6)
        if expires_on is not None:
            payload += ((expires_on - _EPOCH).days).to_bytes(4, "big")
        raw = payload + priv.sign(payload)
        return base64.b32encode(raw).decode().rstrip("=")

    return mint


class TestBackwardCompatibility:
    """이미 판 키(74바이트, 기간 없음)가 계속 유효해야 한다 — 여기가 깨지면 기존
    구매자 전원이 잠긴다."""

    def test_legacy_key_stays_valid(self, signer):
        res = L.verify_key(signer())
        assert res["valid"] is True
        assert res["expires_on"] is None

    def test_legacy_key_never_expires(self, signer):
        assert L._is_expired(L.verify_key(signer())["expires_on"]) is False

    def test_legacy_key_is_74_bytes(self, signer):
        assert len(L._decode(signer())) == 74


class TestExpiringKey:
    def test_key_with_expiry_is_78_bytes(self, signer):
        assert len(L._decode(signer(date.today()))) == 78

    def test_expiry_round_trips(self, signer):
        target = date.today() + timedelta(days=7)
        assert L.verify_key(signer(target))["expires_on"] == target

    def test_valid_until_the_end_of_the_expiry_day(self, signer):
        """"7일 체험"이 6일 반으로 끝나면 억울하다 — 그날까지는 열려 있어야 한다."""
        assert L._is_expired(L.verify_key(signer(date.today()))["expires_on"]) is False

    def test_expired_the_day_after(self, signer):
        yesterday = date.today() - timedelta(days=1)
        assert L._is_expired(L.verify_key(signer(yesterday))["expires_on"]) is True


class TestTampering:
    """만료일이 서명 대상에 들어 있으니 날짜만 고쳐 쓰면 서명이 깨져야 한다 —
    이게 안 되면 체험판을 아무나 영구 키로 바꿀 수 있다."""

    def test_extending_the_expiry_breaks_the_signature(self, signer):
        raw = bytearray(L._decode(signer(date.today() - timedelta(days=1))))
        raw[10:14] = ((date.today() + timedelta(days=365) - _EPOCH).days).to_bytes(4, "big")
        forged = base64.b32encode(bytes(raw)).decode().rstrip("=")
        assert L.verify_key(forged)["valid"] is False

    def test_stripping_the_expiry_breaks_the_signature(self, signer):
        """만료 4바이트를 떼어내 74바이트 영구 키처럼 만들 수 없어야 한다."""
        raw = L._decode(signer(date.today() - timedelta(days=1)))
        stripped = raw[:10] + raw[14:]
        forged = base64.b32encode(stripped).decode().rstrip("=")
        assert L.verify_key(forged)["valid"] is False

    def test_other_lengths_are_rejected(self, signer):
        raw = L._decode(signer())
        for bad in (raw[:-1], raw + b"\x00"):
            forged = base64.b32encode(bad).decode().rstrip("=")
            assert L.verify_key(forged)["valid"] is False


class TestGate:
    """잠금 판정과 안내 문구 — 기간이 끝난 사람에게 할 일은 재입력도 연락도 아니고
    구매다. 문구가 갈려야 그 사람이 헛수고를 안 한다."""

    def _use(self, monkeypatch, key: str) -> None:
        monkeypatch.setattr(L, "stored_key", lambda: key)
        monkeypatch.setattr(L, "is_revoked", lambda _lid: False)
        monkeypatch.setattr(L, "_licensed_cache", False)

    def test_expired_key_locks(self, signer, monkeypatch):
        self._use(monkeypatch, signer(date.today() - timedelta(days=1)))
        assert L.is_licensed() is False
        assert L.license_block_reason() == "expired"

    def test_live_trial_key_unlocks(self, signer, monkeypatch):
        self._use(monkeypatch, signer(date.today() + timedelta(days=3)))
        assert L.is_licensed() is True
        assert L.license_block_reason() is None

    def test_expired_message_is_used(self, signer, monkeypatch):
        self._use(monkeypatch, signer(date.today() - timedelta(days=1)))
        assert L.locked_message() == L.EXPIRED_MESSAGE

    def test_expiring_key_is_not_cached(self, signer, monkeypatch):
        """캐시하면 Claude를 켜둔 채로 며칠 지나도 안 잠긴다 — 서버가 오래 산다."""
        self._use(monkeypatch, signer(date.today() + timedelta(days=3)))
        assert L.is_licensed() is True
        assert L._licensed_cache is False

    def test_permanent_key_is_still_cached(self, signer, monkeypatch):
        """기간 없는 키까지 매번 검증하면 도구 호출마다 비용이 붙는다."""
        self._use(monkeypatch, signer())
        assert L.is_licensed() is True
        assert L._licensed_cache is True

    def test_expires_on_reports_the_stored_key(self, signer, monkeypatch):
        target = date.today() + timedelta(days=5)
        monkeypatch.setattr(L, "stored_key", lambda: signer(target))
        assert L.expires_on() == target

    def test_expires_on_is_none_without_a_key(self, monkeypatch):
        monkeypatch.setattr(L, "stored_key", lambda: None)
        assert L.expires_on() is None
