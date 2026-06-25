"""TelegramLens MCP 서버.

텔레그램 채널의 종목 언급·내러티브 흐름을 구조화해 Claude에 제공한다.
로그인은 `telegramlens-login` 으로 먼저 끝내야 한다(세션 재사용).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from telegram_lens import db, discover, queries
from telegram_lens.classify import run_classification
from telegram_lens.config import data_dir, is_logged_in, secure_data_files
from telegram_lens.client import NoCredentialsError, NotLoggedInError
from telegram_lens.licensing import is_licensed, LOCKED_MESSAGE
from telegram_lens.extract import reset_index
from telegram_lens.stocks import (
    add_alias,
    add_ambiguous,
    load_etf_codes,
    load_stocks,
    resolve_code,
)
from telegram_lens.sync import run_sync

_LOG = logging.getLogger("telegramlens.server")
# stdio MCP: stdout 은 Claude 와의 JSON-RPC 채널이다. 로그가 거기 새면 프로토콜이
# 깨진다 → 서버 로그는 stderr 로만 보내고(propagate 차단), fallback in-process sync
# 시 Telethon 의 수다스러운 로그도 막는다(평상시 수집은 자식 데몬이 전담).
_LOG.addHandler(logging.StreamHandler(sys.stderr))
_LOG.propagate = False
logging.getLogger("telethon").setLevel(logging.CRITICAL)

# ── 백그라운드 수집: 별도 자식 프로세스 ─────────────────────────────
# 수집(데몬)을 MCP 서버 '안'이 아니라 별도 자식 프로세스로 띄운다. 이렇게 하면:
#   - stdout/stderr 를 DEVNULL 로 막아 MCP 의 stdio(JSON-RPC) 채널을 오염시키지 않음
#   - 별도 프로세스라 무거운 수집이 서버 이벤트 루프를 막지 않음(응답 멈춤 방지)
#   - 자동시작 레지스트리·detach·breakaway 가 없어 persistence(백신 행위탐지)에 안 걸림
#   - Claude(=부모 MCP 서버)가 종료되면 함께 정리한다
# 데몬이 DB 를 미리 채워두므로 조회 도구는 즉시(텔레그램 접속 대기 없이) 응답한다.


@asynccontextmanager
async def _lifespan(_server):
    db.init_db()
    secure_data_files()
    # 수집 데몬을 '한 번만' 스폰하면, 데몬이 절전/재개·일시정지 등으로 죽었을 때 서버가
    # 살아있어도 영영 안 되살아난다(2026-06 장애: 3일간 수집 정지). 60초 감시 루프로
    # '실제 가동(is_alive: PID + 가동 신호 신선도)'을 확인해 죽었으면 재스폰한다 — Claude 가
    # 켜진 동안 수집 신선도를 자가치유로 보장. 새 persistence 없이 부모-자식 관계 유지.
    state: dict = {"child": None}

    async def _supervise() -> None:
        from telegram_lens.daemon import is_alive, spawn_child

        while True:
            try:
                if is_logged_in() and not is_alive():
                    child = spawn_child()
                    if child is not None:
                        state["child"] = child
                        _LOG.info("수집 데몬 자식 프로세스 (재)기동 (pid=%s)", child.pid)
            except Exception as e:  # noqa: BLE001 — 감시 실패가 서버를 막으면 안 됨
                _LOG.warning("수집 데몬 감시 실패: %s", e)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    watchdog = asyncio.create_task(_supervise())
    try:
        yield
    finally:
        watchdog.cancel()
        child = state.get("child")
        if child is not None:
            try:
                child.terminate()  # Claude 종료 시 우리가 띄운 데몬도 정리(persistence 방지)
            except Exception:  # noqa: BLE001
                pass


def safe_tool(func):
    """예외를 사용자 친화 메시지로 변환. 아울러 라이선스 게이트를 적용한다 —
    모든 도구가 이 래퍼를 거치므로, 미활성화 시 조회 없이 안내 메시지를 반환한다."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not is_licensed():
            return LOCKED_MESSAGE
        try:
            return await func(*args, **kwargs)
        except NoCredentialsError as e:
            return f"⚠️ {e}"
        except NotLoggedInError as e:
            return f"⚠️ {e}"
        except Exception as e:  # noqa: BLE001
            return f"⚠️ 처리 중 오류: {type(e).__name__}: {e}"

    return wrapper


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _stocks_payload(stocks: list) -> dict:
    """리스트형 결과 공통 포장 — 종목코드 배열(codes)을 맨 위에 같이 준다.

    텔레그램 버즈 결과의 종목들을 외부 시세·수급 도구(예: StockLens get_multi_stocks /
    get_flow_batch)로 한 번에 넘길 때, stocks 를 재파싱하지 않고 codes 를 그대로 배치 입력에
    쓰라고 노출한다. 순서는 결과 정렬(상위 버즈 먼저)을 유지한다.

    각 종목에 is_etf 를 달고, ETF 코드만 모은 etf_codes 도 별도로 준다(주식/ETF 구분용).
    """
    etf = load_etf_codes()
    for s in stocks:
        s["is_etf"] = s["code"] in etf
    return {
        "_guidance": _WHY_GUIDANCE,
        "codes": [s["code"] for s in stocks],
        "etf_codes": [s["code"] for s in stocks if s["code"] in etf],
        "stocks": stocks,
    }


# 트렌딩/모멘텀은 '빈도'만 알려준다. '왜'는 동봉된 samples(원문)에서만 와야 한다.
_WHY_GUIDANCE = (
    "이 목록의 수치는 '무엇이 얼마나' 언급됐는지(빈도)일 뿐, 급등/트렌드의 '이유'가 "
    "아닙니다. 각 종목의 samples 는 실제 텔레그램 원문입니다. 이유·테마·이슈를 설명할 "
    "때는 반드시 이 samples(또는 telegram_stock_buzz / telegram_messages 로 더 가져온 "
    "원문)에 실제로 적힌 내용만 근거로 쓰세요. 원문에 근거가 없으면 배경지식으로 "
    "추측하지 말고, telegram_stock_buzz 로 해당 종목을 더 확인하거나 '원문상 이유 불명'"
    "이라고 답하세요."
)


