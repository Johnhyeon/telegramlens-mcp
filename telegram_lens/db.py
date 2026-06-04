"""로컬 SQLite 저장소.

히스토리를 보존해야 "오늘 갑자기 언급 급증" 같은 모멘텀 감지가 가능하다.
스키마는 단순하게:
  - channels : 추적 중인 채널 메타
  - messages : 원문 메시지(채널·메시지 ID로 중복 방지). 포워드 메타·조회수·확산
               지표(views/forwards) + 룰베이스 태그(sentiment/msg_type)를 비정규화 보관.
  - mentions : 메시지에서 추출한 종목 언급(트렌딩 집계용으로 비정규화)
  - channel_tier : 채널 성격 분류(analyst/research/info/gossip) + 버즈스코어 가중치
  - stock_baseline : 종목별 7일 평균 언급수(현재/평균 = 이상 신호 배율 판단)
  - message_views_log : 게시 후 1h/6h/24h 시점 조회수·확산 이력(확산 velocity 분석용)
  - messages_fts : messages.text 전문검색 인덱스(종목 언급이 없는 거시·산업·테마
                   글까지 키워드로 찾기 위함). trigram 토크나이저 — 한글은 띄어쓰기가
                   불규칙해 일반 토크나이저로는 부분일치가 안 되므로 3글자 단위 trigram
                   으로 부분문자열 검색을 가능케 한다(2글자 이하는 queries에서 LIKE 폴백).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from telegram_lens.config import db_path

# 스키마 버전. 1회성 마이그레이션을 user_version 으로 게이트한다.
#   v1 = FTS 인덱스 도입
#   v2 = 포워드 메타·views/forwards·sentiment/msg_type 컬럼 + tier/baseline/views_log 테이블
#   v3 = cluster_id/text_sig 컬럼(중복제거·원본추적) + 인덱스
#   v4 = media_type/file_name 컬럼(첨부 파일·이미지 인지)
_SCHEMA_VERSION = 4

# messages 에 v2 에서 추가된 컬럼(이름 → 선언). 신규 설치는 _SCHEMA 가, 기존 DB는
# _migrate 의 ALTER 가 채운다. 한 곳에서 관리해 둘이 어긋나지 않게 한다.
_MESSAGES_V2_COLUMNS: dict[str, str] = {
    "fwd_from_chat_id": "INTEGER",
    "fwd_from_chat_title": "TEXT",
    "fwd_from_message_id": "INTEGER",
    "fwd_from_date": "TEXT",
    "views": "INTEGER",
    "forwards": "INTEGER",
    "sentiment": "TEXT",       # positive | negative | neutral
    "msg_type": "TEXT",        # report | breaking | gossip | chat | general
}

# v3: 중복제거·원본추적용. cluster_id = 정규 클러스터 키(원본+파생본이 같은 값으로 수렴),
# text_sig = 정규화 텍스트 서명(포워드 메타 없는 복붙 중복을 묶는 인덱스).
_MESSAGES_V3_COLUMNS: dict[str, str] = {
    "cluster_id": "TEXT",
    "text_sig": "TEXT",
}

# v4: 첨부 인지. media_type = photo|document|webpage|None, file_name = 문서 파일명(있으면).
# 다운로드 없이 메타데이터만 — "이 글에 PDF/이미지 있음"을 알려 텔레그램 원문으로 유도.
_MESSAGES_V4_COLUMNS: dict[str, str] = {
    "media_type": "TEXT",
    "file_name": "TEXT",
}

# 마이그레이션 ensure 루프가 순회할 '추가 컬럼' 전체(v2 + v3 + v4).
_MESSAGES_ADDED_COLUMNS: dict[str, str] = {
    **_MESSAGES_V2_COLUMNS,
    **_MESSAGES_V3_COLUMNS,
    **_MESSAGES_V4_COLUMNS,
}

_MESSAGES_ADDED_COLS_SQL = "".join(
    f"    {name}        {decl},\n" for name, decl in _MESSAGES_ADDED_COLUMNS.items()
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS channels (
    id          INTEGER PRIMARY KEY,          -- telegram channel id
    title       TEXT,
    username    TEXT,
    subscribers INTEGER,
    last_synced TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    date        TEXT NOT NULL,                -- ISO8601 UTC
    text        TEXT NOT NULL,
{_MESSAGES_ADDED_COLS_SQL}    UNIQUE(channel_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);
-- cluster_id/text_sig 인덱스는 컬럼 ALTER 이후라야 만들 수 있어 _migrate 에서 생성.

-- 채널 성격 분류 + 버즈스코어 가중치. source='manual' 은 휴리스틱 재시드가 덮어쓰지 않음.
CREATE TABLE IF NOT EXISTS channel_tier (
    channel_id    INTEGER PRIMARY KEY,
    tier          TEXT,        -- analyst | research | info | gossip
    weight        REAL,        -- 버즈스코어 가중 계수
    source        TEXT,        -- heuristic | manual
    note          TEXT,
    classified_at TEXT
);

-- 종목별 7일 평균 '일별' 언급 메시지 수. 현재 언급/avg_7d = 이상 신호 배율.
CREATE TABLE IF NOT EXISTS stock_baseline (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    avg_7d      REAL,
    window_days INTEGER,
    computed_at TEXT
);

-- 게시 후 horizon(collect/1h/6h/24h) 시점의 조회수·확산 snapshot 이력.
CREATE TABLE IF NOT EXISTS message_views_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    horizon     TEXT NOT NULL,            -- collect | 1h | 6h | 24h
    views       INTEGER,
    forwards    INTEGER,
    captured_at TEXT NOT NULL,
    UNIQUE(message_id, horizon),          -- horizon별 1회만 → '어디까지 찍었나' 판별
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_views_log_msg ON message_views_log(message_id);

CREATE TABLE IF NOT EXISTS mentions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    code        TEXT NOT NULL,                -- 6자리 종목코드
    name        TEXT NOT NULL,
    date        TEXT NOT NULL,                -- 메시지 날짜 복제(집계 속도)
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mentions_code_date ON mentions(code, date);
CREATE INDEX IF NOT EXISTS idx_mentions_date ON mentions(date);

CREATE TABLE IF NOT EXISTS channel_scores (
    channel_id    INTEGER PRIMARY KEY,
    title         TEXT,
    username      TEXT,
    subscribers   INTEGER,
    sampled       INTEGER,        -- 텍스트 있는 샘플 메시지 수
    with_mention  INTEGER,        -- 그중 종목 언급 1개 이상인 수
    mentions      INTEGER,        -- 누적 언급 수
    density       REAL,           -- with_mention / sampled
    is_stock      INTEGER,        -- 주식채널 분류(1/0)
    classified_at TEXT
);

-- 전문검색 인덱스. 외부콘텐츠(content='messages')라 본문은 messages에만 두고
-- FTS는 인덱스만 갖는다(중복 저장 X). 트리거로 messages와 동기화.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
-- text 가 바뀔 때만 재색인. views/cluster_id/sentiment 등 비-text 컬럼 UPDATE(매 사이클
-- 발생하는 조회수 갱신·클러스터 병합)에서 불필요한 FTS 재색인이 일어나지 않게 한다.
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    # busy_timeout: 데몬(write)과 Claude(read)가 동시에 붙어도 잠깐 대기 후 재시도.
    conn = sqlite3.connect(str(path or db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        # WAL: writer(데몬)가 쓰는 동안에도 reader(Claude 조회)가 막히지 않는다.
        # journal_mode 는 DB 파일에 영구 기록되므로 1회 설정으로 충분.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """user_version 게이트 1회성 마이그레이션.

    executescript(_SCHEMA) 는 누락 '테이블'을 IF NOT EXISTS 로 만들지만, 기존 messages
    테이블에 '컬럼'을 추가하지는 못한다 → 여기서 PRAGMA 로 누락 컬럼을 ALTER 한다(idempotent).
    """
    ver = conn.execute("PRAGMA user_version").fetchone()[0]

    # v1: FTS 를 나중에 도입한 기존 DB — 이미 쌓인 메시지를 1회 색인. 트리거는 이후
    # INSERT 부터만 동작하므로 도입 이전 메시지는 rebuild 필요. 외부콘텐츠 FTS 는 COUNT 로
    # '인덱스가 비었는지'를 셀 수 없어 user_version 으로 1회만 rebuild 하도록 게이트한다.
    if ver < 1:
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    # v2+v3: messages 의 추가 컬럼(포워드/조회수/태그 + cluster_id/text_sig). 신규 DB 는
    # _SCHEMA 에 이미 있고, 기존 DB 만 여기서 채워진다. 항상 검사(idempotent)해 부분
    # 마이그레이션 상태도 자가 치유한다.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for name, decl in _MESSAGES_ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")

    # v3: cluster_id/text_sig 컬럼이 확보됐으니 인덱스 생성(idempotent). _SCHEMA 가 아니라
    # 여기 두는 이유는 v2 DB executescript 시점엔 컬럼이 아직 없어 인덱스가 깨지기 때문.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_cluster ON messages(cluster_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_textsig ON messages(text_sig, date)"
    )

    # v3: 기존 DB 의 messages_au 트리거를 'AFTER UPDATE OF text' 버전으로 교체. 옛 트리거는
    # 모든 컬럼 UPDATE 에 재색인이 걸려, 아래 cluster_id 백필·매 사이클 조회수 갱신에서
    # 전체 FTS 가 불필요하게 재구성된다(대량 DB 에선 느리고, 부분색인 DB 에선 오류 위험).
    if ver < 3:
        conn.execute("DROP TRIGGER IF EXISTS messages_au")
        conn.execute(
            """
            CREATE TRIGGER messages_au AFTER UPDATE OF text ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, text)
                    VALUES('delete', old.id, old.text);
                INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
            END
            """
        )

    # v3: cluster_id 1회 백필(set-based SQL). 포워드 메타가 있으면 원본 키, 없으면 자기
    # 자신이 원본 → 원본+포워드가 같은 키로 수렴(COUNT DISTINCT cluster_id = 독립 언급).
    # text_sig(정규화 텍스트 서명)는 Python 정규화가 필요해 여기선 비우고, 수집/데몬이
    # 신규 메시지부터 채우고 최근분만 backfill_text_sig 로 보충한다.
    if ver < 3:
        conn.execute(
            """
            UPDATE messages SET cluster_id =
              CASE WHEN fwd_from_chat_id IS NOT NULL AND fwd_from_message_id IS NOT NULL
                   THEN 'o:' || fwd_from_chat_id || ':' || fwd_from_message_id
                   ELSE 'o:' || channel_id || ':' || msg_id END
            WHERE cluster_id IS NULL
            """
        )

    if ver < _SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def upsert_channel(
    conn: sqlite3.Connection,
    channel_id: int,
    title: str | None,
    username: str | None,
    subscribers: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO channels (id, title, username, subscribers, last_synced)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            subscribers=COALESCE(excluded.subscribers, channels.subscribers),
            last_synced=excluded.last_synced
        """,
        (
            channel_id,
            title,
            username,
            subscribers,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def insert_message(
    conn: sqlite3.Connection,
    channel_id: int,
    msg_id: int,
    date_iso: str,
    text: str,
    *,
    views: int | None = None,
    forwards: int | None = None,
    fwd_from_chat_id: int | None = None,
    fwd_from_chat_title: str | None = None,
    fwd_from_message_id: int | None = None,
    fwd_from_date: str | None = None,
    sentiment: str | None = None,
    msg_type: str | None = None,
    cluster_id: str | None = None,
    text_sig: str | None = None,
    media_type: str | None = None,
    file_name: str | None = None,
) -> int | None:
    """메시지 저장. 새로 들어가면 rowid 반환, 중복이면 None.

    포워드 메타·조회수·룰베이스 태그·클러스터 키/서명을 키워드 인자로 함께 저장한다
    (수집 시점 snapshot).
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO messages (
            channel_id, msg_id, date, text,
            views, forwards,
            fwd_from_chat_id, fwd_from_chat_title, fwd_from_message_id, fwd_from_date,
            sentiment, msg_type, cluster_id, text_sig, media_type, file_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            channel_id, msg_id, date_iso, text,
            views, forwards,
            fwd_from_chat_id, fwd_from_chat_title, fwd_from_message_id, fwd_from_date,
            sentiment, msg_type, cluster_id, text_sig, media_type, file_name,
        ),
    )
    if cur.rowcount == 0:
        return None
    return cur.lastrowid


def insert_mentions(
    conn: sqlite3.Connection,
    message_id: int,
    channel_id: int,
    date_iso: str,
    mentions: list[tuple[str, str]],
) -> None:
    """mentions: [(code, name), ...]"""
    conn.executemany(
        """
        INSERT INTO mentions (message_id, channel_id, code, name, date)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(message_id, channel_id, code, name, date_iso) for code, name in mentions],
    )


