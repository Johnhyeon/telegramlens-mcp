"""집계 쿼리 — 트렌딩·종목 버즈·모멘텀.

AI에게 raw 덤프 대신 구조화 요약을 준다. 토큰 절약 + 노이즈 제거.
모멘텀 스코어는 단순하지만 의미있게:
    score = 언급 메시지 수 × 채널 다양성 가중
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram_lens import db
from telegram_lens.stocks import load_etf_codes

_KST = ZoneInfo("Asia/Seoul")


# KRX 단축코드 — 6자리 숫자(전통) 또는 신형 영숫자(DDDDAD). 이 모양이면 국내,
# 아니면 미국 티커다(extract 가 code 자리에 한국은 단축코드, 미국은 티커를 넣는다).
_KRX_CODE_RE = re.compile(r"^\d{4}[0-9A-Z]\d$")

# 집계 세그먼트 — 국내주식·미국주식·국내ETF·미국ETF 네 갈래.
SEGMENTS = ("kr_stock", "us_stock", "kr_etf", "us_etf")
SEGMENT_LABELS = {
    "kr_stock": "국내주식",
    "us_stock": "미국주식",
    "kr_etf": "국내ETF",
    "us_etf": "미국ETF",
}


def market_of(code: str) -> str:
    """'KR' 또는 'US'. 코드 모양으로 가른다."""
    return "KR" if _KRX_CODE_RE.match(code or "") else "US"


def segment_of(code: str, name: str | None = None) -> str:
    """네 세그먼트 중 하나. 국내 ETF 는 KRX 목록, 미국 ETF 는 us_stocks 목록으로 판정."""
    if market_of(code) == "KR":
        return "kr_etf" if code in load_etf_codes() else "kr_stock"
    from telegram_lens import us_stocks

    return "us_etf" if us_stocks.is_us_etf(code, name) else "us_stock"


def _segment_predicate(kind: str, market: str):
    """종류(kind)·시장(market) 필터를 합친 code→통과여부 함수. 둘 다 'all' 이면 None.

    kind='stock'(개별주)/'etf'(ETF)/'all', market='KR'/'US'/'all'.
    개별주와 ETF는 언급 성격이 다르고(종목 재료 vs 섹터·테마·자금흐름), 한국과 미국은
    언급량 규모가 달라 한 랭킹에 섞으면 한쪽이 다른 쪽의 자리를 먹는다.
    """
    kind = (kind or "all").lower()
    market = (market or "all").upper()
    if kind not in ("stock", "etf"):
        kind = "all"
    if market not in ("KR", "US"):
        market = "ALL"
    if kind == "all" and market == "ALL":
        return None

    def ok(code: str, name: str | None = None) -> bool:
        seg = segment_of(code, name)
        if market != "ALL" and not seg.startswith(market.lower()):
            return False
        if kind != "all" and not seg.endswith(kind):
            return False
        return True

    return ok


def _kind_predicate(kind: str):
    """예전 이름 — kind 만 보는 필터. _segment_predicate 로 위임한다."""
    return _segment_predicate(kind, "all")


def _take_per_segment(rows, top: int, key=lambda r: r["code"], name_key=None):
    """세그먼트별로 상위 top 개씩 뽑아 국내주식→미국주식→국내ETF→미국ETF 순으로 잇는다.

    한 랭킹에서 top 개를 자르면 언급이 많은 쪽(보통 국내주식)이 정원을 다 먹어
    미국 ETF 같은 작은 세그먼트가 통째로 사라진다. 세그먼트마다 따로 자른다.
    입력 rows 는 이미 점수 내림차순이어야 한다.
    """
    buckets: dict[str, list] = {s: [] for s in SEGMENTS}
    for r in rows:
        nm = name_key(r) if name_key else None
        seg = segment_of(key(r), nm)
        if len(buckets[seg]) < top:
            buckets[seg].append(r)
    return [r for s in SEGMENTS for r in buckets[s]]

# 본문 속 URL. 공백·닫는 괄호·따옴표에서 끊고, 흔한 꼬리 구두점은 제거.
_URL_RE = re.compile(r"https?://[^\s)\]>\"'》」』]+")


def _tg_link(username: str | None, channel_id, msg_id) -> str | None:
    """텔레그램 메시지 딥링크. 공개채널은 t.me/<username>/<msg_id>, 아니면 t.me/c/<id>/<msg_id>."""
    if not msg_id:
        return None
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if channel_id:
        return f"https://t.me/c/{channel_id}/{msg_id}"
    return None


def _media_field(media_type, file_name):
    """첨부 인지 필드. 없으면 None(키 자체 생략하지 않고 None 으로 일관)."""
    if not media_type:
        return None
    return {"type": media_type, "file_name": file_name}


def _extract_urls(text: str | None, limit: int = 5) -> list[str]:
    """본문에서 URL 목록(중복 제거, 순서 보존). 샘플 텍스트가 잘려도 링크는 살아남게 별도 노출."""
    if not text:
        return []
    out: list[str] = []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;…·")
        if u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def _cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _to_kst(iso: str | None) -> str | None:
    """저장된 UTC ISO 문자열을 KST 표시용으로 변환 (예: '2026-06-01 20:15 KST')."""
    if not iso:
        return iso
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")
    except ValueError:
        return iso


# 베이스라인이 이보다 짧은 기간으로 계산됐으면 배율을 내지 않는다. 하루 이틀치 평균은
# '평소'라고 부르기 어렵고, 배율이 크게 흔들린다.
BASELINE_MIN_DAYS = 3


def _baseline_fields(b, independent: int, hours: float) -> dict:
    """베이스라인 관련 필드 묶음. b 는 stock_baseline 행(dict/Row) 또는 None.

    baseline_avg_7d      : 최근 7일(수집된 기간만) 하루 평균 독립 언급 수
    baseline_days_covered: 그 평균을 실제로 나눈 일수(최대 7)
    baseline_computed_at : 베이스라인 계산 시각(KST)
    baseline_ratio       : 지금 창의 하루 평균 독립 언급 / baseline_avg_7d.
                           계산 기간이 BASELINE_MIN_DAYS 일 미만이거나 옛 방식 값이면 None.
    """
    avg = b["avg_7d"] if b is not None else None
    covered = b["covered_days"] if b is not None else None
    computed = b["computed_at"] if b is not None else None
    ratio = None
    if avg and avg > 0 and covered is not None and covered >= BASELINE_MIN_DAYS and hours > 0:
        ratio = round((independent / (hours / 24)) / avg, 2)
    return {
        "baseline_avg_7d": round(avg, 2) if avg else None,
        "baseline_days_covered": round(covered, 1) if covered is not None else None,
        "baseline_computed_at": _to_kst(computed),
        "baseline_ratio": ratio,
    }


def _baseline_rows(conn) -> dict:
    return {
        r["code"]: r
        for r in conn.execute(
            "SELECT code, avg_7d, covered_days, computed_at FROM stock_baseline"
        )
    }


def _recent_snippets(
    conn, code: str, cut: str, n: int,
    only_types: list[str] | None = None, exclude_gossip: bool = False,
    sentiment: str | None = None,
) -> list[dict]:
    """특정 종목의 최근 원문 스니펫 n개. '왜 언급됐나'를 근거로 쓰게 하는 용도.

    only_types/exclude_gossip/sentiment 를 주면 그 필터에 맞는 원문만 샘플로 보여준다
    (예: report 필터 결과의 샘플이 report 글이도록 — 필터-샘플 불일치 방지).
    """
    if n <= 0:
        return []
    where = ["men.code = ?", "men.date >= ?"]
    params: list = [code, cut]
    if only_types:
        where.append("m.msg_type IN (%s)" % ",".join("?" * len(only_types)))
        params += list(only_types)
    elif exclude_gossip:
        where.append("(m.msg_type IS NULL OR m.msg_type != 'gossip')")
    if sentiment:
        where.append("m.sentiment = ?")
        params.append(sentiment)
    params.append(n)
    rows = conn.execute(
        f"""
        SELECT m.date, m.text, m.sentiment, m.msg_type, m.views, m.forwards,
               m.fwd_from_chat_title, m.msg_id, m.channel_id, m.media_type, m.file_name,
               c.title AS channel, c.username
        FROM mentions men
        JOIN messages m ON m.id = men.message_id
        LEFT JOIN channels c ON c.id = men.channel_id
        WHERE {' AND '.join(where)}
        ORDER BY m.date DESC LIMIT ?
        """,
        params,
    ).fetchall()
    out = []
    for r in rows:
        text = " ".join((r["text"] or "").split())
        if len(text) > 180:
            text = text[:180] + "…"
        out.append(
            {
                "date": _to_kst(r["date"]),
                "channel": r["channel"],
                "text": text,
                "links": _extract_urls(r["text"]),  # 잘린 본문과 무관하게 원문 URL 보존
                "telegram_link": _tg_link(r["username"], r["channel_id"], r["msg_id"]),
                "media": _media_field(r["media_type"], r["file_name"]),
                "sentiment": r["sentiment"],
                "msg_type": r["msg_type"],
                "views": r["views"],
                "forwards": r["forwards"],
                "forwarded_from": r["fwd_from_chat_title"],
            }
        )
    return out


def trending(
    hours: float = 24,
    top: int = 20,
    samples_per_stock: int = 1,
    kind: str = "all",
    market: str = "all",
) -> list[dict]:
    """기간 내 언급량 상위 종목 (각 종목의 최근 원문 샘플 동봉).

    kind='stock'/'etf'/'all', market='KR'/'US'/'all' 로 네 세그먼트
    (국내주식·미국주식·국내ETF·미국ETF)를 갈라 조회한다. 둘 다 all 이면
    세그먼트마다 top 개씩 뽑아 잇는다.
    """
    cut = _cutoff(hours)
    pred = _segment_predicate(kind, market)
    with db.connect() as conn:
        # 정렬은 SQL, 종류 필터·top 자르기는 Python(필터를 top 전에 적용해야 정원이 참).
        # 샘플 서브쿼리는 자른 뒤에만 돌려 비용을 top 개로 묶는다.
        rows = conn.execute(
            """
            SELECT men.code, men.name,
                   COUNT(DISTINCT m.cluster_id)  AS independent,
                   COUNT(DISTINCT men.message_id) AS raw_messages,
                   COUNT(DISTINCT men.channel_id) AS channels,
                   COALESCE(SUM(m.forwards), 0)  AS total_forwards,
                   MAX(men.date)                 AS last_seen,
                   b.avg_7d                      AS avg_7d,
                   b.covered_days                AS covered_days,
                   b.computed_at                 AS computed_at
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            LEFT JOIN stock_baseline b ON b.code = men.code
            WHERE men.date >= ?
            GROUP BY men.code
            ORDER BY independent DESC, channels DESC
            """,
            (cut,),
        ).fetchall()
        if pred:
            rows = [r for r in rows if pred(r["code"], r["name"])]
        rows = _take_per_segment(
            rows, top, key=lambda r: r["code"], name_key=lambda r: r["name"]
        )
        out = []
        for r in rows:
            d = dict(r)
            d["last_seen"] = _to_kst(d["last_seen"])
            # 확산 강도: 독립 클러스터 외에 복사/포워드로 더 퍼진 양.
            d["spread_copies"] = d["raw_messages"] - d["independent"]
            # 이상 신호 배율: 현재 구간 독립 언급 일평균 / 최근 7일 독립 언급 일평균.
            base = {k: d.pop(k) for k in ("avg_7d", "covered_days", "computed_at")}
            d.update(_baseline_fields(base, d["independent"], hours))
            d["samples"] = _recent_snippets(conn, d["code"], cut, samples_per_stock)
            out.append(d)
    return out


def momentum(
    hours: float = 6,
    baseline_hours: float = 72,
    top: int = 15,
    samples_per_stock: int = 2,
    kind: str = "all",
    market: str = "all",
) -> list[dict]:
    """최근 구간 언급이 기준 구간 평균 대비 급증한 종목 (급증 구간 원문 샘플 동봉).

    spike = recent_rate / baseline_rate (시간당 독립 언급 비율 비교). 배율이라 기준 구간에
    언급이 없으면 낼 수 없다 - 그때 spike 는 None 이고 is_new 로 가른다.
    is_new = True  : 기준 구간을 DB 가 다 덮는데 그동안 언급이 0건(처음 떠오름)
             False : 기준 구간에 언급이 있었다
             None  : 기준 구간 일부를 DB 가 못 덮는데 그 안에서 0건 - 새로 떴는지 알 수 없다
    baseline_hours_covered = 기준 구간 중 DB 에 실제로 있는 시간(배율 분모).

    kind='stock'/'etf'/'all', market='KR'/'US'/'all' 로 네 세그먼트를 갈라 조회한다.
    둘 다 all 이면 세그먼트마다 top 개씩 뽑아 잇는다. 정렬 순서는 예전과 같다(배율,
    배율이 없으면 최근 언급 수).
    """
    pred = _segment_predicate(kind, market)
    now = datetime.now(timezone.utc)
    recent_cut = (now - timedelta(hours=hours)).isoformat()
    base_cut = (now - timedelta(hours=baseline_hours)).isoformat()

    with db.connect() as conn:
        # 독립 언급(클러스터) 기준 — 같은 글의 포워드/복붙이 spike 를 부풀리지 않게.
        recent = {
            r["code"]: dict(r)
            for r in conn.execute(
                """
                SELECT men.code, men.name,
                       COUNT(DISTINCT m.cluster_id) AS m,
                       COUNT(DISTINCT men.channel_id) AS ch
                FROM mentions men
                JOIN messages m ON m.id = men.message_id
                WHERE men.date >= ?
                GROUP BY men.code
                """,
                (recent_cut,),
            ).fetchall()
        }
        base = {
            r["code"]: r["m"]
            for r in conn.execute(
                """
                SELECT men.code, COUNT(DISTINCT m.cluster_id) AS m
                FROM mentions men
                JOIN messages m ON m.id = men.message_id
                WHERE men.date >= ? AND men.date < ?
                GROUP BY men.code
                """,
                (base_cut, recent_cut),
            ).fetchall()
        }
        first_row = conn.execute("SELECT MIN(date) AS d FROM messages").fetchone()

    # 기준 구간 중 DB 가 실제로 덮는 시간. 설치 직후·새로 붙인 기간이면 기준 구간보다 짧다.
    # 예전엔 늘 (baseline_hours - hours) 로 나눠, 이틀치밖에 없어도 사흘 평균처럼 계산했다.
    first_ts = _parse_ts(first_row["d"]) if first_row and first_row["d"] else None
    recent_ts = now.timestamp() - hours * 3600
    base_ts = now.timestamp() - baseline_hours * 3600
    covered_start = max(base_ts, first_ts) if first_ts is not None else recent_ts
    base_span = max(0.0, (recent_ts - covered_start) / 3600)
    # 한 시간 안쪽 차이는 덮었다고 본다(수집 창 여유).
    fully_covered = first_ts is not None and first_ts <= base_ts + 3600

    out = []
    for code, r in recent.items():
        if pred and not pred(code, r["name"]):
            continue
        recent_rate = r["m"] / hours
        base_count = base.get(code, 0)
        if base_count > 0 and base_span > 0:
            spike = round(recent_rate / (base_count / base_span), 2)
            is_new = False
        else:
            # 배율을 만들 분모가 없다. 언급 수를 배율 자리에 넣지 않는다.
            spike = None
            is_new = True if fully_covered else None
        out.append(
            {
                "code": code,
                "name": r["name"],
                "recent_mentions": r["m"],
                "recent_channels": r["ch"],
                "baseline_mentions": base_count,
                "baseline_hours_covered": round(base_span, 1),
                "spike": spike,
                "is_new": is_new,
            }
        )
    # 순서는 예전과 같다 - 배율이 없는 종목은 최근 언급 수로 줄을 선다.
    out.sort(
        key=lambda x: (
            x["spike"] if x["spike"] is not None else float(x["recent_mentions"]),
            x["recent_mentions"],
        ),
        reverse=True,
    )
    out = _take_per_segment(
        out, top, key=lambda r: r["code"], name_key=lambda r: r["name"]
    )
    # 상위 종목에만 급증 구간 원문 샘플을 붙인다(전체에 붙이면 낭비).
    if samples_per_stock > 0 and out:
        with db.connect() as conn:
            for d in out:
                d["samples"] = _recent_snippets(
                    conn, d["code"], recent_cut, samples_per_stock
                )
    return out


def stock_buzz(code: str, name: str, hours: float = 24, samples: int = 8) -> dict:
    """특정 종목의 언급 요약 + 원문 샘플."""
    cut = _cutoff(hours)
    with db.connect() as conn:
        agg = conn.execute(
            """
            SELECT COUNT(DISTINCT m.cluster_id)  AS independent,
                   COUNT(DISTINCT men.message_id) AS raw_messages,
                   COUNT(DISTINCT men.channel_id) AS channels,
                   COALESCE(SUM(m.forwards), 0)  AS total_forwards,
                   MIN(men.date) AS first_seen, MAX(men.date) AS last_seen
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            WHERE men.code = ? AND men.date >= ?
            """,
            (code, cut),
        ).fetchone()

        sample_rows = conn.execute(
            """
            SELECT m.date, m.text, m.sentiment, m.msg_type, m.views, m.forwards,
                   m.fwd_from_chat_title, m.msg_id, m.channel_id, m.media_type,
                   m.file_name, c.title AS channel, c.username
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            LEFT JOIN channels c ON c.id = men.channel_id
            WHERE men.code = ? AND men.date >= ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (code, cut, samples),
        ).fetchall()

    summary = dict(agg) if agg else {}
    if summary:
        summary["spread_copies"] = summary["raw_messages"] - summary["independent"]
        summary["first_seen"] = _to_kst(summary.get("first_seen"))
        summary["last_seen"] = _to_kst(summary.get("last_seen"))
    samples = []
    for r in sample_rows:
        d = dict(r)
        d["date"] = _to_kst(d["date"])
        d["links"] = _extract_urls(d.get("text"))
        d["telegram_link"] = _tg_link(d.pop("username"), d.pop("channel_id"), d.pop("msg_id"))
        d["media"] = _media_field(d.pop("media_type"), d.pop("file_name"))
        samples.append(d)
    return {
        "code": code,
        "name": name,
        "window_hours": hours,
        "summary": summary,
        "samples": samples,
    }