def _collecting_notice() -> str | None:
    """DB가 완전히 비어있을 때(최초 수집 전)만 안내, 그 외엔 항상 답하게 None.

    이전엔 '수집 중 + DB가 오래됨'이면 차단했는데, 조용한 장/주말엔 새 글이 없어 newest 가
    자연히 오래돼도(=수집창이 커져도) 차단이 오발동했다. 'catching_up(큰 창)'으로 한정하려
    했지만, 수집창은 마지막 메시지 이후를 덮으므로 조용한 장에서도 커져 구분이 안 됐다.
    → 결론: DB 에 메시지가 하나라도 있으면 '가장 최신 가용 데이터'로 그냥 답한다(신선도는
    결과에 찍힌 KST 시각으로 드러남). 진짜 막아야 할 경우는 'DB가 텅 빈 최초 수집' 뿐.
    """
    with db.connect() as conn:
        newest = db.newest_message_date(conn)
    if newest is None:
        return "⏳ 텔레그램 데이터를 처음 수집하는 중입니다. 잠시 후(약 1분) 다시 물어봐 주세요."
    return None


def warn_if_collecting(func):
    """조회 도구 전용 — 초기 캐치업 중이면 결과 대신 '수집 중' 안내를 반환."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        notice = _collecting_notice()
        if notice:
            return notice
        return await func(*args, **kwargs)

    return wrapper


mcp = FastMCP(
    "TelegramLens",
    lifespan=_lifespan,
    instructions="""TelegramLens — 텔레그램 채널의 종목 내러티브를 구조화해 제공합니다.

먼저 `telegram_sync` 로 최근 메시지를 수집한 뒤 조회 도구를 쓰세요.
- telegram_trending : 기간 내 언급량 상위 종목
- telegram_momentum : 언급 급증(스파이크) 종목 — 새 내러티브 포착
- telegram_velocity : 종목별 시간대별 언급 흐름·급등 감지(독립 언급 기준)
- telegram_buzz_score : 종합 버즈 스코어(독립 언급×tier×확산×velocity, 감성/유형 필터)
- telegram_timeline : 특정 종목의 버즈 전개(최초 언급→시간대별 확산→베이스라인 배율)
- telegram_stock_buzz : 특정 종목의 언급 요약 + 원문 샘플
- telegram_messages : 원문 메시지 drill-down (채널·시간 범위)
- telegram_search : 원문 키워드 전문검색 — 종목 언급이 없는 거시·산업·테마 글까지 찾음
- telegram_channels : 추적 중인 채널 목록 (tier·weight 포함)
- telegram_set_tier : 채널 성격(analyst/research/info/gossip) 수동 분류
- telegram_status : 로그인·수집 상태 (backfill_offer 가 있으면 아래 참고)

## 수집 메타데이터 (수집 시점 자동 태깅)

각 메시지·종목 샘플에는 다음이 함께 옵니다(룰베이스, 추측 금지 원칙은 동일):
- sentiment(positive/negative/neutral), msg_type(report/breaking/gossip/chat/general)
- views/forwards(조회수·확산), forwarded_from(포워드 원본 채널명)
- telegram_link: 그 메시지의 텔레그램 딥링크(t.me/...) — 원문·첨부를 바로 열어볼 수 있음
- media: 첨부 인지 {type: photo|document|webpage, file_name}. 리포트 PDF·차트 이미지가 있으면
  표시되니, 본문에 안 담긴 내용은 telegram_link 로 안내하라(다운로드·본문읽기는 안 함)
- trending 의 baseline_ratio: 현재 일평균 언급 / 7일 일평균(1 초과면 평소보다 활발).
- 채널 tier·weight: 같은 언급도 analyst > gossip 로 신뢰도가 다름(가중 근거).
- 중복제거: independent(독립 언급=클러스터 수)가 헤드라인. raw_messages 는 포워드/복붙
  포함 원시 건수, spread_copies·total_forwards 는 확산 강도. 순위는 independent 기준.

## 종목코드 배치 연계 (codes)

trending·momentum·velocity·buzz_score·search 결과 맨 위에 `codes` 배열(등장 종목코드,
순위 순)이 있습니다. 이 종목들의 시세·수급을 외부 도구로 확인할 때는 **종목당 개별 호출
대신** `codes` 를 그대로 배치 도구(예: StockLens get_multi_stocks / get_flow_batch)에
한 번에 넘기세요(토큰 절약). 버즈(심리) 위에 시세·수급(데이터)을 얹는 흐름.

## 과거 데이터 추가 수집 제안

자동 수집은 기본 7일까지만 한다. 오래 비웠던 경우 `telegram_status` 에
`backfill_offer` 가 나타난다. 그러면 **임의로 수집하지 말고**, 사용자에게 "그 이전
N일치 데이터까지 더 수집할까요?"라고 **먼저 물어본 뒤**, 동의하면
`telegram_collect_history(days=원하는_일수)` 를, 거절하면 `telegram_dismiss_backfill()`
을 호출한다.

## 🚨 절대 원칙 — '왜'는 원문에서만 (추측 금지)

trending·momentum 결과의 수치는 '무엇이 얼마나 언급됐나'(빈도)일 뿐, **'왜' 언급/급등
했는지가 아니다.** 종목의 급등·트렌드·테마·이슈의 *이유*를 설명할 때는 **반드시**:

