"""텔레그램 글에 붙은 링크의 내용을 읽어 둔다 — 주소 수집 → 발췌 읽기 → 조회.

왜 있나
  수집된 글의 절반이 링크를 달고 오는데(실측 2026-09-17, 7일 3,692건 중 1,832건),
  본문은 제목 한 줄이고 내용은 링크 너머에 있는 채널이 많다. 지금까지는 그 글이
  사실상 비어 있었다. 글자 뒤에 숨은 링크("📜본문보기📜" 같은 텍스트 엔티티 URL)는
  본문에 주소가 없어 아예 안 잡혔다(4,343건).

무엇을 하나
  1. 수집 시점(sync)에 글마다 링크 행을 만든다 — 본문 주소·숨은 링크·미리보기 메타
     (Telethon WebPage 의 제목·설명)를 한 표(message_links)에 모은다.
  2. 데몬이 정상 사이클 끝에 아직 안 읽은 링크를 최신 글부터 읽어 제목·설명·본문
     발췌를 채운다. 사이클당 상한과 마감이 있어 수집을 붙잡지 않는다.
  3. 조회 도구가 글에 link_content 를 붙이고, 검색이 발췌까지 뒤진다.

안 하는 것
  - 기사 전문 저장·재배포. 발췌는 BODY_MAX 자로 자르고 보존창이 지나면 비운다
    (messages.text 와 같은 2층 보존 정책).
  - DART·거래소 공시는 안 읽는다 — rcpNo 만 남겨 DartLens 로 넘긴다.
  - 텔레그램 딥링크·영상·SNS·파일(PDF 등)은 종류만 기록한다.
  - 유료벽 언론(bloomberg·wsj 등)은 요청 자체를 안 보내고 미리보기 제목만 쓴다.
"""

from __future__ import annotations

import html as _html
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx

from telegram_lens._version import CODE_VERSION

_LOG = logging.getLogger("telegramlens.links")

# 본문에서 주소를 찾는 식. queries._extract_urls 가 쓰던 것과 같다(한 곳에서 관리).
URL_RE = re.compile(r"https?://[^\s)\]>\"'》」』]+")
_TRAIL_PUNCT = ".,;…·)」』\"'"

# 발췌·제목·설명 상한(자). 발췌는 "기사 전문 저장"이 되지 않게 하는 선이기도 하다.
BODY_MAX = 2000
DESC_MAX = 300
TITLE_MAX = 200
# 글 하나에 기록할 링크 수 상한(나열형 글이 수십 개를 달고 오는 경우 방어).
LINKS_PER_MESSAGE = 10

# 읽기 정책.
FETCH_TIMEOUT = 8.0            # 링크 하나당 연결+응답 상한(초)
MAX_BYTES = 2_000_000          # 이보다 큰 페이지는 앞부분만
PER_CYCLE_CAP = 120            # 사이클당 읽을 링크 수
CYCLE_DEADLINE_SEC = 150       # 사이클당 읽기에 쓸 시간
SAME_HOST_GAP_SEC = 1.0        # 같은 호스트에 연달아 보낼 때 최소 간격
PENDING_MAX_AGE_DAYS = 3       # 이보다 오래된 글의 링크는 읽지 않는다(백필 폭주 방지)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/128.0 Safari/537.36 LeetKit-TelegramLens/{CODE_VERSION}"
)
_HEADERS = {"User-Agent": _UA, "Accept-Language": "ko,en;q=0.8", "Accept": "text/html,*/*;q=0.5"}

# 링크 종류(kind). 조회 결과에 그대로 나간다.
KIND_ARTICLE = "article"        # 읽어서 발췌를 만드는 일반 웹페이지(기사·블로그·리포트 요약)
KIND_DART = "dart"              # DART 공시 — rcpNo 로 DartLens 에 넘긴다
KIND_DISCLOSURE = "disclosure"  # 거래소(KIND) 공시
KIND_TELEGRAM = "telegram"      # t.me 딥링크
KIND_VIDEO = "video"            # 유튜브
KIND_SOCIAL = "social"          # X·스레드 등
KIND_FILE = "file"              # PDF 등 HTML 이 아닌 파일

