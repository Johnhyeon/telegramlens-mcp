"""가입한 공개 채널을 공유용 링크 목록으로 export — telegramlens-export-channels.

구매자에게 "이 채널들에 가입하면 TelegramLens가 수집한다"고 줄 온보딩 자산을 만든다.
DB(channels + channel_scores)에서 읽으므로 라이브 Telethon 접속이 필요 없고 데몬과
세션 경합도 없다. 공개 채널(@username 보유)만 포함 — 비공개 채널은 제외한다.

  telegramlens-export-channels                 # channels.md 생성(종목밀도순)
  telegramlens-export-channels --stock-only    # 주식 분류된 채널만
  telegramlens-export-channels -o list.md --csv # 경로 지정 + CSV 동시
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from telegram_lens import db


# tier(채널 성격) → 구매자용 한국어 라벨.
_TIER_LABEL = {
    "analyst": "증권사",
    "research": "리서치",
    "info": "정보",
    "gossip": "찌라시",
}


def _tier_label(t) -> str:
    return _TIER_LABEL.get(t or "", "-")


def _collect(stock_only: bool) -> list[dict]:
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.username, c.subscribers, c.about AS about,
                   s.density AS density, s.is_stock AS is_stock, s.mentions AS mentions,
                   t.tier AS tier,
                   (SELECT COUNT(*) FROM messages m WHERE m.channel_id = c.id) AS msgs
            FROM channels c
            LEFT JOIN channel_scores s ON s.channel_id = c.id
            LEFT JOIN channel_tier t ON t.channel_id = c.id
            WHERE c.username IS NOT NULL AND c.username != ''
            """
        ).fetchall()
    chans = [dict(r) for r in rows]
    if stock_only:
        chans = [c for c in chans if c.get("is_stock")]
    # 주식채널 우선 → 종목밀도 → 구독자 → 누적 메시지 순
    chans.sort(
        key=lambda c: (
            1 if c.get("is_stock") else 0,
            c.get("density") or 0,
            c.get("subscribers") or 0,
            c.get("msgs") or 0,
        ),
        reverse=True,
    )
    return chans


def _subs(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "-"


def _density(d) -> str:
    return f"{round(d * 100)}%" if isinstance(d, (int, float)) else "-"


def _write_markdown(chans: list[dict], out: Path) -> None:
    lines = [
        f"# 추천 텔레그램 채널 ({len(chans)}개)",
        "",
        "> 아래 **공개 채널**에 가입하면 TelegramLens가 해당 채널의 종목 언급·내러티브를",
        "> 수집합니다. 링크를 눌러(또는 텔레그램에서 검색해) 가입하세요.",
        "",
        "| # | 채널 | 분류 | 소개 | 가입 링크 | 구독자 | 종목밀도 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(chans, 1):
        title = (c.get("title") or "").replace("|", "\\|")
        about = (c.get("about") or "").replace("|", "\\|") or "-"
        link = f"https://t.me/{c['username']}"
        lines.append(
            f"| {i} | {title} | {_tier_label(c.get('tier'))} | {about} | {link} | "
            f"{_subs(c.get('subscribers'))} | {_density(c.get('density'))} |"
        )
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def _write_csv(chans: list[dict], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            ["title", "tier", "about", "username", "link",
             "subscribers", "density", "is_stock"]
        )
        for c in chans:
            w.writerow(
                [
                    c.get("title"),
                    _tier_label(c.get("tier")),
                    c.get("about") or "",
                    c["username"],
                    f"https://t.me/{c['username']}",
                    c.get("subscribers"),
                    c.get("density"),
                    c.get("is_stock"),
                ]
            )


def main() -> None:
    p = argparse.ArgumentParser(
        prog="telegramlens-export-channels",
        description="가입한 공개 채널을 공유용 링크 목록(Markdown/CSV)으로 export.",
    )
    p.add_argument(
        "-o", "--out", default="channels.md", help="Markdown 출력 경로. 기본 channels.md"
    )
    p.add_argument("--csv", action="store_true", help="같은 이름의 .csv 도 함께 출력.")
    p.add_argument(
        "--stock-only",
        action="store_true",
        help="classify 로 주식채널 판정된 것만 포함(telegram_classify_channels 선행 필요).",
    )
    p.add_argument(
        "--exclude",
        default="",
        help="제외할 채널 username(@ 제외) 쉼표 구분. 예: --exclude LeetKey_Labotory,foo. "
        "본인 채널 등 공유 리스트에서 빼고 싶을 때.",
    )
    args = p.parse_args()

    chans = _collect(args.stock_only)
    excluded = {u.strip().lstrip("@").lower() for u in args.exclude.split(",") if u.strip()}
    if excluded:
        chans = [c for c in chans if (c.get("username") or "").lower() not in excluded]
    if not chans:
        print(
            "공개 채널이 없습니다. 데몬/telegram_sync 로 채널이 수집된 뒤 다시 실행하거나, "
            "--stock-only 라면 telegram_classify_channels 를 먼저 돌리세요."
        )
        raise SystemExit(1)

    out = Path(args.out)
    _write_markdown(chans, out)
    print(f"공개 채널 {len(chans)}개 → {out}")
    if args.csv:
        csv_out = out.with_suffix(".csv")
        _write_csv(chans, csv_out)
        print(f"CSV → {csv_out}")


if __name__ == "__main__":
    main()
