"""종목 이름표와 숫자가 같은 종목·같은 기준을 가리키는가.

실측(2026-09-17, 45채널 수집 DB):
- telegram_stock_buzz 가 summary 에 1건을 싣고도 '무언급일 뿐입니다'라고 적었다.
  건수를 결과에 없는 자리(최상위)에서 읽었고, 가짜 결과로 만든 테스트가 그걸 가렸다.
  그래서 여기서는 실제 queries.stock_buzz 를 임시 DB 위에서 돌린다.
- '엔비디아'가 'ACE 엔비디아밸류체인액티브'로 풀려 ETF 0건을 세고 '무언급'이라 했다
  (NVDA 는 48건). 통용명 41개 중 13개가 이렇게 엉뚱한 국내 종목이 됐다.
- telegram_velocity 는 국내 사전만 봐서 NVDA 에 '사전에서 찾지 못했습니다'라고 했다.
- 추출이 부분 문자열이라 메타버스→META, 애플리케이션→AAPL, 하나마이크론→MU 로 셌다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_lens import db, extract, queries, stocks, us_stocks  # noqa: E402

KR = {
    "005930": "삼성전자",
    "483320": "ACE 엔비디아밸류체인액티브",
    "229500": "노브메타파마",
    "189300": "인텔리안테크",
    "067310": "하나마이크론",
    "329180": "HD현대마린솔루션",
}


@pytest.fixture
def us_seed_only(monkeypatch):
    """SEC 전체 목록을 네트워크로 받으러 가지 않게 시드만 쓴다."""
    monkeypatch.setattr(us_stocks, "load_us_map", lambda refresh=False: us_stocks._seed_map())


def _json(out: str) -> dict:
    # 새 버전 안내가 JSON 뒤에 붙을 수 있다.
    return json.loads(out[: out.rfind("}") + 1])


# ── 해석 순서 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("query,ticker", [
    ("엔비디아", "NVDA"), ("메타", "META"), ("인텔", "INTC"),
    ("마이크론", "MU"), ("테슬라", "TSLA"),
])
def test_korean_alias_beats_kr_partial_match(us_seed_only, query, ticker):
    ent = stocks.resolve_entity(query, by_code=KR)
    assert (ent["market"], ent["code"], ent["entity_status"]) == ("US", ticker, "supported")


@pytest.mark.parametrize("query,code", [
    ("005930", "005930"), ("하나마이크론", "067310"), ("삼성", "005930"),
])
def test_kr_exact_and_partial_still_resolve_to_kr(us_seed_only, query, code):
    ent = stocks.resolve_entity(query, by_code=KR)
    assert (ent["market"], ent["code"]) == ("KR", code)


def test_ascii_abbreviation_keeps_kr_partial_before_us_ticker(monkeypatch):
    """영문 약어는 예전 순서 그대로 - HD 가 Home Depot 으로 가지 않는다."""
    table = us_stocks._seed_map()
    table["HD"] = {"ticker": "HD", "name": "HOME DEPOT, INC.", "aliases": []}
    monkeypatch.setattr(us_stocks, "load_us_map", lambda refresh=False: table)
    ent = stocks.resolve_entity("HD", by_code=KR)
    assert (ent["market"], ent["code"]) == ("KR", "329180")


def test_unknown_and_foreign_market_states(us_seed_only):
    assert stocks.resolve_entity("없는종목명임", by_code=KR)["entity_status"] == "entity_not_found"
    assert stocks.resolve_entity("7203.T", by_code=KR)["entity_status"] == "unsupported_market"


def test_resolve_code_uses_the_same_order(us_seed_only, monkeypatch):
    """봇 명령·내 종목·HTTP API 가 쓰는 resolve_code 도 같은 종목을 고른다."""
    monkeypatch.setattr(stocks, "load_stocks", lambda: KR)
    assert stocks.resolve_code("엔비디아") == ("NVDA", "NVIDIA Corporation")
    assert stocks.resolve_code("하나마이크론") == ("067310", "하나마이크론")
    assert stocks.resolve_code("없는종목명임") == (None, "없는종목명임")


# ── 도구 응답: 실제 쿼리 + 임시 DB ─────────────────────────────────


@pytest.fixture
def server_env(tmp_path, monkeypatch, us_seed_only):
    from telegram_lens import server

    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))
    monkeypatch.setattr(server, "is_licensed", lambda: True)
    monkeypatch.setattr(server, "_collecting_notice", lambda: None)
    monkeypatch.setattr(server, "load_etf_codes", lambda: set())
    monkeypatch.setattr(queries, "load_etf_codes", lambda: set())
    monkeypatch.setattr(server, "load_stocks", lambda: KR)
    monkeypatch.setattr(stocks, "load_stocks", lambda: KR)
    db.init_db()
    return server


_seq = [0]


def _mention(code: str, name: str, ago: timedelta, channel: int = 1) -> None:
    _seq[0] += 1
    when = (datetime.now(timezone.utc) - ago).isoformat()
    with db.connect() as conn:
        db.upsert_channel(conn, channel, f"채널{channel}", f"ch{channel}", 10)
        rid = db.insert_message(conn, channel, _seq[0], when, f"{name} 이야기",
                                cluster_id=f"o:{channel}:{_seq[0]}")
        db.insert_mentions(conn, rid, channel, when, [(code, name)])


def test_stock_buzz_status_agrees_with_its_own_summary(server_env):
    """재현: 168시간 창에 1건 → '무언급'이 붙으면 안 된다. 1시간 창엔 0건 → 무언급."""
    _mention("005930", "삼성전자", timedelta(days=2))

    out = _json(asyncio.run(server_env.telegram_stock_buzz(query="삼성전자", hours=168, samples=3)))
    assert out["summary"]["independent"] == 1
    assert len(out["samples"]) == 1
    assert out["entity_status"] == "supported"
    assert "status_note" not in out

    out = _json(asyncio.run(server_env.telegram_stock_buzz(query="삼성전자", hours=1, samples=3)))
    assert out["summary"]["independent"] == 0
    assert out["entity_status"] == "supported_but_zero_mentions"
    assert "무언급" in out["status_note"]


def test_stock_buzz_korean_alias_counts_the_us_ticker(server_env):
    _mention("NVDA", "NVIDIA Corporation", timedelta(hours=3))

    out = _json(asyncio.run(server_env.telegram_stock_buzz(query="엔비디아", hours=24, samples=3)))
    assert (out["code"], out["market"]) == ("NVDA", "US")
    assert out["summary"]["independent"] == 1
    assert out["entity_status"] == "supported"


def test_velocity_accepts_what_stock_buzz_accepts(server_env):
    _mention("NVDA", "NVIDIA Corporation", timedelta(hours=1))

    out = asyncio.run(server_env.telegram_velocity(query="NVDA", window_hours=6))
    assert not out.startswith("⚠️"), out
    assert [s["code"] for s in _json(out)["stocks"]] == ["NVDA"]


def test_timeline_market_label_follows_the_resolved_entity(server_env):
    _mention("NVDA", "NVIDIA Corporation", timedelta(hours=2))

    out = _json(asyncio.run(server_env.telegram_timeline(query="엔비디아", hours=24)))
    assert out["market"] == "US"
    assert out["timeline"]["code"] == "NVDA"
    assert out["timeline"]["summary"]["independent"] == 1


# ── 수집: 미국 한글 통용명 경계 ────────────────────────────────────


@pytest.fixture
def extractor(monkeypatch, us_seed_only):
    monkeypatch.setattr(extract, "load_stocks", lambda: KR)
    monkeypatch.setattr(extract, "load_source_firms", lambda: set())
    monkeypatch.setattr(extract, "load_aliases", lambda: {})
    monkeypatch.setattr(extract, "load_ambiguous", lambda: set())
    monkeypatch.setattr(extract, "load_us_blocked", lambda: set())
    extract._name_index.cache_clear()
    yield lambda text: dict(extract.extract_mentions(text))
    extract._name_index.cache_clear()


@pytest.mark.parametrize("text,ticker", [
    ("메타버스 테마 강세", "META"),
    ("시술 데이터 기반 메타분석 발표", "META"),
    ("조지아주 메타플랜트 생산능력", "META"),
    ("노브메타파마 상한가", "META"),
    ("AI 애플리케이션 수요 확대", "AAPL"),
    ("블룸버그 인텔리전스는 AI 추론", "INTC"),
    ("인텔리안테크 수주", "INTC"),
    ("하나마이크론 급등", "MU"),
    ("기판은 유니마이크론이 공급", "MU"),
    ("ACE 엔비디아밸류체인액티브 편입", "NVDA"),
])
def test_alias_inside_another_word_is_not_a_us_mention(extractor, text, ticker):
    assert ticker not in extractor(text)


@pytest.mark.parametrize("text,ticker", [
    ("엔비디아향 HBM 공급", "NVDA"),
    ("엔비디아발 훈풍", "NVDA"),
    ("애플향 카메라 모듈", "AAPL"),
    ("퀄컴용 로직칩", "QCOM"),
    ("수요처(구글,메타등)만 확실하게", "META"),
    ("메타가 새 모델 공개", "META"),
    ("마이크론 실적 발표", "MU"),
    ("애플 인텔리전스 기능", "AAPL"),
    ("테슬라칩 양산", "TSLA"),
    ("JP모건 목표가 상향", "JPM"),
])
def test_alias_used_as_the_company_is_still_a_mention(extractor, text, ticker):
    assert ticker in extractor(text)


def test_kr_name_that_contains_an_alias_is_still_the_kr_stock(extractor):
    out = extractor("하나마이크론 급등, 인텔리안테크 수주")
    assert "067310" in out and "189300" in out
    assert "MU" not in out and "INTC" not in out
