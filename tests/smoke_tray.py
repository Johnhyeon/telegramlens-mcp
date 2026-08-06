"""오프라인 스모크 테스트 — tray.py 의 순수 로직(상태 판정·아이콘 생성) 검증.

pystray.Icon.run() 은 실제 OS 트레이·디스플레이가 필요해 자동화 테스트로 못 돌린다.
대신 GUI 와 무관한 부분 — current_health()(procstate 재사용 확인)과 아이콘 이미지
생성·캐시 — 만 검증한다. 트레이가 실제로 보이는지는 수동으로 `telegramlens-tray` 를
띄워 확인해야 한다.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="tglens_tray_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

from telegram_lens import tray  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def check_current_health_reuses_procstate() -> None:
    print("\n=== current_health(): 새 판정 로직 없이 procstate 그대로 재사용 ===")
    health = tray.current_health()
    _assert(
        health["health"] == "failed" and health["problem_code"] == "DAEMON_NOT_RUNNING",
        f"데몬 미가동 상태에서 DAEMON_NOT_RUNNING, got {health}",
    )


def check_icon_generation_and_cache() -> None:
    print("\n=== 아이콘 생성·캐시 ===")
    for health in ("healthy", "degraded", "failed", "unknown"):
        img = tray._icon_for(health)
        _assert(img.size == (64, 64), f"{health} 아이콘 64x64, got {img.size}")
        _assert(img.mode == "RGBA", f"{health} 아이콘 RGBA(투명 배경)")

    healthy_img = tray._icon_for("healthy")
    _assert(tray._icon_for("healthy") is healthy_img, "같은 상태는 캐시된 동일 객체 재사용")

    colors = {h: tray._COLORS[h] for h in ("healthy", "degraded", "failed")}
    _assert(len(set(colors.values())) == 3, "healthy/degraded/failed 색이 서로 다름")


def check_module_entry_points_importable() -> None:
    print("\n=== 엔트리포인트 함수 존재 ===")
    _assert(callable(tray.main), "tray.main 존재")
    _assert(callable(tray._build_menu), "tray._build_menu 존재")


def main() -> None:
    print(f"임시 홈: {_TMP}")
    check_current_health_reuses_procstate()
    check_icon_generation_and_cache()
    check_module_entry_points_importable()
    print("\nOK - tray.py 순수 로직 정상(실제 트레이 표시는 수동 확인 필요)")


if __name__ == "__main__":
    main()
