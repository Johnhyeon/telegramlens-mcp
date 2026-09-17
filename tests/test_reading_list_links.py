"""브리핑 읽을거리 — 링크만 올라온 글도 기사 제목을 달고 후보에 든다.

예전엔 '본문 120자 초과 또는 첨부 문서'만 후보라, 주소 한 줄만 올리는 채널의 글은
읽을거리에 한 번도 못 올랐다. 이제 기사 제목이 읽힌 링크 글은 제목을 snippet 으로
쓰고 매체 이름을 붙인다. 제목이 아직 없는(pending) 링크 글과 잡담 티어는 여전히 뺀다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_lens import db, links, queries, server  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))
    monkeypatch.setattr(queries, "load_etf_codes", lambda: set())
    db.init_db()
    return tmp_path


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _post(conn, channel: int, mid: int, text: str, *, forwards: int = 0, link_title=None,
          link_status="ok", site="연합뉴스"):
    d = _now_iso()
    rid = db.insert_message(conn, channel, mid, d, text, forwards=forwards, views=10,
                            cluster_id=f"o:{channel}:{mid}")
    links.record_message_links(conn, rid, channel, d, text)
    if link_title is not None or link_status != "ok":
        conn.execute(
            "UPDATE message_links SET status = ?, title = ?, site = ? WHERE message_id = ?",
            (link_status, link_title, site, rid),
        )
    return rid


def test_link_only_posts_join_reading_list_with_article_title(home):
    with db.connect() as conn:
        for ch, name in ((1, "링크채널"), (2, "잡담채널"), (3, "심층채널")):
            db.upsert_channel(conn, ch, name, f"ch{ch}", 100, synced_at=_now_iso())
        db.upsert_channel_tier(conn, 2, "gossip", 0.5, "manual")
        # 링크만 있는 글 — 제목이 읽혔으니 후보. 확산이 가장 크다.
        _post(conn, 1, 1, "https://news.example.com/a", forwards=30, link_title="삼성중공업, LNG선 6척 수주")
        # 링크만 있는데 아직 안 읽힘 — 제목이 없으니 후보 아님.
        _post(conn, 1, 2, "https://news.example.com/b", forwards=99, link_title=None, link_status="pending")
        # 잡담 티어의 링크 글 — 제외.
        _post(conn, 2, 3, "https://news.example.com/c", forwards=50, link_title="잡담 기사")
        # 긴 심층글(링크 없음) — 예전 규칙 그대로 후보. 본문 앞부분이 snippet.
        _post(conn, 3, 4, "긴 분석 글 " * 30, forwards=20)

    reading = server._reading_list(hours=6)
    snippets = [r["snippet"] for r in reading]
    assert snippets[0] == "삼성중공업, LNG선 6척 수주"        # 확산 30, 기사 제목이 snippet
    assert reading[0]["link_site"] == "연합뉴스"
    assert reading[0]["article_url"] == "https://news.example.com/a"
    assert reading[0]["link"] == "https://t.me/ch1/1"
    assert any(s.startswith("긴 분석 글") for s in snippets)
    assert "잡담 기사" not in snippets
    assert all("news.example.com/b" not in (r.get("article_url") or "") for r in reading)
    assert len(reading) == 2


def test_one_channel_cannot_fill_the_list(home):
    with db.connect() as conn:
        db.upsert_channel(conn, 1, "봇채널", "bot", 100, synced_at=_now_iso())
        db.upsert_channel(conn, 2, "사람채널", "human", 100, synced_at=_now_iso())
        for i in range(6):
            _post(conn, 1, i + 1, f"공시 정리 {i} " * 20, forwards=100 - i)
        _post(conn, 2, 99, "https://news.example.com/z", forwards=1, link_title="사람이 고른 기사")
    reading = server._reading_list(hours=6, limit=5)
    assert sum(1 for r in reading if r["channel"] == "봇채널") == server._READING_PER_CHANNEL
    assert any(r["snippet"] == "사람이 고른 기사" for r in reading)


def test_long_post_with_link_keeps_its_own_text_as_snippet(home):
    with db.connect() as conn:
        db.upsert_channel(conn, 1, "채널", "ch1", 100, synced_at=_now_iso())
        _post(conn, 1, 1, "직접 쓴 긴 해설이 있는 글 " * 12 + "https://news.example.com/a",
              forwards=5, link_title="기사 제목")
    (r,) = server._reading_list(hours=6)
    assert r["snippet"].startswith("직접 쓴 긴 해설")
    assert r["link_title"] == "기사 제목"


def test_briefing_text_shows_site_next_to_channel(home):
    reading = [{"snippet": "기사 제목", "channel": "링크채널", "forwards": 3, "has_file": False,
                "file_name": None, "link": "https://t.me/ch1/1", "link_title": "기사 제목",
                "link_site": "연합뉴스", "article_url": "https://news.example.com/a"}]
    text = server._format_briefing_ready([], [], [], reading, {}, hours=12)
    assert " · 기사 제목" in text
    assert "링크채널 · 연합뉴스 https://t.me/ch1/1" in text
