"""테스트가 진짜 사용자 폴더를 건드리지 않게 막는다.

체험 시작일은 두 곳에 적힌다 — Lens 폴더(`~/.telegramlens`)와 브랜드 공용
폴더(`~/.leetkit`). 앞엣것은 테스트가 `_home` 을 tmp_path 로 돌려놓으면 피해가지만,
뒤엣것은 `Path.home()` 에서 바로 계산되므로 그것만으로는 못 막는다.

실제로 그렇게 당했다 — 라이선스 테스트를 돌렸더니 개발자 홈의 `trial_started.json`
안에 가짜 license_id 수십 개가 쌓였다. 파일 단위 fixture 로는 새 테스트 파일이 생길
때마다 같은 실수가 반복되므로, 여기서 전체에 건다.
"""

from __future__ import annotations

import os
import tempfile

import pytest

# 수집(import) 시점에 data_dir() 를 부르는 테스트 모듈이 있다 - fixture 는
# 수집 이후에나 돌므로, 홈 격리는 conftest 모듈 레벨에서 가장 먼저 건다.
# 바깥에서 어떤 TELEGRAMLENS_HOME 이 걸려 있어도(오염 포함) 여기서 덮는다.
_SUITE_HOME = tempfile.mkdtemp(prefix="tl_suite_home_")
os.environ["TELEGRAMLENS_HOME"] = _SUITE_HOME


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch):
    """모든 테스트를 스위트 전용 임시 TELEGRAMLENS_HOME 에서 돌린다.

    실측(UAT 검수): 상태 테스트가 실제 사용자 홈의 DB 를 열어, 임시 HOME 을
    지정한 실행에서는 전부 통과하는데 기본 `pytest -q` 에서는 환경에 따라
    깨졌다. 격리는 실행자가 환경변수로 챙길 일이 아니라 테스트 스위트가
    스스로 보장할 일이다. 경로는 위 모듈 레벨에서 이미 고정됐고, 여기서는
    개별 테스트가 setenv 로 바꿔도 다음 테스트로 새지 않게 되돌린다.
    """
    monkeypatch.setenv("TELEGRAMLENS_HOME", _SUITE_HOME)
    yield


@pytest.fixture(autouse=True)
def _isolate_license_state(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("license_state")
    try:
        from telegram_lens import licensing
    except Exception:  # 라이선스 모듈을 안 쓰는 테스트도 있다
        return
    monkeypatch.setattr(
        licensing,
        "_trial_mark_paths",
        lambda: [root / "lens" / "trial_started.json", root / "shared" / "trial_started.json"],
        raising=False,
    )
