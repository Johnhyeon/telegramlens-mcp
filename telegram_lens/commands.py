"""'!' 명령 처리 — 데몬이 사용자의 '나에게(Saved Messages)' 명령을 받아 DB 조회로 즉답.

클로드를 거치지 않고 텔레그램 DB 에서만 뽑아 '정리'한다(환각 0, 즉시). 추론·분석·판단은
데스크탑 Claude 의 몫. 모바일에서 브리핑을 받은 뒤 그 자리에서 종목/키워드를 더 파보는 용도.

명령:
  !<종목명>  또는  !종목 <종목명>  — 그 종목의 텔레그램 언급·원문·링크
  !검색 <키워드>                    — 키워드 원문 검색
  !트렌딩 (또는 !버즈)              — 최근 많이 언급된 종목
"""

from __future__ import annotations

from telegram_lens import queries
from telegram_lens.stocks import resolve_code


def _diverse_samples(samples: list, n: int) -> list:
    """채널 다양화 — 채널당 1개, 확산(forwards) 높은 것 우선, 내용 중복 제거.

    뉴스 firehose 채널 하나가 샘플을 도배하거나 같은 뉴스가 반복되는 걸 막는다.
    distinct 채널이 n 보다 적으면 채널 중복을 허용해 채운다(내용 중복만 계속 제외).
    """
    ranked = sorted(samples, key=lambda s: (s.get("forwards") or 0), reverse=True)
    seen_ch, seen_txt, picked = set(), set(), []
    for s in ranked:
        ch = s.get("channel") or ""
        key = " ".join((s.get("text") or "").split())[:30]
        if not key or key in seen_txt or ch in seen_ch:
            continue
        seen_ch.add(ch)
        seen_txt.add(key)
        picked.append(s)
        if len(picked) >= n:
            return picked
    for s in ranked:  # 채널 부족 시 내용 중복만 피해 채움
        key = " ".join((s.get("text") or "").split())[:30]
        if not key or key in seen_txt:
            continue
        seen_txt.add(key)
        picked.append(s)
        if len(picked) >= n:
            break
    return picked


def _samples_block(samples: list, n: int = 5) -> list:
    lines = []
    for s in _diverse_samples(samples, n):
        txt = " ".join((s.get("text") or "").split())[:90]
        lines.append(f"· {txt}")
        link = s.get("telegram_link")
        if link:
            lines.append(f"  {s.get('channel', '')} {link}")
    return lines


def _fmt_stock(query: str) -> str:
    code, name = resolve_code(query)
    if not code:
        return f"'{query}' — 종목을 못 찾았어요. !검색 {query} 로 원문을 찾아보세요."
    r = queries.stock_buzz(code=code, name=name, hours=24, samples=20)
    sm = r.get("summary") or {}
    stat = (
        f"{sm.get('independent', 0)}건 · {sm.get('channels', 0)}개 채널 "
        f"· 확산 {sm.get('total_forwards', 0)}"
    )
    if (sm.get("channels") or 0) <= 1:
        stat += " · 단일 채널(신호 약함)"
    lines = [f"📊 {name}({code}) — 최근 24시간 텔레그램", stat, ""]
    body = _samples_block(r.get("samples") or [], 5)
    if not body:
        lines.append("최근 24시간 언급이 거의 없어요.")
    else:
        lines += body
    return "\n".join(lines)


def _fmt_search(query: str) -> str:
    query = query.strip()
    if not query:
        return "검색어를 주세요. 예: !검색 전력 ETF"
    r = queries.search_messages(query, hours=48, limit=15)
    n = r.get("matched", 0)
    if not n:
        return f"'{query}' — 최근 48시간 텔레그램에서 못 찾았어요."
    lines = [f"🔎 '{query}' — 최근 48시간 {n}건", ""]
    lines += _samples_block(r.get("results") or [], 6)
    return "\n".join(lines)


def _fmt_trending() -> str:
    rows = queries.trending(hours=12, top=10, kind="all")
    lines = ["🔥 최근 12시간 많이 언급된 종목", ""]
    for i, s in enumerate(rows, 1):
        cnt = s.get("independent") or s.get("raw_messages") or 0
        lines.append(f"{i}. {s.get('name', '?')} — {cnt}건 {s.get('channels', 0)}채널")
    lines += ["", "종목 자세히: !<종목명>  ·  키워드: !검색 <말>"]
    return "\n".join(lines)


def _help() -> str:
    return (
        "명령:\n"
        "· !<종목명> 또는 !종목 <종목명> — 그 종목 텔레그램 언급·원문\n"
        "· !검색 <키워드> — 키워드 원문 검색\n"
        "· !트렌딩 (또는 !버즈) — 많이 언급된 종목\n"
        "분석·판단은 데스크탑 Claude 에서 물어보세요."
    )


def handle_command(cmd: str) -> str:
    """'!' 를 뗀 명령 문자열을 받아 답장 텍스트(plain text)를 반환한다."""
    cmd = (cmd or "").strip()
    if not cmd or cmd in ("?", "help", "도움", "도움말"):
        return _help()
    parts = cmd.split(maxsplit=1)
    head, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    low = head.lower()
    if low in ("트렌딩", "버즈", "trending", "buzz"):
        return _fmt_trending()
    if low in ("검색", "search", "찾기"):
        return _fmt_search(arg)
    if low in ("종목", "stock"):
        return _fmt_stock(arg) if arg else _help()
    # 키워드 없으면 명령 전체를 종목명으로 시도 (예: "!태성")
    return _fmt_stock(cmd)
