"""집계 쿼리 — 트렌딩·종목 버즈·모멘텀.

AI에게 raw 덤프 대신 구조화 요약을 준다. 토큰 절약 + 노이즈 제거.
모멘텀 스코어는 단순하지만 의미있게:
    score = 언급 메시지 수 × 채널 다양성 가중
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram_lens import db

_KST = ZoneInfo("Asia/Seoul")


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
               m.fwd_from_chat_title, c.title AS channel
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
                "sentiment": r["sentiment"],
                "msg_type": r["msg_type"],
                "views": r["views"],
                "forwards": r["forwards"],
                "forwarded_from": r["fwd_from_chat_title"],
            }
        )
    return out


def trending(hours: float = 24, top: int = 20, samples_per_stock: int = 1) -> list[dict]:
    """기간 내 언급량 상위 종목 (각 종목의 최근 원문 샘플 동봉)."""
    cut = _cutoff(hours)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT men.code, men.name,
                   COUNT(DISTINCT m.cluster_id)  AS independent,
                   COUNT(DISTINCT men.message_id) AS raw_messages,
                   COUNT(DISTINCT men.channel_id) AS channels,
                   COALESCE(SUM(m.forwards), 0)  AS total_forwards,
                   MAX(men.date)                 AS last_seen,
                   b.avg_7d                      AS baseline_avg_7d
            FROM mentions men
            JOIN messages m ON m.id = men.message_id
            LEFT JOIN stock_baseline b ON b.code = men.code
            WHERE men.date >= ?
            GROUP BY men.code
            ORDER BY independent DESC, channels DESC
            LIMIT ?
            """,
            (cut, top),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["last_seen"] = _to_kst(d["last_seen"])
            # 확산 강도: 독립 클러스터 외에 복사/포워드로 더 퍼진 양.
            d["spread_copies"] = d["raw_messages"] - d["independent"]
            # 이상 신호 배율: 현재 구간 독립 언급 일평균 / 7일 일평균. baseline 없으면 None.
            avg = d.pop("baseline_avg_7d", None)
            if avg and avg > 0:
                current_daily = d["independent"] / (hours / 24)
                d["baseline_avg_7d"] = round(avg, 2)
                d["baseline_ratio"] = round(current_daily / avg, 2)
            else:
                d["baseline_avg_7d"] = round(avg, 2) if avg else None
                d["baseline_ratio"] = None
            d["samples"] = _recent_snippets(conn, d["code"], cut, samples_per_stock)
            out.append(d)
    return out