# 상태(status).
ST_PENDING = "pending"          # 아직 안 읽음
ST_OK = "ok"                    # 발췌 있음
ST_TITLE_ONLY = "title_only"    # 제목(설명)만 — 유료벽이거나 본문을 못 찾음
ST_SKIPPED = "skipped"          # 안 읽는 종류
ST_FAILED = "failed"            # 읽기 실패(fail_reason)
ST_EXPIRED = "expired"          # 너무 오래된 글이라 안 읽음

_PAYWALL_HOSTS = (
    "bloomberg.com", "wsj.com", "ft.com", "reuters.com", "economist.com",
    "nikkei.com", "barrons.com", "nytimes.com", "washingtonpost.com",
)
_SOCIAL_HOSTS = ("x.com", "twitter.com", "threads.net", "facebook.com", "instagram.com")
_VIDEO_HOSTS = ("youtube.com", "youtu.be")
_TELEGRAM_HOSTS = ("t.me", "telegram.me")

_RCP_RE = re.compile(r"rcpNo=(\d{14})")


# ── 주소 다루기 ─────────────────────────────────────────────────────


def clean_url(url: str) -> str:
    """본문에서 뽑은 주소의 꼬리 문장부호를 뗀다."""
    return (url or "").strip().rstrip(_TRAIL_PUNCT)


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host_in(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def dedupe_key(url: str) -> str:
    """같은 주소를 하나로 보는 키. 텔레그램 미리보기는 주소를 정규화해서 준다(www. 없음,
    끝 슬래시 없음) — 본문의 https://www.forbes.com/a/ 와 미리보기의 https://forbes.com/a 가
    한 행이 되게 스킴·호스트 소문자, www. 제거, 끝 슬래시·프래그먼트 제거."""
    u = clean_url(url).split("#", 1)[0]
    try:
        parts = urlsplit(u)
    except ValueError:
        return u
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{host}{path}" + (f"?{parts.query}" if parts.query else "")


def classify(url: str) -> dict:
    """주소만 보고 종류·초기 상태를 정한다. 읽기 전에 거를 수 있는 건 여기서 거른다.

    반환: {kind, status, dart_rcp_no, fail_reason}
    """
    host = host_of(url)
    if _host_in(host, _TELEGRAM_HOSTS):
        return {"kind": KIND_TELEGRAM, "status": ST_SKIPPED, "dart_rcp_no": None, "fail_reason": None}
    if host == "dart.fss.or.kr":
        m = _RCP_RE.search(url)
        return {"kind": KIND_DART, "status": ST_SKIPPED,
                "dart_rcp_no": m.group(1) if m else None, "fail_reason": None}
    if host == "kind.krx.co.kr":
        return {"kind": KIND_DISCLOSURE, "status": ST_SKIPPED, "dart_rcp_no": None, "fail_reason": None}
    if _host_in(host, _VIDEO_HOSTS):
        return {"kind": KIND_VIDEO, "status": ST_SKIPPED, "dart_rcp_no": None, "fail_reason": None}
    if _host_in(host, _SOCIAL_HOSTS):
        return {"kind": KIND_SOCIAL, "status": ST_SKIPPED, "dart_rcp_no": None, "fail_reason": None}
    if _host_in(host, _PAYWALL_HOSTS):
        return {"kind": KIND_ARTICLE, "status": ST_TITLE_ONLY, "dart_rcp_no": None,
                "fail_reason": "paywall"}
    return {"kind": KIND_ARTICLE, "status": ST_PENDING, "dart_rcp_no": None, "fail_reason": None}


def urls_in_text(text: str | None, limit: int = LINKS_PER_MESSAGE) -> list[str]:
    """본문 주소 목록(중복 제거, 순서 보존)."""
    if not text:
        return []
    out: list[str] = []
    for u in URL_RE.findall(text):
        u = clean_url(u)
        if u and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


# ── 수집 시점: 링크 행 만들기 ────────────────────────────────────────


def _is_stale(date_iso: str, now: datetime) -> bool:
    try:
        d = datetime.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d < now - timedelta(days=PENDING_MAX_AGE_DAYS)


def record_message_links(
    conn: sqlite3.Connection,
    message_id: int,
    channel_id: int,
    date_iso: str,
    text: str | None,
    extra: list[dict] | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """글 하나의 링크 행을 만든다. 본문 주소 + Telethon 이 준 숨은 링크·미리보기(extra).

    extra 원소: {url, source: "entity"|"preview", title?, description?, site?}
    같은 주소는 하나로 합치고, 미리보기 메타는 그 주소 행에 채운다(주소가 본문에도
    있으면 source 는 본문 쪽을 유지). 오래된 글(백필)은 처음부터 expired 로 둔다.
    반환: 새로 들어간 행 수.
    """
    now = now or datetime.now(timezone.utc)
    merged: dict[str, dict] = {}
    for u in urls_in_text(text):
        merged.setdefault(dedupe_key(u), {"url": u, "source": "text"})  # 첫 표기 유지
    for e in extra or []:
        u = clean_url(e.get("url") or "")
        if not u.startswith(("http://", "https://")):
            continue
        key = dedupe_key(u)
        row = merged.get(key)
        if row is None:
            if len(merged) >= LINKS_PER_MESSAGE:
                continue
            row = merged[key] = {"url": u, "source": e.get("source") or "entity"}
        for k in ("title", "description", "site"):
            if e.get(k) and not row.get(k):
                row[k] = e[k]

    stale = _is_stale(date_iso, now)
    inserted = 0
    for row in merged.values():
        c = classify(row["url"])
        status = c["status"]
        if status == ST_PENDING and stale:
            status = ST_EXPIRED
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO message_links (
                message_id, channel_id, msg_date, url, source, kind, status,
                dart_rcp_no, fail_reason, site, title, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, channel_id, date_iso, row["url"], row["source"], c["kind"], status,
                c["dart_rcp_no"], c["fail_reason"], row.get("site"),
                _clip(row.get("title"), TITLE_MAX), _clip(row.get("description"), DESC_MAX),
            ),
        )
        inserted += cur.rowcount
    return inserted


def backfill_from_messages(conn: sqlite3.Connection, since_iso: str, limit: int = 5000) -> int:
    """업그레이드 이전에 모인 글의 본문 주소로 링크 행을 1회 만든다.

    숨은 링크·미리보기는 이미 지나간 수집이라 되살릴 수 없다 — 본문 주소만.
    이미 링크 행이 있는 글은 건너뛴다.
    """
    rows = conn.execute(
        """
        SELECT m.id, m.channel_id, m.date, m.text FROM messages m
        WHERE m.date >= ? AND m.text LIKE '%http%'
          AND NOT EXISTS (SELECT 1 FROM message_links l WHERE l.message_id = m.id)
        ORDER BY m.date DESC LIMIT ?
        """,
        (since_iso, limit),
    ).fetchall()
    n = 0
    for r in rows:
        n += record_message_links(conn, r["id"], r["channel_id"], r["date"], r["text"])
    return n


# ── 읽기 ────────────────────────────────────────────────────────────


def _clip(s: str | None, n: int) -> str | None:
    if not s:
        return None
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _meta(html: str, key: str) -> str | None:
    """<meta property|name="key" content="…"> 값. 속성 순서가 바뀐 꼴도 본다."""
    pat = r'<meta\b[^>]*?(?:property|name)\s*=\s*["\']%s["\'][^>]*?content\s*=\s*["\']([^"\']*)["\']' % re.escape(key)
    m = re.search(pat, html, re.I | re.S)
    if not m:
        pat = r'<meta\b[^>]*?content\s*=\s*["\']([^"\']*)["\'][^>]*?(?:property|name)\s*=\s*["\']%s["\']' % re.escape(key)
        m = re.search(pat, html, re.I | re.S)
    if not m:
        return None
    v = _html.unescape(m.group(1)).strip()
    return v or None


_DROP_RE = re.compile(
    r"<(script|style|noscript|svg|nav|header|footer|aside|form|iframe|template|select)\b.*?</\1\s*>",
    re.S | re.I,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_RE = re.compile(
    r"</?(p|div|br|li|ul|ol|h[1-6]|tr|td|th|table|section|article|blockquote|figure|figcaption|dd|dt|pre)\b[^>]*>",
    re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
# 본문이 끝났다는 표식 — 여기부터는 저작권 고지·관련기사 목록이라 발췌에서 뗀다.
_BODY_END_RE = re.compile(
    r"^(<?\s*저작권자|Copyright|ⓒ|©|\(c\)\s|무단\s?전재|무단\s?복제|관련\s?기사|함께\s?보면|인기\s?기사|많이\s?본)",
    re.I,
)

# 본문 그릇을 찾는 표식 — 앞에 있을수록 우선. 네이버뉴스(dic_area)·다음(article_view)·
# 국내 언론 CMS 들의 흔한 이름·schema.org articleBody·HTML5 article/main 순.
_CONTAINER_PATTERNS = (
    r'id\s*=\s*["\']dic_area["\']',
    r'id\s*=\s*["\']articleBody["\']',
    r'itemprop\s*=\s*["\']articleBody["\']',
    r'class\s*=\s*["\'][^"\']*\b(?:article[_-]?body|news[_-]?body|article[_-]?view[_-]?content|'
    r'article[_-]?txt|news[_-]?txt|view[_-]?content|article[_-]?content|newsct_article|'
    r'se-main-container|post[_-]?content|entry[_-]?content|article_view|news_view)\b[^"\']*["\']',
    r'<article\b',
    r'<main\b',
)
_CONTAINER_RES = tuple(re.compile(p, re.I) for p in _CONTAINER_PATTERNS)


def _to_lines(fragment: str) -> list[str]:
    txt = _BLOCK_RE.sub("\n", fragment)
    txt = _TAG_RE.sub(" ", txt)
    txt = _html.unescape(txt)
    out: list[str] = []
    prev = None
    for raw in txt.split("\n"):
        line = " ".join(raw.split())
        if line and line != prev:
            out.append(line)
        prev = line
    return out


def extract_html(html: str) -> dict:
    """HTML 에서 제목·설명·사이트명·본문 발췌를 뽑는다(외부 파서 없이).

    본문은 알려진 그릇(id/class/article)을 찾으면 거기서부터, 없으면 긴 줄만 모은다.
    발췌는 BODY_MAX 자에서 자른다. 반환: {title, description, site, body}
    """
    title = _meta(html, "og:title") or _meta(html, "twitter:title")
    if not title:
        m = _TITLE_TAG_RE.search(html)
        if m:
            title = _html.unescape(_TAG_RE.sub("", m.group(1))).strip() or None
    description = _meta(html, "og:description") or _meta(html, "description")
    site = _meta(html, "og:site_name")

    body_html = _COMMENT_RE.sub(" ", html)
    body_html = _DROP_RE.sub(" ", body_html)
    fragment = None
    for rx in _CONTAINER_RES:
        m = rx.search(body_html)
        if m:
            start = body_html.rfind("<", 0, m.start())
            fragment = body_html[max(start, 0): m.start() + 200_000]
            break
    if fragment is not None:
        lines = [l for l in _to_lines(fragment) if len(l) >= 25]
    else:
        lines = [l for l in _to_lines(body_html) if len(l) >= 40]
    for i, l in enumerate(lines):
        if _BODY_END_RE.match(l):
            lines = lines[:i]
            break
    body = "\n".join(lines).strip()
    if len(body) > BODY_MAX:
        cut = body[:BODY_MAX]
        # 문장 끝(마침표·줄바꿈)에서 자르면 읽는 쪽이 덜 헷갈린다.
        pos = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("다."))
        body = (cut[: pos + 1] if pos > BODY_MAX // 2 else cut).rstrip() + "…"
    return {
        "title": _clip(title, TITLE_MAX),
        "description": _clip(description, DESC_MAX),
        "site": _clip(site, 80),
        "body": body or None,
    }


_CHARSET_RE = re.compile(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', re.I)


def _decode(content: bytes, header_charset: str | None) -> str:
    """헤더 charset → <meta charset> → utf-8 순. 국내 사이트에 아직 euc-kr 이 있다."""
    enc = header_charset
    if not enc:
        m = _CHARSET_RE.search(content[:4096])
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    for cand in (enc, "utf-8", "cp949"):
        if not cand:
            continue
        try:
            return content.decode(cand)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


def _header_charset(content_type: str) -> str | None:
    m = re.search(r"charset\s*=\s*([A-Za-z0-9_\-]+)", content_type or "", re.I)
    return m.group(1) if m else None


def _unbridge_naver(url: str) -> str | None:
    """link.naver.com/bridge?url=… — 네이버 앱용 다리 페이지. 진짜 주소는 url 인자에 있다."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if host_of(url) != "link.naver.com" or not parts.path.startswith("/bridge"):
        return None
    target = parse_qs(parts.query).get("url")
    if target and target[0].startswith(("http://", "https://")):
        return target[0]
    return None


def make_client(**kw) -> httpx.Client:
    """읽기용 클라이언트. TLS 신뢰 기준은 _tls.apply() 가 프로세스 단위로 이미 세운다."""
    return httpx.Client(
        follow_redirects=True, timeout=FETCH_TIMEOUT, headers=_HEADERS, max_redirects=10, **kw
    )


def fetch_url(url: str, client: httpx.Client | None = None, *, _hop: int = 0) -> dict:
    """링크 하나를 읽어 {final_url, kind, status, fail_reason, site, title, description, body,
    dart_rcp_no} 를 돌려준다. 예외를 올리지 않는다 — 실패도 결과의 한 종류다."""
    own = client is None
    client = client or make_client()
    result = {
        "final_url": None, "kind": KIND_ARTICLE, "status": ST_FAILED, "fail_reason": None,
        "site": None, "title": None, "description": None, "body": None, "dart_rcp_no": None,
    }
    try:
        with client.stream("GET", url) as r:
            final = str(r.url)
            result["final_url"] = final if final != url else None
            bridged = _unbridge_naver(final)
            if bridged and _hop < 1:
                r.close()
                inner = fetch_url(bridged, client, _hop=_hop + 1)
                inner["final_url"] = inner.get("final_url") or bridged
                return inner
            # 단축주소가 DART·텔레그램 등으로 풀리면 종류를 다시 정한다.
            c = classify(final)
            if c["status"] in (ST_SKIPPED, ST_TITLE_ONLY):
                result.update(kind=c["kind"], status=c["status"], dart_rcp_no=c["dart_rcp_no"],
                              fail_reason=c["fail_reason"])
                return result
            code = r.status_code
            if code in (401, 402, 403):
                result["fail_reason"] = "blocked"
                return result
            if code == 404 or code == 410:
                result["fail_reason"] = "not_found"
                return result
            if code >= 400:
                result["fail_reason"] = f"http_{code}"
                return result
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                name = urlsplit(final).path.rsplit("/", 1)[-1] or None
                result.update(kind=KIND_FILE, status=ST_SKIPPED, fail_reason="non_html",
                              title=_clip(name, TITLE_MAX))
                return result
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) >= MAX_BYTES:
                    break
        html = _decode(bytes(buf), _header_charset(ctype))
        ex = extract_html(html)
        result.update(site=ex["site"] or host_of(final), title=ex["title"],
                      description=ex["description"], body=ex["body"])
        if ex["body"] and len(ex["body"]) >= 120:
            result["status"] = ST_OK
        elif ex["title"]:
            result.update(status=ST_TITLE_ONLY, fail_reason="no_body")
        else:
            result["fail_reason"] = "empty"
        m = _RCP_RE.search(final)
        if m:
            result["dart_rcp_no"] = m.group(1)
        return result
    except httpx.TimeoutException:
        result["fail_reason"] = "timeout"
    except httpx.TooManyRedirects:
        result["fail_reason"] = "redirect_loop"
    except httpx.HTTPError as e:
        result["fail_reason"] = f"network:{type(e).__name__}"
    except Exception as e:  # noqa: BLE001 — 읽기 실패는 결과로만 남긴다
        result["fail_reason"] = f"error:{type(e).__name__}"
    finally:
        if own:
            client.close()
    return result


def apply_result(conn: sqlite3.Connection, link_id: int, res: dict, now_iso: str) -> str:
    """읽은 결과를 행에 쓴다. 제목·설명은 읽은 값이 있으면 그것, 없으면 미리보기 값 유지.
    유료벽·읽기 실패라도 미리보기 제목이 있으면 title_only 로 올린다."""
    row = conn.execute(
        "SELECT title, description FROM message_links WHERE id = ?", (link_id,)
    ).fetchone()
    title = res.get("title") or (row["title"] if row else None)
    desc = res.get("description") or (row["description"] if row else None)
    status = res["status"]
    if status == ST_FAILED and title:
        status = ST_TITLE_ONLY
    conn.execute(
        """
        UPDATE message_links SET final_url = ?, kind = ?, status = ?, fail_reason = ?,
            site = ?, title = ?, description = ?, body = ?, dart_rcp_no = COALESCE(?, dart_rcp_no),
            fetched_at = ?
        WHERE id = ?
        """,
        (
            res.get("final_url"), res["kind"], status, res.get("fail_reason"),
            res.get("site"), _clip(title, TITLE_MAX), _clip(desc, DESC_MAX), res.get("body"),
            res.get("dart_rcp_no"), now_iso, link_id,
        ),
    )
    return status


def expire_stale_pending(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=PENDING_MAX_AGE_DAYS)).isoformat()
    cur = conn.execute(
        "UPDATE message_links SET status = ? WHERE status = ? AND msg_date < ?",
        (ST_EXPIRED, ST_PENDING, cutoff),
    )
    return cur.rowcount


def fetch_pending(
    conn: sqlite3.Connection,
    *,
    cap: int = PER_CYCLE_CAP,
    deadline_sec: float = CYCLE_DEADLINE_SEC,
    fetcher=None,
    now: datetime | None = None,
    sleep=time.sleep,
) -> dict:
    """아직 안 읽은 링크를 최신 글부터 읽어 채운다. 데몬이 정상 사이클 끝에 부른다.

    같은 주소가 여러 글에 있으면 한 번만 읽어 모두에 쓴다. 같은 호스트엔 1초 간격.
    반환: {"fetched", "ok", "title_only", "failed", "skipped", "expired", "remaining"}
    """
    now = now or datetime.now(timezone.utc)
    expired = expire_stale_pending(conn, now)
    rows = conn.execute(
        "SELECT id, url FROM message_links WHERE status = ? ORDER BY msg_date DESC, id LIMIT ?",
        (ST_PENDING, cap),
    ).fetchall()
    counts = {"fetched": 0, "ok": 0, "title_only": 0, "failed": 0, "skipped": 0,
              "expired": expired, "remaining": 0}
    if not rows:
        return counts

    fetcher = fetcher or fetch_url
    client = make_client() if fetcher is fetch_url else None
    started = time.monotonic()
    last_hit: dict[str, float] = {}
    seen: dict[str, dict] = {}
    done = 0
    try:
        for r in rows:
            if time.monotonic() - started > deadline_sec:
                break
            key = dedupe_key(r["url"])
            res = seen.get(key)
            if res is None:
                host = host_of(r["url"])
                gap = SAME_HOST_GAP_SEC - (time.monotonic() - last_hit.get(host, -10.0))
                if gap > 0:
                    sleep(gap)
                res = fetcher(r["url"], client) if client is not None else fetcher(r["url"])
                last_hit[host] = time.monotonic()
                seen[key] = res
                counts["fetched"] += 1
            final = apply_result(conn, r["id"], res, now.isoformat())
            done += 1
            counts[final if final in counts else "ok"] += 1
            if done % 20 == 0:
                conn.commit()
    finally:
        if client is not None:
            client.close()
    counts["remaining"] = conn.execute(
        "SELECT COUNT(*) FROM message_links WHERE status = ?", (ST_PENDING,)
    ).fetchone()[0]
    return counts


# ── 조회 ────────────────────────────────────────────────────────────

_PUBLIC_COLS = "id, message_id, url, final_url, source, kind, status, fail_reason, site, title, description, body, dart_rcp_no, fetched_at"


def public_view(row, body_chars: int) -> dict:
    """행(또는 fetch_url 결과에 url 을 얹은 dict) → 도구 응답용 link_content 원소."""
    d = dict(row)
    body = d.pop("body") or None
    d.pop("id", None)
    d.pop("message_id", None)
    for k in ("source", "fetched_at", "channel_id", "msg_date"):  # 내부 필드는 응답에 안 싣는다
        d.pop(k, None)
    if d.get("final_url") == d.get("url"):
        d["final_url"] = None
    if body_chars > 0 and body:
        d["excerpt"] = body if len(body) <= body_chars else body[:body_chars].rstrip() + "…"
    else:
        # 발췌는 있는데 이번 응답엔 안 실었다 — 있다는 사실만 알려 다음 호출을 유도한다.
        d["excerpt"] = None
        d["has_excerpt"] = bool(body)
    return d


def links_for_messages(
    conn: sqlite3.Connection, message_ids: list[int], *, body_chars: int = 0
) -> dict[int, list[dict]]:
    """글 rowid 목록 → {rowid: [link_content, …]}. 링크 없는 글은 키가 없다."""
    ids = [i for i in message_ids if i is not None]
    if not ids:
        return {}
    out: dict[int, list[dict]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i: i + 500]
        rows = conn.execute(
            f"SELECT {_PUBLIC_COLS} FROM message_links WHERE message_id IN (%s) ORDER BY id"
            % ",".join("?" * len(chunk)),
            chunk,
        ).fetchall()
        for r in rows:
            out.setdefault(r["message_id"], []).append(public_view(r, body_chars))
    return out


def link_by_url(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    """주소로 링크 행 하나(가장 최근 글 것). 원주소·최종주소 둘 다 본다."""
    key = dedupe_key(url)
    return conn.execute(
        f"""
        SELECT {_PUBLIC_COLS}, channel_id, msg_date FROM message_links
        WHERE url = ? OR url = ? OR final_url = ? OR final_url = ?
        ORDER BY msg_date DESC LIMIT 1
        """,
        (url, key, url, key),
    ).fetchone()


def messages_referencing(conn: sqlite3.Connection, url: str, limit: int = 5) -> list[sqlite3.Row]:
    key = dedupe_key(url)
    return conn.execute(
        """
        SELECT m.date, m.text, m.msg_id, m.channel_id, c.title AS channel, c.username
        FROM message_links l JOIN messages m ON m.id = l.message_id
        LEFT JOIN channels c ON c.id = m.channel_id
        WHERE l.url = ? OR l.url = ? OR l.final_url = ? OR l.final_url = ?
        ORDER BY m.date DESC LIMIT ?
        """,
        (url, key, url, key, limit),
    ).fetchall()


def prune_link_content(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """보존창 밖 링크의 제목·설명·발췌를 비운다(행은 남겨 '링크가 있었다'는 사실은 보존)."""
    cur = conn.execute(
        """
        UPDATE message_links SET title = '', description = '', body = ''
        WHERE msg_date < ? AND (COALESCE(body, '') != '' OR COALESCE(title, '') != ''
                                OR COALESCE(description, '') != '')
        """,
        (cutoff_iso,),
    )
    return cur.rowcount
