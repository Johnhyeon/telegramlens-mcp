"""테스트가 진짜 사용자 폴더를 건드리지 않게 막는다.

체험 시작일은 두 곳에 적힌다 — Lens 폴더(`~/.telegramlens`)와 브랜드 공용
폴더(`~/.leetkit`). 앞엣것은 테스트가 `_home` 을 tmp_path 로 돌려놓으면 피해가지만,
뒤엣것은 `Path.home()` 에서 바로 계산되므로 그것만으로는 못 막는다.

실제로 그렇게 당했다 — 라이선스 테스트를 돌렸더니 개발자 홈의 `trial_started.json`
안에 가짜 license_id 수십 개가 쌓였다. 파일 단위 fixture 로는 새 테스트 파일이 생길
때마다 같은 실수가 반복되므로, 여기서 전체에 건다.
"""

from __future__ import annotations

import pytest


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
