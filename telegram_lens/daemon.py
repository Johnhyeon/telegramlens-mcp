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
from logging.handlers import RotatingFileHandler

from telethon import events

from telegram_lens import commands, db
from telegram_lens.client import make_client
from telegram_lens.config import data_dir, secure_data_files
from telegram_lens.sync import run_sync

_LOG = logging.getLogger("telegramlens.daemon")
_stop = asyncio.Event()


def _status_path():
    return data_dir() / "daemon_status.json"


def _pid_path():
    return data_dir() / "daemon.pid"


def _alive_path():
    return data_dir() / "daemon_alive"


def _touch_alive() -> None:
    """가동 신호(타임스탬프) 갱신 — 데몬이 30초마다 호출.

    PID 존재가 아니라 이 타임스탬프의 신선도로 '진짜 가동 중'을 판정한다. Windows 의
    OpenProcess 는 종료됐거나 재사용된 PID 도 열어버려(_pid_alive 거짓양성) 죽은 데몬을
    살아있다고 오판하지만, 좀비/재사용 PID 는 이 파일을 갱신하지 못한다.
    """
    try:
        _alive_path().write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
    except OSError:
        pass


def _collector_fresh(max_age_sec: float = 180) -> bool:
    """daemon_alive 타임스탬프가 max_age_sec 이내면 True(= 실제 가동 중)."""
    p = _alive_path()
    if not p.exists():
        return False
    try:
        ts = datetime.fromisoformat(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() <= max_age_sec


def _setup_logging() -> None:
    log_path = data_dir() / "daemon.log"
    # 로테이션: 10분마다 INFO 한 줄을 무기한 append 하면 무한 증식 → 5MB×3 으로 상한.
    handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    # 포그라운드 디버그 실행 시 한국어 로그를 콘솔 코드페이지(예: cp949)로 찍다 나는
    # UnicodeEncodeError 잡음을 막는다(운영에선 stdout 이 DEVNULL 이라 무관).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass
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
    """이미 '실제로' 데몬이 돌고 있으면 False. PID 파일 + 가동 신선도 락.

    PID 존재만 보면 Windows 좀비/재사용 PID 에 속아 '이미 실행 중'으로 오판하고 데몬이
    영영 못 뜬다(2026-06 장애 원인). 가동 신호(_collector_fresh)가 멎었으면 그 PID 는
    우리 데몬이 아니거나 멎은 것이므로 락을 회수해 새로 띄운다.
    """
    p = _pid_path()
    if p.exists():
        try:
            old_pid = int(p.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        if old_pid and _pid_alive(old_pid) and _collector_fresh():
            _LOG.error("이미 데몬이 실행 중입니다 (pid=%s). 종료합니다.", old_pid)
            return False
        if old_pid:
            _LOG.warning("낡은 PID 락 회수 (pid=%s, 가동 신호 멎음) — 새로 기동.", old_pid)
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
    """데몬이 '실제로' 가동 중인지 — PID 존재 AND 가동 신호 신선도. 외부(서버)에서 호출.

    PID 만으로는 Windows 좀비/재사용 PID 에 속는다(_pid_alive 거짓양성). 가동 신호가
    멈췄으면 그 PID 가 우리 데몬이 아니거나 멎은 것이므로 죽은 것으로 본다 → 서버 감시
    루프가 재스폰한다.
    """
    p = _pid_path()
    if not p.exists():
        return False
    try:
        pid = int(p.read_text().strip())
    except (ValueError, OSError):
        return False
    return _pid_alive(pid) and _collector_fresh()


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


def _maintenance_path():
    return data_dir() / "maintenance.json"


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("TELEGRAMLENS_RAW_RETENTION_DAYS", "90")))
    except ValueError:
        return 90


