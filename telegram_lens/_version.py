"""실행 코드의 버전 진실원천 (TL-01).

예전에는 __version__ 이 importlib.metadata(설치된 dist-info)를 읽었다.
그런데 dist-info 는 실행 코드와 어긋난다: editable 설치에서 소스만 갱신되면
옛 버전이 남고(실측: 소스 0.5.4 + 0.5.2.dist-info), 업그레이드가 꼬이면
0.4.2 메타가 0.5.4 코드 옆에 남는다. 그 값으로 업데이트 안내를 만들면
"0.5.4 실행 환경에 0.5.2 로 업데이트하세요"가 나간다.

실행 중인 코드가 몇 버전인지는 코드 자신이 말한다. 이 상수는 pyproject 의
version 과 같아야 하며, tests/test_version_truth.py 가 릴리스 전에 어긋남을
잡는다.
"""

from __future__ import annotations

CODE_VERSION = "0.7.0"


def dist_version() -> str | None:
    """설치 메타(dist-info)가 주장하는 버전. 코드 버전과 다를 수 있다.

    None 은 "메타를 못 읽음"(editable 등)이지 불일치가 아니다.
    """
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("telegramlens-mcp")
    except Exception:
        return None
