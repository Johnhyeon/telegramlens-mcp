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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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

# 기간이 끝난 키 전용 안내. LOCKED_MESSAGE("키를 넣으세요")도, REVOKED_MESSAGE
# ("연락 주세요")도 여기엔 안 맞는다 — 이 사람은 잘 쓰다가 기간이 끝난 것이고,
# 할 일은 구매다. 체험이 끝난 사람에게 가장 자주 보일 문구라 사과나 경고가 아니라
# 다음 걸음을 적는다.
EXPIRED_MESSAGE = (
    "🔒 TelegramLens 사용 기간이 끝났습니다.\n"
    "\n"
    "계속 쓰시려면 라이선스를 구매하신 뒤 받으신 키로 활성화하세요:\n"
    "    telegramlens-activate <라이선스-키>"
    + _purchase_line()
)

# 컴퓨터 날짜가 과거로 돌아가 있을 때. 일부러 돌린 사람과 시계가 고장난 사람(메인보드
# 배터리가 닳으면 실제로 이렇게 된다)을 우리는 구분할 수 없다 — 그래서 추궁하지 않고,
# 두 경우 모두에게 맞는 한 가지 할 일만 적는다. "부정 사용"이라고 썼다가 배터리가
# 닳은 정직한 사용자를 범인 취급하면 그 손해가 훨씬 크다.
CLOCK_MESSAGE = (
    "🔒 이 컴퓨터의 날짜가 실제보다 과거로 설정되어 있어 TelegramLens를 열 수 없습니다.\n"
    "\n"
    "날짜와 시간을 현재에 맞춘 뒤 다시 시도해주세요.\n"
    "(Windows: 설정 → 시간 및 언어 / Mac: 시스템 설정 → 일반 → 날짜 및 시간)"
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


# 키 길이. 서명(64B) 앞에 붙는 payload 두 가지를 모두 받는다.
#   기존(74B): 제품태그(4) + 라이선스ID(6)
#   만료형(78B): 제품태그(4) + 라이선스ID(6) + 만료일(4)
# 이미 발급한 키를 무효화하지 않으려고 둘 다 유효하게 둔다 — 체험판·구독처럼 기간이
# 있는 키만 뒤쪽 형태로 발급하면 된다.
_SIG_LEN = 64
_PAYLOAD_LEN = 10
_PAYLOAD_LEN_WITH_EXPIRY = 14

# 만료일은 1970-01-01(UTC)부터의 '일수'를 4바이트 빅엔디언으로 담는다. 시각이 아니라
# 날짜인 이유: 시:분까지 따지면 시간대가 다른 사용자에게 "오늘까진 줄 알았는데" 하는
# 억울함이 생긴다. 그 날짜의 끝(UTC 자정)까지 유효한 것으로 본다.
_EXPIRY_EPOCH = date(1970, 1, 1)


def _expiry_from_payload(payload: bytes) -> date | None:
    """payload에서 만료일을 꺼낸다. 만료가 없는 기존 키면 None."""
    if len(payload) < _PAYLOAD_LEN_WITH_EXPIRY:
        return None
    days = int.from_bytes(payload[10:14], "big")
    try:
        return _EXPIRY_EPOCH + timedelta(days=days)
    except OverflowError:
        return None


def expires_on() -> date | None:
    """지금 저장된 키의 만료일. 기간 없는 키(기존 구매자)면 None."""
    k = stored_key()
    if not k:
        return None
    return effective_expiry(verify_key(k))


def _is_expired(expiry: "date | None") -> bool:
    """그 날짜가 지났는가. 만료가 없으면 언제나 False.

    비교는 UTC 날짜로 한다 — 로컬 시간대로 하면 같은 키가 나라마다 하루씩 다르게
    끝난다. 날짜를 과거로 돌려 만료를 피하는 건 _clock_turned_back 이 따로 본다.
    """
    if expiry is None:
        return False
    return datetime.now(timezone.utc).date() > expiry


# ── 체험 기간은 '키를 넣은 날'부터 센다 ────────────────────────────────────
#
# 키에 박히는 만료일은 keygen 이 '만든 날 + N일'로 굳혀버린다. 그래서 미리 뽑아두면
# 팔리기도 전에 기간이 흘러가고, 늦게 등록한 사람은 며칠만 쓴다. 그걸 피하려면 키를
# 매일 새로 뽑아야 하는데, 그러려면 판매자 PC가 매일 켜져 있어야 한다 — 1인 운영에서
# 는 안 될 일이다.
#
# 그래서 키에는 넉넉한 만료일을 넣어 한 번에 대량으로 뽑아두고, 실제 기간은 '이
# 컴퓨터에서 처음 확인된 날 + _TRIAL_WINDOW_DAYS'로 센다. 둘 중 빠른 쪽이 진짜 만료다.
#
#     키 만료 2026-12-31 · 9월 10일 등록 → min(12-31, 9-24) = 9-24
#
# 이 파일을 지우면 창이 다시 열린다. 완벽하지 않은 걸 알고 받아들인다 — 바로 아래
# clock_seen 과 같은 급의 방어이고(지우면 기준이 사라진다), 대신 키에 박힌 만료일이
# 상한이라 무한이 아니라 거기서 영구히 끝난다. license_id 별로 적어두므로, 메일에
# 남아 있는 같은 키를 다시 붙여넣는 것으로는 늘어나지 않는다.
#
# 전제: 기간이 있는 키 = 체험판 (2026-08 기준). 나중에 기간제 구독을 팔면 이 전제가
# 깨진다 — 그때는 기간(일수)을 키 payload 에 같이 서명해 넣어야 한다.
_TRIAL_WINDOW_DAYS = 14


def _trial_mark_paths() -> "list[Path]":
    """체험 시작일을 적어두는 자리들. 읽을 때는 가장 이른 날짜를 쓰고, 쓸 때는 전부에
    쓴다 — 한 곳이 지워져도 나머지에서 복원되므로 둘 다 찾아 지워야 창이 열린다.

    두 번째 자리로 Downloads 밑(예: kstock)은 일부러 피했다. 거기는 사람들이 주기적으로
    비우는 거의 유일한 폴더라 '지워지기 어려운 자리'로는 정반대다. 게다가 고지한 적도
    없는 폴더라, 지워지면 지원 문의 때 필요한 실행 기록까지 같이 날아간다.

    브랜드 공용 폴더(~/.leetkit)를 쓴다. Lens 폴더(~/.telegramlens)를 통째로 지우고 다시
    깔아도 여기는 남는다. 매니저가 이미 ~/.leetkit-manager 를 같은 식으로 쓴다.
    """
    return [
        data_dir() / "trial_started.json",
        Path.home() / ".leetkit" / "trial_started.json",
    ]


# 마킹 키에 제품 태그를 붙인다. 세 Lens 가 같은 파일을 쓰는데 태그가 없으면,
# 체험 한 번에 나가는 키 3장(각각 license_id 가 다르다)이 서로를 '남의 체험 키'로
# 읽는다 — 실제로 첫 키만 등록되고 나머지 둘이 거부됐다.
def _mark_key(license_id: str) -> str:
    return PRODUCT.decode() + ":" + license_id


def _read_trial_marks(path: Path) -> dict:
    """그 자리에 적힌 {license_id: 날짜}. 없거나 깨졌으면 빈 dict."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_trial_mark(path: Path, license_id: str, day: date) -> bool:
    """그 자리에 시작일을 적는다(다른 키 기록은 보존). 성공하면 True."""
    marks = _read_trial_marks(path)
    slot = _mark_key(license_id)
    if marks.get(slot) == day.isoformat():
        return True  # 이미 같은 값 — 매번 다시 쓸 이유가 없다
    marks[slot] = day.isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marks), encoding="utf-8")
    except OSError:
        return False
    return True


def _trial_started_on(license_id: str) -> "date | None":
    """이 키를 처음 확인한 날. 어느 자리에도 없으면 오늘로 적고 오늘을 돌려준다.

    처음인데 어디에도 못 적었으면 None을 준다 — 그러면 다음에도 '오늘'이 되어 창이
    매번 새로 시작하는 셈이라 씌우나 마나다. 그때는 창 없이 키에 박힌 만료일만 믿는다.
    상태 파일 때문에 쓰던 사람이 잠기는 쪽이 훨씬 나쁘다(폐기 목록의 '모르면 통과'와
    같은 원칙).
    """
    if not license_id:
        return None

    paths = _trial_mark_paths()
    found = []
    for path in paths:
        marks = _read_trial_marks(path)
        # 태그 붙은 키를 먼저 보고, 없으면 태그 없던 옛 형식(0.7.0 이전)을 본다.
        # 이걸 빠뜨리면 업데이트를 받는 것만으로 체험 기간이 처음부터 다시 시작한다 —
        # 쓰던 사람에게는 이득이라 아무도 신고하지 않고, 그래서 조용히 새어나간다.
        recorded = marks.get(_mark_key(license_id))
        if recorded is None:
            recorded = marks.get(license_id)
        if isinstance(recorded, str):
            try:
                found.append(date.fromisoformat(recorded))
            except ValueError:
                pass  # 손상된 값 — 아래에서 제대로 다시 적는다

    started = min(found) if found else datetime.now(timezone.utc).date()

    # 지워진 자리를 되살리는 것도 여기서 같이 된다 — 살아남은 날짜를 전부에 다시 쓴다.
    wrote_any = False
    for path in paths:
        if _write_trial_mark(path, license_id, started):
            wrote_any = True

    if not found and not wrote_any:
        return None
    return started


def _other_trial_used(res: dict) -> str | None:
    """이 컴퓨터가 예전에 **다른** 체험 키를 쓴 적이 있으면 그 license_id 를 준다.

    기간 있는 키(체험)에만 적용한다. 구매자 키는 만료가 없어 여기 안 걸린다 —
    체험을 쓰던 사람이 사서 정식 키를 넣는 흐름이 막히면 안 된다.
    같은 키를 다시 넣는 것도 막지 않는다(재설치·키 재입력은 정상적인 일이다).

    한계는 분명하다. 기록 파일을 지우면 풀린다 — clock_seen 과 같은 수준의 방어이고,
    작정한 사람을 막는 게 목적이 아니라 '이메일만 바꿔 계속 이어 쓰는' 쉬운 길을
    닫는 게 목적이다. 키에 박힌 절대 만료일이 그 위의 상한으로 남는다.
    """
    if res.get("expires_on") is None:
        return None
    mine = res.get("license_id") or ""
    if not mine:
        return None
    # **같은 제품끼리만 본다.** 체험 한 번에 세 제품 키가 한 장씩 나가는데, 태그 없이
    # 비교하면 자기 다음 키를 남의 것으로 읽어 정상 사용자가 막힌다.
    prefix = PRODUCT.decode() + ":"
    slot = _mark_key(mine)
    for path in _trial_mark_paths():
        for other in _read_trial_marks(path):
            if other.startswith(prefix) and other != slot:
                return other[len(prefix):]
    return None


def effective_expiry(res: dict) -> "date | None":
    """이 키가 실제로 언제 끝나는가. 기간 없는 키(구매자)면 None 그대로.

    화면에 보이는 남은 날짜도 이 값을 써야 한다 — 서명에 박힌 날짜를 그대로 보여주면
    매니저 배지가 "12월까지"라고 했다가 9월에 잠기는 꼴이 된다.
    """
    signed = res.get("expires_on")
    if signed is None:
        return None
    started = _trial_started_on(res.get("license_id", ""))
    if started is None:
        return signed
    return min(signed, started + timedelta(days=_TRIAL_WINDOW_DAYS))


# 시계 되돌리기 감지 — 기간이 있는 키에서만 본다.
#
# 만료일 검사는 이 컴퓨터의 날짜를 믿는다. 날짜를 과거로 돌리면 만료된 키가 다시
# 열린다. 그래서 '지금까지 본 가장 늦은 날짜'를 적어두고, 오늘이 그보다 한참 이르면
# 되돌린 것으로 본다.
#
# 관대하게 잡는 이유: 잘못 걸리면 멀쩡한 사용자가 잠긴다. NTP 보정·시간대 착오처럼
# 하루 안팎으로 뒤로 가는 일은 정상이므로 그만큼은 봐준다. 되돌린 만큼이 누적되므로
# (기록은 앞으로만 간다) 이 여유를 조금씩 나눠 써서 무한히 미룰 수는 없다.
#
# 완벽한 방어는 아니다 — 이 파일을 지우면 기준이 사라진다. 목표는 '날짜만 바꾸면
# 되는' 수준을 넘기는 것이지, 작정한 사람을 막는 게 아니다(로컬 검증의 한계).
_CLOCK_TOLERANCE_DAYS = 2


def _clock_seen_path():
    # 이 Lens는 _home()이 아니라 config.data_dir()을 쓴다(license.key와 같은 자리).
    return data_dir() / "clock_seen"


def _clock_turned_back(expiry: "date | None") -> bool:
    """날짜가 과거로 돌아가 있으면 True. 기간 없는 키면 언제나 False.

    읽거나 쓰지 못하면 막지 않는다 — 상태 파일 하나 때문에 돈 낸 사람이 잠기는 쪽이
    훨씬 나쁘다(폐기 목록의 '모르면 통과'와 같은 원칙).
    """
    if expiry is None:
        return False
    today = datetime.now(timezone.utc).date()

    seen = None
    try:
        seen = date.fromisoformat(_clock_seen_path().read_text(encoding="utf-8").strip())
    except Exception:
        seen = None

    if seen is not None and today < seen - timedelta(days=_CLOCK_TOLERANCE_DAYS):
        return True

    # 기록은 앞으로만 간다. 뒤로 가면 되돌린 사람이 기준선을 낮춰버릴 수 있다.
    if seen is None or today > seen:
        try:
            path = _clock_seen_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(today.isoformat(), encoding="utf-8")
        except OSError:
            pass
    return False


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
    payload_len = len(raw) - _SIG_LEN
    if payload_len not in (_PAYLOAD_LEN, _PAYLOAD_LEN_WITH_EXPIRY) or raw[:4] != PRODUCT:
        return {"valid": False, "reason": "이 제품의 키가 아님"}
    payload, sig = raw[:payload_len], raw[payload_len:]
    try:
        pub.verify(sig, payload)
    except InvalidSignature:
        return {"valid": False, "reason": "서명 불일치(위조/변조)"}
    # 만료일은 서명 안에 들어 있다 — 고쳐 쓰면 서명이 깨지므로 위 검증에서 걸린다.
    return {
        "valid": True,
        "license_id": payload[4:10].hex(),
        "expires_on": _expiry_from_payload(payload),
    }


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
    expiry = effective_expiry(res)
    if _is_expired(expiry):
        return "expired"
    if _clock_turned_back(expiry):
        return "clock"
    if is_revoked(res.get("license_id", "")):
        return "revoked"
    return None


def locked_message() -> str:
    reason = license_block_reason()
    if reason == "revoked":
        return REVOKED_MESSAGE
    if reason == "expired":
        return EXPIRED_MESSAGE
    if reason == "clock":
        return CLOCK_MESSAGE
    return LOCKED_MESSAGE


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
    expiry = effective_expiry(res)
    if _is_expired(expiry):
        return False
    if _clock_turned_back(expiry):
        return False
    if is_revoked(res.get("license_id", "")):
        return False  # 폐기된 키는 캐시하지 않는다 — 목록에서 빠지면 곧바로 다시 풀려야 한다
    if expiry is not None:
        # 기간이 있는 키는 캐시하지 않는다. 캐시하면 서버가 떠 있는 동안(Claude를
        # 켜둔 채 며칠)에는 만료가 지나도 계속 열린다.
        return True
    _licensed_cache = True
    return True


def save_key(key_str: str) -> dict:
    """키를 저장한다. 저장 전에 '지금 쓸 수 있는 키인지'까지 본다.

    예전엔 서명만 맞으면 저장하고 곧바로 _licensed_cache 를 True 로 켰다. verify_key 는
    만료를 안 보므로(그건 is_licensed 의 일이다), **이미 끝난 체험 키를 다시 붙여넣으면
    그 프로세스가 사는 동안 전부 열렸다** — 껐다 켜고 옛 키를 다시 넣으면 되는 우회였다.
    이제 기간이 끝났거나 중지된 키는 저장 자체를 거부하고, 캐시는 켜지 않고 비운다
    (다음 is_licensed 가 파일을 다시 읽어 스스로 판정한다)."""
    res = verify_key(key_str)
    if not res["valid"]:
        return res

    # 체험은 한 컴퓨터에 한 번이다. 안 막으면 이메일만 바꿔 신청해서 계속 이어 쓸 수
    # 있는데, 그건 재고를 태우는 것보다 "14일이면 충분한지" 판단할 이유 자체를 없앤다.
    # 우리는 이 PC 가 예전에 어떤 체험 키를 썼는지 이미 적어두고 있다(체험 시작일 기록).
    already = _other_trial_used(res)
    if already:
        return {
            "valid": False,
            "reason": (
                "이 컴퓨터에서는 이미 체험판을 사용하셨습니다.\n"
                "체험은 한 대에 한 번만 드립니다. 계속 쓰시려면 정식 라이선스를 구매해주세요.\n"
                "착오라고 생각되시면 osy980315@gmail.com 으로 알려주세요."
            ),
        }

    # 여기서 effective_expiry 를 부르는 순간이 곧 '체험 시작'이다 — 키를 붙여넣는
    # 행위가 활성화이므로, 그날이 창의 첫날로 기록된다.
    expiry = effective_expiry(res)
    if _is_expired(expiry):
        return {"valid": False, "reason": "사용 기간이 끝난 키입니다", "expires_on": expiry}
    if is_revoked(res.get("license_id", "")):
        return {"valid": False, "reason": "현재 사용이 중지된 키입니다"}

    p = _license_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key_str.strip(), encoding="utf-8")
    global _licensed_cache
    _licensed_cache = False  # 다음 is_licensed 가 만료·폐기까지 보고 스스로 정한다
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
