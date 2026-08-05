"""프로세스 락 · 상태 파일 무결성 — OS advisory lock 기반 데몬 뮤텍스 + 원자적 JSON 쓰기.

config.py(경로·자격증명)와는 관심사가 다르다: 여기는 "지금 데몬이 실제로 살아있는가"와
"상태 파일이 읽는 도중 깨지지 않는가"만 다룬다.

DaemonLock 설계 근거(2026-06/07 두 장애 재발 방지):
  - 2026-06: 낡은 PID 파일만 보고 "그 번호가 응답하니 살아있다"고 오판(Windows PID 재사용) →
    데몬이 3일간 재기동 못함.
  - 2026-07: 좀비 데몬이 SQLite 쓰기 락을 영구히 쥔 채 하트비트만 멎어, 새 데몬도 못 뜸.
  둘 다 "PID 숫자가 곧 신원"이라는 가정이 원인이었다. 이 모듈은 그 가정을 아예 없앤다 —
  daemon.pid를 프로세스 생애주기 내내 열어 OS advisory lock(POSIX: fcntl.flock, Windows:
  msvcrt.locking)으로 잠그면, 락은 파일 fd에 걸리므로 OS가 프로세스 종료(SIGKILL 포함)와
  동시에 자동 해제한다. PID 재사용으로 락을 오판할 여지가 구조적으로 없다. 락 경합(=누군가
  실제로 살아서 쥐고 있음)이 감지되고 하트비트도 멎었으면, 그 순간 파일에 적힌 PID는 신원
  확인 없이도 "지금 이 락을 실제로 쥔 프로세스"임이 보장되므로 안전하게 종료할 수 있다.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger("telegramlens.procstate")

_LOCK_SCHEMA_VERSION = 2


def atomic_write_json(path: Path, data: dict) -> None:
    """tmp 파일에 쓰고 flush+fsync 후 os.replace로 원자적 치환.

    읽는 쪽(telegram_status 등)이 쓰는 도중의 파일을 열어 부분 JSON을 보고
    JSONDecodeError를 내는 상황을 없앤다.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def heartbeat_age_sec(status: dict | None, field: str = "heartbeat_at") -> float | None:
    """status 딕셔너리의 ISO 타임스탬프 필드로부터 경과 초. 없거나 파싱 실패면 None(판단 불가)."""
    if not status:
        return None
    ts = status.get(field)
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def compute_health(status: dict | None, lock_held: bool, interval_min: int | None = None) -> dict:
    """daemon_status.json(raw facts) + 락 보유 여부로부터 health/problem_code/message 판정.

    daemon.py 는 사실만 기록하고(스스로 멎었는지는 알 수 없으므로), 판정은 이 함수 한 곳에서만
    한다 — telegram_status(server.py)와 doctor.py 가 같은 로직을 써서 서로 어긋나지 않게.

    반환: {"health": "healthy"|"degraded"|"failed", "problem_code": str|None, "message": str}
    """
    if not lock_held:
        return {
            "health": "failed",
            "problem_code": "DAEMON_NOT_RUNNING",
            "message": "수집 데몬이 실행되고 있지 않습니다.",
        }
    if status is None:
        return {
            "health": "degraded",
            "problem_code": "STATUS_FILE_CORRUPTED",
            "message": "데몬은 살아있지만 상태 파일을 읽을 수 없습니다.",
        }

    hb_age = heartbeat_age_sec(status, "heartbeat_at")
    if hb_age is not None and hb_age > 180:
        return {
            "health": "failed",
            "problem_code": "DAEMON_STALLED",
            "message": f"데몬 하트비트가 {int(hb_age)}초째 멎어 있습니다.",
        }

    consecutive = status.get("consecutive_failures") or 0
    interval = interval_min or status.get("interval_minutes") or 10
    lag_threshold_sec = interval * 2 * 60 + 120

    lag_sec = None
    last_success_at = status.get("last_success_at")
    if last_success_at:
        try:
            dt = datetime.fromisoformat(last_success_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            lag_sec = (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            lag_sec = None

    backfill = status.get("backfill") or {}
    backfill_stalled = False
    if backfill.get("state") == "running":
        prog_age = heartbeat_age_sec(backfill, "last_progress_at")
        if prog_age is not None and prog_age > 300:
            backfill_stalled = True

    if consecutive >= 3:
        return {
            "health": "failed",
            "problem_code": "SYNC_TIMEOUT",
            "message": f"연속 {consecutive}회 수집에 실패했습니다.",
        }
    if lag_sec is not None and lag_sec > lag_threshold_sec:
        return {
            "health": "failed",
            "problem_code": "COLLECTION_LAGGING",
            "message": f"마지막 성공 수집 이후 {int(lag_sec / 60)}분이 지났습니다.",
        }
    if backfill_stalled:
        return {
            "health": "failed",
            "problem_code": "BACKFILL_STALLED",
            "message": "백필 진행이 5분 이상 멈춰 있습니다.",
        }

    if 1 <= consecutive <= 2:
        return {
            "health": "degraded",
            "problem_code": "SYNC_TIMEOUT",
            "message": f"최근 {consecutive}회 수집 실패(재시도 중).",
        }
    channels = status.get("channels") or {}
    if channels.get("failed"):
        return {
            "health": "degraded",
            "problem_code": "CHANNEL_TIMEOUT",
            "message": f"이번 사이클 채널 {channels['failed']}개 수집 실패.",
        }
    if backfill.get("state") == "running":
        return {
            "health": "degraded",
            "problem_code": None,
            "message": "과거 데이터 백필 진행 중.",
        }

    return {"health": "healthy", "problem_code": None, "message": "정상 가동 중."}


def _kill(pid: int) -> None:
    """락을 실제로 쥔(=지금 이 순간 신원이 보장된) 좀비 프로세스를 강제 종료.

    Windows os.kill 은 시그널 값과 무관하게 TerminateProcess 를 호출한다(강제 종료) — 존재
    확인 목적으로 os.kill(pid, 0) 을 쓰면 안 된다(그 자체가 프로세스를 죽여버림). 여기서는
    의도적으로 죽이는 것이 목적이라 문제 없다.
    """
    try:
        if sys.platform == "win32":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class DaemonLock:
    """daemon.pid(내용) + daemon.pid.lock(순수 OS 락)을 분리 보관.

    Windows 의 msvcrt.locking 은 POSIX flock 과 달리 잠긴 byte-range 에 대한 '읽기'까지
    다른 프로세스에 거부한다(mandatory locking) — 그래서 내용과 락 대상을 같은 파일에
    두면, 락을 쥐지 않은 프로세스가 신원 확인을 위해 PID 를 읽으려는 시도 자체가 Windows
    에서 실패한다. 락(.lock, 내용 없음)과 내용(.pid, 언제나 자유롭게 읽힘)을 분리해
    이 문제를 피한다.
    """

    def __init__(self, path: Path):
        self.path = path  # 사람이 읽는 JSON 내용(pid 등) — 누구나 자유롭게 읽을 수 있음
        self._lock_path = path.with_name(path.name + ".lock")  # 실제 OS 락 대상
        self._fh = None  # 열어둔 락 파일 핸들 — 보유 중임을 나타냄. None 이면 미보유.

    def _try_lock(self) -> bool:
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fh = os.fdopen(fd, "r+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"\x00")  # byte-range 락이 잠글 대상 바이트 확보
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def _write_self(self) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": _LOCK_SCHEMA_VERSION,
                "pid": os.getpid(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _read_holder_pid(self) -> int | None:
        data = read_json(self.path)
        if not data:
            return None
        try:
            return int(data.get("pid"))
        except (ValueError, TypeError):
            return None

    def acquire(
        self,
        stale_after_sec: float = 180,
        status_path: Path | None = None,
        kill_wait_sec: float = 5,
    ) -> bool:
        """락 획득 시도.

        이미 정상 가동 중인 보유자가 있으면 False. 보유자는 있는데 status_path 의 하트비트가
        stale_after_sec 넘게 멎었으면(=좀비) 그 보유자를 종료하고 락 회수를 재시도한다.
        status_path 를 안 주거나 하트비트 나이를 못 구하면(판단 불가) 절대 죽이지 않는다 —
        애매하면 안 죽이는 쪽이 2026-06 오탐 재발보다 낫다.
        """
        if self._try_lock():
            self._write_self()
            return True
        if status_path is None:
            return False

        age = heartbeat_age_sec(read_json(status_path))
        if age is None or age <= stale_after_sec:
            return False

        holder = self._read_holder_pid()
        if not holder:
            return False
        _LOG.warning("좀비 데몬 감지(하트비트 %.0f초 정지) — 종료 시도 (pid=%s)", age, holder)
        _kill(holder)

        deadline = time.monotonic() + kill_wait_sec
        while time.monotonic() < deadline:
            if self._try_lock():
                self._write_self()
                _LOG.warning("좀비 데몬 정리 후 락 회수 완료 (pid=%s)", holder)
                return True
            time.sleep(0.2)
        _LOG.error("좀비 데몬(pid=%s) 종료 후에도 락 회수 실패", holder)
        return False

    def is_held(self) -> bool:
        """논블로킹 프로브. 획득에 성공하면 즉시 반납하고 False(=아무도 안 쥐고 있었음)."""
        if self._try_lock():
            self.release()
            return False
        return True

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            fh.close()
        except OSError:
            pass
        try:
            self._lock_path.unlink()
        except OSError:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass
