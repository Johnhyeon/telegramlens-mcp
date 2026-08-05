"""TelegramLens — 텔레그램 종목 내러티브를 구조화하는 로컬 MCP 서버."""

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("telegramlens-mcp")
except Exception:
    __version__ = "0.0.0"
