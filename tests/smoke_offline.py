"""오프라인 스모크 테스트 — 로그인/네트워크 없이 파이프라인 검증.

임시 TELEGRAMLENS_HOME 에 가짜 메시지를 넣고 추출→집계가 도는지 확인.
Phase 1(고도화) 검증도 포함: 스키마 마이그레이션, 룰베이스 태깅, tier 시드, 베이스라인.
"""

import os
import tempfile
from datetime import datetime, timezone

# 임시 데이터 디렉토리로 격리 (import 전에 설정)
_TMP = tempfile.mkdtemp(prefix="tglens_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

from telegram_lens import db, queries  # noqa: E402
from telegram_lens.stocks import refresh_stocks, _SEED  # noqa: E402
from telegram_lens.extract import extract_mentions, reset_index  # noqa: E402
from telegram_lens import tagging  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def check_schema() -> None:
    print("\n=== 스키마 마이그레이션 (v2) ===")
    db.init_db()
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        for c in db._MESSAGES_V2_COLUMNS:
            _assert(c in cols, f"messages.{c} 컬럼 존재")
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for t in ("channel_tier", "stock_baseline", "message_views_log"):
            _assert(t in tables, f"{t} 테이블 존재")
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        _assert(ver == db._SCHEMA_VERSION, f"user_version == {db._SCHEMA_VERSION}")


def check_tagging() -> None:
    print("\n=== 룰베이스 태깅 ===")
    _assert(tagging.tag_sentiment("목표주가 상향, 강세 기대") == "positive", "긍정 감성")
    _assert(tagging.tag_sentiment("실적 부진에 급락 우려") == "negative", "부정 감성")
    _assert(tagging.tag_sentiment("급등하다 급락, 혼조") == "neutral", "혼재 → neutral")
    _assert(tagging.tag_sentiment("그냥 잡담") == "neutral", "무신호 → neutral")

    _assert(tagging.tag_msg_type("아무말", 0, "gossip") == "gossip", "tier=gossip → gossip")
    _assert(tagging.tag_msg_type("[속보] 어쩌고", 1, None) == "breaking", "[속보] → breaking")
    _assert(tagging.tag_msg_type("09:31 장중 특징주", 1, None) == "breaking", "시각패턴 → breaking")
    _assert(
        tagging.tag_msg_type("삼성전자 목표주가 10만원", 1, None) == "report",
        "목표주가 → report",
    )
    _assert(tagging.tag_msg_type("리포트 요약", 0, "analyst") == "report", "tier=analyst → report")
    _assert(tagging.tag_msg_type("ㅎㅇ", 0, None) == "chat", "짧고 코드없음 → chat")

    print("  tier 휴리스틱:")
    _assert(tagging.classify_tier("[키움 반도체] 김소원") == "analyst", "증권사명 → analyst")
    _assert(tagging.classify_tier("스몰인사이트리서치") == "research", "리서치 → research")
    _assert(tagging.classify_tier("실시간 단타방") == "info", "그 외 → info")


def check_tier_seed_and_manual() -> None:
    print("\n=== tier 시드 + 수동 보존 ===")
    with db.connect() as conn:
        db.upsert_channel(conn, 2001, "[미래에셋 IT] 박준서", "miraeit", 3000)
        db.upsert_channel(conn, 2002, "동네 잡담방", "chitchat", 50)
        n = tagging.seed_channel_tiers(conn, only_missing=True)
        _assert(n >= 2, f"휴리스틱 시드 {n}개 분류")
        tiers = db.channel_tiers(conn)
        _assert(tiers[2001]["tier"] == "analyst", "증권사 채널 → analyst 시드")
        _assert(tiers[2001]["source"] == "heuristic", "자동시드 source=heuristic")

        # 수동 override 후 재시드해도 보존되는지
        db.upsert_channel_tier(conn, 2001, "gossip", 0.3, source="manual", note="test")
        tagging.seed_channel_tiers(conn, only_missing=False)  # 강제 재분류
        tiers = db.channel_tiers(conn)
        _assert(tiers[2001]["tier"] == "gossip", "수동 분류는 재시드가 덮어쓰지 않음")
        _assert(tiers[2001]["source"] == "manual", "source=manual 보존")


def check_pipeline() -> None:
    print("\n=== 수집 파이프라인(태그 저장 + 노출) ===")
    samples = [
        "오늘 삼성전자 005930 대량 매수 들어왔다는 찌라시. SK하이닉스도 같이 간다",
        "카카오 035720 신저가... 급락 손절각인가",
        "삼성전자 또 외인 매수 / 현대차 실적 서프라이즈 기대",
        "그냥 잡담 메시지 종목 없음 123 가나다",
        "기아 목표주가 상향, 카카오 반등 시도",
    ]
    with db.connect() as conn:
        db.upsert_channel(conn, 1001, "테스트찌라시방", "test_jjirashi", 12000)
        tagging.seed_channel_tiers(conn, only_missing=True)
        tier_map = db.channel_tiers(conn)
        for i, text in enumerate(samples):
            date = datetime.now(timezone.utc).isoformat()
            mentions = extract_mentions(text)
            tier = (tier_map.get(1001) or {}).get("tier")
            sentiment = tagging.tag_sentiment(text)
            msg_type = tagging.tag_msg_type(text, len(mentions), tier)
            rid = db.insert_message(
                conn, 1001, 5000 + i, date, text,
                views=100 + i, forwards=i,
                fwd_from_chat_title="원본채널" if i == 0 else None,
                sentiment=sentiment, msg_type=msg_type,
            )
            print(f"  [{i}] 추출={mentions} sentiment={sentiment} type={msg_type}")
            if rid:
                db.insert_views_log(conn, rid, 1001, "collect", 100 + i, i)
                if mentions:
                    db.insert_mentions(conn, rid, 1001, date, mentions)

    msgs = queries.recent_messages(hours=24, limit=10)
    _assert(len(msgs) >= 5, "recent_messages 반환")
    _assert(
        all("sentiment" in m and "msg_type" in m for m in msgs),
        "recent_messages 에 sentiment/msg_type 노출",
    )
    _assert(any(m.get("views") for m in msgs), "views 노출")
    _assert(any(m.get("forwarded_from") for m in msgs), "forwarded_from 노출")

    buzz = queries.stock_buzz("005930", "삼성전자", hours=24, samples=5)
    _assert(buzz["samples"], "stock_buzz 샘플 반환")
    _assert("sentiment" in buzz["samples"][0], "stock_buzz 샘플에 sentiment")


def check_baseline_and_views_query() -> None:
    print("\n=== 베이스라인 + 조회수 refresh 쿼리 ===")
    with db.connect() as conn:
        n = db.compute_baselines(conn, days=7)
        _assert(n >= 1, f"compute_baselines {n}개")
    trend = queries.trending(hours=24, top=10)
    _assert(trend, "trending 반환")
    _assert("baseline_ratio" in trend[0], "trending 에 baseline_ratio 노출")
    _assert("baseline_avg_7d" in trend[0], "trending 에 baseline_avg_7d 노출")

    # refresh_views 대상 선정 쿼리(오프라인에선 Telegram 호출 없이 쿼리만 검증).
    with db.connect() as conn:
        # 방금 넣은 메시지는 나이가 1h 미만이라 어떤 horizon 도 안 잡혀야 함.
        for horizon in ("1h", "6h", "24h"):
            due = db.messages_needing_view_refresh(conn, horizon, 60, 120, 50)
            _assert(due == [], f"갓 수집한 메시지는 {horizon} refresh 대상 아님")


def check_channels_tier_exposed() -> None:
    print("\n=== channels() tier 노출 ===")
    chans = queries.channels()
    _assert(any("tier" in c for c in chans), "channels() 에 tier 키 노출")


def main() -> None:
    print(f"임시 홈: {_TMP}")
    stocks = refresh_stocks()  # 네트워크 실패해도 시드 폴백
    reset_index()
    print(f"종목 사전: {len(stocks)}개")

    check_schema()
    check_tagging()
    check_tier_seed_and_manual()
    check_pipeline()
    check_baseline_and_views_query()
    check_channels_tier_exposed()

    print("\n=== status ===")
    with db.connect() as conn:
        print(" ", db.stats(conn))

    print("\nOK - 오프라인 파이프라인 + Phase1 고도화 정상")


if __name__ == "__main__":
    main()
