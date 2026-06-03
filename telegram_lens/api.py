"""외부 조회 HTTP 엔드포인트 — `telegramlens-api`.

Claude(MCP) 세션 없이도 외부 스크립트·대시보드가 같은 집계를 읽게 하는 로컬 읽기전용
JSON API. 표준 라이브러리 http.server 만 쓴다(의존성 0). 기본 127.0.0.1 바인딩 —
데이터 주권·외부 노출 차단. DB 는 db.connect()(WAL) 로 읽으므로 백그라운드 수집 데몬과
동시 접근해도 안전하다.

라우트(GET 만):
  /health                  상태(메시지·언급 수, 최신 시각)
  /trending                기간 내 언급량 상위(독립 언급 기준)
  /momentum                급증 종목
  /velocity                시간대별 언급 흐름·급등
  /buzz_score              종합 버즈 스코어
  /timeline/<code|name>    종목 버즈 타임라인
  /stock/<code|name>       종목 언급 요약 + 원문 샘플
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from telegram_lens import db, queries
from telegram_lens.stocks import resolve_code

_LOG = logging.getLogger("telegramlens.api")

_DEFAULT_PORT = 8787


class _ApiError(Exception):
    """라우트 처리 중 사용자 오류(HTTP status 동반)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _f(qs: dict, key: str, default: float) -> float:
    try:
        return float(qs[key][0]) if key in qs else default
    except (ValueError, IndexError):
        raise _ApiError(400, f"'{key}' 는 숫자여야 합니다.")


def _i(qs: dict, key: str, default: int) -> int:
    return int(_f(qs, key, default))


def _s(qs: dict, key: str, default=None):
    return qs[key][0] if key in qs and qs[key] else default


def _bool(qs: dict, key: str, default: bool = False) -> bool:
    if key not in qs or not qs[key]:
        return default
    return qs[key][0].lower() in ("1", "true", "yes", "on")


def _resolve_or_400(seg: str) -> tuple[str, str]:
    """경로 세그먼트(코드/종목명) → (code, name). 못 찾으면 400."""
    code, name = resolve_code(unquote(seg))
    if code is None:
        raise _ApiError(400, f"'{seg}' 종목을 사전에서 찾지 못했습니다(코드/종목명).")
    return code, name


def handle(path: str, qs: dict):
    """경로+쿼리 → JSON 직렬화 가능한 결과. 미지원 경로는 _ApiError(404)."""
    parts = [p for p in path.split("/") if p]

    if not parts or parts == ["health"]:
        with db.connect() as conn:
            s = db.stats(conn)
            newest = db.newest_message_date(conn)
        return {"ok": True, "messages": s.get("messages"),
                "mentions": s.get("mentions"), "newest_message": newest}

    head = parts[0]
    if head == "trending":
        return {"stocks": queries.trending(hours=_f(qs, "hours", 24),
                                           top=_i(qs, "top", 20))}
    if head == "momentum":
        return {"stocks": queries.momentum(
            hours=_f(qs, "hours", 6),
            baseline_hours=_f(qs, "baseline_hours", 72),
            top=_i(qs, "top", 15))}
    if head == "velocity":
        code = None
        if _s(qs, "code"):
            code, _ = _resolve_or_400(_s(qs, "code"))
        return {"stocks": queries.buzz_velocity(
            code=code,
            bucket_minutes=_i(qs, "bucket_minutes", 30),
            window_hours=_f(qs, "window_hours", 6),
            spike_min=_i(qs, "spike_min", 5),
            top=_i(qs, "top", 15))}
    if head == "buzz_score":
        only = _s(qs, "only_types")
        only_types = [t for t in only.split(",") if t] if only else None
        return {"stocks": queries.buzz_score(
            window_hours=_f(qs, "window_hours", 24),
            only_types=only_types,
            exclude_gossip=_bool(qs, "exclude_gossip"),
            sentiment=_s(qs, "sentiment"),
            top=_i(qs, "top", 20))}
    if head == "timeline":
        if len(parts) < 2:
            raise _ApiError(400, "사용법: /timeline/<코드 또는 종목명>")
        code, name = _resolve_or_400(parts[1])
        return queries.stock_timeline(
            code=code, name=name,
            hours=_f(qs, "hours", 72),
            bucket_minutes=_i(qs, "bucket_minutes", 60))
    if head == "stock":
        if len(parts) < 2:
            raise _ApiError(400, "사용법: /stock/<코드 또는 종목명>")
        code, name = _resolve_or_400(parts[1])
        return queries.stock_buzz(
            code=code, name=name,
            hours=_f(qs, "hours", 24),
            samples=_i(qs, "samples", 8))

    raise _ApiError(404, f"알 수 없는 경로: /{head}")


class _Handler(BaseHTTPRequestHandler):
    server_version = "TelegramLensAPI"

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 규약)
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            self._send(200, handle(parsed.path, qs))
        except _ApiError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001 — 어떤 예외도 500 JSON 으로
            _LOG.exception("처리 오류")
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):  # 기본 stderr 접속 로그 소음 억제
        _LOG.debug("%s - %s", self.address_string(), fmt % args)


def make_server(host: str = "127.0.0.1", port: int = _DEFAULT_PORT) -> ThreadingHTTPServer:
    db.init_db()
    return ThreadingHTTPServer((host, port), _Handler)


def main() -> None:
    p = argparse.ArgumentParser(
        prog="telegramlens-api",
        description="TelegramLens 외부 조회 HTTP 엔드포인트(로컬 읽기전용 JSON).",
    )
    p.add_argument("--host", default="127.0.0.1", help="바인딩 호스트(기본 127.0.0.1).")
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TELEGRAMLENS_API_PORT", _DEFAULT_PORT)),
        help=f"포트(기본 {_DEFAULT_PORT}, env TELEGRAMLENS_API_PORT).",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        _LOG.warning(
            "⚠️ %s 로 바인딩합니다 — localhost 외 노출은 인증이 없습니다. 주의하세요.",
            args.host,
        )

    httpd = make_server(args.host, args.port)
    _LOG.info("TelegramLens API listening on http://%s:%d", args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        _LOG.info("API 종료.")


if __name__ == "__main__":
    main()
