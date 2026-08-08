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
import json
import os
import sys
import time

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

# 폐기된 키 전용 안내. LOCKED_MESSAGE와 달리 "키를 넣으세요"라고 하면 안 된다 —
# 이 사람은 키를 갖고 있고, 그 키가 중지된 것이다. 할 일은 연락이지 재입력이 아니다.
REVOKED_MESSAGE = (
    "🔒 이 라이선스 키는 현재 사용이 중지되어 있습니다.\n"
    "\n"
    "환불 또는 결제 취소된 키로 확인됩니다.\n"
    "착오라고 생각되시면 알려주세요 — 확인 후 바로 풀어드리겠습니다.\n"
    "\n"
    "· 문의: osy980315@gmail.com"
)

_licensed_cache = False  # 한 번 유효하면 프로세스 동안 재검증 생략


def _license_path():
    return data_dir() / "license.key"


def _decode(key_str: str) -> bytes:
    s = key_str.strip().upper().replace("-", "").replace(" ", "")
    s += "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s)


def mask_tail(value: str, keep: int = 4) -> str:
    """마지막 keep자만 남기고 나머지는 '*'로 가림. 로그·JSON에 원문 노출 방지용."""
    v = (value or "").strip()
    if len(v) <= keep:
        return "*" * len(v)
    return "*" * 4 + v[-keep:]


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


# ---------- 폐기(거부) 목록 ----------
#
# 환불·취소된 키를 막는 최소 장치. **원격 종료가 아니라 거부 목록이다** — 목록은
# GitHub의 텍스트 파일이고, 판정은 이 컴퓨터에서 이 코드가 한다.
#
# 지켜야 할 규칙은 하나뿐: **모르면 통과시킨다.**
# 네트워크 실패·파일 깨짐·목록 없음 — 전부 통과다. 목록을 못 읽는다고 돈 낸 사람이
# 잠기는 쪽이, 환불한 사람이 며칠 더 쓰는 것보다 훨씬 비싸다.
#
# 한계(알고 시작한 것): 업데이트를 안 한 옛 버전에는 이 검사 자체가 없어서 안 걸린다.
# 대신 키를 공유받아 새로 까는 사람은 최신을 받으므로 바로 걸린다.
_REVOKED_URL = "https://raw.githubusercontent.com/Johnhyeon/leetkit-manager/main/revoked.json"
_REVOKED_TTL = 86400.0  # 하루 한 번만 다시 받는다
_REVOKED_TIMEOUT = 2.5  # 도구 호출을 오래 붙잡고 있으면 안 된다

_revoked_fetched_this_process = False


def _revoked_cache_path():
    return data_dir() / "revoked_cache.json"


def _load_revoked_cache():
    try:
        data = json.loads(_revoked_cache_path().read_text(encoding="utf-8"))
        ids = [str(x).strip().lower() for x in data.get("revoked", []) if isinstance(x, str)]
        return ids, float(data.get("fetched_at", 0))
    except Exception:
        return [], 0.0


def _save_revoked_cache(ids, now) -> None:
    try:
        p = _revoked_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"revoked": ids, "fetched_at": now}), encoding="utf-8")
    except Exception:
        pass  # 캐시를 못 써도 동작에는 영향이 없어야 한다


