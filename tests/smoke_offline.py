"""오프라인 스모크 테스트 — 로그인/네트워크 없이 파이프라인 검증.

임시 TELEGRAMLENS_HOME 에 가짜 메시지를 넣고 추출→집계가 도는지 확인.
Phase 1(고도화) 검증도 포함: 스키마 마이그레이션, 룰베이스 태깅, tier 시드, 베이스라인.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

# 임시 데이터 디렉토리로 격리 (import 전에 설정)
_TMP = tempfile.mkdtemp(prefix="tglens_test_")
os.environ["TELEGRAMLENS_HOME"] = _TMP

from telegram_lens import db, queries  # noqa: E402
from telegram_lens.stocks import refresh_stocks, _SEED  # noqa: E402
from telegram_lens.extract import extract_mentions, reset_index  # noqa: E402
from telegram_lens import tagging, cluster  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  [ok] {msg}")


def check_schema() -> None:
    print("\n=== 스키마 마이그레이션 (v2) ===")
    db.init_db()
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        for c in db._MESSAGES_ADDED_COLUMNS:  # v2 + v3 컬럼 전체
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
    # tier(누가)는 msg_type(내용)과 분리 — analyst 채널의 뉴스단신은 report 아님
    _assert(
        tagging.tag_msg_type("그냥 뉴스 한 줄 링크 첨부 https://x.com", 1, "analyst") != "report",
        "analyst tier 만으론 report 아님(내용 기반)",
    )
    _assert(tagging.tag_msg_type("ㅎㅇ", 0, None) == "chat", "짧고 코드없음 → chat")
    _assert(
        tagging.tag_msg_type("삼성전자 수급 동향 정리한 긴 분석 글입니다", 1, None) == "general",
        "코드 있고 신호 없는 글 → general(폴백)",
    )

    print("  tier 휴리스틱:")
    _assert(tagging.classify_tier("[키움 반도체] 김소원") == "analyst", "증권사명 → analyst")
    _assert(tagging.classify_tier("스몰인사이트리서치") == "research", "리서치 → research")
    _assert(tagging.classify_tier("실시간 단타방") == "info", "그 외 → info")
    _assert(tagging.classify_tier("DB증권 Tech") == "analyst", "'증권' 포함 → analyst")
    # analyst recall: 짧은 증권사명(증권 접미사 없음)도 잡아야 함
    _assert(tagging.classify_tier("[하나 Global ETF] 박승진") == "analyst", "짧은명 '하나' → analyst")
    _assert(tagging.classify_tier("KB전략 이은택의 그림 전략") == "analyst", "짧은명 'KB' → analyst")
    _assert(tagging.classify_tier("한화 유통/의류/지주 이진협") == "analyst", "짧은명 '한화' → analyst")
    # gossip tier: 찌라시/정보방 신호
    _assert(tagging.classify_tier("[찌라시!] 가장 빠른 찌라시") == "gossip", "찌라시 → gossip")
    _assert(tagging.classify_tier("빠르고 정확한 주식정보방") == "gossip", "정보방 → gossip")
    _assert(tagging.tier_weight("gossip") < tagging.tier_weight("info"), "gossip weight < info")

    print("  추출 튜닝 회귀:")
    # 하이닉스 alias → SK하이닉스(000660). '하이닉스' 부분일치가 '이닉스' 오탐을 막음.
    codes = {c for c, _ in extract_mentions("삼성전자와 하이닉스 수급 쏠림 격차")}
    _assert("000660" in codes, "'하이닉스' → SK하이닉스(000660) 매칭")
    # 2글자 영문 홀딩스·NEW 이름단독 차단 등록(영어속보 부분일치 오탐 방지).
    from telegram_lens.stocks import load_ambiguous
    amb = load_ambiguous()
    _assert(
        all(c in amb for c in ("006260", "034730", "078930", "000210", "093050", "160550")),
        "LS/SK/GS/DL/LF/NEW 모호어 차단 등록",
    )
    # 모회사·자회사 이름 포함관계: 자식명(두산로보틱스)만 있으면 코드미확인 부모(두산) 제외
    from telegram_lens.stocks import load_stocks as _ls
    _bc = _ls()
    if "454910" in _bc and "000150" in _bc:
        c2 = {c for c, _ in extract_mentions("젠슨 황 방한, 두산로보틱스와 두산 그룹 로봇 동맹")}
        _assert("454910" in c2 and "000150" not in c2, "자회사만 있으면 모회사(두산) 미집계")
        c3 = {c for c, _ in extract_mentions("두산로보틱스 그리고 두산(000150) 별도 언급")}
        _assert("000150" in c3, "모회사 코드 동반 시 집계 유지")
    else:
        print("  (두산 코드 미적재 — 포함관계 테스트 skip)")


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
                cluster_id=cluster.canonical_key(1001, 5000 + i, None, None),
                text_sig=cluster.text_signature(text),
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


def check_text_signature() -> None:
    print("\n=== text_signature (정규화 서명) ===")
    a = "삼성전자 목표주가 상향! https://t.me/abc 🚀🚀 매수 추천드립니다"
    b = "삼성전자  목표주가 상향  매수 추천드립니다"  # 공백·URL·이모지만 다름
    _assert(cluster.text_signature(a) == cluster.text_signature(b), "장식만 다른 글 동일 서명")
    _assert(cluster.text_signature("짧음") is None, "20자 미만 → None")
    _assert(
        cluster.canonical_key(7, 100, None, None) == "o:7:100", "원본 키 = 자기 자신"
    )
    _assert(
        cluster.canonical_key(8, 200, 7, 100) == "o:7:100", "포워드 키 = 원본으로 수렴"
    )


def check_forward_clustering() -> None:
    print("\n=== 포워드 클러스터링 (원본+포워드 = 1) ===")
    text = "LG에너지솔루션 373220 수주 대박 소식, 강세 기대된다 함께 가자"
    date = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        # 원본(채널 3001, msg 700) + 그 포워드(채널 3002, fwd=3001:700)
        for ch, mid, fwd_chat, fwd_mid in [
            (3001, 700, None, None),
            (3002, 701, 3001, 700),
        ]:
            db.upsert_channel(conn, ch, f"채널{ch}", f"ch{ch}", 1000)
            cid = cluster.canonical_key(ch, mid, fwd_chat, fwd_mid)
            rid = db.insert_message(
                conn, ch, mid, date, text,
                fwd_from_chat_id=fwd_chat, fwd_from_message_id=fwd_mid,
                cluster_id=cid, text_sig=cluster.text_signature(text),
            )
            db.insert_mentions(conn, rid, ch, date, extract_mentions(text))
    trend = {t["code"]: t for t in queries.trending(hours=24, top=50)}
    lg = trend["373220"]
    _assert(lg["independent"] == 1, f"독립 언급=1 (원본+포워드 묶임), got {lg['independent']}")
    _assert(lg["raw_messages"] == 2, f"raw_messages=2, got {lg['raw_messages']}")
    _assert(lg["spread_copies"] == 1, f"spread_copies=1, got {lg['spread_copies']}")


def check_heuristic_merge() -> None:
    print("\n=== 휴리스틱 복붙 병합 ===")
    base = "셀트리온 068270 단독 입수 정보입니다 오늘 장 마감 후 큰 거 터진다 매수"
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        # 다른 두 채널이 거의 동일 텍스트를 10분 간격으로 복붙(포워드 메타 없음).
        for ch, mid, mins, deco in [
            (4001, 800, 0, ""),
            (4002, 801, 10, " 🚀"),  # 장식만 차이 → 같은 서명
        ]:
            d = (now - timedelta(minutes=20 - mins)).isoformat()
            db.upsert_channel(conn, ch, f"찌라시{ch}", f"jj{ch}", 5000)
            text = base + deco
            rid = db.insert_message(
                conn, ch, mid, d, text,
                cluster_id=cluster.canonical_key(ch, mid, None, None),
                text_sig=cluster.text_signature(text),
            )
            db.insert_mentions(conn, rid, ch, d, extract_mentions(text))
        merged = cluster.merge_heuristic_duplicates(conn, window_min=30)
        _assert(merged == 1, f"복붙 1건 병합, got {merged}")
    trend = {t["code"]: t for t in queries.trending(hours=24, top=50)}
    cel = trend["068270"]
    _assert(cel["independent"] == 1, f"복붙 → 독립 언급=1, got {cel['independent']}")
    _assert(cel["raw_messages"] == 2, "raw_messages=2 보존")


def check_velocity() -> None:
    print("\n=== buzz_velocity (시간버킷 + 급등) ===")
    now = datetime.now(timezone.utc)
    code = "000660"  # SK하이닉스
    with db.connect() as conn:
        db.upsert_channel(conn, 5001, "벨로시티방", "velo", 9000)
        # 가장 최근 30분 버킷에 6건(서로 다른 cluster_id) → spike_min=5 초과.
        for k in range(6):
            d = (now - timedelta(minutes=2 + k)).isoformat()
            text = f"SK하이닉스 000660 급등 신호 {k} 매수세 유입 강하게 들어온다"
            rid = db.insert_message(
                conn, 5001, 9000 + k, d, text,
                cluster_id=cluster.canonical_key(5001, 9000 + k, None, None),
                text_sig=cluster.text_signature(text),
            )
            db.insert_mentions(conn, rid, 5001, d, [(code, "SK하이닉스")])
    vel = {v["code"]: v for v in queries.buzz_velocity(bucket_minutes=30, window_hours=6)}
    _assert(code in vel, "velocity 에 종목 포함")
    sk = vel[code]
    _assert(sk["last_bucket"] >= 5, f"최근 버킷 {sk['last_bucket']}건")
    _assert(sk["spike"] is True, "급등 플래그 True")
    _assert("baseline_ratio" in sk, "baseline_ratio 노출")
    # 단일 종목 조회
    one = queries.buzz_velocity(code=code, bucket_minutes=30, window_hours=6)
    _assert(len(one) == 1 and one[0]["code"] == code, "code 지정 시 해당 종목만")


def check_buzz_score() -> None:
    print("\n=== buzz_score (종합 스코어 + 필터) ===")
    now = datetime.now(timezone.utc)
    code = "207940"  # 삼성바이오로직스
    with db.connect() as conn:
        # analyst 채널(가중 1.0)에서 report 3건 + gossip 채널에서 1건.
        db.upsert_channel(conn, 6001, "[키움] 바이오 김OO", "kiwoombio", 5000)
        db.upsert_channel(conn, 6002, "찌라시속보방", "jjsok", 8000)
        tagging.seed_channel_tiers(conn, only_missing=True)
        db.upsert_channel_tier(conn, 6002, "gossip", 0.3, source="manual")
        for k in range(3):
            d = (now - timedelta(minutes=3 + k)).isoformat()
            text = f"삼성바이오로직스 207940 목표주가 상향 리포트 {k} 투자의견 매수 유지"
            rid = db.insert_message(
                conn, 6001, 11000 + k, d, text,
                msg_type="report", sentiment="positive",
                cluster_id=cluster.canonical_key(6001, 11000 + k, None, None),
                text_sig=cluster.text_signature(text),
            )
            db.insert_mentions(conn, rid, 6001, d, [(code, "삼성바이오로직스")])
        # gossip 1건
        dg = (now - timedelta(minutes=5)).isoformat()
        gtext = "삼성바이오로직스 207940 단독 찌라시 카더라 통신 받은 정보 살포"
        rid = db.insert_message(
            conn, 6002, 12000, dg, gtext,
            msg_type="gossip", sentiment="neutral",
            cluster_id=cluster.canonical_key(6002, 12000, None, None),
            text_sig=cluster.text_signature(gtext),
        )
        db.insert_mentions(conn, rid, 6002, dg, [(code, "삼성바이오로직스")])
        db.compute_baselines(conn, days=7)

    full = {s["code"]: s for s in queries.buzz_score(window_hours=24, top=50)}
    _assert(code in full, "buzz_score 에 종목 포함")
    s = full[code]
    _assert(s["independent"] == 4, f"독립 언급 4 (report3+gossip1), got {s['independent']}")
    _assert(s["buzz_score"] > 0, "스코어 양수")
    for key in ("tier_factor", "spread_factor", "velocity_mult", "baseline_ratio"):
        _assert(key in s, f"컴포넌트 {key} 노출")

    # report 만 필터 → gossip 1건 제외, 독립 3.
    rep = {s["code"]: s for s in queries.buzz_score(window_hours=24, only_types=["report"], top=50)}
    _assert(rep[code]["independent"] == 3, f"only report → 독립 3, got {rep[code]['independent']}")
    # gossip 제외 필터도 동일하게 3.
    nog = {s["code"]: s for s in queries.buzz_score(window_hours=24, exclude_gossip=True, top=50)}
    _assert(nog[code]["independent"] == 3, f"exclude_gossip → 독립 3, got {nog[code]['independent']}")
    # report-only(analyst 1.0) tier_factor 가 전체(gossip 섞임)보다 높아야 함.
    _assert(rep[code]["tier_factor"] >= full[code]["tier_factor"], "report-only tier_factor 더 높음")


def check_timeline() -> None:
    print("\n=== stock_timeline (종목 종단 전개) ===")
    now = datetime.now(timezone.utc)
    code = "035420"  # NAVER
    with db.connect() as conn:
        db.upsert_channel(conn, 7001, "최초채널", "firstch", 3000)
        db.upsert_channel(conn, 7002, "확산채널", "spreadch", 4000)
        # 90분 전(최초) 1건, 10분 전 2건(다른 채널) — 서로 다른 버킷·채널.
        plan = [(7001, 13000, 90), (7002, 13001, 10), (7001, 13002, 8)]
        for ch, mid, mins in plan:
            d = (now - timedelta(minutes=mins)).isoformat()
            text = f"NAVER 035420 클라우드 수주 소식 {mid} 강세 기대 매수세 유입"
            rid = db.insert_message(
                conn, ch, mid, d, text,
                cluster_id=cluster.canonical_key(ch, mid, None, None),
                text_sig=cluster.text_signature(text),
            )
            db.insert_mentions(conn, rid, ch, d, [(code, "NAVER")])
        db.compute_baselines(conn, days=7)

    tl = queries.stock_timeline(code, "NAVER", hours=72, bucket_minutes=60)
    _assert(tl["first_mention"] is not None, "first_mention 존재")
    _assert(tl["first_mention"]["channel"] == "최초채널", "최초 언급 채널 정확")
    # 벽시계 정렬이라 경계 위치에 따라 72~73개(now 가 시간 중간이면 +1).
    _assert(72 <= len(tl["timeline"]) <= 73, f"버킷 72~73개(72h/60m), got {len(tl['timeline'])}")
    # 버킷 시작이 벽시계 정렬(60분 → 분 '00')인지 확인
    _assert(tl["timeline"][0]["bucket_start"].endswith(":00 KST"), "버킷 경계 정시 정렬")
    _assert(tl["summary"]["independent"] == 3, f"독립 언급 3, got {tl['summary']['independent']}")
    _assert(tl["summary"]["spreading_channels"] == 2, "확산 채널 2")
    _assert("baseline_ratio" in tl["summary"], "summary 에 baseline_ratio")
    # 최근(10분 이내) 버킷에 언급이 잡혀야 함.
    _assert(tl["timeline"][-1]["independent"] >= 1, "최근 버킷 independent>=1")


def check_http_endpoint() -> None:
    print("\n=== HTTP 외부 조회 엔드포인트 ===")
    import json as _json_mod
    import threading
    import urllib.request
    import urllib.error
    import urllib.parse
    from telegram_lens import api

    httpd = api.make_server("127.0.0.1", 0)  # 포트 0 → 임시 포트
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def get(path):
        path = urllib.parse.quote(path, safe="/?=&")  # 한글 종목명 경로 인코딩
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, _json_mod.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, _json_mod.loads(e.read().decode("utf-8"))

    try:
        st, body = get("/health")
        _assert(st == 200 and body.get("ok") is True, "GET /health 200 ok")
        st, body = get("/trending?hours=24&top=5")
        _assert(st == 200 and "stocks" in body, "GET /trending 200 + stocks")
        st, body = get("/timeline/035420?hours=72")
        _assert(st == 200 and "timeline" in body, "GET /timeline/<code> 200 + timeline")
        st, body = get("/timeline/NAVER")
        _assert(st == 200 and body["code"] == "035420", "종목명으로도 해석(NAVER→035420)")
        st, body = get("/nope")
        _assert(st == 404 and "error" in body, "알 수 없는 경로 404")
        st, body = get("/timeline/없는종목xyz123")
        _assert(st == 400 and "error" in body, "미해석 종목 400")
    finally:
        httpd.shutdown()
    _assert(True, "서버 정상 종료")


def check_reindex() -> None:
    print("\n=== reindex (과거 데이터 소급 재색인) ===")
    from telegram_lens import reindex as rx
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        db.upsert_channel(conn, 8001, "[키움 리서치] 반도체", "kiwoomrx", 2000)
        # 옛 코드처럼 태그·mentions·text_sig 없이 메시지만 삽입(raw insert).
        for mid, text in [
            (21000, "삼성전자 005930 목표주가 상향, 강세 기대 매수 추천 의견입니다"),
            (21001, "그냥 짧은 잡담"),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO messages (channel_id,msg_id,date,text) VALUES (?,?,?,?)",
                (8001, mid, now.isoformat(), text),
            )
        # 재색인 전: 이 메시지들엔 mentions·sentiment 없음
        pre = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id=8001 AND sentiment IS NULL"
        ).fetchone()[0]
        _assert(pre == 2, "재색인 전 sentiment 비어있음")
        res = rx.reindex(conn)
        _assert(res["messages"] >= 2 and res["mentions"] >= 1, "reindex 처리 결과 반환")
        # 재색인 후: 태그·추출 채워짐
        row = conn.execute(
            "SELECT sentiment, msg_type, text_sig, cluster_id FROM messages WHERE channel_id=8001 AND msg_id=21000"
        ).fetchone()
        _assert(row["sentiment"] == "positive", "재색인: 감성 채워짐(positive)")
        _assert(row["msg_type"] == "report", "재색인: msg_type 채워짐(report, 목표주가)")
        _assert(row["text_sig"] is not None and row["cluster_id"], "재색인: text_sig/cluster_id 채워짐")
        men = conn.execute(
            "SELECT COUNT(*) FROM mentions WHERE channel_id=8001"
        ).fetchone()[0]
        _assert(men >= 1, "재색인: mentions 재추출됨")


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
    check_text_signature()
    check_forward_clustering()
    check_heuristic_merge()
    check_velocity()
    check_buzz_score()
    check_timeline()
    check_http_endpoint()
    check_reindex()
    check_channels_tier_exposed()

    print("\n=== status ===")
    with db.connect() as conn:
        print(" ", db.stats(conn))

    print("\nOK - 오프라인 파이프라인 + Phase1 고도화 정상")


if __name__ == "__main__":
    main()
