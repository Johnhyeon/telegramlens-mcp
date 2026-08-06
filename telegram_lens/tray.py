"""데스크톱 트레이 아이콘 — 수집 상태를 색으로 보여준다. `telegramlens-tray`.

데몬이 자동으로 띄우지 않는 별도 수동 실행 프로그램이다. 새 판정 로직은 하나도 없다 —
daemon_status.json 과 procstate.compute_health() 를 그대로 재사용해, 이미 doctor/
telegram_status 가 쓰는 것과 동일한 기준으로 색만 입힌다.

초록(healthy) / 노랑(degraded) / 빨강(failed) / 회색(확인 불가)을 몇 초마다 갱신하고,
우클릭 메뉴(또는 더블클릭)로 진행상황 상세 창·즉시 새로고침·`telegramlens-doctor` 실행·
종료를 제공한다.

스레드 모델(중요 — macOS 호환을 위한 설계):
  GUI(트레이 아이콘도, tkinter 창도) 는 macOS 의 Cocoa/AppKit 특성상 반드시 메인 스레드
  에서만 안전하게 다룰 수 있다. pystray 도 tkinter 도 '나를 메인 스레드에서 돌려달라'고
  요구하는데, 둘 다 동시에 메인 스레드를 가질 수는 없다. 그래서 여기서는:
    - tkinter(Tk 루트, 평소엔 숨김)가 메인 스레드를 갖고 계속 mainloop() 를 돈다.
    - pystray 아이콘은 icon.run_detached() 로 '백그라운드' 스레드에서 돈다.
    - 트레이 메뉴 클릭(pystray 스레드에서 실행됨)은 tkinter 위젯을 직접 건드리지 않고
      스레드 안전한 큐에 요청만 넣는다. 실제 위젯 조작(창 띄우기·종료)은 Tk 메인 스레드가
      root.after() 로 그 큐를 주기적으로 비우면서 처리한다.
  Windows 는 이 정도로 엄격하지 않아 더 느슨한 방식도 보통 되지만, 이 구조가 두 OS 모두에
  맞는 정석이라 플랫폼별로 분기하지 않고 하나로 통일했다. tkinter 가 아예 없는 드문 환경
  에서는 트레이 아이콘만(진행상황 창 없이) 동작하는 예전 방식으로 자동 폴백한다.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import time
from datetime import datetime, timezone

from PIL import Image, ImageDraw

from telegram_lens import procstate
from telegram_lens.config import data_dir
from telegram_lens.daemon import lock_path as daemon_lock_path, status_path as daemon_status_path

_POLL_SEC = 5
_QUIT_CHECK_SEC = 0.5  # tkinter 없는 폴백 모드에서만 씀(아래 _poll_loop 참고)
_WINDOW_REFRESH_MS = 3000
_QUEUE_POLL_MS = 150

_COLORS: dict[str, tuple[int, int, int, int]] = {
    "healthy": (34, 197, 94, 255),
    "degraded": (234, 179, 8, 255),
    "failed": (239, 68, 68, 255),
    "unknown": (148, 163, 184, 255),
}
_HEALTH_KOR = {"healthy": "정상", "degraded": "주의", "failed": "실패", "unknown": "확인 불가"}
_HEALTH_COLOR = {
    "healthy": "#16a34a", "degraded": "#ca8a04", "failed": "#dc2626", "unknown": "#64748b",
}

_ICON_CACHE: dict[str, "Image.Image"] = {}

# pystray 스레드 → tkinter 메인 스레드로 요청을 넘기는 큐. 두 개의 sentinel 값만 오간다.
_gui_queue: "queue.Queue" = queue.Queue()
_SHOW_STATUS = object()
_QUIT = object()


def _make_dot(color: tuple[int, int, int, int]) -> "Image.Image":
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color)
    return img


def _icon_for(health: str) -> "Image.Image":
    if health not in _ICON_CACHE:
        _ICON_CACHE[health] = _make_dot(_COLORS.get(health, _COLORS["unknown"]))
    return _ICON_CACHE[health]


def current_health() -> dict:
    """procstate.compute_health() 를 그대로 호출 — doctor/telegram_status 와 동일 기준.

    이 함수 자체가 실패해도(예: 상태 파일 손상 이상의 예외) 트레이 앱이 죽으면 안 되므로
    흡수하고 'unknown' 으로 표시한다.
    """
    try:
        held = procstate.DaemonLock(daemon_lock_path()).is_held()
        status = procstate.read_json(daemon_status_path())
        return procstate.compute_health(status, held)
    except Exception as e:  # noqa: BLE001
        return {"health": "unknown", "problem_code": None, "message": f"상태 확인 실패: {e}"}


def _run_doctor(icon) -> None:
    """telegramlens-doctor 를 별도 콘솔에 띄운다(Windows). 그 외 OS는 백그라운드 실행."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/k", "telegramlens-doctor"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        else:
            subprocess.Popen(["telegramlens-doctor"])
    except Exception as e:  # noqa: BLE001 — 실행 실패가 트레이 앱을 죽이면 안 됨
        try:
            icon.notify(f"doctor 실행 실패: {e}", "TelegramLens")
        except Exception:  # noqa: BLE001 — notify 미지원 플랫폼일 수 있음
            pass


