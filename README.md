# TelegramLens

텔레그램 채널의 **종목 언급·내러티브 흐름을 구조화**해 AI(Claude)에게 전달하는 로컬 MCP 서버.

> AI는 이미 퍼져 있는 정보를 정리할 뿐, 아직 구조화되지 않은 내러티브 흐름은 못 잡는다.
> 텔레그램에서 도는 찌라시·모멘텀·테마를 구조화해 던져주면 그 갭을 메운다.

**비공개·판매용 (Proprietary).** 공개 배포하지 않는다.

---

## 구조

```
사용자 Telegram 계정 (Telethon 세션, 로컬)
    → 가입한 채널 메시지 수집
    → 종목 언급 추출 (KRX 2700+ 종목 사전 검증)
    → 로컬 SQLite (히스토리 보존 → 모멘텀 감지)
    → MCP 툴로 Claude에 구조화 요약 제공
```

데이터는 전부 사용자 PC(`~/.telegramlens/`)에만 저장된다. 서버로 보내지 않는다.

---

## 설치 & 로그인

```powershell
pip install -e .

# 1) Telegram API 자격증명 발급: https://my.telegram.org → API development tools
# 2) 로그인 (전화번호 인증 — 1회만)
telegramlens-login
```

로그인하면 세션 파일·DB·종목 사전(KRX)이 `~/.telegramlens/` 에 준비된다.

## Claude 등록

```powershell
telegramlens-setup           # Claude Desktop/Code 자동 등록
```

또는 수동으로 `claude_desktop_config.json`:

```json
{ "mcpServers": { "telegramlens": { "command": "telegramlens" } } }
```

## 백그라운드 자동 수집 (별도 자식 데몬)

수동 `telegram_sync` 를 매번 부르지 않아도, **Claude 가 이 MCP 서버를 켜둔 동안**
수집 데몬이 10분 주기로 백그라운드 수집한다. 삭제되기 전 찌라시를 박제하고
momentum 히스토리를 쌓는 게 목적. 데몬이 DB를 미리 채워두므로 **조회 도구는 즉시
응답**한다(질문할 때 텔레그램 접속을 기다리지 않는다).

**왜 '별도 자식 프로세스'인가.** 두 함정을 동시에 피하려는 설계다:

1. **백신(persistence)** — 데몬을 창 없이 *detach* 하거나 부팅 자동시작(Run키/예약작업)을
   걸면, 행위가 멀웨어의 persistence/defense-evasion 패턴과 같아 행위 엔진(예: AhnLab
   V3 `Persistence/MDP.Event`)에 잡힌다.
2. **stdio 오염·멈춤** — 반대로 수집을 MCP 서버 *안*에서 돌리면, Telethon 로그가
   stdout(= Claude 와의 JSON-RPC 채널)을 오염시키고 무거운 수집이 이벤트 루프를 막아
   "응답 멈춤/용량" 에러가 난다.

그래서 데몬을 **평범한 자식 프로세스**로 띄운다 — detach·breakaway·자동시작
레지스트리가 전혀 없고(= persistence 아님), `stdout/stderr` 는 DEVNULL 로 막아(= stdio
오염 없음), 별도 프로세스라 서버 이벤트 루프를 막지 않는다(= 멈춤 없음). Claude(부모
MCP 서버)가 종료되면 함께 정리된다.

- **동작**: 서버 기동 시 데몬 자식 1개 spawn → 데몬이 즉시 1회(백필) 후 10분 주기.
  데몬이 떠 있는 동안 `telegram_sync` 는 세션을 데몬에 양보하고 DB 신선도만 보고한다.
- **공백 처리(정합성)**: Claude 를 닫으면 데몬도 종료. 다시 열면 데몬이 꺼져 있던
  구간을 **DB 최신 메시지 시각(watermark)부터 지금까지** 자동 백필한다 — 시간 상한
  최대 7일(`--max-window`), 채널당 상한은 창 길이에 비례해 자동 확대(정상 500 →
  대형 캐치업 시 최대 5000)되어 바쁜 채널도 날짜 경계까지 빠짐없이 채운다(중복은
  UNIQUE 제약으로 스킵). 긴 갭 백필은 첫 사이클이 수 분 걸릴 수 있고, 그동안 조회
  도구는 "수집 중"을 안내한다.