def _fetch_revoked():
    """목록을 새로 받는다. 실패하면 None(= 모름, 통과)."""
    try:
        import httpx

        response = httpx.get(_REVOKED_URL, timeout=_REVOKED_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        ids = response.json().get("revoked")
    except Exception:
        return None
    if not isinstance(ids, list):
        return None
    return [str(x).strip().lower() for x in ids if isinstance(x, str)]


def is_revoked(license_id: str) -> bool:
    """이 번호가 거부 목록에 있으면 True. 확실하지 않으면 언제나 False."""
    global _revoked_fetched_this_process
    lid = (license_id or "").strip().lower()
    if not lid:
        return False

    ids, fetched_at = _load_revoked_cache()
    now = time.time()
    # 프로세스당 최대 한 번, 그것도 캐시가 하루 지났을 때만 네트워크를 탄다.
    # is_licensed()는 도구 호출마다 불리므로 여기서 매번 받으면 안 된다.
    if not _revoked_fetched_this_process and (now - fetched_at) > _REVOKED_TTL:
        _revoked_fetched_this_process = True
        fresh = _fetch_revoked()
        if fresh is not None:
            ids = fresh
            _save_revoked_cache(ids, now)
    return lid in ids


def license_block_reason():
    """잠긴 이유. 정상이면 None.

    "없음/깨짐"과 "폐기됨"은 사용자가 할 일이 완전히 다르다 — 앞은 키를 넣어야 하고,
    뒤는 연락해야 한다. 같은 문구를 보여주면 환불 착오인 사람이 키를 다시 넣어보며
    시간을 버린다.
    """
    k = stored_key()
    if not k:
        return "missing"
    res = verify_key(k)
    if not res["valid"]:
        return "invalid"
    if is_revoked(res.get("license_id", "")):
        return "revoked"
    return None


def locked_message() -> str:
    return REVOKED_MESSAGE if license_block_reason() == "revoked" else LOCKED_MESSAGE


def is_licensed() -> bool:
    global _licensed_cache
    if _licensed_cache:
        return True
    k = stored_key()
    if not k:
        return False
    res = verify_key(k)
    if not res["valid"]:
        return False
    if is_revoked(res.get("license_id", "")):
        return False  # 폐기된 키는 캐시하지 않는다 — 목록에서 빠지면 곧바로 다시 풀려야 한다
    _licensed_cache = True
    return True


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


def _read_key_from_stdin() -> str | None:
    """표준입력 한 줄을 키로 읽는다. argv에 담지 않으므로 프로세스 목록(ps/작업관리자)에
    키가 노출되지 않는다 — Manager 등 자동화가 activation을 호출할 때 쓰는 경로."""
    try:
        line = sys.stdin.readline()
    except Exception:
        return None
    return line.strip() or None


def _build_activate_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="telegramlens-activate",
        description="TelegramLens 라이선스 키 활성화",
    )
    p.add_argument(
        "key",
        nargs="*",
        help=(
            "라이선스 키 (기존 방식 — 공백으로 잘려도 합쳐서 인식). "
            "프로세스 목록에 노출될 수 있어 자동화에는 --stdin 권장."
        ),
    )
    p.add_argument(
        "--stdin",
        action="store_true",
        help="표준입력에서 키를 한 줄 읽는다 (프로세스 목록에 노출 안 됨).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="결과를 JSON으로 stdout에 출력 (Manager 연동용).",
    )
    return p


def activate_cli() -> None:
    """`telegramlens-activate` 진입점.

    사용법:
        telegramlens-activate <KEY>           기존 방식 (인자 없으면 대화형 입력)
        telegramlens-activate --stdin         표준입력에서 키 읽기 (프로세스 목록 비노출)
        telegramlens-activate --stdin --json  Manager 연동용 구조화 출력
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = _build_activate_parser().parse_args(sys.argv[1:])

    if args.stdin:
        key = _read_key_from_stdin()
    elif args.key:
        key = " ".join(args.key).strip()
    elif not args.json:
        key = _prompt_key()
    else:
        key = None

    if not key:
        active = is_licensed()
        if args.json:
            print(json.dumps({"ok": active, "status": "active" if active else "not_active"}, ensure_ascii=False))
            sys.exit(0 if active else 1)
        if active:
            print("현재 상태: 활성화됨 ✅")
            sys.exit(0)
        print("현재 상태: 미활성화 ❌\n")
        print(_usage())
        sys.exit(1)

    res = save_key(key)
    key = None  # 저장 직후 로컬 참조 제거 — 이후 경로에서 키 원문을 다시 쓰지 않는다.

    if res["valid"]:
        if args.json:
            print(json.dumps(
                {"ok": True, "status": "active", "license_id_masked": mask_tail(res["license_id"].upper())},
                ensure_ascii=False,
            ))
            sys.exit(0)
        print(f"활성화 완료 ✅  (license_id: {res['license_id']})")
        print("Claude Desktop을 완전히 종료했다가 다시 켜면 TelegramLens 도구를 쓸 수 있습니다.")
        sys.exit(0)

    if args.json:
        print(json.dumps({"ok": False, "status": "invalid", "reason": res["reason"]}, ensure_ascii=False))
        sys.exit(1)
    print(f"활성화 실패 ❌  — {res['reason']}\n")
    print("· 결제 후 발송된 키를 공백 없이 정확히 붙여넣었는지 확인하세요.")
    if PURCHASE_URL:
        print(f"· 키 재발송·문의: {PURCHASE_URL}")
    sys.exit(1)
