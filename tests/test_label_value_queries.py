"""집계 수치의 이름표가 실제로 계산한 것과 맞는가 - 라벨-값 계약.

감사에서 나온 결함들:
- baseline_ratio: 분자는 독립 언급(포워드·복붙을 한 건으로)인데 분모는 원시 메시지 수였고,
  DB 에 이틀치만 있어도 7로 나눠 배율이 부풀었다.
- timeline: 진행 중인 마지막 칸·창 시작에서 잘린 첫 칸을 온전한 칸처럼 보여 delta 가
  감소로 읽혔고, 마지막 수집 뒤의 구간을 0건으로 채웠다.
- velocity: 창 전체 독립 언급을 버킷별 수의 합으로 셌다(경계에 걸친 복사본 이중 계산).
- momentum: 기준 구간에 언급이 없으면 spike 자리에 언급 수를 넣었고, DB 가 기준 구간을
  다 덮지 못해도 is_new=True 로 '평소 거의 없다가'가 됐다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_lens import db, queries  # noqa: E402

CODE = "123450"
OTHER = "543210"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))
    # 세그먼트 판정이 종목 사전을 네트워크로 받으러 가지 않게 한다.
    monkeypatch.setattr(queries, "load_etf_codes", lambda: set())
    db.init_db()
    return tmp_path


def _now():
    return datetime.now(timezone.utc)


_msg_seq = [0]


def _put(conn, when: datetime, code: str = CODE, *, cluster: str | None = None, channel: int = 1):
    _msg_seq[0] += 1
    mid = _msg_seq[0]
    d = when.isoformat()
    rid = db.insert_message(
        conn, channel, mid, d, f"{code} 이야기 {mid}",
        cluster_id=cluster or f"o:{channel}:{mid}",
    )
    db.insert_mentions(conn, rid, channel, d, [(code, "테스트종목")])
    return rid


def _channel(conn, synced_at: datetime | None = None, channel: int = 1):
    db.upsert_channel(
        conn, channel, f"채널{channel}", f"ch{channel}", 10,
        synced_at=(synced_at or _now()).isoformat(),
    )


# ── baseline_ratio ─────────────────────────────────────────────────


def test_baseline_uses_independent_mentions_and_covered_days(home):
    now = _now()
    with db.connect() as conn:
        _channel(conn)
        # 4.5일 전: 원본 1 + 복사본 3 (같은 클러스터)
        for _ in range(4):
            _put(conn, now - timedelta(days=4.5), cluster="o:9:1")
        # 최근 24시간: 원본 1 + 복사본 3
        for _ in range(4):
            _put(conn, now - timedelta(hours=2), cluster="o:9:2")
        db.compute_baselines(conn, days=7)
        row = conn.execute("SELECT * FROM stock_baseline WHERE code = ?", (CODE,)).fetchone()

    assert abs(row["covered_days"] - 4.5) < 0.05
    # 7일 창 안의 독립 언급 2건 / 실제 4.5일. 예전 방식이면 원시 8건 / 7일.
    assert abs(row["avg_7d"] - 2 / 4.5) < 0.01

    t = {s["code"]: s for s in queries.trending(hours=24, top=10)}[CODE]
    assert t["independent"] == 1
    assert t["baseline_days_covered"] == 4.5
    assert t["baseline_computed_at"].endswith("KST")
    assert t["baseline_ratio"] == round(1 / (2 / 4.5), 2)


def test_baseline_ratio_is_none_when_history_is_short(home):
    now = _now()
    with db.connect() as conn:
        _channel(conn)
        _put(conn, now - timedelta(days=2))
        _put(conn, now - timedelta(hours=1))
        db.compute_baselines(conn, days=7)

    t = {s["code"]: s for s in queries.trending(hours=24, top=10)}[CODE]
    assert t["baseline_days_covered"] < queries.BASELINE_MIN_DAYS
    assert t["baseline_avg_7d"] is not None
    assert t["baseline_ratio"] is None  # 이틀치로는 '평소'를 말하지 않는다

    b = {s["code"]: s for s in queries.buzz_score(window_hours=24, top=10)}[CODE]
    assert b["baseline_ratio"] is None
    tl = queries.stock_timeline(CODE, hours=24)
    assert tl["summary"]["baseline_ratio"] is None


def test_old_style_rows_are_outdated_and_stale_codes_are_dropped(home):
    now = _now()
    with db.connect() as conn:
        _channel(conn)
        _put(conn, now - timedelta(days=5))
        _put(conn, now - timedelta(hours=1))
        conn.execute(
            "INSERT INTO stock_baseline (code, name, avg_7d, window_days, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (OTHER, "옛종목", 9.0, 7, now.isoformat()),
        )
        assert db.baselines_outdated(conn) is True

    # 옛 방식 행은 배율을 내지 않는다.
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stock_baseline (code, name, avg_7d, window_days, computed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (CODE, "테스트종목", 0.1, 7, now.isoformat()),
        )
    t = {s["code"]: s for s in queries.trending(hours=24, top=10)}[CODE]
    assert t["baseline_ratio"] is None

    with db.connect() as conn:
        db.compute_baselines(conn, days=7)
        codes = {r["code"] for r in conn.execute("SELECT code FROM stock_baseline")}
        assert db.baselines_outdated(conn) is False
    assert OTHER not in codes  # 이번 기간에 언급이 없는 옛 값은 남기지 않는다


# ── timeline ───────────────────────────────────────────────────────


def test_timeline_marks_partial_edges_and_nulls_after_last_collection(home):
    now = _now()
    with db.connect() as conn:
        _channel(conn, synced_at=now - timedelta(hours=2, minutes=30))
        _put(conn, now - timedelta(hours=4))

    tl = queries.stock_timeline(CODE, hours=6, bucket_minutes=60)
    buckets = tl["timeline"]
    assert tl["collected_until"].endswith("KST")

    # 첫 칸은 창 시작(cut)에서 시작하고 덜 찬 칸이다.
    cut_label = queries._to_kst((now - timedelta(hours=6)).isoformat())
    assert buckets[0]["bucket_start"][:13] == cut_label[:13]
    if buckets[0].get("partial"):
        assert 0 < buckets[0]["coverage"] < 1
        assert buckets[0]["delta"] is None

    # 마지막 수집(2시간 30분 전) 뒤에 시작한 칸은 0이 아니라 None.
    tail = buckets[-2:]
    assert all(b["independent"] is None for b in tail)
    assert all(b["delta"] is None for b in tail)

    # 값이 있는 칸 중 어떤 칸도 None 과 0 을 섞지 않는다.
    counted = [b for b in buckets if b["independent"] is not None]
    assert sum(b["independent"] for b in counted) == 1


def test_timeline_in_progress_last_bucket_has_no_delta(home):
    now = _now()
    with db.connect() as conn:
        _channel(conn, synced_at=now)
        for k in range(5):
            _put(conn, now - timedelta(minutes=70 + k))

    tl = queries.stock_timeline(CODE, hours=4, bucket_minutes=60)
    last = tl["timeline"][-1]
    assert last["partial"] is True
    assert 0 < last["coverage"] < 1
    assert last["delta"] is None  # 덜 찬 칸을 온전한 칸과 비교해 '감소'로 쓰지 않는다


# ── velocity ───────────────────────────────────────────────────────


def test_velocity_counts_window_clusters_once_and_nulls_uncollected(home):
    now = _now()
    with db.connect() as conn:
        _channel(conn, synced_at=now - timedelta(hours=2))
        # 같은 클러스터가 두 버킷에 걸쳐 복사됨
        _put(conn, now - timedelta(hours=3, minutes=10), cluster="o:5:1")
        _put(conn, now - timedelta(hours=2, minutes=40), cluster="o:5:1")

    v = queries.buzz_velocity(code=CODE, bucket_minutes=30, window_hours=6)[0]
    assert v["window_independent"] == 1
    assert v["last_bucket"] is None  # 마지막 수집 뒤 구간
    assert v["series"][-1] is None
    assert v["spike"] is False
    assert v["growth"] is None


# ── momentum ───────────────────────────────────────────────────────


def test_momentum_spike_is_none_without_baseline_and_is_new_needs_coverage(home):
    now = _now()
    with db.connect() as conn:
        _channel(conn)
        # DB 는 30시간 전부터만 있다(기준 구간 72h 를 다 덮지 못함).
        _put(conn, now - timedelta(hours=30), code=OTHER)
        _put(conn, now - timedelta(hours=1), code=OTHER)
        _put(conn, now - timedelta(hours=1), code=CODE)

    rows = {r["code"]: r for r in queries.momentum(hours=6, baseline_hours=72, samples_per_stock=0)}
    new = rows[CODE]
    assert new["spike"] is None
    assert new["is_new"] is None  # 기준 구간을 못 덮었으니 새로 떴는지 모른다
    old = rows[OTHER]
    assert old["is_new"] is False
    assert old["baseline_hours_covered"] == 24.0
    # 기준 구간 1건 / 실제 덮은 24시간. 예전엔 66시간으로 나눴다.
    assert old["spike"] == round((1 / 6) / (1 / 24), 2)

    with db.connect() as conn:
        _put(conn, now - timedelta(hours=100), code=OTHER)  # 이제 기준 구간을 다 덮는다
    rows = {r["code"]: r for r in queries.momentum(hours=6, baseline_hours=72, samples_per_stock=0)}
    assert rows[CODE]["is_new"] is True
    assert rows[CODE]["spike"] is None
