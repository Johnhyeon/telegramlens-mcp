"""TelegramLens MCP 서버.

텔레그램 채널의 종목 언급·내러티브 흐름을 구조화해 Claude에 제공한다.
로그인은 `telegramlens-login` 으로 먼저 끝내야 한다(세션 재사용).
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from telegram_lens import db, discover, queries
from telegram_lens.classify import run_classification
from telegram_lens.config import data_dir, is_logged_in
from telegram_lens.client import NoCredentialsError, NotLoggedInError
from telegram_lens.extract import reset_index
from telegram_lens.stocks import add_alias, add_ambiguous, load_stocks, resolve_code
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
    child = None
    try:
        from telegram_lens.daemon import is_alive, spawn_child

        if is_logged_in() and not is_alive():
            child = spawn_child()
            if child is not None:
                _LOG.info("수집 데몬 자식 프로세스 기동 (pid=%s)", child.pid)
    except Exception as e:  # noqa: BLE001 — 수집 기동 실패가 서버를 막으면 안 됨
        _LOG.warning("수집 데몬 기동 실패: %s", e)
        child = None
    try:
        yield
    finally:
        if child is not None:
            try:
                child.terminate()
            except Exception:  # noqa: BLE001
                pass


def safe_tool(func):
    """예외를 사용자 친화 메시지로 변환."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
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


# 트렌딩/모멘텀은 '빈도'만 알려준다. '왜'는 동봉된 samples(원문)에서만 와야 한다.
_WHY_GUIDANCE = (
    "이 목록의 수치는 '무엇이 얼마나' 언급됐는지(빈도)일 뿐, 급등/트렌드의 '이유'가 "
    "아닙니다. 각 종목의 samples 는 실제 텔레그램 원문입니다. 이유·테마·이슈를 설명할 "
    "때는 반드시 이 samples(또는 telegram_stock_buzz / telegram_messages 로 더 가져온 "
    "원문)에 실제로 적힌 내용만 근거로 쓰세요. 원문에 근거가 없으면 배경지식으로 "
    "추측하지 말고, telegram_stock_buzz 로 해당 종목을 더 확인하거나 '원문상 이유 불명'"
    "이라고 답하세요."
)


def _daemon_pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        from telegram_lens.daemon import _pid_alive

        return _pid_alive(int(pid))
    except Exception:
        return False


# 데이터가 이만큼 오래되면 "신선하지 않음"으로 본다(분). 정상 10분 사이클 여유 위.
_STALE_MIN = 30


