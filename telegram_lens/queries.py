"""집계 쿼리 — 트렌딩·종목 버즈·모멘텀.

AI에게 raw 덤프 대신 구조화 요약을 준다. 토큰 절약 + 노이즈 제거.
모멘텀 스코어는 단순하지만 의미있게:
    score = 언급 메시지 수 × 채널 다양성 가중
"""

from __future__ import annotations

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


def _recent_snippets(conn, code: str, cut: str, n: int) -> list[dict]:
    """특정 종목의 최근 원문 스니펫 n개. '왜 언급됐나'를 근거로 쓰게 하는 용도."""
    if n <= 0:
        return []
    rows = conn.execute(
        """
        SELECT m.date, m.text, m.sentiment, m.msg_type, m.views, m.forwards,
               m.fwd_from_chat_title, c.title AS channel
        FROM mentions men
        JOIN messages m ON m.id = men.message_id
        LEFT JOIN channels c ON c.id = men.channel_id
        WHERE men.code = ? AND men.date >= ?
        ORDER BY m.date DESC LIMIT ?
        """,
        (code, cut, n),
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
                   COUNT(*)                      AS mentions,
                   COUNT(DISTINCT men.channel_id) AS channels,
                   COUNT(DISTINCT men.message_id) AS messages,
                   MAX(men.date)                 AS last_seen,
                   b.avg_7d                      AS baseline_avg_7d
            FROM mentions men
            LEFT JOIN stock_baseline b ON b.code = men.code
            WHERE men.date >= ?
            GROUP BY men.code
            ORDER BY messages DESC, channels DESC
            LIMIT ?
            """,
            (cut, top),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["last_seen"] = _to_kst(d["last_seen"])
            # 이상 신호 배율: 현재 구간 일평균 언급 / 7일 일평균. baseline 없으면 None.
            avg = d.pop("baseline_avg_7d", None)
            if avg and avg > 0:
                current_daily = d["messages"] / (hours / 24)
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
        recent = {
            r["code"]: dict(r)
            for r in conn.execute(
                """
                SELECT code, name, COUNT(DISTINCT message_id) AS m,
                       COUNT(DISTINCT channel_id) AS ch
                FROM mentions WHERE date >= ?
                GROUP BY code
                """,
                (recent_cut,),
            ).fetchall()
        }
        base = {
            r["code"]: r["m"]
            for r in conn.execute(
                """
                SELECT code, COUNT(DISTINCT message_id) AS m
                FROM mentions WHERE date >= ? AND date < ?
                GROUP BY code
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
            SELECT COUNT(DISTINCT message_id) AS messages,
                   COUNT(DISTINCT channel_id) AS channels,
                   MIN(date) AS first_seen, MAX(date) AS last_seen
            FROM mentions WHERE code = ? AND date >= ?
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