def _parse_ts(iso: str) -> float | None:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def buzz_velocity(
    code: str | None = None,
    bucket_minutes: int = 30,
    window_hours: float = 6,
    spike_min: int = 5,
    growth_threshold: float = 2.0,
    top: int = 15,
) -> list[dict]:
    """종목별 독립 언급(클러스터)을 시간 버킷으로 집계해 직전 대비 증가율·급등을 감지.

    버킷 인덱스 0 = 가장 최근 구간([now-bucket, now]), 1 = 그 직전. last_bucket(최근) 이
    spike_min 이상이거나 growth(=last/prev) 가 growth_threshold 이상이면 급등(spike).
    Phase 1 베이스라인(stock_baseline)과 결합해 평소 대비 배율도 함께 준다.

    Args:
        code: 특정 종목만(생략 시 최근 velocity 상위 top).
        bucket_minutes: 시간 버킷 크기(분).
        window_hours: 집계 윈도우(시간).
        spike_min: 최근 버킷 독립 언급 급등 임계값(건).
        growth_threshold: 직전 대비 증가율 급등 임계값(배).
        top: code 미지정 시 상위 N개.
    """
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    window_cut = (now - timedelta(hours=window_hours)).isoformat()
    bucket_sec = bucket_minutes * 60
    nbuckets = int((window_hours * 60 + bucket_minutes - 1) // bucket_minutes)

    with db.connect() as conn:
        sql = """
            SELECT men.code, men.name, men.date, m.cluster_id
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            WHERE men.date >= ?
        """
        params: list = [window_cut]
        if code:
            sql += " AND men.code = ?"
            params.append(code)
        rows = conn.execute(sql, params).fetchall()
        baselines = _baseline_rows(conn)
        last_collected = db.last_collection_at(conn)

    # 마지막 수집 이후에 시작한 버킷은 '0건'이 아니라 '아직 안 모은 구간'이다(None).
    last_collected_ts = _parse_ts(last_collected) if last_collected else None

    def _uncollected(i: int) -> bool:
        return last_collected_ts is not None and now_ts - (i + 1) * bucket_sec >= last_collected_ts

    # code → {name, buckets: {idx: set(cluster_id)}, clusters}. 버킷별 '독립' 카운트는 set 크기.
    per: dict[str, dict] = {}
    for r in rows:
        ts = _parse_ts(r["date"])
        if ts is None:
            continue
        idx = int((now_ts - ts) // bucket_sec)
        if idx < 0:
            idx = 0
        e = per.setdefault(r["code"], {"name": r["name"], "buckets": {}, "clusters": set()})
        e["buckets"].setdefault(idx, set()).add(r["cluster_id"])
        e["clusters"].add(r["cluster_id"])

    out = []
    for c, e in per.items():
        counts = {i: len(s) for i, s in e["buckets"].items()}
        last = None if _uncollected(0) else counts.get(0, 0)
        prev = None if _uncollected(1) else counts.get(1, 0)
        growth = round(last / max(prev, 1), 2) if last is not None and prev is not None else None
        # 창 전체 독립 언급 = 창 안의 서로 다른 클러스터 수. 버킷별 수를 더하면 경계에
        # 걸친 복사본이 두 번 세진다.
        total = len(e["clusters"])
        out.append(
            {
                "code": c,
                "name": e["name"],
                "bucket_minutes": bucket_minutes,
                "window_hours": window_hours,
                # 오래된→최신 순 시계열(독립 언급 수). 마지막 수집 이후 구간은 None.
                "series": [
                    None if _uncollected(i) else counts.get(i, 0)
                    for i in range(nbuckets - 1, -1, -1)
                ],
                "last_bucket": last,
                "prev_bucket": prev,
                "delta": last - prev if last is not None and prev is not None else None,
                "growth": growth,
                "spike": bool(
                    last is not None
                    and (last >= spike_min or (growth is not None and growth >= growth_threshold))
                ),
                "window_independent": total,
                **_baseline_fields(baselines.get(c), total, window_hours),
            }
        )
    # 급등 우선, 그다음 최근 버킷·증가율 순.
    out.sort(key=lambda x: (x["spike"], x["last_bucket"], x["growth"]), reverse=True)
    if not code:
        out = out[:top]
    return out


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


def buzz_score(
    window_hours: float = 24,
    only_types: list[str] | None = None,
    exclude_gossip: bool = False,
    sentiment: str | None = None,
    bucket_minutes: int = 30,
    top: int = 20,
    samples_per_stock: int = 1,
    kind: str = "all",
    market: str = "all",
    sort_by: str = "buzz_score",
    min_independent: int = 0,
) -> list[dict]:
    """종목별 종합 버즈 스코어 (Phase 2-3).

    score = independent × tier_factor × spread_factor × velocity_mult
      - independent   : 윈도우 내 독립 언급(클러스터) 수 — 포워드/복붙 1건 취급(2-1).
      - tier_factor   : 운반 채널 tier weight 평균(0.3~1.0, 미분류 0.5) — '누가 말했나' 품질.
      - spread_factor : 1 + log1p(spread_copies + total_forwards) — 확산 강도(2-1).
      - velocity_mult : clamp(최근 버킷/직전 버킷, 1.0~3.0) — '지금 가속 중인가'(2-2).
    감성·유형 필터를 적용할 수 있다(예: report 만, gossip 제외, positive 만).

    Args:
        window_hours: 집계 윈도우(시간).
        only_types: 포함할 msg_type 목록(예: ["report"]). 주면 이 유형만.
        exclude_gossip: only_types 미지정 시, msg_type='gossip' 제외.
        sentiment: 특정 감성만(positive/negative/neutral).
        bucket_minutes: velocity 버킷 크기(분).
        top: 상위 N개.
        samples_per_stock: 종목별 원문 샘플 수(근거용).
    """
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    cut = (now - timedelta(hours=window_hours)).isoformat()
    bucket_sec = bucket_minutes * 60
    pred = _segment_predicate(kind, market)

    where = ["men.date >= ?"]
    params: list = [cut]
    if only_types:
        where.append("m.msg_type IN (%s)" % ",".join("?" * len(only_types)))
        params += list(only_types)
    elif exclude_gossip:
        where.append("(m.msg_type IS NULL OR m.msg_type != 'gossip')")
    if sentiment:
        where.append("m.sentiment = ?")
        params.append(sentiment)

    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT men.code, men.name, men.date, men.message_id, men.channel_id,
                   m.cluster_id, m.forwards
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            WHERE {' AND '.join(where)}
            """,
            params,
        ).fetchall()
        tier_w = {
            cid: (t.get("weight") if t.get("weight") is not None else 0.5)
            for cid, t in db.channel_tiers(conn).items()
        }
        baselines = _baseline_rows(conn)

        agg: dict[str, dict] = {}
        for r in rows:
            e = agg.setdefault(
                r["code"],
                {
                    "name": r["name"],
                    "clusters": set(),
                    "messages": {},   # message_id -> forwards (중복합산 방지)
                    "channels": set(),
                    "b0": set(),      # 최근 버킷 클러스터
                    "b1": set(),      # 직전 버킷 클러스터
                },
            )
            e["clusters"].add(r["cluster_id"])
            e["messages"][r["message_id"]] = r["forwards"] or 0
            e["channels"].add(r["channel_id"])
            ts = _parse_ts(r["date"])
            if ts is not None:
                idx = int((now_ts - ts) // bucket_sec)
                if idx == 0:
                    e["b0"].add(r["cluster_id"])
                elif idx == 1:
                    e["b1"].add(r["cluster_id"])

        out = []
        for c, e in agg.items():
            if pred and not pred(c, e.get("name")):
                continue
            independent = len(e["clusters"])
            raw_messages = len(e["messages"])
            spread_copies = raw_messages - independent
            total_forwards = sum(e["messages"].values())
            tier_factor = (
                sum(tier_w.get(ch, 0.5) for ch in e["channels"]) / len(e["channels"])
                if e["channels"]
                else 0.5
            )
            spread_factor = 1 + math.log1p(spread_copies + total_forwards)
            last, prev = len(e["b0"]), len(e["b1"])
            growth = last / max(prev, 1)
            velocity_mult = _clamp(growth, 1.0, 3.0)
            score = independent * tier_factor * spread_factor * velocity_mult
            base = _baseline_fields(baselines.get(c), independent, window_hours)
            out.append(
                {
                    "code": c,
                    "name": e["name"],
                    "buzz_score": round(score, 2),
                    "independent": independent,
                    "spread_copies": spread_copies,
                    "total_forwards": total_forwards,
                    "channels": len(e["channels"]),
                    "tier_factor": round(tier_factor, 2),
                    "spread_factor": round(spread_factor, 2),
                    "velocity_mult": round(velocity_mult, 2),
                    **base,
                }
            )
        if sort_by == "baseline_ratio":
            # buzz_score 는 절대 언급량이 커서 대형주가 늘 위를 차지한다.
            # baseline_ratio 는 '그 종목의 평소 대비 몇 배'라, 새로 관심이
            # 붙는 자리를 찾을 때는 이쪽이 맞다(없으면 0으로 내린다).
            #
            # 다만 배율은 분모가 작을수록 폭발한다 — 평소 7일에 1건 언급되던
            # 종목이 오늘 1건만 나와도 7배가 되어, 한두 건짜리가 상위를
            # 독식한다. 배율로 줄 세울 때는 최소 언급 수를 요구한다.
            floor = min_independent if min_independent > 0 else 3
            ranked = [d for d in out if d["independent"] >= floor]
            if not ranked:  # 조용한 날 빈손으로 돌려보내지 않는다
                ranked = out
            ranked.sort(key=lambda x: ((x.get("baseline_ratio") or 0.0), x["buzz_score"]), reverse=True)
            out = ranked
        else:
            out.sort(key=lambda x: x["buzz_score"], reverse=True)
        out = _take_per_segment(
            out, top, key=lambda r: r["code"], name_key=lambda r: r.get("name")
        )
        for d in out:
            # 샘플도 동일 필터를 적용 — report 필터 결과엔 report 원문만 보이게(신뢰도).
            d["samples"] = _recent_snippets(
                conn, d["code"], cut, samples_per_stock,
                only_types=only_types, exclude_gossip=exclude_gossip,
                sentiment=sentiment,
            )
    return out


def stock_timeline(
    code: str,
    name: str | None = None,
    hours: float = 72,
    bucket_minutes: int = 60,
    samples: int = 3,
) -> dict:
    """한 종목의 버즈 전개(종단) — 최초 언급 → 시간대별 확산 → 베이스라인 배율.

    trending/velocity/buzz_score 가 '어떤 종목들'(횡단)이라면, 타임라인은 '이 종목이
    언제 어디서 터져 어떻게 번졌나'(종단)를 한 번에 본다.

    Args:
        code: 6자리 종목코드.
        name: 표시명(없으면 사전에서 보충).
        hours: 윈도우(시간).
        bucket_minutes: 버킷 크기(분).
        samples: 동봉할 최근 원문 샘플 수(근거용).
    """
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    cut = (now - timedelta(hours=hours)).isoformat()
    bucket_sec = bucket_minutes * 60

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT men.date, men.message_id, men.channel_id, m.cluster_id, m.forwards
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            WHERE men.code = ? AND men.date >= ?
            """,
            (code, cut),
        ).fetchall()
        first = conn.execute(
            """
            SELECT men.date, c.title AS channel, c.username, m.cluster_id
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            LEFT JOIN channels c ON c.id = men.channel_id
            WHERE men.code = ? AND men.date >= ?
            ORDER BY men.date ASC LIMIT 1
            """,
            (code, cut),
        ).fetchone()
        brow = conn.execute(
            "SELECT avg_7d, covered_days, computed_at FROM stock_baseline WHERE code = ?",
            (code,),
        ).fetchone()
        sample_list = _recent_snippets(conn, code, cut, samples)
        last_collected = db.last_collection_at(conn)

    # 벽시계 정렬 버킷: 버킷 키 = floor(epoch / bucket_sec). now-상대가 아니라 절대 경계
    # (예: 60분 버킷 → 매시 정각, 30분 → :00/:30)라 first_mention 시각과 버킷이 어긋나지
    # 않고 "12:00~13:00 구간" 처럼 읽힌다.
    buckets: dict[int, dict] = {}
    all_clusters: set = set()
    all_channels: set = set()
    all_messages: dict = {}
    for r in rows:
        ts = _parse_ts(r["date"])
        if ts is None:
            continue
        bk = int(ts // bucket_sec)
        b = buckets.setdefault(bk, {"clusters": set(), "channels": set(), "messages": {}})
        b["clusters"].add(r["cluster_id"])
        b["channels"].add(r["channel_id"])
        b["messages"][r["message_id"]] = r["forwards"] or 0
        all_clusters.add(r["cluster_id"])
        all_channels.add(r["channel_id"])
        all_messages[r["message_id"]] = r["forwards"] or 0

    # 윈도우 첫 버킷~현재 버킷을 절대 경계로 순회(오래된→최신).
    #
    # 버킷마다 '실제로 모은 구간'을 따진다. 첫 버킷은 창 시작(cut)에서, 마지막 버킷은
    # 지금(또는 마지막 수집 시각)에서 잘리므로 한 칸을 다 채우지 못한다. 그런 칸에
    # partial·coverage 를 달고 delta 를 비운다 - 13:05 에 본 13시 칸의 1건을 직전 칸
    # 8건과 비교해 '-7'이라고 쓰면 감소로 읽힌다. 마지막 수집 이후에 시작한 칸은
    # 0건이 아니라 아직 안 모은 구간이라 값을 None 으로 둔다.
    cut_ts = now_ts - hours * 3600
    collected_end = now_ts
    last_collected_ts = _parse_ts(last_collected) if last_collected else None
    if last_collected_ts is not None:
        collected_end = min(now_ts, last_collected_ts)
    first_bk = int(cut_ts // bucket_sec)
    last_bk = int(now_ts // bucket_sec)
    timeline = []
    prev_independent = None
    for bk in range(first_bk, last_bk + 1):
        b = buckets.get(bk)
        b_start = bk * bucket_sec
        cov_start = max(b_start, cut_ts)
        cov_end = min(b_start + bucket_sec, collected_end)
        coverage = max(0.0, (cov_end - cov_start) / bucket_sec)
        entry = {
            "bucket_start": _to_kst(
                datetime.fromtimestamp(cov_start, tz=timezone.utc).isoformat()
            ),
        }
        if coverage <= 0:
            entry.update({"independent": None, "raw": None, "channels": None, "delta": None})
            entry["partial"] = True
            entry["coverage"] = 0.0
            prev_independent = None
            timeline.append(entry)
            continue
        independent = len(b["clusters"]) if b else 0
        whole = coverage >= 0.999
        entry.update(
            {
                "independent": independent,                  # 주축: 그 구간 독립 언급 수
                "raw": len(b["messages"]) if b else 0,       # 포워드/복붙 포함 원시 건수
                "channels": len(b["channels"]) if b else 0,
                # 보조: 직전 칸 대비 증감(±). 두 칸이 모두 온전할 때만.
                "delta": (
                    independent - prev_independent
                    if whole and prev_independent is not None
                    else None
                ),
            }
        )
        if not whole:
            entry["partial"] = True
            entry["coverage"] = round(coverage, 2)
        timeline.append(entry)
        prev_independent = independent if whole else None

    independent_total = len(all_clusters)
    raw_total = len(all_messages)
    # 배율 분자는 실제로 모은 시간으로 나눈다(마지막 수집이 창 끝보다 이르면 그만큼 짧다).
    covered_hours = max(0.0, (collected_end - cut_ts) / 3600)

    return {
        "code": code,
        "name": name or code,
        "window_hours": hours,
        "bucket_minutes": bucket_minutes,
        "collected_until": _to_kst(last_collected),
        # 차트/해석 가이드 — 주축은 independent(언급 '수'). delta 는 보조(증감).
        "_fields": {
            "independent": "그 구간 독립 언급 수(포워드/복붙 1건). ★차트 주축",
            "raw": "포워드/복붙 포함 원시 건수(raw≥independent, 차이=확산 복사본)",
            "channels": "그 구간 언급 채널 수(확산 폭)",
            "delta": "직전 버킷 대비 independent 증감(±, 보조지표 — 음수=감소이지 '하락'이 아님). "
            "두 칸 중 하나라도 덜 찬 칸이면 null",
            "partial": "true 면 한 칸을 다 채우지 못한 칸(창 시작·지금·마지막 수집에서 잘림). "
            "coverage 는 채운 비율(0~1). 이 칸의 수를 다른 칸과 그대로 비교하지 마세요",
            "null": "independent 가 null 인 칸은 0건이 아니라 마지막 수집(collected_until) 뒤라 "
            "아직 모으지 않은 구간",
        },
        "first_mention": (
            {
                "date": _to_kst(first["date"]),
                "channel": first["channel"],
                "username": first["username"],
                "cluster_id": first["cluster_id"],
            }
            if first
            else None
        ),
        "summary": {
            "independent": independent_total,
            "raw_messages": raw_total,
            "spread_copies": raw_total - independent_total,
            "total_forwards": sum(all_messages.values()),
            "spreading_channels": len(all_channels),
            **_baseline_fields(brow, independent_total, covered_hours),
        },
        "timeline": timeline,
        "samples": sample_list,
    }


def recent_messages(
    channel_username: str | None = None,
    hours: float = 6,
    limit: int = 30,
) -> list[dict]:
    """원문 메시지 drill-down."""
    cut = _cutoff(hours)
    with db.connect() as conn:
        cols = (
            "m.date, m.text, m.sentiment, m.msg_type, m.views, m.forwards, "
            "m.fwd_from_chat_title, m.msg_id, m.channel_id, m.media_type, m.file_name, "
            "c.title AS channel, c.username"
        )
        if channel_username:
            rows = conn.execute(
                f"""
                SELECT {cols}
                FROM messages m JOIN channels c ON c.id = m.channel_id
                WHERE c.username = ? AND m.date >= ?
                ORDER BY m.date DESC LIMIT ?
                """,
                (channel_username, cut, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT {cols}
                FROM messages m LEFT JOIN channels c ON c.id = m.channel_id
                WHERE m.date >= ?
                ORDER BY m.date DESC LIMIT ?
                """,
                (cut, limit),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["date"] = _to_kst(d["date"])
        d["forwarded_from"] = d.pop("fwd_from_chat_title")
        d["links"] = _extract_urls(d.get("text"))
        d["telegram_link"] = _tg_link(d.pop("username"), d.pop("channel_id"), d.pop("msg_id"))
        d["media"] = _media_field(d.pop("media_type"), d.pop("file_name"))
        out.append(d)
    return out


def _fts_match_expr(tokens: list[str]) -> str:
    """trigram FTS MATCH 식. 각 토큰을 부분문자열(따옴표 구)로 보고 AND 결합.

    따옴표는 두 번 써서 이스케이프('"' → '""'). trigram 에선 따옴표로 감싼
    토큰이 '그 문자열을 포함' 검색이 된다.
    """
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def search_messages(
    query: str,
    hours: float = 72,
    limit: int = 30,
    channel: str | None = None,
) -> dict:
    """원문 메시지를 키워드로 전문검색. 종목 언급이 없는 거시·산업·테마 글도 잡힌다.

    토큰(공백 분리) 전부 3글자 이상이면 trigram FTS(인덱스, 빠름)를 쓰고,
    2글자 이하 토큰이 하나라도 있으면(금리·환율·관세 등) LIKE 폴백으로 정확성을
    지킨다. 여러 토큰은 모두 포함(AND)해야 매칭된다.
    """
    tokens = query.split()
    if not tokens:
        return {"query": query, "match_mode": None, "matched": 0, "results": []}

    cut = _cutoff(hours)
    use_fts = all(len(t) >= 3 for t in tokens)

    with db.connect() as conn:
        if use_fts:
            sql = """
                SELECT m.date, m.text, m.msg_id, m.channel_id, m.media_type,
                       m.file_name, c.title AS channel, c.username
                FROM messages_fts f
                JOIN messages m ON m.id = f.rowid
                LEFT JOIN channels c ON c.id = m.channel_id
                WHERE messages_fts MATCH ? AND m.date >= ?
            """
            params: list = [_fts_match_expr(tokens), cut]
            if channel:
                sql += " AND c.username = ?"
                params.append(channel)
            sql += " ORDER BY m.date DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        else:
            # LIKE 폴백 — 토큰별 '%token%' AND. 와일드카드(%,_)는 ESCAPE 로 무력화.
            def _esc(t: str) -> str:
                return t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

            sql = """
                SELECT m.date, m.text, m.msg_id, m.channel_id, m.media_type,
                       m.file_name, c.title AS channel, c.username
                FROM messages m
                LEFT JOIN channels c ON c.id = m.channel_id
                WHERE m.date >= ?
            """
            params = [cut]
            if channel:
                sql += " AND c.username = ?"
                params.append(channel)
            for t in tokens:
                sql += " AND m.text LIKE ? ESCAPE '\\'"
                params.append(f"%{_esc(t)}%")
            sql += " ORDER BY m.date DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()

    from telegram_lens.extract import extract_mentions

    out = []
    codes: list[str] = []  # 검색 결과에 등장한 종목코드(첫 등장 순서, 중복 제거)
    for r in rows:
        full = r["text"] or ""
        for code, _ in extract_mentions(full):
            if code not in codes:
                codes.append(code)
        text = " ".join(full.split())
        if len(text) > 300:
            text = text[:300] + "…"
        out.append(
            {
                "date": _to_kst(r["date"]),
                "channel": r["channel"],
                "username": r["username"],
                "text": text,
                "links": _extract_urls(full),
                "telegram_link": _tg_link(r["username"], r["channel_id"], r["msg_id"]),
                "media": _media_field(r["media_type"], r["file_name"]),
            }
        )
    # 이어달리기용 코드 배열은 시장별로 나눈다 — 받는 쪽 도구가 다르다
    # (국내 6자리는 StockLens get_multi_stocks, 미국 티커는 get_us_multi_price).
    kr = [c for c in codes if market_of(c) == "KR"]
    us = [c for c in codes if market_of(c) == "US"]
    return {
        "query": query,
        "match_mode": "fts" if use_fts else "like",
        "matched": len(out),
        "codes": kr,  # 국내 종목코드 → StockLens 국내 배치 입력용
        "us_codes": us,  # 미국 티커 → StockLens 미국 배치 입력용
        "results": out,
    }


def channels() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.username, c.subscribers, c.last_synced,
                   t.tier, t.weight,
                   (SELECT COUNT(*) FROM messages m WHERE m.channel_id = c.id) AS messages
            FROM channels c
            LEFT JOIN channel_tier t ON t.channel_id = c.id
            ORDER BY messages DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_synced"] = _to_kst(d["last_synced"])
        out.append(d)
    return out