def _open_status_window(icon=None, item=None) -> None:
    """'진행상황 보기'(우클릭 메뉴 / 더블클릭) — pystray 자신의 스레드에서 실행된다.

    tkinter 위젯은 메인(Tk) 스레드에서만 안전하게 건드릴 수 있어(특히 macOS), 여기서
    직접 창을 만들지 않고 큐에 요청만 넣는다. 실제 생성은 Tk 쪽 _drain_queue 가 처리.
    tkinter 자체가 없는 폴백 모드에서는 이 큐를 아무도 안 비우므로 사실상 무시된다.
    """
    _gui_queue.put(_SHOW_STATUS)


def _quit(icon) -> None:
    icon.visible = False
    icon.stop()
    _gui_queue.put(_QUIT)


def _fmt_minutes_ago(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    return f"{mins:.0f}분 전" if mins >= 1 else "방금"


def _show_status_toplevel(root, win_state: dict) -> None:
    """진행상황 창을 띄우거나(없으면 새로) 이미 열려있으면 앞으로 가져온다.

    반드시 Tk 메인 스레드(root.after() 콜백 안)에서만 호출돼야 한다.
    """
    import tkinter as tk
    from tkinter import ttk

    existing = win_state.get("toplevel")
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass

    win = tk.Toplevel(root)
    win.title("TelegramLens 수집 현황")
    win.geometry("380x380")
    win.resizable(False, False)

    def _on_close() -> None:
        win_state["toplevel"] = None
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close)
    win_state["toplevel"] = win

    header = tk.Label(win, text="확인 중...", font=("Segoe UI", 12, "bold"), anchor="w")
    header.pack(fill="x", padx=14, pady=(12, 6))

    def _section(title: str):
        frame = tk.LabelFrame(win, text=title, padx=10, pady=6)
        frame.pack(fill="x", padx=12, pady=4)
        return frame

    daemon_frame = _section("데몬")
    daemon_labels: dict[str, tk.Label] = {}
    for key, label in [
        ("state", "상태"), ("heartbeat_at", "하트비트"),
        ("last_success_at", "마지막 성공"), ("consecutive_failures", "연속 실패"),
    ]:
        row = tk.Frame(daemon_frame)
        row.pack(fill="x")
        tk.Label(row, text=f"{label}:", width=10, anchor="w").pack(side="left")
        val = tk.Label(row, text="-", anchor="w")
        val.pack(side="left", fill="x", expand=True)
        daemon_labels[key] = val

    channel_frame = _section("채널(이번 사이클)")
    channel_label = tk.Label(channel_frame, text="-", anchor="w")
    channel_label.pack(fill="x")

    backfill_frame = _section("백필")
    backfill_label = tk.Label(backfill_frame, text="-", anchor="w", justify="left", wraplength=320)
    backfill_label.pack(fill="x")
    progress = ttk.Progressbar(backfill_frame, length=320, mode="determinate")
    progress.pack(fill="x", pady=(6, 0))

    error_label = tk.Label(
        win, text="", fg=_HEALTH_COLOR["failed"], wraplength=340, justify="left", anchor="w",
    )
    error_label.pack(fill="x", padx=14, pady=6)

    btns = tk.Frame(win)
    btns.pack(fill="x", padx=12, pady=(4, 10))
    tk.Button(btns, text="닫기", command=_on_close).pack(side="right")
    tk.Button(btns, text="새로고침", command=lambda: _update()).pack(side="right", padx=(0, 6))

    def _update() -> None:
        health = current_health()
        status = procstate.read_json(daemon_status_path())
        header.config(
            text=f"{_HEALTH_KOR.get(health['health'], health['health'])} — {health['message']}",
            fg=_HEALTH_COLOR.get(health["health"], "#000000"),
        )
        if status:
            daemon_labels["state"].config(text=str(status.get("state") or "-"))
            daemon_labels["heartbeat_at"].config(text=_fmt_minutes_ago(status.get("heartbeat_at")))
            daemon_labels["last_success_at"].config(
                text=_fmt_minutes_ago(status.get("last_success_at"))
            )
            daemon_labels["consecutive_failures"].config(
                text=str(status.get("consecutive_failures", 0))
            )

            ch = status.get("channels") or {}
            channel_label.config(
                text=f"총 {ch.get('total', 0)} · 처리 {ch.get('processed', 0)} · "
                     f"성공 {ch.get('succeeded', 0)} · 실패 {ch.get('failed', 0)}"
            )

            bf = status.get("backfill") or {}
            if bf.get("state") == "running":
                total = bf.get("total_channels") or 0
                done = bf.get("processed_channels") or 0
                backfill_label.config(
                    text=f"요청 {bf.get('requested_days')}일 소급 — {done}/{total} 채널, "
                         f"{bf.get('fetched_messages', 0)}건 수집 "
                         f"(마지막 진행: {_fmt_minutes_ago(bf.get('last_progress_at'))})"
                )
                progress["maximum"] = max(total, 1)
                progress["value"] = done
            elif bf.get("state") == "interrupted":
                backfill_label.config(text="이전 백필이 중단된 채로 남아있습니다(재요청 필요).")
                progress["value"] = 0
            else:
                backfill_label.config(text="진행 중인 백필 없음.")
                progress["value"] = 0

            le = status.get("last_error")
            error_label.config(text=f"[{le['code']}] {le['message']}" if le else "")
        else:
            channel_label.config(text="-")
            backfill_label.config(text="상태 파일을 아직 읽을 수 없습니다.")
            error_label.config(text="")

        try:
            win.after(_WINDOW_REFRESH_MS, _update)
        except tk.TclError:
            pass  # 창이 이미 닫힘 — 다음 예약을 안 잡음

    _update()


def _refresh(icon, state: dict) -> None:
    health = current_health()
    state["summary"] = health["message"]
    icon.icon = _icon_for(health["health"])
    icon.title = f"TelegramLens — {health['message']}"


def _build_menu(state: dict):
    import pystray

    return pystray.Menu(
        pystray.MenuItem(lambda item: state.get("summary", "상태 확인 중..."), None, enabled=False),
        pystray.Menu.SEPARATOR,
        # default=True — 더블클릭이 지원되는 백엔드(대표적으로 Windows)에서는 더블클릭도
        # 이 항목과 같은 동작을 한다. 미지원 백엔드에서는 그냥 평범한 메뉴 항목.
        pystray.MenuItem("진행상황 보기", _open_status_window, default=True),
        pystray.MenuItem("지금 새로고침", lambda icon, item: _refresh(icon, state)),
        pystray.MenuItem("telegramlens-doctor 실행", lambda icon, item: _run_doctor(icon)),
        pystray.MenuItem("종료", lambda icon, item: _quit(icon)),
    )


def _poll_loop(icon, state: dict) -> None:
    """tkinter 가 없는 폴백 모드 전용 — pystray 를 메인 스레드에서 블로킹 실행할 때의
    상태 갱신 루프(진행상황 창은 지원 안 됨). '종료' 반응은 짧은 주기로 확인한다."""
    icon.visible = True
    _refresh(icon, state)
    elapsed = 0.0
    while icon.visible:
        time.sleep(_QUIT_CHECK_SEC)
        elapsed += _QUIT_CHECK_SEC
        if elapsed < _POLL_SEC:
            continue
        elapsed = 0.0
        try:
            _refresh(icon, state)
        except Exception:  # noqa: BLE001 — 폴링 실패로 트레이 앱 자체가 죽으면 안 됨
            pass


