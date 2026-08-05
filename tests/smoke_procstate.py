"""오프라인 스모크 테스트 — procstate(원자적 쓰기 + DaemonLock) 검증.

실제 서브프로세스를 띄워 락 경합·좀비(하트비트 정지) 회수를 검증한다 — daemon.py 가
실제로 마주치는 프로세스 간 상황을 그대로 재현한다(단일 프로세스 내 흉내가 아님).
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tglens_procstate_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

from telegram_lens import procstate  # noqa: E402
from telegram_lens.config import data_dir  # noqa: E402
import telegram_lens  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def check_atomic_write() -> None:
    print("\n=== atomic_write_json ===")
    p = data_dir() / "atomic_test.json"
    procstate.atomic_write_json(p, {"a": 1, "b": "가나다"})
    _assert(p.exists(), "파일 생성됨")
    tmp = p.with_name(p.name + ".tmp")
    _assert(not tmp.exists(), "tmp 파일이 안 남음(원자적 치환 완료)")
    _assert(procstate.read_json(p) == {"a": 1, "b": "가나다"}, "내용 왕복 일치")

    procstate.atomic_write_json(p, {"c": 2})
    _assert(procstate.read_json(p) == {"c": 2}, "덮어쓰기 후 내용 갱신")

    p.write_text("{broken json", encoding="utf-8")
    _assert(procstate.read_json(p) is None, "손상 JSON은 예외 없이 None 반환")


def check_single_process_lock() -> None:
    print("\n=== DaemonLock: 단일 프로세스 획득/해제 ===")
    lock_p = data_dir() / "single.lock"
    lock = procstate.DaemonLock(lock_p)
    _assert(procstate.DaemonLock(lock_p).is_held() is False, "처음엔 보유자 없음")
    _assert(lock.acquire(), "락 획득 성공")
    _assert(procstate.DaemonLock(lock_p).is_held() is True, "다른 인스턴스가 보유 중으로 인식")
    lock.release()
    _assert(procstate.DaemonLock(lock_p).is_held() is False, "해제 후 보유자 없음")


_WORKER = """
import os, sys, time
sys.path.insert(0, {pkg_root!r})
os.environ["TELEGRAMLENS_HOME"] = {home!r}
from telegram_lens import procstate
from telegram_lens.config import data_dir

lock = procstate.DaemonLock(data_dir() / {lock_name!r})
ok = lock.acquire()
print("ACQUIRED" if ok else "FAILED", flush=True)
if not ok:
    sys.exit(1)

touch_heartbeat = {touch_heartbeat}
status_p = data_dir() / {status_name!r}
deadline = time.time() + 60
while time.time() < deadline:
    if touch_heartbeat:
        import json, datetime
        status_p.write_text(
            json.dumps({{"heartbeat_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}}),
            encoding="utf-8",
        )
    time.sleep(0.3)
"""


def _spawn_worker(lock_name: str, status_name: str, touch_heartbeat: bool) -> subprocess.Popen:
    pkg_root = str(Path(telegram_lens.__file__).parent.parent)
    script = _WORKER.format(
        pkg_root=pkg_root, home=_TMP, lock_name=lock_name,
        status_name=status_name, touch_heartbeat=touch_heartbeat,
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _first_line(proc: subprocess.Popen) -> str:
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read()
        raise AssertionError(f"워커가 아무 출력도 없이 종료됨. stderr:\n{err}")
    return line.strip()


def check_cross_process_contention() -> None:
    print("\n=== DaemonLock: 프로세스 간 락 경합 ===")
    lock_name, status_name = "cross.lock", "cross_status.json"
    worker = _spawn_worker(lock_name, status_name, touch_heartbeat=True)
    try:
        _assert(_first_line(worker) == "ACQUIRED", "워커가 락 획득")
        time.sleep(0.5)  # 하트비트 최소 1회 기록될 시간

        lock_p = data_dir() / lock_name
        _assert(procstate.DaemonLock(lock_p).is_held() is True, "본 프로세스에서도 보유 중으로 보임")

        second = procstate.DaemonLock(lock_p)
        ok = second.acquire(stale_after_sec=180, status_path=data_dir() / status_name)
        _assert(ok is False, "정상 워커가 살아있는 동안 두 번째 획득은 실패")
    finally:
        worker.terminate()
        worker.wait(timeout=5)
    _assert(
        procstate.DaemonLock(data_dir() / lock_name).is_held() is False,
        "워커 종료 후 OS 가 락을 자동 해제(별도 정리 없이도 즉시 재획득 가능)",
    )


def check_zombie_recovery() -> None:
    print("\n=== DaemonLock: 좀비(하트비트 정지) 회수 ===")
    lock_name, status_name = "zombie.lock", "zombie_status.json"
    worker = _spawn_worker(lock_name, status_name, touch_heartbeat=False)  # 하트비트 절대 안 찍음
    try:
        _assert(_first_line(worker) == "ACQUIRED", "워커가 락 획득")

        lock_p = data_dir() / lock_name
        status_p = data_dir() / status_name
        # 좀비 판정을 실시간으로 180초 기다릴 수 없으니, 일부러 '오래 전' 하트비트를 심는다.
        import datetime
        old_ts = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=999)
        ).isoformat()
        procstate.atomic_write_json(status_p, {"heartbeat_at": old_ts})

        recovered = procstate.DaemonLock(lock_p)
        ok = recovered.acquire(stale_after_sec=180, status_path=status_p)
        _assert(ok, "좀비 워커를 종료하고 락을 회수함")
        recovered.release()

        _assert(worker.poll() is not None, "워커 프로세스가 실제로 종료됨(좀비 kill 확인)")
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)


def check_ambiguous_never_kills() -> None:
    print("\n=== DaemonLock: 판단 불가면 절대 안 죽임 ===")
    lock_name = "ambiguous.lock"
    # status_path 없이(=하트비트 나이를 알 수 없음) 락 경합 상황을 만든다.
    worker = _spawn_worker(lock_name, "unused_status.json", touch_heartbeat=False)
    try:
        _assert(_first_line(worker) == "ACQUIRED", "워커가 락 획득")
        lock_p = data_dir() / lock_name
        contender = procstate.DaemonLock(lock_p)
        ok = contender.acquire(stale_after_sec=180, status_path=None)
        _assert(ok is False, "status_path 없음(판단 불가) → 절대 죽이지 않고 실패 반환")
        _assert(worker.poll() is None, "워커는 여전히 살아있음")
    finally:
        worker.terminate()
        worker.wait(timeout=5)


def main() -> None:
    print(f"임시 홈: {_TMP}")
    check_atomic_write()
    check_single_process_lock()
    check_cross_process_contention()
    check_zombie_recovery()
    check_ambiguous_never_kills()
    print("\nOK - procstate(원자적 쓰기 + DaemonLock) 정상")


if __name__ == "__main__":
    main()
