from __future__ import annotations

import base64
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telegram_lens import licensing as L

_EPOCH = date(1970, 1, 1)


def _today() -> date:
    """오늘 날짜를 UTC 로 센다.

    제품 코드가 UTC 로 판단하기 때문이다(`licensing._is_expired`). 여기서만
    `_today()`(로컬)를 쓰면 KST 00~09시에 하루가 어긋나 테스트가 깨진다.
    CI(우분투, UTC)는 늘 통과하므로 로컬에서만, 그것도 아침에만 터진다 —
    가장 알아채기 어려운 종류의 실패다.
    """
    return datetime.now(timezone.utc).date()


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
        assert len(L._decode(signer(_today()))) == 78

    def test_expiry_round_trips(self, signer):
        target = _today() + timedelta(days=7)
        assert L.verify_key(signer(target))["expires_on"] == target

    def test_valid_until_the_end_of_the_expiry_day(self, signer):
        """"7일 체험"이 6일 반으로 끝나면 억울하다 — 그날까지는 열려 있어야 한다."""
        assert L._is_expired(L.verify_key(signer(_today()))["expires_on"]) is False

    def test_expired_the_day_after(self, signer):
        yesterday = _today() - timedelta(days=1)
        assert L._is_expired(L.verify_key(signer(yesterday))["expires_on"]) is True


class TestTampering:
    """만료일이 서명 대상에 들어 있으니 날짜만 고쳐 쓰면 서명이 깨져야 한다 —
    이게 안 되면 체험판을 아무나 영구 키로 바꿀 수 있다."""

    def test_extending_the_expiry_breaks_the_signature(self, signer):
        raw = bytearray(L._decode(signer(_today() - timedelta(days=1))))
        raw[10:14] = ((_today() + timedelta(days=365) - _EPOCH).days).to_bytes(4, "big")
        forged = base64.b32encode(bytes(raw)).decode().rstrip("=")
        assert L.verify_key(forged)["valid"] is False

    def test_stripping_the_expiry_breaks_the_signature(self, signer):
        """만료 4바이트를 떼어내 74바이트 영구 키처럼 만들 수 없어야 한다."""
        raw = L._decode(signer(_today() - timedelta(days=1)))
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
        self._use(monkeypatch, signer(_today() - timedelta(days=1)))
        assert L.is_licensed() is False
        assert L.license_block_reason() == "expired"

    def test_live_trial_key_unlocks(self, signer, monkeypatch):
        self._use(monkeypatch, signer(_today() + timedelta(days=3)))
        assert L.is_licensed() is True
        assert L.license_block_reason() is None

    def test_expired_message_is_used(self, signer, monkeypatch):
        self._use(monkeypatch, signer(_today() - timedelta(days=1)))
        assert L.locked_message() == L.EXPIRED_MESSAGE

    def test_expiring_key_is_not_cached(self, signer, monkeypatch):
        """캐시하면 Claude를 켜둔 채로 며칠 지나도 안 잠긴다 — 서버가 오래 산다."""
        self._use(monkeypatch, signer(_today() + timedelta(days=3)))
        assert L.is_licensed() is True
        assert L._licensed_cache is False

    def test_permanent_key_is_still_cached(self, signer, monkeypatch):
        """기간 없는 키까지 매번 검증하면 도구 호출마다 비용이 붙는다."""
        self._use(monkeypatch, signer())
        assert L.is_licensed() is True
        assert L._licensed_cache is True

    def test_expires_on_reports_the_stored_key(self, signer, monkeypatch):
        target = _today() + timedelta(days=5)
        monkeypatch.setattr(L, "stored_key", lambda: signer(target))
        assert L.expires_on() == target

    def test_expires_on_is_none_without_a_key(self, monkeypatch):
        monkeypatch.setattr(L, "stored_key", lambda: None)
        assert L.expires_on() is None