def _run_maintenance() -> None:
    """주기적 정리 — 원문 보존 prune(하루 1회) + VACUUM(월 1회). 시그널(mentions)은 영구 보존.

    매 사이클 호출되지만 타임스탬프 게이트로 실제 작업은 하루/월 1회만 한다(호출 자체는 저렴).
    """
    now = datetime.now(timezone.utc)
    state = {}
    p = _maintenance_path()
    if p.exists():
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            state = {}

    def _age_h(key):
        ts = state.get(key)
        if not ts:
            return None
        try:
            t = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds() / 3600

    changed = False
    # 원문 prune: 하루 1회
    ah = _age_h("last_prune")
    if ah is None or ah >= 24:
        try:
            days = _retention_days()
            with db.connect() as conn:
                blanked = db.prune_raw_content(days, conn)
            if blanked:
                _LOG.info("보존 정리 — 원문 %d건 비움(>%d일, 시그널 보존)", blanked, days)
            state["last_prune"] = now.isoformat()
            changed = True
        except Exception as e:  # noqa: BLE001 — 정리 실패가 수집을 막으면 안 됨
            _LOG.error("보존 정리 실패: %s: %s", type(e).__name__, e)
    # VACUUM: 월 1회. 첫 실행은 타임스탬프만 찍고 건너뜀(불필요한 초기 VACUUM 방지).
    vh = _age_h("last_vacuum")
    if vh is None:
        state["last_vacuum"] = now.isoformat()
        changed = True
    elif vh >= 24 * 30:
        try:
            db.vacuum()
            _LOG.info("VACUUM 완료 — 디스크 반환")
            state["last_vacuum"] = now.isoformat()
            changed = True
        except Exception as e:  # noqa: BLE001
            _LOG.error("VACUUM 실패: %s: %s", type(e).__name__, e)

    if changed:
        try:
            _maintenance_path().write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass


def _send_request_path():
    return data_dir() / "send_request.json"


def _send_result_path():
    return data_dir() / "send_result.json"


def _read_send_request():
    """서버(telegram_send_me 툴)가 남긴 '나에게 전송' 요청. {req_id, messages:[...]} | None.

    전송은 데몬만 한다(세션 소유자 단일화 → 수집용 세션과 충돌 0). 서버는 요청 파일만 남기고,
    데몬이 sleep 구간(수집 client 비활성)에 처리한 뒤 결과 파일을 남긴다.
    """
    p = _send_request_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    msgs = data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    return {"req_id": data.get("req_id"), "messages": [str(m) for m in msgs]}


def _split_message(text: str, limit: int = 4096) -> list[str]:
    """텔레그램 길이 제한(4096) 안전망. 줄 경계로 나눠 청크가 한도를 넘지 않게 한다."""
    text = text.rstrip("\n")
    if not text.strip():
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:  # 한 줄 자체가 한도 초과(드묾) → 강제 분할
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur = cur + "\n" + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


async def _drain_send_request(client) -> None:
    """'나에게 전송' 요청이 있으면 (sleep 구간 listener client 로) 'me' 에 보낸다.

    결과(req_id·성공·건수·오류)를 send_result.json 에 남겨 서버 툴이 회수한다.
    """
    req = _read_send_request()
    if not req:
        return
    result = {"req_id": req.get("req_id"), "ok": False, "sent": 0}
    try:
        if not await client.is_user_authorized():
            result["error"] = "로그인이 필요합니다 (`telegramlens-login`)."
        else:
            sent = 0
            for msg in req["messages"]:
                for chunk in _split_message(msg):
                    await client.send_message("me", chunk)
                    sent += 1
            result["ok"] = True
            result["sent"] = sent
    except Exception as e:  # noqa: BLE001 — 전송 실패가 데몬을 죽이면 안 됨
        result["error"] = f"{type(e).__name__}: {e}"
        _LOG.error("나에게 전송 실패: %s", e)

    try:
        _send_result_path().write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
    _clear_file(_send_request_path())
    if result["ok"]:
        _LOG.info("나에게 전송 완료 — %d개 메시지", result["sent"])


_my_id_cache = [None]            # 본인(Saved Messages) user id
_seen_cmd_ids: set = set()       # 처리(예약)한 '!' 메시지 id — 이벤트·폴링 중복 차단
_cmd_init_done = [False]         # 기동 직후 기존 메시지 1회 스킵 여부


async def _get_my_id(client):
    if _my_id_cache[0] is None:
        try:
            me = await client.get_me()
            _my_id_cache[0] = me.id if me else None
        except Exception:  # noqa: BLE001
            pass
    return _my_id_cache[0]


async def _process_command_msg(client, msg) -> None:
    """'나에게'의 한 메시지를 '!' 명령이면 처리해 답장. 이벤트·폴링 양쪽에서 호출(중복은 id로 차단).

    '!' 로 시작하는 메시지만 처리 → 브리핑·답장·개인 메모는 무시(루프 방지·프라이버시).
    """
    text = (getattr(msg, "raw_text", None) or getattr(msg, "text", None) or "").strip()
    if not text.startswith("!"):
        return
    mid = getattr(msg, "id", None)
    if mid in _seen_cmd_ids:  # await 전 체크-마킹 → asyncio 단일스레드라 atomic
        return
    _seen_cmd_ids.add(mid)
    if len(_seen_cmd_ids) > 500:  # 메모리 상한
        for x in sorted(_seen_cmd_ids)[:300]:
            _seen_cmd_ids.discard(x)
    try:
        reply = commands.handle_command(text[1:])
        if reply:
            for chunk in _split_message(reply):
                await client.send_message("me", chunk)
            _LOG.info("! 명령 처리: %s", text[:24])
    except Exception as e:  # noqa: BLE001
        _LOG.error("! 명령 실패: %s: %s", type(e).__name__, e)