1. 결과에 동봉된 각 종목 `samples`(실제 원문)를 먼저 읽고,
2. 부족하면 `telegram_stock_buzz(종목)` / `telegram_messages` 로 원문을 더 확인한 뒤,
3. **원문에 실제로 적힌 내용만** 근거로 답한다.

❌ 원문 확인 없이 배경지식·뉴스 기억으로 "아마 OO 이슈 때문" 식의 이유를 **지어내지 마라.**
   (예: 언급 급증을 보고 원문도 안 본 채 "유심 해킹 후속"이라 단정 → 금지)
원문에 이유가 안 보이면 "원문상 이유는 불명확"이라고 솔직히 말하라.

종목 추천이 아니라 '시장이 무엇을 보고 있는지' 신호를 전달하는 도구입니다.
""",
)


@mcp.tool()
@safe_tool
async def telegram_status() -> str:
    """로그인·수집 상태와 백그라운드 수집 데몬 상태를 반환합니다."""
    db.init_db()
    with db.connect() as conn:
        s = db.stats(conn)
    s["logged_in"] = is_logged_in()
    s["stocks_loaded"] = len(load_stocks())
    # 사용자에게 보이는 시각은 KST 로(저장은 UTC). 한국 사용자가 status 의 UTC 보고
    # 헷갈리던 부분 — 다른 조회 도구는 이미 _to_kst 로 변환해 노출한다.
    s["last_synced"] = queries._to_kst(s.get("last_synced"))
    s["baselines_computed"] = queries._to_kst(s.get("baselines_computed"))

    # 백그라운드 수집 데몬(별도 자식 프로세스)의 하트비트.
    hb = data_dir() / "daemon_status.json"
    beat = None
    if hb.exists():
        try:
            beat = json.loads(hb.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            beat = None
    from telegram_lens.daemon import is_alive as _collector_alive
    if beat and _collector_alive():
        last_result = beat.get("last_result")
        if isinstance(last_result, dict) and last_result.get("since"):
            last_result = {**last_result, "since": queries._to_kst(last_result["since"])}
        s["collector"] = {
            "running": True,
            "mode": "별도 자식 프로세스 (Claude 켜진 동안 백그라운드 수집)",
            "state": beat.get("state"),
            "interval_minutes": beat.get("interval_minutes"),
            "last_run": queries._to_kst(beat.get("last_run")),
            "last_result": last_result,
        }
    else:
        s["collector"] = {
            "running": False,
            "note": "수집 데몬 미가동 — Claude 재시작 시 자동 기동(로그인 상태일 때).",
        }

    # 7일(자동 백필 상한)을 넘는 공백이 감지되면, 더 수집할지 사용자에게 제안.
    offer = data_dir() / "pending_offer.json"
    if offer.exists():
        try:
            o = json.loads(offer.read_text(encoding="utf-8"))
            s["backfill_offer"] = {
                "gap_days": o.get("gap_days"),
                "auto_collected_days": o.get("auto_collected_days"),
                "action": (
                    f"최근 약 {o.get('gap_days')}일치 데이터가 있는데 자동으로는 "
                    f"{o.get('auto_collected_days')}일까지만 수집했습니다. 사용자에게 "
                    "'그 이전 데이터까지 더 수집할까요?'라고 물어보고, 원하면 "
                    "telegram_collect_history(days=원하는_일수)를 호출하세요. "
                    "거절하면 telegram_dismiss_backfill()."
                ),
            }
        except (json.JSONDecodeError, OSError):
            pass
    return _json(s)


@mcp.tool()
@safe_tool
async def telegram_collect_history(days: int = 7) -> str:
    """더 오래된 과거 데이터를 소급 수집하도록 백그라운드 데몬에 요청합니다.

    자동 수집은 기본 7일까지. 더 오래 비웠을 때(telegram_status 의 backfill_offer)
    **사용자 동의 시에만** 호출하세요. 무거운 작업이라 동의 없이 호출 금지.

    Args:
        days: 소급 수집할 일수(1~90). 예: 14면 최근 14일치.
    """
    days = max(1, min(int(days), 90))
    (data_dir() / "backfill_request.json").write_text(
        json.dumps(
            {"days": days, "requested_at": datetime.now(timezone.utc).isoformat()},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 제안은 처리됐으니 정리.
    try:
        (data_dir() / "pending_offer.json").unlink()
    except OSError:
        pass
    return _json(
        {
            "requested_days": days,
            "note": f"최근 {days}일치 소급 수집을 요청했습니다. 데몬이 곧 처리합니다"
            " (telegram_status 의 collector.last_run 으로 완료 확인). 데몬이 가동 중이"
            "아니면 다음 기동 시 처리됩니다.",
        }
    )


@mcp.tool()
@safe_tool
async def telegram_dismiss_backfill() -> str:
    """과거 데이터 추가 수집 제안을 거절(제안 플래그 제거)합니다."""
    try:
        (data_dir() / "pending_offer.json").unlink()
        return _json({"dismissed": True})
    except OSError:
        return _json({"dismissed": False, "note": "제안이 없었습니다."})


@mcp.tool()
@safe_tool
async def telegram_send_me(messages: list[str]) -> str:
    """데일리 브리핑 등을 사용자 본인 텔레그램 'Saved Messages(나에게)'로 전송합니다.

    - messages: plain-text 문자열 리스트. 각 원소 = 텔레그램 메시지 1개.
      예) telegram_send_me(["오늘 시황 요약 ...", "버즈: 많이 언급 vs 급증 ..."])
    - 마크다운/HTML 미적용 — 텔레그램에 기호가 그대로 보이므로 plain text 로 작성(별표·표 금지).
    - 수신자는 항상 본인(Saved Messages)으로 고정 — 남에게는 보낼 수 없습니다(ToS 안전).
    - 전송은 수집 데몬을 통해 이뤄집니다(수집용 세션과 충돌 방지). 데몬 미가동이면 에러 반환.
    - 4096자 초과 메시지는 자동 분할. 반환: 성공 여부 + 전송된 메시지 수.

    '아침 주식 비서'처럼 스케줄(매일 07:00) 작업에서 브리핑을 작성해 이 도구로 보냅니다.
    내용·개수·구성(시황/버즈/시세/공시)은 호출하는 쪽에서 자유롭게 정합니다.

    ★ 한 번에 완성해 단 한 번만 호출하세요. 보내기 *전*에 한글 오타·깨진 글자를 검토하고,
      전송 후에는 어떤 경우에도 재발송/정정 재호출하지 마세요(중복 메시지 방지).
    ★ 수치·사실은 데이터(텔레그램 원문/도구 결과)에 있는 것만 쓰세요. 출처에 없는 숫자를
      지어내지 말고, 가능하면 근거 채널/원문을 함께 밝히세요.
    """
    if isinstance(messages, str):
        msgs = [messages] if messages.strip() else []
    elif isinstance(messages, list):
        msgs = [str(m) for m in messages if str(m).strip()]
    else:
        return "⚠️ messages 는 문자열 리스트여야 합니다."
    if not msgs:
        return "⚠️ 보낼 내용이 없습니다."
    if len(msgs) > 10:
        return "⚠️ 한 번에 최대 10개 메시지까지 보낼 수 있습니다."

    from telegram_lens.daemon import is_alive

    if not is_alive():
        return (
            "⚠️ 수집 데몬이 가동 중이 아닙니다 — 전송은 데몬을 통해 이뤄집니다. "
            "Claude 와 PC가 켜져 있는지 확인하세요(잠시 후 watchdog 이 데몬을 다시 띄웁니다)."
        )

    import uuid

    req_id = uuid.uuid4().hex
    req_path = data_dir() / "send_request.json"
    res_path = data_dir() / "send_result.json"
    try:
        res_path.unlink()  # 직전 결과 제거(매칭 오인 방지)
    except OSError:
        pass
    req_path.write_text(
        json.dumps({"req_id": req_id, "messages": msgs}, ensure_ascii=False),
        encoding="utf-8",
    )

    # 데몬이 sleep 구간(≈15초 주기)에 처리 → 결과 회수. 대량 수집 중이면 다소 지연 가능.
    for _ in range(150):  # 150 × 0.5s = 75s
        await asyncio.sleep(0.5)
        if not res_path.exists():
            continue
        try:
            res = json.loads(res_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if res.get("req_id") != req_id:
            continue
        if res.get("ok"):
            return _json(
                {"ok": True, "sent": res.get("sent", 0), "target": "Saved Messages(나에게)"}
            )
        return f"⚠️ 전송 실패: {res.get('error', '알 수 없음')}"
    return (
        "⚠️ 전송 확인 시간 초과(데몬이 대량 수집 중이면 지연될 수 있습니다). "
        "중복 전송 방지를 위해 자동 재시도하지 않습니다 — 텔레그램 '나에게'를 확인하세요."
    )


@mcp.tool()
@safe_tool
async def telegram_sync(minutes: int = 60, per_channel_limit: int = 500) -> str:
    """최근 N분간의 텔레그램 메시지를 수집·구조화해 로컬 DB에 저장합니다.

    평소엔 백그라운드 수집 데몬이 DB를 채우므로 수동 호출은 보통 불필요합니다.
    데몬이 가동 중이면 그쪽이 텔레그램 세션을 소유하므로(동시 접속 시 충돌) 직접
    sync는 건너뛰고 DB 신선도만 보고합니다. 데몬이 없을 때만 직접 1회 수집합니다.

    Args:
        minutes: 수집 대상 시간 범위(분). 기본 60.
        per_channel_limit: 채널당 최대 조회 메시지 수. 기본 500.
    """
    from telegram_lens.daemon import is_alive

    if is_alive():
        # 수집 데몬이 세션을 소유 중 — 직접 연결하면 세션 경합(session 충돌)이 난다.
        with db.connect() as conn:
            newest = db.newest_message_date(conn)
            s = db.stats(conn)
        return _json(
            {
                "synced_by": "daemon",
                "note": "백그라운드 수집 데몬이 가동 중이라 직접 sync를 건너뜀(세션 경합 방지).",
                "newest_message": newest,
                "messages": s.get("messages"),
                "mentions": s.get("mentions"),
            }
        )

    result = await run_sync(minutes=minutes, per_channel_limit=per_channel_limit)
    return _json(result)


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_trending(hours: float = 24, top: int = 20, kind: str = "all") -> str:
    """기간 내 텔레그램 언급량 상위 종목을 반환합니다.

    종목코드 매칭 전용 — 거시·지정학·테마(예: "미국 이란", "금리") 질문은 telegram_search 사용.

    Args:
        hours: 집계 시간 범위(시간). 기본 24.
        top: 상위 N개. 기본 20.
        kind: 종목 종류 — "stock"(개별주만)/"etf"(ETF만)/"all"(전체). 기본 all.
    """
    return _json(_stocks_payload(queries.trending(hours=hours, top=top, kind=kind)))


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_momentum(
    hours: float = 6, baseline_hours: float = 72, top: int = 15, kind: str = "all"
) -> str:
    """최근 언급이 기준 구간 대비 급증한 종목(새 내러티브)을 반환합니다.

    종목코드 매칭 전용 — 거시·지정학·테마(예: "미국 이란", "금리") 질문은 telegram_search 사용.

    Args:
        hours: 최근 구간(시간). 기본 6.
        baseline_hours: 비교 기준 구간(시간). 기본 72.
        top: 상위 N개. 기본 15.
        kind: 종목 종류 — "stock"(개별주만)/"etf"(ETF만)/"all"(전체). 기본 all.
    """
    return _json(
        _stocks_payload(
            queries.momentum(
                hours=hours, baseline_hours=baseline_hours, top=top, kind=kind
            )
        )
    )


_BRIEFING_PLAYBOOK = (
    "이 데이터로 '나에게 보낼' 시장 브리핑을 작성해 telegram_send_me 로 보내세요.\n"
    "[메시지 2개로 분리 — 섹션마다 1차 소스가 다릅니다]\n"
    "1) 오늘 시황(거시): '사실'(코스피/코스닥·미국 지수 종가, 공식 수치)은 StockLens(SL) 우선으로 "
    "잡으세요 — SL 수치는 정확하니 웹으로 다시 확인하지 마세요(환각·구버전 방지). SL이 못 주는 것"
    "(매크로 일정·이벤트, 글로벌/지정학)만 웹으로 보강. 거기에 macro_거시버즈로 '국내가 그 매크로를 "
    "어떻게 해석·베팅하는지'를 2차로 얹어 종합하세요. 오늘 매크로 이벤트(FOMC·PCE·CPI·GDP·고용·관세 "
    "등)가 있으면 거시 흐름·해석을 종목 얘기로 건너뛰지 말고 충분히 짚으세요.\n"
    "2) 텔레그램 버즈(종목): 텔레그램을 1차로 '갑자기 급증'(momentum_급증)·'많이 언급'(trending_많이언급)과 "
    "왜 거론되는지·근거 채널을 잡고, 그 종목들의 정확한 시세·수급·펀더멘털은 StockLens(SL)로, 공시는 "
    "DartLens(DL)로 확인하세요. 텔레그램이 '무엇을 볼지'를 정하고 SL/DL이 숫자를 확정합니다.\n"
    "   · 급증 노이즈 거르기: momentum_급증 에서 listy_noise=true 인 종목은 '오늘의 리포트·상한가 "
    "리스트·지분공시 정리' 같은 여러 종목 나열형 메시지에 한 줄 끼인 것(실제 논의 아님)이니 빼세요. "
    "listy_noise=false 인 종목만, samples 원문을 읽고 왜 거론되는지 근거를 들어 짚으세요"
    "('노이즈 가능성'으로 얼버무리지 말고 원문으로 판단).\n"
    "   · 평이한 표현: '베이스라인'·'N배 스파이크'·'평소 대비 N배' 같은 전문용어·배수 수치는 쓰지 "
    "마세요(사용자가 모름). '평소 거의 안 나오다 오늘 N개 채널에서 거론' 처럼 일반인 말로.\n"
    "3) 보면 좋은 글·자료(읽을거리_links): 확산 높은 심층글·첨부 리포트 '후보'입니다. 전부 나열하지 "
    "말고, 오늘 시황·버즈와 관련 있고 읽을 가치 높은 것만 3~5개로 추려 '한 줄 내용 → URL' 형식으로 "
    "주세요(plain text라 URL 그대로 — 텔레그램이 자동 링크, 마크다운 [이동] 금지). has_file 이면 "
    "파일명도 적고. 모바일에서 원문으로 바로 점프하는 용도.\n"
    "[작성 원칙]\n"
    "- plain text. 마크다운 기호(**, |, # 등) 쓰지 마세요 — 텔레그램에 그대로 보입니다.\n"
    "- 수치·사실은 데이터에 있는 것만. 출처에 없는 숫자를 지어내지 말고, 근거가 약하면 '~설/언급'.\n"
    "- 소스 우선순위(사실/수치): 지수·시세·수급·펀더멘털=StockLens, 공시=DartLens, 이 둘이 못 주는 "
    "것(매크로 일정·글로벌·지정학)만 웹. SL/DL 이 준 수치는 정확하니 웹 재확인 금지. 해석·테마=텔레그램.\n"
    "- 한국어, 간결한 불릿. 특정 종목 매수/매도 추천이 아니라 '정보 정리'로.\n"
    "- 보내기 전에 한글 오타·깨진 글자를 검토해 완성하고, telegram_send_me 는 단 한 번만 "
    "호출하세요(전송 후 재검토·재발송 금지)."
)

# 거시 키워드 — trending/momentum 은 종목코드 기반이라 거시(Fed·PCE·금리 등)를 못 잡는다.
# 시황(거시) 섹션에 '데이터 앵커'를 주려고 search 로 끌어온다.
_MACRO_TERMS = [
    "FOMC", "연준", "PCE", "CPI", "GDP", "고용",
    "금리", "환율", "국채", "나스닥", "관세", "유가",
]


def _macro_snippet(text: str, term: str, width: int = 150) -> str:
    """매칭된 키워드 주변을 보여줘 '왜 거시인지'가 드러나게(앞부분만 자르면 맥락 손실)."""
    idx = text.find(term)
    if idx < 0:
        return text[:width]
    start = max(0, idx - 40)
    seg = text[start:start + width]
    return ("…" + seg if start > 0 else seg) + ("…" if start + width < len(text) else "")


def _macro_buzz(hours: float, limit: int = 12) -> list[dict]:
    """텔레그램에서 거시 키워드 언급을 모아 시황(거시) 섹션 앵커로 반환(최근순 상위 N)."""
    seen: dict = {}
    for term in _MACRO_TERMS:
        try:
            res = queries.search_messages(term, hours=hours, limit=4)
        except Exception:  # noqa: BLE001 — 한 키워드 실패가 브리핑을 막으면 안 됨
            continue
        for r in res.get("results", []):
            txt = " ".join((r.get("text") or "").split())
            key = (r.get("date"), txt[:40])
            if not txt or key in seen:
                continue
            seen[key] = {
                "term": term,
                "date": r.get("date"),
                "channel": r.get("channel"),
                "text": _macro_snippet(txt, term),
                "telegram_link": r.get("telegram_link"),
            }
    items = sorted(seen.values(), key=lambda x: x.get("date") or "", reverse=True)
    return items[:limit]


def _list_noise_density(hours: float, codes: list[str]) -> dict:
    """종목별 '한 메시지당 평균 동시언급 종목수'. 높으면(>=8) 일일 나열형 메시지(오늘의 리포트·
    상한가 리스트·지분공시 정리)에 한 줄 끼인 것 = 실제 논의가 아닌 저신뢰 노이즈. 낮으면 포커스."""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    recent_cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    qmarks = ",".join("?" * len(codes))
    try:
        with db.connect() as conn:
            rows = conn.execute(
                f"""
                WITH mc AS (
                    SELECT message_id, COUNT(DISTINCT code) n FROM mentions
                    WHERE date >= ? GROUP BY message_id
                )
                SELECT men.code, AVG(mc.n) avg_codes
                FROM mentions men JOIN mc ON mc.message_id = men.message_id
                WHERE men.date >= ? AND men.code IN ({qmarks})
                GROUP BY men.code
                """,
                [recent_cut, recent_cut, *codes],
            ).fetchall()
        return {r["code"]: r["avg_codes"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _reading_list(hours: float, limit: int = 8) -> list[dict]:
    """그 시간대 '보면 좋은 글'(확산 높은 심층글)·첨부 리포트(document)를 링크와 함께 모은다.

    모바일에서 원문으로 바로 점프하라고 브리핑에 붙인다. forwards(확산)가 핵심 신호.
    잡담(gossip) 티어·짧은 글·인라인 사진(photo)은 제외.
    """
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        with db.connect() as conn:
            rows = conn.execute(
                """
                SELECT m.text, m.msg_id, m.channel_id, m.media_type, m.file_name,
                       m.forwards, c.title channel, c.username, t.tier
                FROM messages m
                LEFT JOIN channels c ON c.id = m.channel_id
                LEFT JOIN channel_tier t ON t.channel_id = m.channel_id
                WHERE m.date >= ? AND m.text != ''
                  AND (LENGTH(m.text) > 120 OR m.media_type = 'document')
                  AND (t.tier IS NULL OR t.tier != 'gossip')
                ORDER BY m.forwards DESC
                LIMIT ?
                """,
                (cut, limit),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        link = queries._tg_link(r["username"], r["channel_id"], r["msg_id"])
        if not link:
            continue
        is_doc = r["media_type"] == "document"
        out.append(
            {
                "snippet": " ".join((r["text"] or "").split())[:60],
                "channel": r["channel"],
                "forwards": r["forwards"],
                "has_file": bool(r["file_name"]) and is_doc,
                "file_name": r["file_name"] if is_doc else None,
                "link": link,
            }
        )
    return out


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_briefing(hours: float = 12) -> str:
    """'오늘 브리핑 / 장전 / (텔레그램) 시황 / 시장 브리핑' 등을 요청받으면 호출하세요.

    시장 브리핑용 텔레그램 언급 데이터를 한 번에 모아 반환합니다:
    - trending_많이언급: 기간 내 '많이 언급된' 종목
    - momentum_급증: 평소 대비 '갑자기 급증한' 종목(새 내러티브)

    반환값의 _playbook 지침대로 plain-text 메시지를 작성해 telegram_send_me 로 '한 번만' 보내세요.
    내용 작성·전송 규칙은 _playbook 과 telegram_send_me docstring 을 따르세요.

    Args:
        hours: 집계 시간 범위(시간). 장전이면 12 권장(간밤~아침). 기본 12.
    """
    baseline = max(hours * 6, 72)
    trending = queries.trending(hours=hours, top=15, kind="all")
    momentum = queries.momentum(hours=hours, baseline_hours=baseline, top=12, kind="all")
    etf = load_etf_codes()
    for s in trending:
        s["is_etf"] = s.get("code") in etf
    # 급증 종목 정제: '나열형 리스트 끼임' 노이즈를 표시하고, 베이스라인/배수 같은 전문용어
    # 필드는 빼서(평이한 카운트만 남김) 그대로 echo 되지 않게 한다.
    dens = _list_noise_density(hours, [s.get("code") for s in momentum])
    keep = {"code", "name", "recent_mentions", "recent_channels", "is_new", "samples"}
    slim_momentum = []
    for s in momentum:
        avg = dens.get(s.get("code"))
        d = {k: s[k] for k in keep if k in s}
        d["is_etf"] = s.get("code") in etf
        d["stocks_per_msg_avg"] = round(avg, 1) if avg else None
        d["listy_noise"] = bool(avg and avg >= 8)
        slim_momentum.append(d)
    return _json(
        {
            "_playbook": _BRIEFING_PLAYBOOK,
            "window_hours": hours,
            "macro_거시버즈": _macro_buzz(hours),
            "trending_많이언급": trending,
            "momentum_급증": slim_momentum,
            "읽을거리_links": _reading_list(hours),
        }
    )


def _resolve_code(query: str) -> tuple[str | None, str]:
    """종목명/코드 입력을 (code, name) 으로 해석. 못 찾으면 (None, query). (stocks 공용)"""
    return resolve_code(query)


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_velocity(
    query: str | None = None,
    bucket_minutes: int = 30,
    window_hours: float = 6,
    spike_min: int = 5,
    top: int = 15,
) -> str:
    """종목별 언급의 시간대별 흐름과 급등(velocity)을 반환합니다.

    시간 버킷별 독립 언급을 집계해 직전 대비 증가율과 spike 여부를 봅니다.
    베이스라인 배율(baseline_ratio) 동봉.

    Args:
        query: 종목명/6자리 코드(생략 시 velocity 상위 top 종목).
        bucket_minutes: 시간 버킷 크기(분). 기본 30.
        window_hours: 집계 윈도우(시간). 기본 6.
        spike_min: 최근 버킷 급등 임계값(건수). 기본 5.
        top: query 미지정 시 상위 N개. 기본 15.
    """
    code = None
    if query:
        code, _ = _resolve_code(query)
        if code is None:
            return f"⚠️ '{query}' 종목을 사전에서 찾지 못했습니다."
    return _json(
        _stocks_payload(
            queries.buzz_velocity(
                code=code,
                bucket_minutes=bucket_minutes,
                window_hours=window_hours,
                spike_min=spike_min,
                top=top,
            )
        )
    )


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_timeline(
    query: str, hours: float = 72, bucket_minutes: int = 60
) -> str:
    """특정 종목의 버즈 전개(타임라인)를 반환합니다.

    이 종목이 언제 어느 채널에서 처음 터져 어떻게 번졌나(종단): 최초 언급 채널·시각,
    시간대별 독립 언급·확산 채널 수·velocity·베이스라인 배율·원문 샘플.

    종목코드 매칭 전용 — 거시·지정학·테마(예: "미국 이란", "금리") 질문은 telegram_search 사용.

    Args:
        query: 종목명 또는 6자리 종목코드.
        hours: 윈도우(시간). 기본 72.
        bucket_minutes: 시간 버킷 크기(분). 기본 60.
    """
    code, name = _resolve_code(query)
    if code is None:
        return f"⚠️ '{query}' 종목을 사전에서 찾지 못했습니다."
    return _json(
        {
            "_guidance": _WHY_GUIDANCE,
            "is_etf": code in load_etf_codes(),
            "timeline": queries.stock_timeline(
                code=code, name=name, hours=hours, bucket_minutes=bucket_minutes
            ),
        }
    )


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_buzz_score(
    window_hours: float = 24,
    only_types: list[str] | None = None,
    exclude_gossip: bool = False,
    sentiment: str | None = None,
    top: int = 20,
    kind: str = "all",
) -> str:
    """종목별 종합 버즈 스코어(독립언급×tier×확산×velocity). 감성·유형 필터 지원.

    종목코드 매칭 전용 — 거시·지정학·테마(예: "미국 이란", "금리") 질문은 telegram_search 사용.

    Args:
        window_hours: 집계 윈도우(시간). 기본 24.
        only_types: 포함할 메시지 유형(예: ["report"]). 생략 시 전체.
        exclude_gossip: only_types 미지정 시 gossip 제외. 기본 False.
        sentiment: positive/negative/neutral 중 하나만. 생략 시 전체.
        top: 상위 N개. 기본 20.
        kind: 종목 종류 — "stock"(개별주만)/"etf"(ETF만)/"all"(전체). 기본 all.
    """
    return _json(
        _stocks_payload(
            queries.buzz_score(
                window_hours=window_hours,
                only_types=only_types,
                exclude_gossip=exclude_gossip,
                sentiment=sentiment,
                top=top,
                kind=kind,
            )
        )
    )


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_stock_buzz(query: str, hours: float = 24, samples: int = 8) -> str:
    """특정 종목의 텔레그램 언급 요약과 원문 샘플을 반환합니다.

    Args:
        query: 종목명 또는 6자리 종목코드.
        hours: 집계 시간 범위(시간). 기본 24.
        samples: 원문 샘플 개수. 기본 8.
    """
    by_code = load_stocks()
    code = None
    name = query
    if query in by_code:  # 코드로 들어옴
        code, name = query, by_code[query]
    else:  # 이름으로 들어옴 — 부분일치 우선
        for c, n in by_code.items():
            if n == query:
                code, name = c, n
                break
        if code is None:
            for c, n in by_code.items():
                if query in n:
                    code, name = c, n
                    break
    if code is None:
        return f"⚠️ '{query}' 종목을 사전에서 찾지 못했습니다."
    result = queries.stock_buzz(code=code, name=name, hours=hours, samples=samples)
    if isinstance(result, dict):
        result["is_etf"] = code in load_etf_codes()
    return _json(result)


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_messages(
    channel: str | None = None, hours: float = 6, limit: int = 30
) -> str:
    """원문 메시지를 그대로 조회합니다(drill-down).

    Args:
        channel: 채널 username(@ 제외). 생략 시 전체 채널.
        hours: 시간 범위(시간). 기본 6.
        limit: 최대 메시지 수. 기본 30.
    """
    return _json(queries.recent_messages(channel_username=channel, hours=hours, limit=limit))


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_search(
    query: str, hours: float = 72, limit: int = 30, channel: str | None = None
) -> str:
    """원문 메시지를 키워드/주제로 전문검색합니다('내용' 축 도구).

    종목코드가 안 붙은 거시·산업·테마 글도 본문 키워드로 찾습니다(예: "반도체 HBM",
    "금리 인하"). 여러 단어는 공백 구분 AND 매칭, 3글자 이상이 정확·빠름.

    Args:
        query: 검색 키워드(여러 단어는 공백 구분, AND 매칭).
        hours: 검색 시간 범위(시간). 기본 72.
        limit: 최대 결과 수. 기본 30.
        channel: 특정 채널 username(@ 제외)으로 한정. 생략 시 전체.
    """
    return _json(
        queries.search_messages(query=query, hours=hours, limit=limit, channel=channel)
    )


@mcp.tool()
@safe_tool
async def telegram_classify_channels(
    sample: int = 80, threshold: float = 0.05, min_mentions: int = 3
) -> str:
    """가입한 모든 채널을 스캔해 채널별 '종목 언급 밀도'를 측정·리포트합니다.

    어느 채널이 종목 위주이고 어느 채널이 거시·잡담 위주인지 보여주는 진단 도구.
    수집 대상을 제한하지는 않습니다(전 채널 자동 포함). 전 채널을 훑어 느리니 필요할 때만.

    Args:
        sample: 채널당 샘플링 메시지 수. 기본 80.
        threshold: 주식채널 판정 밀도(0~1). 기본 0.05(5%).
        min_mentions: 최소 누적 언급 수. 기본 3.
    """
    result = await run_classification(
        sample=sample, threshold=threshold, min_mentions=min_mentions
    )
    # 응답이 과대해지지 않게 채널 리스트는 상위/하위 요약만 보여준다.
    chans = result.pop("channels")
    result["stock_channel_list"] = [
        {"title": c["title"], "username": c["username"], "density": c["density"],
         "mentions": c["mentions"]}
        for c in chans if c["is_stock"]
    ]
    result["filtered_examples"] = [
        {"title": c["title"], "username": c["username"], "density": c["density"]}
        for c in chans if not c["is_stock"]
    ][:15]
    return _json(result)


@mcp.tool()
@safe_tool
async def telegram_channels() -> str:
    """수집된 채널 목록과 채널별 누적 메시지 수·tier(분류)·weight 를 반환합니다."""
    return _json({"channels": queries.channels()})


_VALID_TIERS = ("analyst", "research", "info", "gossip")


@mcp.tool()
@safe_tool
async def telegram_set_tier(
    channel: str, tier: str, weight: float | None = None, note: str = ""
) -> str:
    """채널 tier(성격)를 수동 지정합니다. 자동시드를 덮어쓰며 이후 재시드가 보존합니다.

    tier 는 버즈 집계의 채널 가중치 근거입니다.

    Args:
        channel: 채널 username(@ 제외) 또는 6자리가 아닌 숫자 channel_id.
        tier: analyst(애널리스트) | research(독립리서치) | info(종합·속보) | gossip(찌라시).
        weight: 가중 계수(생략 시 기본 analyst1.0/research0.8/info0.5/gossip0.3).
        note: 메모(선택).
    """
    tier = tier.strip().lower()
    if tier not in _VALID_TIERS:
        return f"⚠️ tier 는 {_VALID_TIERS} 중 하나여야 합니다. 받은 값: '{tier}'"

    from telegram_lens.tagging import tier_weight

    db.init_db()
    with db.connect() as conn:
        # username 또는 숫자 id 로 채널 해석.
        ch = None
        if channel.lstrip("-").isdigit():
            ch = conn.execute(
                "SELECT id, title, username FROM channels WHERE id = ?", (int(channel),)
            ).fetchone()
        if ch is None:
            uname = channel.lstrip("@")
            ch = conn.execute(
                "SELECT id, title, username FROM channels WHERE username = ?", (uname,)
            ).fetchone()
        if ch is None:
            return f"⚠️ '{channel}' 채널을 찾지 못했습니다(username 또는 channel_id)."

        w = float(weight) if weight is not None else tier_weight(tier)
        db.upsert_channel_tier(conn, ch["id"], tier, w, source="manual", note=note)
    return _json(
        {
            "channel_id": ch["id"],
            "title": ch["title"],
            "username": ch["username"],
            "tier": tier,
            "weight": w,
            "source": "manual",
        }
    )


@mcp.tool()
@safe_tool
async def telegram_fp_candidates(
    days: float = 7, max_name_len: int = 3, min_count: int = 3, top: int = 40
) -> str:
    """오탐(잘못 잡힌 종목명) 후보를 반환합니다.

    코드 없이 이름만으로 자주 잡힌 짧은 종목명 → 일반명사/은어 충돌 의심.
    검토 후 telegram_block_name 으로 차단 목록에 추가하세요.

    Args:
        days: 분석 기간(일). 기본 7.
        max_name_len: 검사할 최대 이름 길이. 기본 3.
        min_count: 최소 이름단독 매칭 수. 기본 3.
        top: 상위 N개. 기본 40.
    """
    return _json(
        discover.false_positive_candidates(
            days=days, max_name_len=max_name_len, min_count=min_count, top=top
        )
    )


@mcp.tool()
@safe_tool
async def telegram_alias_candidates(days: float = 7, min_count: int = 2, top: int = 40) -> str:
    """누락된 별칭 후보를 반환합니다.

    텍스트에 `이름(123456)` 형태로 나오지만 현재 사전이 그 이름을 해당 코드로
    매칭하지 못하는 토큰. 코드가 정답을 알려주므로 고정밀. 검토 후
    telegram_add_alias 로 등록하세요.

    Args:
        days: 분석 기간(일). 기본 7.
        min_count: 최소 등장 횟수. 기본 2.
        top: 상위 N개. 기본 40.
    """
    return _json(discover.alias_candidates(days=days, min_count=min_count, top=top))


@mcp.tool()
@safe_tool
async def telegram_add_alias(alias: str, code: str) -> str:
    """별칭을 사전에 등록합니다(통용어/약어 → 6자리 코드). 즉시 반영됩니다.

    Args:
        alias: 텔레그램에서 쓰이는 통용어/약어 (예: 현대차).
        code: 6자리 종목코드 (예: 005380).
    """
    result = add_alias(alias, code)
    reset_index()
    return _json({"added": result})


@mcp.tool()
@safe_tool
async def telegram_block_name(code: str, note: str = "") -> str:
    """종목을 모호어 차단 목록에 추가합니다(이름 단독 매칭 차단, 코드 동반 시만 인정).

    Args:
        code: 6자리 종목코드 (예: 001680).
        note: 메모(예: '대상 = target/object 충돌').
    """
    result = add_ambiguous(code, note)
    reset_index()
    return _json({"blocked": result})


def main() -> None:
    # 수집은 _lifespan 이 띄우는 별도 자식 데몬이 담당(자동시작 레지스트리 없음).
    db.init_db()
    mcp.run()


if __name__ == "__main__":
    main()
