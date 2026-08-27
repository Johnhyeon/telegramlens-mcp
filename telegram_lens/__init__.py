"""TelegramLens — 텔레그램 종목 내러티브를 구조화하는 로컬 MCP 서버."""

# 실행 코드의 버전은 코드 자신이 말한다(TL-01). dist-info(importlib.metadata)는
# editable 설치·꼬인 업그레이드에서 실행 코드와 어긋난다 - 그 값은
# _version.dist_version() 으로 따로 읽는다.
from telegram_lens._version import CODE_VERSION as __version__  # noqa: F401