def _collecting_notice() -> str | None:
    """초기 캐치업 진행 중(데몬이 수집 중 + DB가 아직 오래됨)이면 안내 문자열,
    아니면 None. 조회 도구가 옛 데이터를 조용히 내보내는 대신 '수집 중'을 알리게 한다.
    """
    hb = data_dir() / "daemon_status.json"
    if not hb.exists():
        return None
    try:
        beat = json.loads(hb.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # 데몬이 지금 한 사이클을 돌리는 중일 때만(= state running) 의미 있음.
    if beat.get("state") != "running" or not _daemon_pid_alive(beat.get("pid")):
        return None
    with db.connect() as conn:
        newest = db.newest_message_date(conn)
    if not newest:
        return "⏳ 텔레그램 데이터를 처음 수집하는 중입니다. 잠시 후(약 1분) 다시 물어봐 주세요."
    try:
        dt = datetime.fromisoformat(newest)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        gap_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except ValueError:
        return None
    if gap_min <= _STALE_MIN:
        return None  # 이미 충분히 최신 — 그냥 답해도 됨
    hours = int(gap_min // 60)
    span = f"약 {hours}시간 전" if hours >= 1 else f"약 {int(gap_min)}분 전"
    return (
        f"⏳ 백그라운드에서 누락분을 수집 중입니다(현재 DB는 {span}까지 반영). "
        "첫 수집 사이클이 끝나면 최신이 반영됩니다 — 잠시 후 다시 물어봐 주세요. "
        "(`telegram_status` 의 collector.last_run 으로 완료 확인)"
    )


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
- trending 의 baseline_ratio: 현재 일평균 언급 / 7일 일평균(1 초과면 평소보다 활발).
- 채널 tier·weight: 같은 언급도 analyst > gossip 로 신뢰도가 다름(가중 근거).
- 중복제거: independent(독립 언급=클러스터 수)가 헤드라인. raw_messages 는 포워드/복붙
  포함 원시 건수, spread_copies·total_forwards 는 확산 강도. 순위는 independent 기준.

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

    # 백그라운드 수집 데몬(별도 자식 프로세스)의 하트비트.
    hb = data_dir() / "daemon_status.json"
    beat = None
    if hb.exists():
        try:
            beat = json.loads(hb.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            beat = None
    if beat and _daemon_pid_alive(beat.get("pid")):
        s["collector"] = {
            "running": True,
            "mode": "별도 자식 프로세스 (Claude 켜진 동안 백그라운드 수집)",
            "state": beat.get("state"),
            "interval_minutes": beat.get("interval_minutes"),
            "last_run": beat.get("last_run"),
            "last_result": beat.get("last_result"),
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

    자동 수집은 기본 7일까지만 합니다. 그보다 오래 비웠을 때(telegram_status 의
    backfill_offer 참고) **사용자가 동의하면** 이 도구로 더 깊은 백필을 요청하세요.
    데몬이 다음 사이클(보통 ~15초 내)에 처리하며, 그동안 telegram_status 로 진행을
    확인할 수 있습니다. 무거운 작업이라 사용자 동의 없이 호출하지 마세요.

    Args:
        days: 소급 수집할 일수(1~90). 예: 14면 최근 14일치를 소급 수집.
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
async def telegram_trending(hours: float = 24, top: int = 20) -> str:
    """기간 내 텔레그램 언급량 상위 종목을 반환합니다.

    Args:
        hours: 집계 시간 범위(시간). 기본 24.
        top: 상위 N개. 기본 20.
    """
    return _json(
        {"_guidance": _WHY_GUIDANCE, "stocks": queries.trending(hours=hours, top=top)}
    )


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_momentum(
    hours: float = 6, baseline_hours: float = 72, top: int = 15
) -> str:
    """최근 언급이 기준 구간 대비 급증한 종목(새 내러티브)을 반환합니다.

    Args:
        hours: 최근 구간(시간). 기본 6.
        baseline_hours: 비교 기준 구간(시간). 기본 72.
        top: 상위 N개. 기본 15.
    """
    return _json(
        {
            "_guidance": _WHY_GUIDANCE,
            "stocks": queries.momentum(
                hours=hours, baseline_hours=baseline_hours, top=top
            ),
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

    독립 언급(같은 글의 포워드/복붙은 1건으로 묶음)을 시간 버킷으로 집계해, 직전 대비
    증가율과 급등 여부를 봅니다. last_bucket(가장 최근 구간)이 spike_min 이상이거나
    증가율이 임계값을 넘으면 spike=true. Phase1 베이스라인 배율(baseline_ratio)도 동봉.

    Args:
        query: 종목명/6자리 코드(생략 시 최근 velocity 상위 top 종목).
        bucket_minutes: 시간 버킷 크기(분). 기본 30.
        window_hours: 집계 윈도우(시간). 기본 6.
        spike_min: 최근 버킷 급등 임계값(독립 언급 건수). 기본 5.
        top: query 미지정 시 상위 N개. 기본 15.
    """
    code = None
    if query:
        code, _ = _resolve_code(query)
        if code is None:
            return f"⚠️ '{query}' 종목을 사전에서 찾지 못했습니다."
    return _json(
        {
            "_guidance": _WHY_GUIDANCE,
            "stocks": queries.buzz_velocity(
                code=code,
                bucket_minutes=bucket_minutes,
                window_hours=window_hours,
                spike_min=spike_min,
                top=top,
            ),
        }
    )


@mcp.tool()
@safe_tool
@warn_if_collecting
async def telegram_timeline(
    query: str, hours: float = 72, bucket_minutes: int = 60
) -> str:
    """특정 종목의 버즈 전개(타임라인)를 반환합니다.

    '어떤 종목들'(trending/velocity)이 아니라 '이 종목이 언제 어느 채널에서 처음 터져
    어떻게 번졌나'(종단)를 봅니다: 최초 언급 채널·시각, 시간대별 독립 언급·확산 채널 수·
    velocity, 베이스라인 대비 배율, 원문 샘플.

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
) -> str:
    """종목별 종합 버즈 스코어를 반환합니다(중복제거·채널 tier·확산·velocity 결합).

    score = 독립 언급 수 × 채널 tier 품질 × 확산 강도 × velocity 배율.
    - 독립 언급(independent): 같은 글의 포워드/복붙은 1건으로 묶음.
    - tier_factor: 운반 채널 신뢰도 평균(analyst 1.0 … gossip 0.3).
    - spread_factor: 복사본·포워드로 퍼진 정도.
    - velocity_mult: 지금 가속 중일수록 가점(1.0~3.0).
    감성·유형 필터로 '리포트만', 'gossip 제외', '긍정 글만' 같은 관점을 줄 수 있습니다.

    Args:
        window_hours: 집계 윈도우(시간). 기본 24.
        only_types: 포함할 메시지 유형만(예: ["report"]). 생략 시 전체.
        exclude_gossip: only_types 미지정 시 gossip(찌라시) 유형 제외. 기본 False.
        sentiment: 특정 감성만(positive/negative/neutral). 생략 시 전체.
        top: 상위 N개. 기본 20.
    """
    return _json(
        {
            "_guidance": _WHY_GUIDANCE,
            "stocks": queries.buzz_score(
                window_hours=window_hours,
                only_types=only_types,
                exclude_gossip=exclude_gossip,
                sentiment=sentiment,
                top=top,
            ),
        }
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
    return _json(queries.stock_buzz(code=code, name=name, hours=hours, samples=samples))


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
    """원문 메시지를 키워드/주제로 전문검색합니다.

    trending·stock_buzz 가 '종목' 축의 도구라면, 이건 '내용' 축의 도구입니다.
    종목 언급이 없는 거시경제 뉴스·반도체 산업 설명글·테마 흐름 등 종목코드가
    안 붙은 글도 본문 키워드로 찾습니다(예: "반도체 HBM", "금리 인하", "관세").

    여러 단어는 공백으로 구분하면 모두 포함(AND)하는 글을 찾습니다. 3글자 이상
    키워드가 더 정확·빠릅니다(2글자 이하는 자동으로 부분일치 폴백). 결과는 실제
    텔레그램 원문이므로, 내용을 설명할 때는 이 원문에 적힌 것만 근거로 쓰세요.

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

    어느 채널이 종목 위주이고 어느 채널이 거시·뉴스·잡담 위주인지 보여주는 진단
    도구입니다. 수집 자체는 가입된 모든 브로드캐스트 채널을 대상으로 하므로(새 채널
    자동 포함), 이 분류가 수집 대상을 제한하지는 않습니다.
    (전 채널을 훑으므로 시간이 걸립니다. 필요할 때만 실행하세요.)

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
    """채널의 tier(성격 분류)를 수동 지정합니다. 휴리스틱 자동시드를 덮어쓰고 보존됩니다.

    tier 는 버즈 집계의 채널 가중치 근거입니다(analyst 가장 신뢰, gossip 가장 낮음).
    수동 지정(source='manual')은 이후 자동 재시드가 절대 덮어쓰지 않습니다.

    Args:
        channel: 채널 username(@ 제외) 또는 6자리가 아닌 숫자 channel_id.
        tier: analyst | research | info | gossip 중 하나.
            - analyst : 증권사 애널리스트 채널
            - research: 독립리서치 채널
            - info    : 종합정보·속보 채널
            - gossip  : 찌라시·커뮤니티 채널
        weight: 가중 계수(생략 시 tier 기본값 analyst1.0/research0.8/info0.5/gossip0.3).
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
