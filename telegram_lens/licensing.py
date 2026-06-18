"""라이선스 검증 — Ed25519 전자서명 기반, 서버 없이 로컬 검증.

판매자가 개인키로 서명해 발급한 라이선스 키를, 패키지에 박힌 공개키로 검증한다.
공개키는 '검증'만 가능하므로 코드에 노출돼도 새 키를 위조할 수 없다. 유효키
목록도, 인증 서버도 필요 없다(완전 오프라인).

StockLens/DartLens 와 같은 판매자 키쌍을 쓰되, PRODUCT 태그(TGLN)로 제품을 가른다.
같은 키쌍이라도 태그가 달라 StockLens(STKL)·DartLens(DART) 키는 여기서 거부된다.

활성화:  telegramlens-activate <라이선스-키>
저장 위치:  ~/.telegramlens/license.key  (config.data_dir 기준, TELEGRAMLENS_HOME 존중)
개발 우회:  환경변수 TELEGRAMLENS_LICENSE_KEY
"""

from __future__ import annotations

import base64
import os
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from telegram_lens.config import data_dir

# 제품 태그(4글자). 같은 판매자 키쌍이라도 태그가 달라 StockLens/DartLens 키는 거부됨.
PRODUCT = b"TGLN"

# 판매자 공개키(raw 32B, base64). 검증 전용. 개인키는 판매자 PC에만 존재한다.
# StockLens/DartLens 와 동일한 키쌍 — _seller/keygen.py 의 공개키와 같아야 한다.
_PUBLIC_KEY_B64 = "hHyMqV47+capkk0UTwy9C5dP85RN7KhL1txJ25aZkqw="

_ENV_KEY = "TELEGRAMLENS_LICENSE_KEY"

# 구매(상품) 페이지 링크 — 확정되면 이 한 줄만 채우면 모든 안내에 자동 노출된다.
PURCHASE_URL = "https://litt.ly/leetkey_lab/sale/hzGHnRY"


def _purchase_line(prefix: str = "· 구매: ") -> str:
    """PURCHASE_URL이 설정돼 있을 때만 안내 줄을 반환(없으면 빈 문자열)."""
    return f"\n{prefix}{PURCHASE_URL}" if PURCHASE_URL else ""


LOCKED_MESSAGE = (
    "🔒 TelegramLens는 유료 라이선스가 필요합니다.\n"
    "\n"
    "구매 시 발송된 라이선스 키로 활성화하세요:\n"
    "    telegramlens-activate <라이선스-키>\n"
    "\n"
    "· 키는 결제 완료 후 이메일로 발송됩니다."
    + _purchase_line()
)

_licensed_cache = False  # 한 번 유효하면 프로세스 동안 재검증 생략


def _license_path():
    return data_dir() / "license.key"


def _decode(key_str: str) -> bytes:
    s = key_str.strip().upper().replace("-", "").replace(" ", "")
    s += "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s)


def verify_key(key_str: str) -> dict:
    """키 문자열이 '판매자가 서명한 이 제품의 진짜 키'인지 검증."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(_PUBLIC_KEY_B64))
    except Exception:
        return {"valid": False, "reason": "공개키 설정 오류"}
    try:
        raw = _decode(key_str)
    except Exception:
        return {"valid": False, "reason": "형식 오류(깨진 키)"}
    if len(raw) != 74 or raw[:4] != PRODUCT:
        return {"valid": False, "reason": "이 제품의 키가 아님"}
    payload, sig = raw[:10], raw[10:]
    try:
        pub.verify(sig, payload)
    except InvalidSignature:
        return {"valid": False, "reason": "서명 불일치(위조/변조)"}
    return {"valid": True, "license_id": payload[4:].hex()}


def stored_key() -> str | None:
    env = os.environ.get(_ENV_KEY)
    if env and env.strip():
        return env.strip()
    p = _license_path()
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def is_licensed() -> bool:
    global _licensed_cache
    if _licensed_cache:
        return True
    k = stored_key()
    if k and verify_key(k)["valid"]:
        _licensed_cache = True
        return True
    return False


def save_key(key_str: str) -> dict:
    res = verify_key(key_str)
    if not res["valid"]:
        return res
    p = _license_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key_str.strip(), encoding="utf-8")
    global _licensed_cache
    _licensed_cache = True
    return res


def _usage() -> str:
    lines = [
        "TelegramLens 라이선스 활성화",
        "",
        "사용법:",
        "    telegramlens-activate <라이선스-키>",
        "",
        "· 키는 결제 완료 후 이메일로 발송됩니다.",
    ]
    if PURCHASE_URL:
        lines.append(f"· 구매: {PURCHASE_URL}")
    return "\n".join(lines)


def _prompt_key() -> str | None:
    """인자 없이 실행하면 터미널에서 키를 직접 붙여넣도록 안내.

    파이프/비대화형 환경(tty 아님)에서는 멈추지 않도록 None을 반환한다.
    """
    if not sys.stdin.isatty():
        return None
    print("TelegramLens 라이선스 활성화")
    print("결제 후 이메일로 받은 라이선스 키를 붙여넣으세요.")
    if PURCHASE_URL:
        print(f"아직 구매 전이라면 → {PURCHASE_URL}")
    try:
        return input("라이선스 키 ▸ ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def activate_cli() -> None:
    """`telegramlens-activate <KEY>` 진입점. 인자가 없으면 키 입력을 안내한다."""
    args = sys.argv[1:]
    key = " ".join(args).strip() if args else _prompt_key()

    if not key:
        if is_licensed():
            print("현재 상태: 활성화됨 ✅")
            sys.exit(0)
        print("현재 상태: 미활성화 ❌\n")
        print(_usage())
        sys.exit(1)

    res = save_key(key)
    if res["valid"]:
        print(f"활성화 완료 ✅  (license_id: {res['license_id']})")
        print("Claude Desktop을 완전히 종료했다가 다시 켜면 TelegramLens 도구를 쓸 수 있습니다.")
        sys.exit(0)
    print(f"활성화 실패 ❌  — {res['reason']}\n")
    print("· 결제 후 발송된 키를 공백 없이 정확히 붙여넣었는지 확인하세요.")
    if PURCHASE_URL:
        print(f"· 키 재발송·문의: {PURCHASE_URL}")
    sys.exit(1)
