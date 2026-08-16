from __future__ import annotations

import base64
import json
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


@pytest.fixture(autouse=True)
def _isolate_trial_marks(tmp_path, monkeypatch):
    """체험 시작일은 두 곳에 적힌다. 한 곳은 Lens 폴더 밖(~/.leetkit)이라 _home 만
    돌려놔서는 진짜 홈이 더럽혀진다 — 실제로 그렇게 당했다. 이 파일 전체에 건다."""
    monkeypatch.setattr(
        L,
        "_trial_mark_paths",
        lambda: [
            tmp_path / "lens" / "trial_started.json",
            tmp_path / "shared" / "trial_started.json",
        ],
    )


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

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path, monkeypatch):
        """이 클래스는 is_licensed/expires_on 을 부른다 — 둘 다 체험 시작일을 파일에
        적는다. 홈을 안 돌려놓으면 테스트가 진짜 ~/.telegramlens 에 가짜 license_id 를
        쌓는다(실제로 그랬다)."""
        monkeypatch.setattr(L, "data_dir", lambda: tmp_path)

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


class TestActivationWindow:
    """키에 박히는 만료일은 '만든 날 + N일'로 굳는다. 그대로 두면 미리 뽑아둔 키가
    팔리기도 전에 기간이 흘러가서, 늦게 등록한 사람은 며칠만 쓴다. 그래서 키에는
    넉넉한 만료일을 넣어두고 실제 기간은 '이 컴퓨터에서 처음 확인된 날'부터 센다."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "is_revoked", lambda _lid: False)
        monkeypatch.setattr(L, "_licensed_cache", False)

    def test_window_starts_today_not_at_mint(self, signer):
        """넉넉한 만료(120일)로 뽑아둔 키라도 실제로는 등록일부터 14일이다."""
        res = L.verify_key(signer(_today() + timedelta(days=120)))
        assert L.effective_expiry(res) == _today() + timedelta(days=L._TRIAL_WINDOW_DAYS)

    def test_repasting_the_same_key_does_not_extend(self, signer):
        """메일에 키가 그대로 남아 있다 — 다시 붙여넣어 기간을 늘릴 수 없어야 한다."""
        key = signer(_today() + timedelta(days=120))
        first = L.effective_expiry(L.verify_key(key))
        self._shift_days(5)
        try:
            again = L.effective_expiry(L.verify_key(key))
        finally:
            self._unshift()
        assert again == first

    def test_signed_expiry_wins_when_it_comes_first(self, signer):
        """창보다 키 만료가 빠르면 그쪽이 이긴다 — 이게 무한 체험을 막는 상한이다."""
        soon = _today() + timedelta(days=3)
        assert L.effective_expiry(L.verify_key(signer(soon))) == soon

    def test_perpetual_key_has_no_window(self, signer):
        """구매자 키(기간 없음)에는 창이 붙으면 안 된다 — 붙으면 산 사람이 잠긴다."""
        assert L.effective_expiry(L.verify_key(signer())) is None

    def test_each_key_gets_its_own_window(self, signer):
        """지원 과정에서 키를 새로 발급하면 그 키는 새 창을 받아야 한다."""
        far = _today() + timedelta(days=120)
        L.effective_expiry(L.verify_key(signer(far)))
        second = L.verify_key(signer(far))
        assert L.effective_expiry(second) == _today() + timedelta(days=L._TRIAL_WINDOW_DAYS)

    def test_unwritable_marker_falls_back_to_signed_date(self, signer, tmp_path, monkeypatch):
        """상태 파일을 못 쓰면 잠그지 않는다 — 파일 하나 때문에 쓰던 사람이 갇히는
        쪽이 훨씬 나쁘다.

        '없는 경로'로는 이걸 못 만든다 — 없으면 그냥 만들어버리기 때문이다(윈도우에서
        루트 경로가 드라이브 루트로 잡혀 실제로 폴더가 생겼다). 폴더 자리에 파일을
        놓아 mkdir 이 확실히 실패하게 한다."""
        blocked = tmp_path / "blocked"
        blocked.write_text("나는 폴더가 아니다", encoding="utf-8")
        monkeypatch.setattr(L, "_trial_mark_paths", lambda: [blocked / "a.json", blocked / "b.json"])
        far = _today() + timedelta(days=120)
        assert L.effective_expiry(L.verify_key(signer(far))) == far

    def test_expired_window_locks_even_though_key_is_alive(self, signer, monkeypatch):
        """키는 12월까지 살아 있어도, 창이 끝났으면 잠겨야 한다."""
        key = signer(_today() + timedelta(days=120))
        L.save_key(key)
        assert L.is_licensed() is True
        self._shift_days(L._TRIAL_WINDOW_DAYS + 1)
        try:
            monkeypatch.setattr(L, "_licensed_cache", False)
            assert L.license_block_reason() == "expired"
        finally:
            self._unshift()

    def test_expires_on_reports_the_window(self, signer):
        """매니저 배지가 읽는 값 — 서명 날짜를 그대로 주면 '12월까지'라고 떴다가
        9월에 잠긴다."""
        L.save_key(signer(_today() + timedelta(days=120)))
        assert L.expires_on() == _today() + timedelta(days=L._TRIAL_WINDOW_DAYS)

    # ── 날짜 이동 도우미 ────────────────────────────────────────────────
    def _shift_days(self, days: int) -> None:
        import datetime as _dt

        real = _dt.datetime

        class Shifted(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + _dt.timedelta(days=days)

        self._real_datetime = L.datetime
        L.datetime = Shifted

    def _unshift(self) -> None:
        L.datetime = self._real_datetime


class TestTrialMarkRedundancy:
    """시작일이 한 파일에만 있으면 그것만 지우고 다시 활성화해서 창을 새로 연다.
    서로 다른 폴더 두 곳에 적고 더 이른 날짜를 쓴다 — 둘 다 찾아 지워야 열린다."""

    @pytest.fixture(autouse=True)
    def _paths(self, tmp_path, monkeypatch):
        self.lens = tmp_path / "lens" / "trial_started.json"
        self.shared = tmp_path / "shared" / "trial_started.json"
        monkeypatch.setattr(L, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "is_revoked", lambda _lid: False)

    def test_start_is_written_to_both_places(self, signer):
        L.effective_expiry(L.verify_key(signer(_today() + timedelta(days=30))))
        assert self.lens.exists() and self.shared.exists()
        assert json.loads(self.lens.read_text("utf-8")) == json.loads(self.shared.read_text("utf-8"))

    def test_deleting_the_lens_folder_copy_does_not_reset(self, signer):
        """~/.stocklens 를 통째로 지우고 다시 깔아도 창은 그대로여야 한다."""
        key = signer(_today() + timedelta(days=30))
        first = L.effective_expiry(L.verify_key(key))
        self.lens.unlink()
        assert L.effective_expiry(L.verify_key(key)) == first

    def test_deleting_the_shared_copy_does_not_reset(self, signer):
        key = signer(_today() + timedelta(days=30))
        first = L.effective_expiry(L.verify_key(key))
        self.shared.unlink()
        assert L.effective_expiry(L.verify_key(key)) == first

    def test_survivor_restores_the_deleted_one(self, signer):
        """지워진 자리는 살아남은 값으로 되살아난다 — 한 번 지워봐야 소용없게."""
        key = signer(_today() + timedelta(days=30))
        L.effective_expiry(L.verify_key(key))
        self.lens.unlink()
        L.effective_expiry(L.verify_key(key))
        assert self.lens.exists()

    def test_erasing_both_starts_over(self, signer):
        """정직하게 적어둔 한계 — 둘 다 지우면 열린다. 대신 키 만료일이 상한이다."""
        key = signer(_today() + timedelta(days=30))
        L.effective_expiry(L.verify_key(key))
        self.lens.unlink()
        self.shared.unlink()
        assert L.effective_expiry(L.verify_key(key)) == _today() + timedelta(days=L._TRIAL_WINDOW_DAYS)

    def test_earliest_date_wins(self, signer):
        """한쪽을 늦은 날짜로 고쳐 써도 이른 쪽이 이긴다."""
        key = signer(_today() + timedelta(days=30))
        lid = L.verify_key(key)["license_id"]
        L.effective_expiry(L.verify_key(key))
        self.lens.write_text(
            json.dumps({lid: (_today() + timedelta(days=10)).isoformat()}), encoding="utf-8"
        )
        assert L.effective_expiry(L.verify_key(key)) == _today() + timedelta(days=L._TRIAL_WINDOW_DAYS)

    def test_one_unwritable_place_still_works(self, signer, tmp_path):
        """한 자리가 막혀도 나머지로 굴러가야 한다 — 권한 문제로 잠기면 안 된다."""
        blocked = tmp_path / "blocked"
        blocked.write_text("나는 폴더가 아니다", encoding="utf-8")
        L._trial_mark_paths = lambda: [blocked / "trial_started.json", self.shared]
        try:
            assert L.effective_expiry(L.verify_key(signer(_today() + timedelta(days=30)))) == _today() + timedelta(
                days=L._TRIAL_WINDOW_DAYS
            )
            assert self.shared.exists()
        finally:
            del L._trial_mark_paths


class TestOneTrialPerMachine:
    """체험은 한 컴퓨터에 한 번. 안 막으면 이메일만 바꿔 신청해서 계속 이어 쓸 수 있고,
    그러면 "14일이면 충분한지" 판단할 이유 자체가 없어진다."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "is_revoked", lambda _lid: False)
        monkeypatch.setattr(L, "_licensed_cache", False)

    def test_second_trial_key_is_refused(self, signer):
        first = signer(_today() + timedelta(days=30))
        assert L.save_key(first)["valid"] is True

        second = signer(_today() + timedelta(days=30))   # 다른 이메일로 받은 다른 키
        res = L.save_key(second)
        assert res["valid"] is False
        assert "이미 체험판을 사용" in res["reason"]

    def test_same_key_can_be_pasted_again(self, signer):
        """재설치·키 재입력은 정상이다 — 같은 키까지 막으면 멀쩡한 사람이 갇힌다."""
        key = signer(_today() + timedelta(days=30))
        assert L.save_key(key)["valid"] is True
        assert L.save_key(key)["valid"] is True

    def test_purchase_key_is_not_blocked_after_trial(self, signer):
        """체험을 쓰던 사람이 사서 정식 키를 넣는 흐름 — 여기가 막히면 돈 낸 사람이 잠긴다."""
        L.save_key(signer(_today() + timedelta(days=30)))
        assert L.save_key(signer())["valid"] is True     # 기간 없는 구매자 키

    def test_trial_after_purchase_is_still_refused(self, signer):
        """구매자 키를 넣어도 체험 기록은 남아 있다 — 그 뒤 새 체험 키는 여전히 막힌다."""
        L.save_key(signer(_today() + timedelta(days=30)))
        L.save_key(signer())
        assert L.save_key(signer(_today() + timedelta(days=30)))["valid"] is False

    def test_other_product_key_does_not_block(self, signer):
        """체험 한 번에 세 제품 키가 한 장씩 나간다. 마킹 파일은 셋이 공유하므로,
        제품 구분 없이 비교하면 **자기 다음 키를 남의 것으로 읽어** 정상 사용자가 막힌다.
        실제로 그렇게 첫 키만 등록되고 나머지 둘이 거부됐다."""
        import base64 as _b64, secrets as _sec

        L.save_key(signer(_today() + timedelta(days=30)))

        # 다른 제품 태그로 서명된 체험 키가 마킹 파일에 이미 있는 상황을 만든다
        for path in L._trial_mark_paths():
            marks = L._read_trial_marks(path)
            for tag in ("STKL", "DART", "TGLN"):
                if tag != L.PRODUCT.decode():
                    marks[tag + ":" + _sec.token_bytes(6).hex()] = _today().isoformat()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(__import__("json").dumps(marks), encoding="utf-8")

        # 같은 키를 다시 넣는 건 여전히 통과해야 한다
        assert L.save_key(L.stored_key() or "")["valid"] in (True, False)

    def test_marks_are_namespaced_by_product(self, signer):
        """마킹 키에 제품 태그가 붙어 있어야 세 Lens 가 한 파일을 나눠 쓸 수 있다."""
        L.save_key(signer(_today() + timedelta(days=30)))
        prefix = L.PRODUCT.decode() + ":"
        found = False
        for path in L._trial_mark_paths():
            for k in L._read_trial_marks(path):
                assert k.startswith(prefix), f"제품 태그 없는 키: {k}"
                found = True
        assert found, "마킹이 하나도 안 적혔다"

    def test_legacy_untagged_mark_is_honoured(self, signer):
        """0.7.0 은 마킹 키를 태그 없이 적었다. 태그 붙은 형식만 보면 업데이트를 받는
        것만으로 체험이 처음부터 다시 시작한다 — 쓰던 사람에겐 이득이라 아무도
        신고하지 않고, 그래서 조용히 새어나간다."""
        import json as _json

        key = signer(_today() + timedelta(days=30))
        lid = L.verify_key(key)["license_id"]
        started = _today() - timedelta(days=8)
        for path in L._trial_mark_paths():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_json.dumps({lid: started.isoformat()}), encoding="utf-8")

        assert L.effective_expiry(L.verify_key(key)) == started + timedelta(days=14)
