"""링크 내용 읽기(links.py) — 주소 수집·종류 판정·읽기·조회·검색·보존 정리.

지키는 것:
- 본문 주소·글자 뒤 숨은 링크·미리보기 메타가 한 글의 링크 행으로 합쳐진다(같은 주소는 하나).
- DART·텔레그램·영상·유료벽은 읽지 않고 종류만 남긴다. DART 는 rcpNo 를 준다.
- 단축주소는 풀린 끝 주소로 다시 판정하고, 네이버 다리 페이지는 url 인자를 따라간다.
- 발췌는 상한 안에서 잘리고, 검색은 본문에 없는 말을 링크 발췌에서 찾는다(matched_in).
- 보존창 밖 링크의 제목·발췌는 비워진다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram_lens import db, links, queries  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAMLENS_HOME", str(tmp_path))
    monkeypatch.setattr(queries, "load_etf_codes", lambda: set())
    db.init_db()
    return tmp_path


def _now():
    return datetime.now(timezone.utc)


_seq = [0]


def _msg(conn, text: str, *, when: datetime | None = None, channel: int = 1, extra=None):
    _seq[0] += 1
    d = (when or _now()).isoformat()
    rid = db.insert_message(conn, channel, _seq[0], d, text, cluster_id=f"o:{channel}:{_seq[0]}")
    links.record_message_links(conn, rid, channel, d, text, extra)
    return rid


def _rows(conn, rid):
    return conn.execute(
        "SELECT * FROM message_links WHERE message_id = ? ORDER BY id", (rid,)
    ).fetchall()


# ── 수집 시점 ────────────────────────────────────────────────────────


def test_text_hidden_and_preview_links_merge_into_one_row_per_url(home):
    with db.connect() as conn:
        db.upsert_channel(conn, 1, "채널", "ch1", 10, synced_at=_now().isoformat())
        rid = _msg(
            conn,
            "기사 보세요 https://n.news.naver.com/mnews/article/001/0001. 다시 https://n.news.naver.com/mnews/article/001/0001#top",
            extra=[
                {"url": "https://research.example.com/report", "source": "entity"},
                {"url": "https://n.news.naver.com/mnews/article/001/0001", "source": "preview",
                 "title": "미리보기 제목", "description": "미리보기 설명", "site": "네이버뉴스"},
            ],
        )
        rows = _rows(conn, rid)
    assert [r["url"] for r in rows] == [
        "https://n.news.naver.com/mnews/article/001/0001",
        "https://research.example.com/report",
    ]
    news, hidden = rows
    assert news["source"] == "text"            # 본문에도 있으면 본문 쪽 출처 유지
    assert news["title"] == "미리보기 제목"    # 미리보기 메타는 그 행에 채워진다
    assert news["site"] == "네이버뉴스"
    assert news["status"] == links.ST_PENDING
    assert hidden["source"] == "entity"


def test_classification_skips_what_we_do_not_read(home):
    dart = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260916000378"
    with db.connect() as conn:
        rid = _msg(conn, " ".join([
            dart, "https://t.me/somechannel/123", "https://youtu.be/abc",
            "https://www.bloomberg.com/news/x", "https://kind.krx.co.kr/x",
        ]))
        by_url = {r["url"]: r for r in _rows(conn, rid)}
    assert by_url[dart]["kind"] == links.KIND_DART
    assert by_url[dart]["status"] == links.ST_SKIPPED
    assert by_url[dart]["dart_rcp_no"] == "20260916000378"
    assert by_url["https://t.me/somechannel/123"]["kind"] == links.KIND_TELEGRAM
    assert by_url["https://youtu.be/abc"]["kind"] == links.KIND_VIDEO
    assert by_url["https://kind.krx.co.kr/x"]["kind"] == links.KIND_DISCLOSURE
    pay = by_url["https://www.bloomberg.com/news/x"]
    assert pay["status"] == links.ST_TITLE_ONLY and pay["fail_reason"] == "paywall"


def test_preview_url_without_www_merges_into_text_row(home):
    """텔레그램 미리보기는 www. 와 끝 슬래시를 뗀 주소를 준다 — 실사용에서 같은 링크가 두 행으로 갈라졌다."""
    with db.connect() as conn:
        rid = _msg(
            conn, "기사 https://www.forbes.com/sites/x/2026/09/17/story/",
            extra=[{"url": "https://forbes.com/sites/x/2026/09/17/story", "source": "preview",
                    "title": "Forbes 제목", "site": "Forbes"}],
        )
        rows = _rows(conn, rid)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.forbes.com/sites/x/2026/09/17/story/"
    assert rows[0]["title"] == "Forbes 제목" and rows[0]["source"] == "text"


def test_extract_stops_at_copyright_and_related_articles():
    html = ("<html><body><article><p>첫 문단은 기사 본문이고 충분히 길다. 두 번째 문장도 있다.</p>"
            "<p>둘째 문단도 기사 본문이며 이 역시 충분히 길게 이어진다.</p>"
            "<p>&lt;저작권자 (c) 연합인포맥스, 무단전재 및 재배포 금지&gt;</p>"
            "<p>스냅, AR 글래스로 B2B시장 진출…엔비디아·세일즈포스와 협력</p>"
            "<p>아마존, 비상발전기 업체 신주인수권 확보…주가 40% 상승 소식</p></article></body></html>")
    body = links.extract_html(html)["body"]
    assert "둘째 문단" in body
    assert "저작권자" not in body and "스냅, AR" not in body


def test_old_messages_get_expired_not_pending(home):
    with db.connect() as conn:
        rid = _msg(conn, "https://example.com/old", when=_now() - timedelta(days=10))
        (row,) = _rows(conn, rid)
    assert row["status"] == links.ST_EXPIRED


def test_links_per_message_cap(home):
    text = " ".join(f"https://example.com/{i}" for i in range(30))
    with db.connect() as conn:
        rid = _msg(conn, text)
        assert len(_rows(conn, rid)) == links.LINKS_PER_MESSAGE


# ── 추출 ────────────────────────────────────────────────────────────


_NAVER_LIKE = """<html><head><title>사이트 제목</title>
<meta property="og:title" content="삼성중공업, LNG선 6척 수주"/>
<meta property="og:description" content="1.6조 규모"/>
<meta property="og:site_name" content="연합뉴스"/>
<script>var junk = "스크립트 안의 긴 문장은 본문이 아니다 스크립트 안의 긴 문장은 본문이 아니다";</script>
</head><body>
<nav><a>홈</a><a>정치</a><a>경제</a>메뉴 메뉴 메뉴 메뉴 메뉴 메뉴 메뉴 메뉴 메뉴 메뉴 메뉴 메뉴</nav>
<article id="dic_area">
<p>삼성중공업이 LNG 운반선 4척과 원유운반선 2척을 총 1조6천억원에 수주했다고 14일 밝혔다.</p>
<p>이번 계약으로 올해 누적 수주는 작년 연간 실적을 넘어섰다.</p>
<p>짧은 줄</p>
</article>
<footer>저작권 안내 문장이 길게 이어지는 꼬리말 저작권 안내 문장이 길게 이어지는 꼬리말</footer>
</body></html>"""


def test_extract_html_takes_og_meta_and_article_container():
    ex = links.extract_html(_NAVER_LIKE)
    assert ex["title"] == "삼성중공업, LNG선 6척 수주"
    assert ex["description"] == "1.6조 규모"
    assert ex["site"] == "연합뉴스"
    assert ex["body"].startswith("삼성중공업이 LNG 운반선")
    assert "누적 수주" in ex["body"]
    assert "스크립트" not in ex["body"] and "메뉴" not in ex["body"] and "꼬리말" not in ex["body"]


def test_extract_body_is_capped():
    para = "<p>" + ("가나다라마바사아자차카타파하 " * 20).strip() + ".</p>"
    html = "<html><body><article>" + para * 40 + "</article></body></html>"
    ex = links.extract_html(html)
    assert len(ex["body"]) <= links.BODY_MAX + 1
    assert ex["body"].endswith("…")


def test_decode_prefers_meta_charset_for_euc_kr():
    raw = '<html><head><meta charset="euc-kr"><title>한글 제목</title></head><body></body></html>'.encode("cp949")
    assert "한글 제목" in links._decode(raw, None)


# ── 읽기(가짜 서버) ──────────────────────────────────────────────────


def _transport():
    article = "<html><head><meta property='og:title' content='기사 제목'/></head><body><article>" \
              + "<p>본문 첫 문장은 충분히 길어야 발췌로 인정된다. 그래서 이렇게 길게 쓴다.</p>" * 4 \
              + "</article></body></html>"

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u.startswith("https://buly.kr/"):
            return httpx.Response(302, headers={"location": "https://news.example.com/a1"})
        if u == "https://news.example.com/a1":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=article)
        if u.startswith("https://naver.me/"):
            return httpx.Response(302, headers={"location": "https://link.naver.com/bridge?url=https%3A%2F%2Fnews.example.com%2Fa1&dst=x"})
        if u.startswith("https://link.naver.com/bridge"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>redirecting…</html>")
        if u.startswith("https://short.example.com/dart"):
            return httpx.Response(302, headers={"location": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260101000001"})
        if u.startswith("https://dart.fss.or.kr/"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>dart</html>")
        if u.endswith(".pdf"):
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")
        if "blocked" in u:
            return httpx.Response(403, text="forbidden")
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


@pytest.fixture
def client():
    with links.make_client(transport=_transport()) as c:
        yield c


def test_fetch_follows_shortener_and_extracts(client):
    res = links.fetch_url("https://buly.kr/abc", client)
    assert res["status"] == links.ST_OK
    assert res["final_url"] == "https://news.example.com/a1"
    assert res["title"] == "기사 제목"
    assert res["body"].startswith("본문 첫 문장")
    assert res["site"] == "news.example.com"


def test_fetch_follows_naver_bridge_url_param(client):
    res = links.fetch_url("https://naver.me/xyz", client)
    assert res["status"] == links.ST_OK
    assert res["final_url"] == "https://news.example.com/a1"


def test_fetch_reclassifies_after_redirect_to_dart(client):
    res = links.fetch_url("https://short.example.com/dart", client)
    assert res["kind"] == links.KIND_DART
    assert res["status"] == links.ST_SKIPPED
    assert res["dart_rcp_no"] == "20260101000001"


def test_fetch_non_html_is_file(client):
    res = links.fetch_url("https://files.example.com/report.pdf", client)
    assert res["kind"] == links.KIND_FILE and res["status"] == links.ST_SKIPPED
    assert res["title"] == "report.pdf"


def test_fetch_blocked_and_missing(client):
    assert links.fetch_url("https://x.example.com/blocked", client)["fail_reason"] == "blocked"
    assert links.fetch_url("https://x.example.com/none", client)["fail_reason"] == "not_found"


def test_fetch_pending_fills_rows_dedupes_urls_and_keeps_preview_title(home, monkeypatch):
    calls: list[str] = []

    def fake(url, client=None):
        calls.append(url)
        if "blocked" in url:
            return {"final_url": None, "kind": links.KIND_ARTICLE, "status": links.ST_FAILED,
                    "fail_reason": "blocked", "site": None, "title": None, "description": None,
                    "body": None, "dart_rcp_no": None}
        return {"final_url": None, "kind": links.KIND_ARTICLE, "status": links.ST_OK,
                "fail_reason": None, "site": "s", "title": "읽은 제목", "description": None,
                "body": "본문 " * 100, "dart_rcp_no": None}

    with db.connect() as conn:
        a = _msg(conn, "https://news.example.com/same")
        b = _msg(conn, "다른 글 https://news.example.com/same", channel=2)
        c = _msg(conn, "https://x.example.com/blocked",
                 extra=[{"url": "https://x.example.com/blocked", "source": "preview",
                         "title": "미리보기 제목"}])
        counts = links.fetch_pending(conn, fetcher=fake, sleep=lambda s: None)
        ra, rb, rc = _rows(conn, a)[0], _rows(conn, b)[0], _rows(conn, c)[0]
    assert calls.count("https://news.example.com/same") == 1   # 같은 주소는 한 번만 읽는다
    assert counts["fetched"] == 2 and counts["ok"] == 2 and counts["remaining"] == 0
    assert ra["status"] == rb["status"] == links.ST_OK and ra["title"] == "읽은 제목"
    # 읽기는 막혔지만 미리보기 제목이 있으면 '제목만'으로 올린다.
    assert rc["status"] == links.ST_TITLE_ONLY and rc["title"] == "미리보기 제목"
    assert counts["title_only"] == 1


def test_fetch_pending_expires_stale_and_respects_cap(home):
    seen = []

    def fake(url, client=None):
        seen.append(url)
        return {"final_url": None, "kind": links.KIND_ARTICLE, "status": links.ST_OK,
                "fail_reason": None, "site": None, "title": "t", "description": None,
                "body": "x" * 200, "dart_rcp_no": None}

    with db.connect() as conn:
        for i in range(5):
            _msg(conn, f"https://news.example.com/{i}")
        old = _msg(conn, "https://news.example.com/old", when=_now() - timedelta(days=2, hours=23))
        conn.execute("UPDATE message_links SET msg_date = ? WHERE message_id = ?",
                     ((_now() - timedelta(days=5)).isoformat(), old))
        counts = links.fetch_pending(conn, cap=3, fetcher=fake, sleep=lambda s: None)
    assert len(seen) == 3 and counts["remaining"] == 2 and counts["expired"] == 1


# ── 조회·검색 ───────────────────────────────────────────────────────


def _filled(conn, rid, body="발췌 본문 " * 30, title="링크 제목"):
    conn.execute(
        "UPDATE message_links SET status = ?, title = ?, body = ?, fetched_at = ? WHERE message_id = ?",
        (links.ST_OK, title, body, _now().isoformat(), rid),
    )


def test_messages_carry_link_content_and_excerpt_only_on_request(home):
    with db.connect() as conn:
        db.upsert_channel(conn, 1, "채널", "ch1", 10, synced_at=_now().isoformat())
        rid = _msg(conn, "보세요 https://news.example.com/a")
        _filled(conn, rid)
        _msg(conn, "링크 없는 글")
    lite = queries.recent_messages(hours=1, limit=10)
    with_body = queries.recent_messages(hours=1, limit=10, link_body=True)
    no_link = next(m for m in lite if m["text"] == "링크 없는 글")
    assert no_link["link_content"] == []
    lc = next(m for m in lite if m["text"].startswith("보세요"))["link_content"][0]
    assert lc["title"] == "링크 제목" and lc["status"] == "ok"
    assert lc["excerpt"] is None and lc["has_excerpt"] is True
    assert "_rowid" not in lite[0]
    full = next(m for m in with_body if m["text"].startswith("보세요"))["link_content"][0]
    assert full["excerpt"].startswith("발췌 본문")


def test_search_finds_keyword_that_lives_only_in_link_body(home):
    with db.connect() as conn:
        db.upsert_channel(conn, 1, "채널", "ch1", 10, synced_at=_now().isoformat())
        rid = _msg(conn, "오늘 기사 https://news.example.com/a")
        _filled(conn, rid, body="삼성중공업이 LNG선을 수주했다는 내용의 본문이다.", title="수주 소식")
        _msg(conn, "삼성중공업 얘기 본문에 직접 있음")
    res = queries.search_messages("삼성중공업", hours=1)
    by_text = {r["text"]: r for r in res["results"]}
    assert by_text["삼성중공업 얘기 본문에 직접 있음"]["matched_in"] == "text"
    assert by_text["오늘 기사 https://news.example.com/a"]["matched_in"] == "link"
    assert res["matched"] == 2
    # 짧은 토큰(LIKE 폴백)도 같은 규칙
    res2 = queries.search_messages("수주", hours=1)
    assert any(r["matched_in"] == "link" for r in res2["results"])
    # 끄면 본문만
    res3 = queries.search_messages("LNG선", hours=1, in_links=False)
    assert res3["matched"] == 0


def test_link_by_url_view_hides_internal_fields(home):
    with db.connect() as conn:
        rid = _msg(conn, "https://news.example.com/a")
        _filled(conn, rid)
        view = links.public_view(links.link_by_url(conn, "https://news.example.com/a"), links.BODY_MAX)
    assert not {"channel_id", "msg_date", "id", "message_id", "source", "fetched_at"} & set(view)
    assert view["excerpt"].startswith("발췌 본문")


def test_prune_blanks_link_content_but_keeps_row(home):
    with db.connect() as conn:
        rid = _msg(conn, "https://news.example.com/a", when=_now() - timedelta(days=100))
        _filled(conn, rid)
        db.prune_raw_content(90, conn)
        (row,) = _rows(conn, rid)
    assert row["body"] == "" and row["title"] == ""
    assert queries.search_messages("발췌", hours=24 * 200)["matched"] == 0


def test_backfill_creates_rows_for_existing_messages_once(home):
    with db.connect() as conn:
        _seq[0] += 1
        rid = db.insert_message(conn, 1, _seq[0], _now().isoformat(),
                                "예전 글 https://news.example.com/old", cluster_id="o:1:x")
        n = links.backfill_from_messages(conn, (_now() - timedelta(days=3)).isoformat())
        again = links.backfill_from_messages(conn, (_now() - timedelta(days=3)).isoformat())
        assert n == 1 and again == 0
        assert _rows(conn, rid)[0]["status"] == links.ST_PENDING