def momentum(
    hours: float = 6,
    baseline_hours: float = 72,
    top: int = 15,
    samples_per_stock: int = 2,
) -> list[dict]:
    """최근 구간 언급이 기준 구간 평균 대비 급증한 종목 (급증 구간 원문 샘플 동봉).

    spike = recent_rate / baseline_rate (시간당 언급 비율 비교)
    """
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

    out = []
    base_span = max(baseline_hours - hours, 1e-9)
    for code, r in recent.items():
        recent_rate = r["m"] / hours
        base_count = base.get(code, 0)
        base_rate = base_count / base_span
        # 기준이 0이면 신규 등장 — 큰 spike로 취급(상한 둠)
        spike = recent_rate / base_rate if base_rate > 0 else float(r["m"]) * 1.0
        out.append(
            {
                "code": code,
                "name": r["name"],
                "recent_mentions": r["m"],
                "recent_channels": r["ch"],
                "baseline_mentions": base_count,
                "spike": round(spike, 2),
                "is_new": base_rate == 0,
            }
        )
    out.sort(key=lambda x: (x["spike"], x["recent_mentions"]), reverse=True)
    out = out[:top]
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
                   m.fwd_from_chat_title, c.title AS channel, c.username
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
        baselines = {
            r["code"]: r["avg_7d"]
            for r in conn.execute("SELECT code, avg_7d FROM stock_baseline")
        }

    # code → {name, buckets: {idx: set(cluster_id)}}. 버킷별 '독립' 카운트는 set 크기.
    per: dict[str, dict] = {}
    for r in rows:
        ts = _parse_ts(r["date"])
        if ts is None:
            continue
        idx = int((now_ts - ts) // bucket_sec)
        if idx < 0:
            idx = 0
        e = per.setdefault(r["code"], {"name": r["name"], "buckets": {}})
        e["buckets"].setdefault(idx, set()).add(r["cluster_id"])

    out = []
    for c, e in per.items():
        counts = {i: len(s) for i, s in e["buckets"].items()}
        last = counts.get(0, 0)
        prev = counts.get(1, 0)
        growth = round(last / max(prev, 1), 2)
        total = sum(counts.values())
        avg = baselines.get(c)
        if avg and avg > 0:
            baseline_ratio = round((total / (window_hours / 24)) / avg, 2)
        else:
            baseline_ratio = None
        out.append(
            {
                "code": c,
                "name": e["name"],
                "bucket_minutes": bucket_minutes,
                "window_hours": window_hours,
                # 오래된→최신 순 시계열(독립 언급 수).
                "series": [counts.get(i, 0) for i in range(nbuckets - 1, -1, -1)],
                "last_bucket": last,
                "prev_bucket": prev,
                "delta": last - prev,
                "growth": growth,
                "spike": last >= spike_min or growth >= growth_threshold,
                "window_independent": total,
                "baseline_avg_7d": round(avg, 2) if avg else None,
                "baseline_ratio": baseline_ratio,
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
        baselines = {
            r["code"]: r["avg_7d"]
            for r in conn.execute("SELECT code, avg_7d FROM stock_baseline")
        }

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
            avg = baselines.get(c)
            baseline_ratio = (
                round((independent / (window_hours / 24)) / avg, 2)
                if avg and avg > 0
                else None
            )
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
                    "baseline_ratio": baseline_ratio,
                }
            )
        out.sort(key=lambda x: x["buzz_score"], reverse=True)
        out = out[:top]
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
            "SELECT avg_7d FROM stock_baseline WHERE code = ?", (code,)
        ).fetchone()
        sample_list = _recent_snippets(conn, code, cut, samples)

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
    first_bk = int((now_ts - hours * 3600) // bucket_sec)
    last_bk = int(now_ts // bucket_sec)
    timeline = []
    prev_independent = 0
    for bk in range(first_bk, last_bk + 1):
        b = buckets.get(bk)
        independent = len(b["clusters"]) if b else 0
        bucket_start = datetime.fromtimestamp(bk * bucket_sec, tz=timezone.utc)
        timeline.append(
            {
                "bucket_start": _to_kst(bucket_start.isoformat()),
                "independent": independent,
                "raw": len(b["messages"]) if b else 0,
                "channels": len(b["channels"]) if b else 0,
                "velocity": independent - prev_independent,
            }
        )
        prev_independent = independent

    independent_total = len(all_clusters)
    raw_total = len(all_messages)
    avg = brow["avg_7d"] if brow else None
    baseline_ratio = (
        round((independent_total / (hours / 24)) / avg, 2) if avg and avg > 0 else None
    )

    return {
        "code": code,
        "name": name or code,
        "window_hours": hours,
        "bucket_minutes": bucket_minutes,
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
            "baseline_avg_7d": round(avg, 2) if avg else None,
            "baseline_ratio": baseline_ratio,
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
            "m.fwd_from_chat_title, c.title AS channel, c.username"
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
                SELECT m.date, m.text, c.title AS channel, c.username
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
                SELECT m.date, m.text, c.title AS channel, c.username
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

    out = []
    for r in rows:
        text = " ".join((r["text"] or "").split())
        if len(text) > 300:
            text = text[:300] + "…"
        out.append(
            {
                "date": _to_kst(r["date"]),
                "channel": r["channel"],
                "username": r["username"],
                "text": text,
            }
        )
    return {
        "query": query,
        "match_mode": "fts" if use_fts else "like",
        "matched": len(out),
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