class TestClockRollback:
    """만료 검사는 이 컴퓨터의 날짜를 믿는다 — 날짜만 과거로 돌리면 만료된 키가 다시
    열린다. '지금까지 본 가장 늦은 날짜'를 적어두고 그보다 한참 이르면 막는다."""

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "data_dir", lambda: tmp_path)

    def _shift(self, monkeypatch, days: int) -> None:
        import datetime as _dt

        class Shifted(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return _dt.datetime.now(tz) + _dt.timedelta(days=days)

        monkeypatch.setattr(L, "datetime", Shifted)

    def test_first_check_records_today_and_allows(self, tmp_path):
        assert L._clock_turned_back(_today() + timedelta(days=5)) is False
        assert (tmp_path / "clock_seen").read_text(encoding="utf-8") == _today().isoformat()

    def test_small_backward_drift_is_tolerated(self, monkeypatch):
        """NTP 보정·시간대 착오로 하루쯤 뒤로 가는 건 정상이다 — 여기서 잠그면
        멀쩡한 사용자가 갇힌다."""
        expiry = _today() + timedelta(days=5)
        L._clock_turned_back(expiry)
        self._shift(monkeypatch, -1)
        assert L._clock_turned_back(expiry) is False

    def test_large_rollback_is_caught(self, monkeypatch):
        expiry = _today() + timedelta(days=5)
        L._clock_turned_back(expiry)
        self._shift(monkeypatch, -5)
        assert L._clock_turned_back(expiry) is True

    def test_permanent_key_is_never_checked(self, monkeypatch):
        """기존 구매자는 이 경로를 아예 안 탄다 — 시계가 어떻든 영향이 없어야 한다."""
        self._shift(monkeypatch, -400)
        assert L._clock_turned_back(None) is False

    def test_baseline_only_moves_forward(self, tmp_path, monkeypatch):
        """뒤로도 기록하면 되돌린 사람이 기준선을 낮춰 계속 미룰 수 있다."""
        expiry = _today() + timedelta(days=5)
        L._clock_turned_back(expiry)
        self._shift(monkeypatch, -1)
        L._clock_turned_back(expiry)
        assert (tmp_path / "clock_seen").read_text(encoding="utf-8") == _today().isoformat()

    def test_unwritable_state_never_locks(self, monkeypatch):
        """상태 파일 하나 때문에 돈 낸 사람이 잠기는 쪽이 훨씬 나쁘다."""
        monkeypatch.setattr(L, "data_dir", lambda: Path("/존재하지-않는-경로/xyz"))
        self._shift(monkeypatch, -400)
        assert L._clock_turned_back(_today() + timedelta(days=5)) is False

    def test_gate_reports_clock_reason(self, signer, monkeypatch):
        """만료와 다른 사유여야 한다 — 할 일이 '구매'가 아니라 '시계 맞추기'다."""
        key = signer(_today() + timedelta(days=5))
        monkeypatch.setattr(L, "stored_key", lambda: key)
        monkeypatch.setattr(L, "is_revoked", lambda _lid: False)
        monkeypatch.setattr(L, "_licensed_cache", False)
        L._clock_turned_back(_today() + timedelta(days=5))
        self._shift(monkeypatch, -5)
        assert L.is_licensed() is False
        assert L.license_block_reason() == "clock"
        assert L.locked_message() == L.CLOCK_MESSAGE


class TestSaveKeyGate:
    """저장 전에 '지금 쓸 수 있는 키인지'까지 본다. 예전엔 서명만 맞으면 저장하고
    캐시를 켜버려서, 이미 끝난 체험 키를 다시 붙여넣으면 그 프로세스가 사는 동안
    전부 열렸다 — 껐다 켜고 옛 키를 다시 넣으면 되는 우회였다."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "is_revoked", lambda _lid: False)
        monkeypatch.setattr(L, "_licensed_cache", False)

    def test_expired_key_is_refused(self, signer):
        res = L.save_key(signer(_today() - timedelta(days=1)))
        assert res["valid"] is False
        assert "기간" in res["reason"]

    def test_expired_key_does_not_turn_on_the_cache(self, signer):
        L.save_key(signer(_today() - timedelta(days=1)))
        assert L._licensed_cache is False
        assert L.is_licensed() is False

    def test_revoked_key_is_refused(self, signer, monkeypatch):
        monkeypatch.setattr(L, "is_revoked", lambda _lid: True)
        res = L.save_key(signer())
        assert res["valid"] is False
        assert "중지" in res["reason"]

    def test_live_trial_key_is_saved(self, signer):
        res = L.save_key(signer(_today() + timedelta(days=5)))
        assert res["valid"] is True
        assert L.is_licensed() is True

    def test_permanent_key_is_saved(self, signer):
        assert L.save_key(signer())["valid"] is True
        assert L.is_licensed() is True

    def test_cache_is_not_forced_on(self, signer):
        """캐시는 켜지 않고 비운다 — 다음 is_licensed 가 만료·폐기까지 보고 정한다."""
        L.save_key(signer(_today() + timedelta(days=5)))
        assert L._licensed_cache is False