def upsert_channel_score(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute(
        """
        INSERT INTO channel_scores
            (channel_id, title, username, subscribers, sampled,
             with_mention, mentions, density, is_stock, classified_at)
        VALUES (:channel_id, :title, :username, :subscribers, :sampled,
                :with_mention, :mentions, :density, :is_stock, :classified_at)
        ON CONFLICT(channel_id) DO UPDATE SET
            title=excluded.title, username=excluded.username,
            subscribers=excluded.subscribers, sampled=excluded.sampled,
            with_mention=excluded.with_mention, mentions=excluded.mentions,
            density=excluded.density, is_stock=excluded.is_stock,
            classified_at=excluded.classified_at
        """,
        s,
    )


def newest_message_date(conn: sqlite3.Connection) -> str | None:
    """저장된 메시지 중 가장 최근 날짜(ISO UTC). 없으면 None."""
    row = conn.execute("SELECT MAX(date) AS d FROM messages").fetchone()
    return row["d"] if row else None


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM channels) AS channels,
            (SELECT COUNT(*) FROM messages) AS messages,
            (SELECT COUNT(*) FROM mentions) AS mentions,
            (SELECT COUNT(*) FROM stock_baseline) AS baselines,
            (SELECT MAX(computed_at) FROM stock_baseline) AS baselines_computed,
            (SELECT COUNT(*) FROM message_views_log) AS views_log,
            (SELECT MAX(last_synced) FROM channels) AS last_synced
        """
    ).fetchone()
    return dict(row)


# ── 조회수·확산 snapshot 이력 ───────────────────────────────────────

def insert_views_log(
    conn: sqlite3.Connection,
    message_id: int,
    channel_id: int,
    horizon: str,
    views: int | None,
    forwards: int | None,
) -> None:
    """horizon(collect/1h/6h/24h) 시점 snapshot 1건. 같은 horizon 재기록은 무시."""
    conn.execute(
        """
        INSERT OR IGNORE INTO message_views_log
            (message_id, channel_id, horizon, views, forwards, captured_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            message_id, channel_id, horizon, views, forwards,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def update_message_views(
    conn: sqlite3.Connection,
    message_id: int,
    views: int | None,
    forwards: int | None,
) -> None:
    """messages 의 views/forwards 를 최신 snapshot 으로 갱신(NULL 은 기존값 보존)."""
    conn.execute(
        """
        UPDATE messages
           SET views    = COALESCE(?, views),
               forwards = COALESCE(?, forwards)
         WHERE id = ?
        """,
        (views, forwards, message_id),
    )


def messages_needing_view_refresh(
    conn: sqlite3.Connection,
    horizon: str,
    min_age_min: float,
    max_age_min: float,
    limit: int,
) -> list[dict]:
    """해당 horizon snapshot 이 아직 없고, 나이가 [min,max)분 구간에 든 메시지.

    반환: [{id, channel_id, msg_id}, ...]. 나이 = now - messages.date.
    구간 상한(max)은 '뒤늦게 발견해도 한 번은 찍되, 너무 오래된 건 포기'하는 슬랙.
    """
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(minutes=max_age_min)).isoformat()  # 가장 오래된 허용 시각
    hi = (now - timedelta(minutes=min_age_min)).isoformat()  # 가장 최근 허용 시각
    rows = conn.execute(
        """
        SELECT m.id, m.channel_id, m.msg_id
        FROM messages m
        WHERE m.date >= ? AND m.date < ?
          AND NOT EXISTS (
              SELECT 1 FROM message_views_log v
              WHERE v.message_id = m.id AND v.horizon = ?
          )
        ORDER BY m.date ASC
        LIMIT ?
        """,
        (lo, hi, horizon, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── 채널 tier ───────────────────────────────────────────────────────

def channel_tiers(conn: sqlite3.Connection) -> dict[int, dict]:
    """channel_id → {tier, weight, source} 매핑."""
    return {
        r["channel_id"]: dict(r)
        for r in conn.execute(
            "SELECT channel_id, tier, weight, source FROM channel_tier"
        )
    }


def upsert_channel_tier(
    conn: sqlite3.Connection,
    channel_id: int,
    tier: str,
    weight: float,
    source: str,
    note: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO channel_tier
            (channel_id, tier, weight, source, note, classified_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            tier=excluded.tier, weight=excluded.weight, source=excluded.source,
            note=excluded.note, classified_at=excluded.classified_at
        """,
        (
            channel_id, tier, weight, source, note,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


# ── 종목 베이스라인(7일 평균 언급수) ────────────────────────────────

def compute_baselines(conn: sqlite3.Connection, days: int = 7) -> int:
    """code별 최근 days일 distinct-message 언급수 / days → stock_baseline upsert.

    반환: 갱신된 종목 수. avg_7d 는 '하루 평균 언급 메시지 수'. 현재 언급/avg_7d 가
    이상 신호 배율(queries.trending 에서 노출)이 된다.
    """
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """
        SELECT code, name, COUNT(DISTINCT message_id) AS msgs
        FROM mentions WHERE date >= ?
        GROUP BY code
        """,
        (cut,),
    ).fetchall()
    payload = [
        (r["code"], r["name"], r["msgs"] / days, days, now_iso) for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO stock_baseline (code, name, avg_7d, window_days, computed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name, avg_7d=excluded.avg_7d,
            window_days=excluded.window_days, computed_at=excluded.computed_at
        """,
        payload,
    )
    return len(payload)


def baselines_age_minutes(conn: sqlite3.Connection) -> float | None:
    """베이스라인 마지막 계산으로부터 경과(분). 아직 없으면 None."""
    row = conn.execute("SELECT MAX(computed_at) AS c FROM stock_baseline").fetchone()
    if not row or not row["c"]:
        return None
    try:
        dt = datetime.fromisoformat(row["c"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except ValueError:
        return None


# ── 클러스터(중복제거·원본추적) ─────────────────────────────────────

def update_cluster_id(
    conn: sqlite3.Connection, message_id: int, cluster_id: str
) -> None:
    """휴리스틱 병합 시 파생본의 cluster_id 를 원본 키로 재할당."""
    conn.execute(
        "UPDATE messages SET cluster_id = ? WHERE id = ?", (cluster_id, message_id)
    )


def messages_missing_text_sig(
    conn: sqlite3.Connection, since_iso: str, limit: int
) -> list[dict]:
    """text_sig 가 아직 없는 최근 메시지 (id, text). 업그레이드 이전 메시지 백필용."""
    rows = conn.execute(
        """
        SELECT id, text FROM messages
        WHERE text_sig IS NULL AND date >= ?
        ORDER BY date DESC LIMIT ?
        """,
        (since_iso, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def set_text_sig(conn: sqlite3.Connection, message_id: int, text_sig: str) -> None:
    conn.execute(
        "UPDATE messages SET text_sig = ? WHERE id = ?", (text_sig, message_id)
    )