def _make_command_handler(client):
    """'나에게'의 새 메시지를 '즉시' 처리하는 이벤트 핸들러(cross-client = 폰에서 보낸 명령)."""
    async def _handler(event):
        try:
            await _process_command_msg(client, event.message)
        except Exception as e:  # noqa: BLE001
            _LOG.error("! 명령 핸들러 오류: %s", e)

    return _handler


async def _poll_commands(client) -> None:
    """폴백 — 이벤트를 못 받았을 때 '나에게'를 읽어 새 '!' 명령 처리(get_messages 라 어떤
    클라이언트가 보냈든 잡힘). 기동 직후 기존 메시지는 seen 처리해 과거 명령 replay 방지."""
    try:
        msgs = await client.get_messages("me", limit=15)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("명령 폴링 실패: %s", e)
        return
    if not _cmd_init_done[0]:
        for m in msgs:
            _seen_cmd_ids.add(m.id)
        _cmd_init_done[0] = True
        return
    for m in sorted(msgs, key=lambda x: x.id):
        await _process_command_msg(client, m)


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

    # 가동 신호: 30초마다 타임스탬프를 갱신해 서버 감시 루프/락이 '실제 가동 중'을
    # 신뢰성 있게 판정하게 한다(PID 거짓양성 회피). 즉시 1회 찍어 빠르게 인식되게.
    _touch_alive()

    async def _beat() -> None:
        while not _stop.is_set():
            _touch_alive()
            try:
                await asyncio.wait_for(_stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    beat_task = asyncio.create_task(_beat())

    while not _stop.is_set():
        last_result = None
        # 사용자 요청 백필이 있으면 '요청 일수'와 '자동 캐치업 창'(연속 수집 갭) 중 큰 쪽.
        req_days = _read_backfill_request()
        auto_window = _catchup_window(min_window, max_window)
        if req_days:
            # 작은 요청(예: 1일)이 큰 다운타임 갭(예: 3일)을 덮어써, 중간 구간을 영영
            # 수집하지 않고 영구·무성 구멍을 남기던 버그를 막는다(P0: 히스토리 무결성).
            window_min = max(req_days * 1440, auto_window)
            _LOG.info(
                "사용자 요청 백필 — 요청 %d일, 실효 창 %d분 소급 수집",
                req_days, window_min,
            )
        else:
            window_min = auto_window
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

        # 정상 사이클에서만 정리(백필/캐치업 중엔 부하 안 주기 위해 건너뜀).
        if not catching_up:
            _run_maintenance()

        # interval 만큼 자되, 그동안 '나에게'의 '!' 명령을 지속연결 핸들러로 '즉시' 처리하고
        # 대기 중인 '나에게 전송'도 보낸다. 수집 client 는 이미 끊겼으므로(run_sync finally)
        # 이 listener 와 세션이 동시에 안 떠 충돌이 없다(순차).
        listener = make_client()
        try:
            await listener.connect()
            my_id = await _get_my_id(listener)
            if my_id is not None:  # 이벤트 핸들러: '나에게'의 ! 명령을 즉시 처리(instant)
                listener.add_event_handler(
                    _make_command_handler(listener),
                    events.NewMessage(chats=my_id),
                )
            waited = 0
            total = interval_min * 60
            while waited < total and not _stop.is_set():
                await _drain_send_request(listener)
                await _poll_commands(listener)  # 폴백(이벤트 못 받은 명령 ≤15초)
                if _read_backfill_request():
                    break
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=min(15, total - waited))
                except asyncio.TimeoutError:
                    pass
                waited += 15
        except Exception as e:  # noqa: BLE001 — listener 오류가 데몬을 죽이면 안 됨
            _LOG.warning("명령 listener 오류: %s: %s", type(e).__name__, e)
            try:  # listener 가 죽어도 수집·하트비트는 계속 — 남은 주기만큼 대기
                await asyncio.wait_for(_stop.wait(), timeout=interval_min * 60)
            except asyncio.TimeoutError:
                pass
        finally:
            try:
                await listener.disconnect()
            except Exception:  # noqa: BLE001
                pass

    beat_task.cancel()
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
    secure_data_files()
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