- **상태**: `telegram_status` 의 `collector` 필드. 로그: `~/.telegramlens/daemon.log`.
- DB는 WAL 모드 — 수집(write)이 도는 중에도 Claude 조회(read)가 막히지 않는다.

> 디버그·옵트인용으로 포그라운드 수동 실행도 가능: `telegramlens-daemon`
> (콘솔 창에서 직접 실행). 자동 기동 데몬과 PID 락을 공유해 중복 실행은 막힌다.
> 자동시작(부팅) 등록 기능은 없다.

---

## MCP 툴

**조회**
| 툴 | 용도 |
|---|---|
| `telegram_sync(minutes)` | 최근 메시지 수집·구조화 (먼저 실행) |
| `telegram_trending(hours, top)` | 기간 내 언급량 상위 종목 |
| `telegram_momentum(hours, baseline_hours)` | 언급 급증 종목 — 새 내러티브 포착 |
| `telegram_stock_buzz(query, hours)` | 특정 종목 언급 요약 + 원문 샘플 |
| `telegram_messages(channel, hours)` | 원문 메시지 drill-down (채널·시간) |
| `telegram_search(query, hours, channel)` | 원문 키워드 전문검색 — 종목 언급 없는 거시·산업·테마 글까지 |
| `telegram_channels()` | 수집된 채널 목록 |
| `telegram_status()` | 로그인·수집 상태 |

> 수집 대상은 **가입된 모든 브로드캐스트 채널**이다. 새로 가입한 채널은 다음 사이클부터 자동 포함된다(별도 등록 불필요).

**채널 진단**
| 툴 | 용도 |
|---|---|
| `telegram_classify_channels(threshold)` | 채널별 종목 언급 밀도 리포트(어느 채널이 종목 위주인지 진단 — 수집을 제한하진 않음) |

**사전 관리 (오탐·별칭 루프)**
| 툴 | 용도 |
|---|---|
| `telegram_fp_candidates(days)` | 오탐 후보(일반명사 충돌 의심) 리뷰 리스트 |
| `telegram_alias_candidates(days)` | 누락 별칭 후보(`이름(코드)` 표기 기반, 고정밀) |
| `telegram_block_name(code)` | 모호어 차단(이름단독 매칭 차단, 코드 동반 시만 인정) |
| `telegram_add_alias(alias, code)` | 별칭 등록 (즉시 반영) |

---

## 추출 품질 2층

| 층 | 역할 | 데이터 |
|---|---|---|
| 코드 검증 | 6자리 코드는 KRX 사전에 있는 것만 채택 | KRX 2700+ |
| 별칭 (recall↑) | 통용어/약어 → 코드 (`현대차→005380`) | `data/aliases.json` + 사용자 override |
| 블록리스트 (precision↑) | 일반명사 충돌 종목은 코드 동반 시만 (`대상`, `TP`) | `data/ambiguous_codes.json` + override |

사용자 사전은 `~/.telegramlens/aliases.json`, `~/.telegramlens/ambiguous_codes.json` 로 확장(번들 위에 병합).

---

## 알려진 한계 (MVP)

- **오탐 판별**: `telegram_fp_candidates` 는 후보를 좁혀줄 뿐, "대형주를 약칭으로 부른 것"과 "일반명사 충돌"을 자동 구분하진 못한다. 최종 판단은 사람.
- **별칭 재현율**: 코드 없이 쓰인 별칭(`현대차` 단독)은 누군가 `현대차(005380)` 형태로 쓸 때 비로소 후보로 잡힌다. 볼륨 누적형.
- **수집 트리거**: 현재 `telegram_sync` 수동 호출. 백그라운드 주기 sync 는 추후.

---

## 종목 사전 갱신

```powershell
telegramlens-refresh-stocks   # KRX 상장종목 최신화
```
