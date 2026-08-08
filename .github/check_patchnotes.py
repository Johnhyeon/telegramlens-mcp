#!/usr/bin/env python3
"""릴리스하려는 버전의 패치노트가 실제로 쓰여 있는지 확인한다. 없으면 exit 1.

이 게이트가 없으면 자동화는 몇 달 안에 썩는다 — 바쁠 때 한 번 건너뛰면 그 뒤로
아무도 안 쓰고, 고객은 커밋 메시지에서 자동 생성된 개발 용어를 읽게 된다.
PATCHNOTES.md는 구매자가 앱 안에서 그대로 읽는 화면이라 비면 바로 티가 난다.

사용: python .github/check_patchnotes.py 0.2.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# leetkit_manager/patch_notes.py의 _HEADING과 같은 규칙이어야 한다 — CI는 통과했는데
# 앱은 못 읽는 상황을 만들지 않기 위해서다(대시는 —, –, - 셋 다 허용).
_DASH = "—–-"


def find_section(text: str, version: str) -> str | None:
    """해당 버전 절의 본문. 절 자체가 없으면 None."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    heading = re.search(
        rf"^##\s+v?{re.escape(version)}\s*[{_DASH}]\s*\S+\s*$", text, re.M
    )
    if not heading:
        return None
    rest = text[heading.end() :]
    # 다음 절 제목 전까지가 이 버전의 내용
    return re.split(r"^##\s", rest, maxsplit=1, flags=re.M)[0]


def has_content(section: str) -> bool:
    """제목만 있고 내용이 없는 절은 안 쓴 것과 같다."""
    return any(line.strip().startswith(("-", "*", ">")) for line in section.splitlines())


def main(argv: list[str]) -> int:
    # CI(우분투)는 UTF-8이지만 손으로 돌려볼 때 윈도우 콘솔은 cp949라, 한글·em dash를
    # 찍는 순간 UnicodeEncodeError로 죽는다 — 정작 무엇이 잘못됐는지는 못 읽게 된다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(argv) != 2:
        print("usage: check_patchnotes.py <version>", file=sys.stderr)
        return 2
    version = argv[1]
    path = Path("PATCHNOTES.md")
    if not path.is_file():
        print("::error::PATCHNOTES.md가 없습니다.")
        return 1

    section = find_section(path.read_text(encoding="utf-8"), version)
    if section is None:
        print(
            f"::error::PATCHNOTES.md에 '## {version} — <날짜>' 절이 없습니다. "
            "구매자가 앱에서 읽는 내용이라 릴리스 전에 반드시 씁니다."
        )
        return 1
    if not has_content(section):
        print(f"::error::PATCHNOTES.md의 {version} 절이 비어 있습니다.")
        return 1

    print(f"Patch notes OK: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
