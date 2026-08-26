"""문서가 메타 규약 v3 를 실제로 설명하는가 - 정적 검사.

코드에 필드를 넣어 놓고 문서에 안 적으면, 그 필드는 없는 것과 같다. 읽는 쪽은
문서를 보고 무엇을 믿을지 정한다. 여기서 보는 것은 "그 말이 문서 어딘가에
있는가"뿐이고, 문장이 좋은지까지는 사람이 본다.

패치노트 버전 검사도 함께 둔다. 릴리스 워크플로가 같은 걸 보는데, 거기서 걸리면
이미 태그를 밀어 놓은 뒤다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _version() -> str:
    m = re.search(r'^version = "([^"]+)"', _text("pyproject.toml"), re.M)
    assert m, "pyproject.toml 에 version 이 없습니다."
    return m.group(1)


def test_patchnotes_has_a_section_for_the_current_version():
    """앱은 main 브랜치의 패치노트를 그대로 읽는다. 버전이 없으면 빈 화면이 뜬다."""
    version = _version()
    sections = re.findall(r"^## ([^\s]+) ", _text("PATCHNOTES.md"), re.M)
    assert version in sections, f"PATCHNOTES.md 에 {version} 절이 없습니다 (있는 것: {sections[:5]})"


def test_patchnote_sections_keep_the_shape_the_app_parses():
    heads = [l for l in _text("PATCHNOTES.md").splitlines() if l.startswith("## ")]
    assert heads, "패치노트 절이 하나도 없습니다."
    for h in heads:
        assert re.match(r"^## \S+ . \d{4}-\d{2}-\d{2}$", h), h


def test_patchnotes_say_tools_did_not_change():
    """TelegramLens 는 메타 버전만 맞췄다. 도구가 바뀐 것처럼 읽히면 안 된다."""
    body = _text("PATCHNOTES.md")
    assert "v3" in body
    assert "도구" in body