def lock_path():
    return data_dir() / "tray.pid"


_tray_lock = procstate.DaemonLock(lock_path())


def is_alive() -> bool:
    """트레이 앱이 이미 떠 있는지(수동 실행이든 자동 스폰이든) — server.py 감시 루프가
    중복 스폰을 막는 데 쓴다. daemon.is_alive() 와 동일한 락 기반 판정."""
    return procstate.DaemonLock(lock_path()).is_held()


def spawn():
    """트레이 앱을 '평범한 자식 프로세스'로 띄운다 — MCP 서버 감시 루프가 호출.

    이미 떠 있으면(수동 실행 포함) None. daemon.spawn_child() 와 동일한 패턴 —
    detach·자동시작 레지스트리 없는 단순 자식 프로세스, stdio 는 DEVNULL.
    CREATE_NO_WINDOW 는 콘솔 창만 숨긴다 — 트레이 아이콘 자체는 그대로 보인다.
    """
    if is_alive():
        return None

    import shutil

    exe = shutil.which("telegramlens-tray")
    args = [exe] if exe else [sys.executable, "-m", "telegram_lens.tray"]

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.Popen(args, **kwargs)
    except OSError:
        return None


def _run(pystray) -> None:
    try:
        import tkinter as tk
    except ImportError:
        tk = None

    state: dict = {"summary": "상태 확인 중..."}
    icon = pystray.Icon(
        "telegramlens", _icon_for("unknown"), "TelegramLens", menu=_build_menu(state),
    )

    if tk is None:
        # tkinter 없는 드문 환경 — 진행상황 창 없이 트레이 아이콘만(예전 방식 폴백).
        icon.run(setup=lambda ic: _poll_loop(ic, state))
        return

    # tkinter 가 메인 스레드를 갖고, pystray 는 백그라운드 스레드(run_detached)에서 돈다.
    # 모듈 docstring의 스레드 모델 설명 참고 — macOS 호환을 위한 핵심 설계.
    root = tk.Tk()
    root.withdraw()  # 평소엔 숨김 — '진행상황 보기' 눌렀을 때만 Toplevel 로 띄운다.

    win_state: dict = {"toplevel": None}

    def _tick_refresh() -> None:
        try:
            _refresh(icon, state)
        except Exception:  # noqa: BLE001 — 갱신 실패로 앱 전체가 죽으면 안 됨
            pass
        root.after(_POLL_SEC * 1000, _tick_refresh)

    def _drain_queue() -> None:
        try:
            while True:
                msg = _gui_queue.get_nowait()
                if msg is _QUIT:
                    root.quit()
                    return
                if msg is _SHOW_STATUS:
                    _show_status_toplevel(root, win_state)
        except queue.Empty:
            pass
        root.after(_QUEUE_POLL_MS, _drain_queue)

    icon.visible = True
    icon.run_detached()
    _refresh(icon, state)
    root.after(_POLL_SEC * 1000, _tick_refresh)
    root.after(_QUEUE_POLL_MS, _drain_queue)
    try:
        root.mainloop()
    finally:
        # 정상 종료(root.quit())든 창 강제로 닫혀서든, pystray 스레드가 아직 안 죽었으면 마저 정리.
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    import pystray

    if not _tray_lock.acquire():
        # 이미 다른 트레이 인스턴스가 떠 있음(수동 실행 포함) — 아이콘 중복 방지, 조용히 종료.
        # stdout 이 DEVNULL 인 자동 스폰 경로에서는 어차피 안 보이지만, 사용자가 터미널에서
        # 직접 실행했을 때는 '왜 아무것도 안 뜨지'로 헷갈리지 않게 이유를 남긴다.
        print("TelegramLens 트레이 아이콘이 이미 실행 중입니다.")
        return
    try:
        _run(pystray)
    finally:
        _tray_lock.release()


if __name__ == "__main__":
    main()
