# CGV 잔여 좌석 감시

CGV 상영 회차의 잔여 좌석을 감시해서, **매진(0석)이던 회차에 자리가 나면** 텔레그램/디스코드로 알립니다.
GitHub Actions cron으로 돌아갑니다.

> **알림 전용입니다.** 예매나 결제는 절대 자동화하지 않습니다. 조회와 알림만 합니다.

---

## 목차

1. [빠른 시작](#빠른-시작)
2. [프로젝트 구조](#프로젝트-구조)
3. [config.yaml 작성법](#configyaml-작성법)
4. [Secrets 등록 절차](#secrets-등록-절차)
5. [알림 채널 연결](#알림-채널-연결)
6. [동작 방식](#동작-방식)
7. [상태 유지: 왜 actions/cache인가](#상태-유지-왜-actionscache인가)
8. [스케줄 지연에 대하여](#스케줄-지연에-대하여)
9. [알려진 한계](#알려진-한계)
10. [파싱이 깨졌을 때](#파싱이-깨졌을-때)
11. [로컬 실행](#로컬-실행)

---

## 빠른 시작

1. 이 레포를 본인 계정으로 만듭니다 (**private 권장** — 감시 조건이 노출될 이유가 없습니다).
2. `config.yaml` 을 본인 조건에 맞게 고쳐서 커밋합니다.
3. Settings → Secrets → `DISCORD_WEBHOOK_URL` 또는 텔레그램 2종을 등록합니다.
4. Actions 탭 → **CGV 잔여좌석 감시** → *Run workflow* → `test_notify` 체크 → 실행.
   알림이 오면 연결 완료입니다.
5. 이후 5분마다 자동으로 돕니다.

> **첫 실행은 알림을 보내지 않습니다.** 현재 좌석 상황을 "기준선"으로 기록만 합니다.
> 그래야 다음 실행부터 *변화*를 감지할 수 있습니다. 이건 정상 동작입니다.

---

## 프로젝트 구조

```
.
├── .github/workflows/watch.yml   # cron(*/5) + workflow_dispatch
├── config.yaml                   # 감시 조건 (레포에 커밋. 비밀정보 금지)
├── requirements.txt
├── pytest.ini
├── README.md
└── src/cgv_watcher/
    ├── __main__.py               # python -m cgv_watcher
    ├── cli.py                    # 실행 오케스트레이션, 폴링 루프
    ├── config.py                 # config.yaml 로드 + 검증
    ├── models.py                 # Showtime / SeatState, 시각 처리
    ├── cgv_client.py         ★   # HTTP 계층 — 엔드포인트와 헤더
    ├── parser.py             ★   # 응답 → 모델, 필드명 상수
    ├── matcher.py                # 규칙 ↔ 회차 대조, 지점명 해석
    ├── state.py                  # 이전 상태 저장/복원, 전이 판정
    ├── ratelimit.py              # 랜덤 지연, 지수 백오프
    ├── errors.py                 # 예외 정의
    └── notify/
        ├── __init__.py           # secrets 있는 채널만 활성화
        ├── base.py               # 메시지 본문 생성
        ├── discord.py
        └── telegram.py
└── tests/
    ├── fixtures/
    │   └── searchMovScnInfo.json # 실제 CGV 응답에서 잘라낸 것
    ├── test_parser.py
    ├── test_matcher.py
    └── test_state.py
```

★ 표시한 두 파일이 CGV 스펙에 의존하는 **유일한** 지점입니다.
CGV가 바뀌면 여기만 고치면 됩니다.

---

## config.yaml 작성법

```yaml
defaults:
  min_seats: 1              # 이 좌석 수 이상 열려야 알림
  cooldown_minutes: 30      # 같은 회차 재알림 억제 간격
  notify_on_first_seen: false
  failure_threshold: 3      # 연속 실패 N회면 장애 알림

polling:
  rounds_per_run: 3         # 한 번 실행에서 폴링할 횟수
  round_interval_sec: [60, 90]
  request_delay_sec: [2.0, 5.0]

watches:
  - name: "오디세이 영등포 IMAX"
    movie:
      title: "오디세이"      # 부분 일치. 또는 code: "30001323"
    theater: "영등포타임스퀘어"
    screen_types: ["IMAX"]
    date_range:
      start: "2026-08-11"
      end: "2026-08-17"
    time_range:
      start: "17:00"
      end: "26:00"
    min_seats: 2
```

### 필드 설명

| 필드 | 필수 | 설명 |
|---|:---:|---|
| `name` | | 알림에 표시될 조건 이름. 생략 시 `watch-1` 등 자동 부여 |
| `movie.title` | △ | 영화명 **부분 일치**. "오디세" 로도 "오디세이"가 잡힙니다 |
| `movie.code` | △ | CGV의 `movNo`. 정확히 일치. title과 둘 중 하나는 필수 |
| `theater` | ✅ | 지점명 또는 `siteNo`(예: `"0059"`) |
| `screen_types` | | `["IMAX"]`, `["4DX", "SCREENX"]` 등. 생략 = 전체 상영관 |
| `date_range` | ✅ | `start` / `end`, `YYYY-MM-DD`. 양끝 포함 |
| `time_range` | | 상영 **시작** 시각 기준. 생략 = 전체 |
| `min_seats` | | 기본 1. `defaults.min_seats` 를 덮어씁니다 |

### 상영관 표기

CGV는 같은 상영관을 자리마다 다르게 표기합니다(`IMAX관` / `아이맥스`).
아래는 어떻게 적어도 동일하게 인식합니다:

| 적는 값 | 인식 |
|---|---|
| `IMAX`, `imax`, `아이맥스` | IMAX |
| `SCREENX`, `스크린X`, `스크린엑스` | SCREENX |
| `4DX` | 4DX |
| `ULTRA4DX` | ULTRA 4DX |
| `DOLBYATMOS`, `돌비` | DOLBY ATMOS |

표에 없는 값을 적으면 상영관명에 대한 부분 일치로 처리됩니다.

### `time_range` 와 심야 회차 ⚠️

CGV는 심야 회차를 **24시를 넘겨서** 표기합니다. `24:30` 은 다음 날 새벽 0시 30분입니다.
이 프로젝트도 CGV 표기를 그대로 따릅니다.

```yaml
time_range:
  start: "18:00"
  end: "23:59"   # ← 24:30 회차는 안 잡힙니다

time_range:
  start: "18:00"
  end: "26:00"   # ← 익일 새벽 2시까지 포함
```

### 지점명 확인

`theater` 에 적은 이름이 여러 지점에 걸리면 (예: `"용산"` → `용산아이파크몰`, `씨네드쉐프 용산`)
**실행이 실패합니다.** 조용히 엉뚱한 지점을 감시하는 것보다 낫기 때문입니다.
로그에 후보가 전부 찍히니, 정확한 이름이나 `siteNo` 로 바꿔 적으세요.

전체 목록은 아래로 확인할 수 있습니다:

```bash
curl -s -H "User-Agent: Mozilla/5.0" -H "Referer: https://cgv.co.kr/" "https://cgv.co.kr/api/v1/booking/searchRegnList?coCd=A420&defaultTabType=region"
```

---

## Secrets 등록 절차

GitHub 레포 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| 이름 | 필요 | 설명 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | 디스코드 쓸 때 | 웹훅 URL 전체 |
| `TELEGRAM_BOT_TOKEN` | 텔레그램 쓸 때 | `123456:ABC-...` |
| `TELEGRAM_CHAT_ID` | 텔레그램 쓸 때 | 숫자 ID (그룹은 `-` 로 시작) |

**설정된 채널만 사용됩니다.** 디스코드만 넣으면 디스코드로만 갑니다.
텔레그램은 토큰과 chat_id가 **둘 다** 있어야 켜집니다 (하나만 있으면 경고 로그가 남습니다).
둘 다 없으면 실행 자체가 종료코드 2로 멈춥니다.

> ⚠️ 웹훅 URL과 봇 토큰은 **그 자체가 인증 수단**입니다.
> 코드나 `config.yaml` 에 절대 넣지 마세요. 실수로 커밋했거나 채팅·이슈 등에
> 노출했다면 **즉시 재발급**하세요 (아래 각 채널 항목 참고).

---

## 알림 채널 연결

### 디스코드

1. 알림 받을 서버의 채널 → **채널 편집** → **연동** → **웹후크** → **새 웹후크**
2. **웹후크 URL 복사**
3. 레포 secret `DISCORD_WEBHOOK_URL` 에 붙여넣기

재발급이 필요하면: 같은 화면에서 기존 웹후크 **삭제** 후 새로 만들고, secret을 새 URL로 교체하세요.
(기존 URL은 삭제 즉시 무효가 됩니다.)

### 텔레그램

1. 텔레그램에서 [@BotFather](https://t.me/BotFather) 에게 `/newbot` → 이름 지정 → **토큰** 받기
2. 만든 봇과 대화를 시작하고 아무 메시지나 보냅니다 (봇이 먼저 말을 걸 수 없습니다)
3. chat_id 확인:
   ```bash
   curl -s "https://api.telegram.org/bot<토큰>/getUpdates"
   ```
   응답의 `result[].message.chat.id` 가 chat_id 입니다.
4. secret `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록

그룹으로 받으려면 봇을 그룹에 초대한 뒤 그룹에서 메시지를 보내고 같은 방법으로 확인하세요.
그룹 chat_id 는 `-1001234567890` 처럼 음수입니다.

토큰 재발급은 BotFather 의 `/revoke` 입니다.

### 연결 테스트

Actions 탭 → **CGV 잔여좌석 감시** → *Run workflow* → `test_notify` 체크 → 실행.
CGV에는 요청하지 않고 알림만 한 발 보냅니다.

### 알림 예시

```
🎟️ 좌석 발생

🎬 오디세이
🏢 CGV 영등포타임스퀘어
🎦 IMAX관
📅 2026-08-11 (화)  ⏰ 17:30
💺 잔여 4석 / 전체 387석  (0석 → 4석)

🔗 https://cgv.co.kr/cnm/movieBook/cinema

↑ 위 링크에서 'CGV 영등포타임스퀘어' 와 2026-08-11 (화) 을 선택하세요.
(조건: 오디세이 영등포 IMAX)
```

---

## 동작 방식

### 알림 조건

**직전 관측에서 `min_seats` 미만이던 회차가 `min_seats` 이상이 됐을 때만** 알립니다.
`min_seats: 1` 이면 정확히 `0석 → 1석 이상` 전이입니다.

알리지 **않는** 경우:

- 이미 좌석이 있던 회차 (5석 → 9석): 취소표가 아니라 그냥 여유석입니다
- 계속 0석인 회차
- 처음 보는 회차 (`notify_on_first_seen: false` 기본값)
- `cooldown_minutes` 안에 이미 알린 회차 (0↔1 반복 시 도배 방지)
- 이미 시작한 회차

### 한 번의 실행

```
state 복원 (actions/cache)
  → 지점명 → siteNo 해석
  → 각 지점의 예매 오픈 날짜 조회 (오픈 안 된 날은 요청 자체를 안 보냄)
  → 라운드 1..N:
       지점 × 날짜마다 회차 조회 (요청 사이 랜덤 2~5초, 동시 요청 없음)
       규칙 대조 → 전이 판정 → 알림
       라운드 사이 랜덤 60~90초
  → 지난 회차 정리 → state 저장 (cache + artifact)
```

요청 수는 `지점 수 × 날짜 수 × 라운드 수` 입니다.
잔여석은 회차 목록 응답에 이미 들어 있어서, 회차마다 따로 조회하지 않습니다.

### 요청 예절

- 요청 사이 랜덤 지연, **동시 요청 없음**
- User-Agent는 일반 브라우저 값 **하나로 고정**. 로테이션·우회 없음
- 429/403 → 지수 백오프(2·4·8초 + 지터), 최대 4회
- 연속 실패가 `failure_threshold` 에 도달하면 알림으로 통보 (알림 자체는 60분 쿨다운)

---

## 상태 유지: 왜 actions/cache인가

Actions 러너는 매 실행마다 초기화되므로, "직전에 몇 석이었나"를 실행 밖으로 실어 날라야 합니다.
선택지는 두 가지였습니다.

| | **actions/cache** (채택) | 레포에 커밋 |
|---|---|---|
| 커밋 히스토리 | 깨끗함 | 5분마다 1커밋 = **하루 288개** |
| 동시 실행 | 겹쳐도 안전 | `git push` 충돌 처리 필요 |
| 내구성 | 7일 미접근 시 삭제 (5분 주기면 무관) | 영구 |
| 디버깅 | 캐시를 직접 열 수 없음 → **artifact로 보완** | `git log` 로 추적 쉬움 |
| 부수효과 | 없음 | 커밋 활동이 스케줄 자동 비활성화를 막아줌 |

**actions/cache를 택한 이유는 실패했을 때 손해가 작기 때문입니다.**
state가 유실되면 최악의 경우 "이미 열린 좌석을 한 번 더 알림"이고, 알림을 **놓치지는 않습니다**.
반면 커밋 방식은 push 경합과 288커밋/일이라는 비용을 상시로 지불해야 합니다.

보완책:

- 캐시를 직접 볼 수 없으므로 **state 파일을 artifact로도 업로드**합니다 (보관 7일).
  Actions 실행 페이지 하단에서 내려받아 열어볼 수 있습니다.
- 캐시 항목은 불변이라 같은 키로 덮어쓸 수 없습니다. 그래서 키에 `run_id` 를 넣고
  `restore-keys` 로 직전 것을 끌어옵니다.
- state 저장 단계는 `if: always()` 입니다. 실패해도 저장해야 다음 실행이 기준선을
  다시 잡으면서 중복 알림을 내는 걸 막습니다.

### 커밋 방식으로 바꾸려면

`watch.yml` 의 cache 스텝 2개를 지우고, `permissions: contents: write` 로 바꾼 뒤
마지막에 `git add state/ && git commit && git push` 를 추가하면 됩니다.
`.gitignore` 에서 `state/` 도 빼야 합니다.

---

## 스케줄 지연에 대하여

**GitHub Actions의 `schedule` 은 정시를 보장하지 않습니다.**

- 지정한 시각보다 **수 분에서 수십 분까지 늦게** 시작될 수 있습니다.
  전체 부하가 높은 시간대(매시 정각 부근)에 특히 심합니다.
- 부하가 매우 높으면 해당 실행이 **아예 건너뛰어집니다.**
- 레포에 **60일간 활동이 없으면 스케줄이 자동 비활성화**됩니다.
  (이메일로 안내가 오고, Actions 탭에서 다시 켤 수 있습니다.)
- 스케줄 워크플로는 기본 브랜치에서만 돕니다.

그래서 `*/5` 로 잡아도 실제 간격은 5분보다 길어질 수 있습니다.
이걸 완화하려고 **한 번의 실행 안에서 여러 번 폴링**합니다:

```yaml
polling:
  rounds_per_run: 3
  round_interval_sec: [60, 90]
```

3라운드 × 60~90초 = 한 번 깨어나면 약 2~3분에 걸쳐 3번 봅니다.
정상 동작 시 실효 감지 주기는 약 90초, 실행이 지연되거나 건너뛰어도 커버 폭이 넓어집니다.

`round_interval_sec` 합이 job `timeout-minutes: 15` 를 넘지 않게 하세요.

**정확한 주기가 꼭 필요하다면** Actions는 맞지 않습니다.
상시 켜져 있는 서버나 라즈베리파이에서 `while true` 루프로 돌리는 편이 낫습니다.
코드는 그대로 쓸 수 있습니다 (`state/seats.json` 이 로컬에 남으므로 캐시 로직이 불필요).

---

## 알려진 한계

**회차 단위 예매 링크가 없습니다.**
CGV 예매 페이지는 회차 버튼이 `<a href>` 가 아니라 JS 라우터로만 이동해서,
`?siteNo=0059&scnYmd=20260811` 같은 쿼리를 붙여도 무시됩니다 (확인함).
그래서 알림에는 극장별 예매 페이지 링크를 넣고, 어느 지점·어느 날짜를 골라야 하는지
본문에 명시합니다.

**잔여석은 실시간이 아닙니다.**
`frSeatCnt` 는 조회 시점의 값입니다. 알림을 받고 들어가도 이미 나갔을 수 있습니다.
응답에는 임시 점유를 포함한 `frtmpSeatCnt` 도 있는데, 이 프로젝트는 더 보수적인
`frSeatCnt` 를 씁니다.

**로그인이 필요한 조건은 다루지 않습니다.**
사용하는 API는 전부 비로그인으로 접근 가능한 조회 API입니다.

---

## 파싱이 깨졌을 때

CGV가 API를 바꾸면 이 프로젝트는 **조용히 실패하지 않습니다.**
로그에 남기고, 알림 채널로 통보하고, job을 실패 처리하고, **응답 원문을 artifact로 남깁니다**.

### 1. 증상 확인

| 증상 | 원인 | 고칠 곳 |
|---|---|---|
| `HTTP 403` 이 계속됨 | WAF 차단 또는 헤더 조건 변경 | `cgv_client.py` → `DEFAULT_HEADERS` |
| `HTTP 404` | 엔드포인트 경로 변경 | `cgv_client.py` → `BASE_URL`, 각 `fetch_*` |
| `JSON 파싱 실패` | HTML 오류 페이지를 받음 | `cgv_client.py` (요청이 거부된 것) |
| `필수 필드 누락 [...]` | 응답 필드명 변경 | `parser.py` → 상단 `F_*` 상수 |
| `CGV가 오류 상태를 반환` | `statusCode != 0` | 요청 파라미터 확인 |
| `하나도 파싱하지 못함` | 필드 타입/형식 변경 | `parser.py` → `_as_int`, 시간 형식 |
| 회차는 잡히는데 조건이 안 맞음 | 상영관/영화 표기 변경 | `matcher.py` → `SCREEN_ALIASES` |

### 2. 원문 확인

실패한 Actions 실행 페이지 하단 **Artifacts** → `cgv-state-<run_id>` 다운로드 →
`dumps/raw-*.json` 이 실제로 받은 응답입니다. 여기서 필드명을 직접 확인하세요.

### 3. 현재 요청 사양 (2026-08-10 확인)

```
GET https://cgv.co.kr/api/v1/booking/searchMovScnInfo
    ?coCd=A420&siteNo=0059&scnYmd=20260811&rtctlScopCd=08

필수 헤더:
  User-Agent: <브라우저 값>        ← 없으면 403
  Referer:    https://cgv.co.kr/  ← 없으면 403
쿠키/로그인: 불필요
응답: application/json;charset=UTF-8
```

사용하는 필드:

| 코드 상수 | 응답 필드 | 의미 |
|---|---|---|
| `F_FREE_SEATS` | `frSeatCnt` | **잔여석** |
| `F_TOTAL_SEATS` | `cpSeatCnt` | 총 좌석 |
| `F_SITE_NO` / `F_SITE_NAME` | `siteNo` / `siteNm` | 지점 |
| `F_SCREEN_NO` / `F_SCREEN_NAME` | `scnsNo` / `scnsNm` | 상영관 |
| `F_SCREEN_GRADE` | `tcscnsGradNm` | 특별관 종류 |
| `F_MOVIE_NO` / `F_MOVIE_NAME` | `movNo` / `movNm` | 영화 |
| `F_PLAY_DATE` | `scnYmd` | 상영일 `YYYYMMDD` |
| `F_START_TIME` | `scnsrtTm` | 시작시각 `HHMM` (심야는 `2430`) |
| `F_SEQ` | `scnSseq` | 회차 번호 |

회차 고유키: `siteNo|scnsNo|scnYmd|scnSseq`
이 키를 바꾸면 **기존 state가 전부 새 회차로 보여 알림이 한꺼번에 터집니다.**
바꿔야 한다면 `state.py` 의 `STATE_VERSION` 을 올려서 기존 state를 버리게 하세요.

### 4. 고친 뒤 검증

새로 받은 응답을 `tests/fixtures/searchMovScnInfo.json` 에 넣고:

```bash
python -m pytest -q
```

픽스처는 **실제 응답에서 잘라낸 것**입니다. 손으로 만들지 마세요 —
그러면 진짜 스키마가 바뀌어도 테스트가 통과해 버립니다.

### 5. `requests` 를 쓰지 않는 이유

`cgv_client.py` 는 `requests` 가 아니라 stdlib `urllib` 을 씁니다.
같은 헤더·같은 OpenSSL인데도 `requests`(urllib3)로 보내면 **전 엔드포인트가 403**,
`urllib` 으로 보내면 200입니다. urllib3가 자체 cipher 목록을 쓰면서 TLS 핸드셰이크
지문이 달라지는 탓으로 보입니다.

**여기를 `requests` 로 되돌리지 마세요.** 전부 403이 됩니다.
(알림 채널 쪽은 이런 제약이 없어서 `requests` 를 그대로 씁니다.)

---

## 로컬 실행

Python 3.8 이상에서 동작합니다 (CI는 3.11 사용).

```bash
pip install -r requirements.txt
```

```bash
export PYTHONPATH=src
export DISCORD_WEBHOOK_URL="..."
python -m cgv_watcher --config config.yaml --state state/seats.json -v
```

Windows PowerShell:

```powershell
$env:PYTHONPATH="src"; $env:DISCORD_WEBHOOK_URL="..."; python -m cgv_watcher -v
```

옵션:

| 옵션 | 설명 |
|---|---|
| `--config` | config 경로 (기본 `config.yaml`) |
| `--state` | state 경로 (기본 `state/seats.json`) |
| `--dump-dir` | 파싱 실패 시 원문 저장 위치 (기본 `dumps`) |
| `--test-notify` | 알림 채널 테스트만 하고 종료 |
| `-v` | 디버그 로그 |

종료 코드: `0` 정상 / `1` 감시 또는 알림 전달 실패 / `2` 설정 오류

테스트:

```bash
python -m pytest -q
```
