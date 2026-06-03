"""백그라운드 자동 sync 데몬 — `telegramlens-daemon`.

PC가 켜져 있는 동안 주기적으로 sync 를 돌려 DB를 촘촘히 채운다.
삭제되기 전 찌라시를 박제하고, momentum 히스토리를 끊김 없이 쌓는 게 목적.

설계:
  - 수집 창(window) > 주기(interval): 한 사이클이 늦어도 공백 없이 겹쳐 수집.
    중복은 DB의 UNIQUE 제약으로 자동 스킵.
  - 사이클 실패(네트워크/FloodWait)는 로그만 남기고 다음 사이클로 계속.
  - 하트비트(daemon_status.json)를 매 사이클 기록 → telegram_status 가 표시.
  - 락 파일(daemon.pid)로 중복 실행 방지.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
from datetime import datetime, timezone

from telegram_lens import db
from telegram_lens.config import data_dir
from telegram_lens.sync import run_sync

_LOG = logging.getLogger("telegramlens.daemon")
_stop = asyncio.Event()


def _status_path():
    return data_dir() / "daemon_status.json"


def _pid_path():
    return data_dir() / "daemon.pid"


def _setup_logging() -> None:
    log_path = data_dir() / "daemon.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    stream = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    stream.setFormatter(fmt)
    _LOG.setLevel(logging.INFO)
    _LOG.addHandler(handler)
    _LOG.addHandler(stream)


def _write_heartbeat(
    state: str,
    interval_min: int,
    last_result: dict | None,
    catching_up: bool = False,
    window_minutes: int | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    data = {
        "pid": os.getpid(),
        "state": state,  # running | sleeping | error
        "interval_minutes": interval_min,
        # catching_up: 이번 'running' 사이클이 다운타임 후 큰 창을 소급 수집 중인지.
        # 정상 사이클(작은 창)은 False — 조회 도구가 정상 사이클엔 '수집 중' 차단을 안 한다.
        "catching_up": catching_up,
        "window_minutes": window_minutes,
        "last_run": now.isoformat(),
        "last_result": last_result,
    }
    try:
        _status_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _acquire_lock() -> bool:
    """이미 데몬이 돌고 있으면 False. 단순 PID 파일 락."""
    p = _pid_path()
    if p.exists():
        try:
            old_pid = int(p.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            _LOG.error("이미 데몬이 실행 중입니다 (pid=%s). 종료합니다.", old_pid)
            return False
    p.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _release_lock() -> None:
    try:
        _pid_path().unlink()
    except OSError:
        pass


def is_alive() -> bool:
    """데몬이 현재 실행 중인지 — PID 락 파일 기준. 외부(서버)에서 호출."""
    p = _pid_path()
    if not p.exists():
        return False
    try:
        pid = int(p.read_text().strip())
    except (ValueError, OSError):
        return False
    return _pid_alive(pid)


def spawn_child(interval: int = 10):
    """수집 데몬을 '평범한 자식 프로세스'로 띄운다 — MCP 서버가 기동 시 호출.

    detach·breakaway·자동시작 레지스트리가 전혀 없는 단순 자식 프로세스다(= persistence
    아님 → 백신 행위탐지 회피). stdout/stderr 를 DEVNULL 로 막아 부모(MCP 서버)의
    stdio(JSON-RPC) 채널을 오염시키지 않는다. 데몬 자신의 로그는 daemon.log 로 남는다.

    이미 살아있으면 None(데몬의 PID 락도 이중 안전망). 반환: subprocess.Popen | None.
    """
    if is_alive():
        return None

    import shutil
    import subprocess

    exe = shutil.which("telegramlens-daemon")
    if exe:
        args = [exe, "--interval", str(interval)]
    else:
        args = [sys.executable, "-m", "telegram_lens.daemon", "--interval", str(interval)]

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # 콘솔 창만 안 뜨게. detach/breakaway/새 프로세스그룹 없음 → 평범한 자식.
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.Popen(args, **kwargs)
    except OSError:
        return None


def _catchup_window(min_window: int, max_window: int, margin: int = 5) -> int:
    """DB 최신 메시지 이후를 덮는 수집 창(분)을 동적으로 계산.

    정상 가동이면 ≈ 주기, 다운타임 후 첫 사이클이면 공백 전체로 자동 확장.
    상한(max_window)으로 과도한 backfill 방지.
    """
    try:
        db.init_db()
        with db.connect() as conn:
            newest = db.newest_message_date(conn)
    except Exception:
        newest = None
    if not newest:
        return max_window  # DB 비어있으면 최대로 시딩
    try:
        dt = datetime.fromisoformat(newest)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        gap_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except ValueError:
        return min_window
    return int(max(min_window, min(math.ceil(gap_min) + margin, max_window)))


def _catchup_limit(window_min: int, base_limit: int, per_hour: int = 80,
                   hard_cap: int = 5000) -> int:
    """수집 창에 비례해 채널당 메시지 상한을 키운다.

    정상(작은 창)에선 base_limit 로 충분하지만, 다운타임 백필(큰 창)에선 바쁜 채널이
    base_limit 에서 잘려 구멍이 난다. 창 길이에 비례(시간당 per_hour)해 깊이를 키워
    '날짜 경계까지' 놓치지 않게 하되, hard_cap 으로 폭주를 막는다. 정상 사이클은
    fetch 가 어차피 날짜 경계에서 일찍 멈추므로 상한을 키워도 비용이 없다.
    """
    scaled = int(window_min / 60 * per_hour)
    return max(base_limit, min(scaled, hard_cap))


def _offer_path():
    return data_dir() / "pending_offer.json"


def _request_path():
    return data_dir() / "backfill_request.json"


def _gap_minutes():
    """DB 최신 메시지로부터 지금까지의 갭(분). 상한 없는 raw 값. DB 비면 None."""
    try:
        db.init_db()
        with db.connect() as conn:
            newest = db.newest_message_date(conn)
    except Exception:
        return None
    if not newest:
        return None
    try:
        dt = datetime.fromisoformat(newest)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except ValueError:
        return None


def _maybe_offer_backfill(max_window: int) -> None:
    """기동 시 1회: 갭이 자동 백필 상한을 넘으면 '더 수집할까요?' 제안 플래그를 남긴다.

    이 플래그(pending_offer.json)는 sticky — telegram_status 가 읽어 Claude 가 사용자에게
    제안하고, 사용자가 telegram_collect_history 로 동의하거나 telegram_dismiss_backfill
    로 거절할 때까지 유지된다.
    """
    gap = _gap_minutes()
    if not gap or gap <= max_window:
        return
    try:
        _offer_path().write_text(
            json.dumps(
                {
                    "gap_days": int(math.ceil(gap / 1440)),
                    "auto_collected_days": round(max_window / 1440, 1),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _read_backfill_request():
    """사용자가 요청한 깊은 백필 일수. 없으면 None."""
    p = _request_path()
    if not p.exists():
        return None
    try:
        days = int(json.loads(p.read_text(encoding="utf-8")).get("days", 0))
        return days if days > 0 else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _clear_file(path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


async def _loop(
    interval_min: int, min_window: int, max_window: int, per_channel_limit: int
) -> None:
    _LOG.info(
        "데몬 시작 — 주기 %d분, 창 %d~%d분(캐치업), 채널당 %d개",
        interval_min,
        min_window,
        max_window,
        per_channel_limit,
    )
    # 기동 시 1회: 갭이 자동 백필 상한(기본 7일)을 넘으면 사용자에게 제안 플래그를 남긴다.
    _maybe_offer_backfill(max_window)

    while not _stop.is_set():
        last_result = None
        # 사용자가 명시적으로 요청한 깊은 백필이 있으면 그 일수를 우선.
        req_days = _read_backfill_request()
        if req_days:
            window_min = req_days * 1440
            _LOG.info("사용자 요청 백필 — 최근 %d일 소급 수집", req_days)
        else:
            window_min = _catchup_window(min_window, max_window)
        eff_limit = _catchup_limit(window_min, per_channel_limit)
        # 큰 창(평소보다 깊은 소급) = 다운타임 캐치업. 사용자 요청 백필도 캐치업으로 본다.
        catching_up = bool(req_days) or window_min > min_window
        try:
            _write_heartbeat(
                "running", interval_min, None,
                catching_up=catching_up, window_minutes=window_min,
            )
            if window_min > min_window:
                _LOG.info(
                    "캐치업 — %d분 소급 수집(채널당 최대 %d개)", window_min, eff_limit
                )
            last_result = await run_sync(
                minutes=window_min, per_channel_limit=eff_limit
            )
            _LOG.info(
                "sync 완료 — 신규 메시지 %d, 신규 언급 %d, 채널 %d",
                last_result.get("new_messages", 0),
                last_result.get("new_mentions", 0),
                last_result.get("channels", 0),
            )
            if req_days:  # 요청 처리 완료 → 요청·제안 정리
                _clear_file(_request_path())
                _clear_file(_offer_path())
        except Exception as e:  # noqa: BLE001 — 사이클 실패는 치명적이지 않게
            _LOG.error("sync 실패: %s: %s", type(e).__name__, e)
            _write_heartbeat("error", interval_min, {"error": str(e)})
        else:
            _write_heartbeat("sleeping", interval_min, last_result)

        # interval 만큼 자되, 중간에 백필 요청이 들어오면 ~15초 내 깨어 처리.
        waited = 0
        total = interval_min * 60
        while waited < total and not _stop.is_set():
            if _read_backfill_request():
                break
            try:
                await asyncio.wait_for(_stop.wait(), timeout=min(15, total - waited))
            except asyncio.TimeoutError:
                pass
            waited += 15

    _LOG.info("데몬 종료.")


def _install_signal_handlers() -> None:
    def _handle(*_):
        _LOG.info("종료 신호 수신 — 현재 사이클 후 정지합니다.")
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="telegramlens-daemon",
        description="백그라운드 자동 sync 데몬.",
    )
    p.add_argument("--interval", type=int, default=10, help="수집 주기(분). 기본 10.")
    p.add_argument(
        "--min-window",
        type=int,
        default=None,
        help="최소 수집창(분). 기본 = 주기×2+10 (정상 가동 시 공백 방지).",
    )
    p.add_argument(
        "--max-window",
        type=int,
        default=10080,
        help="최대 수집창(분). 다운타임 캐치업 상한. 기본 10080(7일).",
    )
    p.add_argument(
        "--per-channel-limit", type=int, default=500, help="채널당 최대 메시지. 기본 500."
    )
    p.add_argument("--once", action="store_true", help="한 사이클만 돌고 종료(테스트용).")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    _setup_logging()
    min_window = (
        args.min_window if args.min_window is not None else args.interval * 2 + 10
    )
    max_window = max(args.max_window, min_window)

    if not _acquire_lock():
        sys.exit(1)

    _install_signal_handlers()
    try:
        if args.once:
            window = _catchup_window(min_window, max_window)
            eff_limit = _catchup_limit(window, args.per_channel_limit)
            _LOG.info("--once 캐치업 창 %d분(채널당 최대 %d개)", window, eff_limit)
            result = asyncio.run(
                run_sync(minutes=window, per_channel_limit=eff_limit)
            )
            _LOG.info("--once 완료 — %s", result)
        else:
            asyncio.run(
                _loop(args.interval, min_window, max_window, args.per_channel_limit)
            )
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
