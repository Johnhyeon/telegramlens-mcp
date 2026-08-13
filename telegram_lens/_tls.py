"""TLS 신뢰 기준을 OS 인증서 저장소에 맞춘다 — 프로세스당 한 번.

파이썬 HTTP 클라이언트는 기본적으로 `certifi` 번들만 믿고 OS 인증서 저장소를 보지
않는다. 백신이나 회사망 장비가 TLS를 가로채 자기 루트로 재서명하는 PC에서는
브라우저는 멀쩡한데 우리만 `CERTIFICATE_VERIFY_FAILED` 로 죽는다
(2026-08-13 문의에서 확인된 원인).

TelegramLens 의 본체인 Telethon 은 MTProto 라 이 문제와 무관하다. 다만 종목 시세·
목록 조회(`prices.py`, `stocks.py`)와 폐기 목록·업데이트 확인은 httpx 를 쓰므로
같은 환경에서 같이 죽는다. 세 Lens 가 같은 기준을 쓰게 맞춘다 — 한쪽만 고치면
같은 PC 에서 어떤 도구는 되고 어떤 도구는 안 되는, 더 설명하기 어려운 상태가 된다.
"""

from __future__ import annotations

_applied = False


def apply() -> None:
    """프로세스당 한 번 적용. 어떤 경우에도 예외를 올리지 않는다 — 신뢰 범위를
    넓히려다 서버가 안 뜨면 본말전도다. 실패하면 조용히 기존 동작(certifi)으로 남는다.
    """
    global _applied
    if _applied:
        return
    _applied = True
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass
