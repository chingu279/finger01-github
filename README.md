# 개인 건강 관리 에이전트 시스템

웨어러블·문진·검진 데이터를 모아 **근실시간으로 상태를 판정**하고,
운동·수면·영양·명상 개입을 처방하며, **주간 회고로 시스템 자체를 개선**하는
멀티에이전트 시스템.

> ⚠️ **의료기기가 아닙니다.** 진단·치료를 제공하지 않습니다.
> 자기관리를 돕는 기록·분석 도구이며, 모든 의학적 판단은 의료 전문가와 함께 하세요.
> 응급 상황이면 **119**, 정신건강 위기라면 **109**(자살예방 상담전화, 24시간).

---

## 핵심 설계

**계산과 안전 판정은 코드가, 해석과 코칭은 에이전트가 한다.**

```
사용자 ─▶ health-orchestrator ─▶ [결정적 코어] ─▶ 14개 전문 에이전트 ─▶ 통합 답변
                                  triage.py         (수집·분석·개입·개선)
                                  readiness.py            │
                                  baseline.py             ▼
                                       ▲            주간 회고 →  시스템 자체 수정
                                       └───────────────────┘
```

LLM은 맥락에 따라 출력이 흔들린다. "흉통 + 식은땀"을 100번 중 1번 놓치는 것도
허용되지 않는다. 그래서 **레드플래그 판정과 점수 계산은 규칙 엔진**이 하고,
에이전트는 그 결과를 **낮출 수 없다**(상향만 가능).

## 빠른 시작

```bash
git clone https://github.com/chingu279/finger01-github.git
cd finger01-github
./health init -i
```
```bash
./health checkin
```
```bash
./health status
```

`./health` 는 저장소 루트의 셸 진입점이다. `PYTHONPATH` 를 export 할 필요도,
`python` 인지 `python3` 인지 신경 쓸 필요도 없다 — macOS 처럼 `python` 이
없는 환경에서도 알아서 찾는다. Python 3.9 이상이면 동작한다.

| 명령 | 언제 |
|---|---|
| `./health init -i` | 처음 한 번. 프로필과 추적 항목을 정한다 (5분) |
| `./health checkin` | **매일 이것 하나.** 90초 안에 끝나야 한다 |
| `./health status` | Phase 0 게이트 통과 여부 |
| `./health import <파일>` | 애플 건강 내보내기 적재 |
| `./health serve` | 웹 체크인 화면 (이 컴퓨터에서만) |

인터프리터를 직접 지정하려면 `PYTHON=/path/to/python3 ./health checkin`.
`pip install -e .` 를 하면 어느 디렉터리에서든 `health` 로 부를 수 있다.

`init -i` 는 목표와 웨어러블 보유 여부로 매일 물을 항목을 제안한다 —
스마트워치가 있으면 수면·심박은 자동 수집에 맡기고 묻지 않는다.
`checkin` 은 그렇게 정한 항목만, 아직 안 채워진 것만 묻는다.
리커트 항목은 키 한 번. 생리학적 범위를 벗어난 값은 저장하지 않고 되묻는다.
자유 서술 한 줄도 레드플래그 스캔 대상이라, 증상은 **본인 표현 그대로** 적으면 된다.

그다음 Claude Code에서:
```
/daily-checkin        오늘 체크인 + 브리핑
/weekly-review        주간 회고 + 시스템 개선
/health-report        진료 전 요약 · 월간 리포트
```

### 그 밖의 명령

| 명령 | 하는 일 |
|---|---|
| `./health score` | 준비도 0~100 + 기여 요인 |
| `./health triage` | 레드플래그. 종료코드가 심각도(0~3) |
| `./health brief` | 에이전트에 먹일 일일 팩트시트 |
| `./health weekly` | 주간 팩트시트 |
| `./health seed --days 30` | 데모용 합성 데이터 (실제 값 아님) |
| `./health log --set vitals.weight_kg=70.2` | 단건 기록·보정 |

## 웹 화면과 아침 알람

터미널에서 Enter 를 두 번 눌러 입력이 씹히는 문제가 있다면 웹 화면을 쓴다.
리커트 항목은 버튼 한 번, Enter 를 쓸 일이 없다.

```bash
./health serve
```

**서버는 `127.0.0.1` 에만 바인딩된다.** 같은 와이파이의 다른 기기에서도 열리지
않고, 데이터가 이 컴퓨터를 떠나지 않는다. 표준 라이브러리만 쓰므로 설치할 것도 없다.

화면은 셋이다 — **체크인**, **오늘**(준비도·안전 판정·지표),
**추세**(지표별 그래프 + 평소 범위 띠).

체크인은 두 부분이다. **측정값**(혈압·체중·체온 …)은 계기판을 옮겨 적는 것이라
항상 보이고 다시 고칠 수 있다. **질문**(활력·근육통·메모 …)은 자기를 돌아봐야
답이 나오므로 최대 5개로 제한하고, 오늘 이미 답했으면 다시 묻지 않는다.

매일 아침 알림을 받으려면 (macOS):
```bash
sh scripts/morning-alarm.sh 07:30
```
LaunchAgent 두 개를 설치한다 — 서버(로그인 시 자동 실행)와 알람(지정 시각에
알림 + 브라우저 열기). 제거는 `sh scripts/morning-alarm.sh --uninstall`.

## 애플워치 데이터 넣기

아이폰 **건강** 앱 → 우상단 프로필 사진 → 맨 아래 **모든 건강 데이터 내보내기**
→ 압축 파일을 맥으로 보내 압축 해제 → `apple_health_export/export.xml`

```bash
./health import ~/Downloads/apple_health_export/export.xml
```

먼저 `--dry-run` 을 붙이면 저장하지 않고 무엇이 들어올지만 보여준다.
`--since 2026-08-01` 로 범위를 좁힐 수 있다 (전체 파일은 보통 수백 MB 다).

수면·안정시심박·HRV·SpO₂·걸음·운동이 들어온다. 21일치만 있어도 준비도가
`UNKNOWN` 에서 실제 점수로 바뀐다. `upsert` 는 병합이라 **수기로 기록한
기분·통증·메모는 지워지지 않는다.**

적재할 때 걸러내는 것들:
- 아이폰과 애플워치가 각각 기록한 **걸음 중복** (합치면 두 배가 된다)
- `unit="%"` 에 `0.97` 로 들어오는 **SpO₂ 분율** → 97%
- 파운드로 기록된 체중 → kg
- 생리학적 범위 밖의 오작동 값 (버리지 않고 보고한다)
- 25분 낮잠을 밤잠에 더하는 것

심방세동 이력(AFib burden)·ECG 판정·고심박/저심박 알림도 함께 읽는다.
**심방세동 중의 HRV 는 자율신경이 아니라 부정맥을 반영**하므로, AF 신호가 있는 날은
준비도와 베이스라인 양쪽에서 HRV 를 빼고 그 사실을 알린다.

**Apple 이 주는 HRV 는 SDNN 이지 rMSSD 가 아니다.** 두 값은 스케일이 달라
한 칸에 섞으면 기기를 바꿨을 때 베이스라인이 조용히 망가진다. 그래서
`vitals.hrv_sdnn_ms` 로 따로 저장하고, 준비도는 가진 쪽 하나만 쓴다.

## 데이터는 어디에 사는가

`data/` 는 **이 저장소를 클론한 로컬 머신**에만 있다. `.gitignore` 로 전부 제외되어
커밋되지 않고, 원격 세션(Claude Code on the web 등)의 컨테이너는 회수되면 사라진다.
**매일의 기록은 본인 기기에서 돌려야 한다.**

`HEALTH_DATA_DIR` 로 위치를 옮길 수 있다 — 예를 들어 암호화된 볼륨 안으로:
```bash
export HEALTH_DATA_DIR=~/Vault/health-data
```

백업은 평문 클라우드 동기화 폴더가 아니라 암호화 아카이브로 한다
(`age` 대신 `gpg -c` 도 된다):
```bash
tar czf - "$HEALTH_DATA_DIR" | age -p > health-backup.tar.gz.age
```

## 에이전트 14개

| 층 | 에이전트 |
|---|---|
| **L0 오케스트레이션** | `health-orchestrator` |
| **L1 수집** | `checkin-interviewer` · `wearable-ingest` · `clinical-record` |
| **L2 분석** | `risk-triage` · `vitals-analyst` · `insight-correlator` |
| **L3 개입** | `exercise-coach` · `sleep-coach` · `nutrition-coach` · `mind-coach` |
| **L4 순응·개선** | `adherence-agent` · `experiment-designer` · `reflection-agent` |

**한 번에 다 만들지 않는다.** Phase 1은 4개로 시작한다 → [로드맵](docs/02-roadmap.md)

## 문서

| | |
|---|---|
| [01. 아키텍처](docs/01-architecture.md) | 왜 14개인가, 왜 안전은 에이전트가 아닌가 |
| [02. 로드맵](docs/02-roadmap.md) | Phase 0~4, 각 단계의 통과 조건 |
| [03. 데이터 모델](docs/03-data-model.md) | 4개 층, 스키마, 수집 경로, 품질 규칙 |
| [04. 안전과 개인정보](docs/04-safety.md) | 레드플래그 목록, 금지선, 데이터 보호 |
| [05. 개선 루프](docs/05-improvement-loop.md) | 일간·주간·분기 루프, N-of-1 실험, 메타 지표 |

## 구조

```
src/health/          결정적 코어 (표준 라이브러리만)
├── schema.py        DailyRecord — 모든 필드 optional, 단위를 이름에 박음
├── store.py         로컬 JSON 저장소 + 병합 upsert + 감사 로그
├── baseline.py      28일 개인 베이스라인, z-점수, ACWR, 결측률
├── readiness.py     준비도 0~100 (GREEN/AMBER/CAUTION/RED)
├── triage.py        ★ 레드플래그 규칙 엔진 — 안전 바닥
├── report.py        에이전트용 팩트 브리핑
└── cli.py           에이전트가 호출하는 도구 표면

.claude/agents/      14개 에이전트 정의
.claude/skills/      daily-checkin · weekly-review · health-report
tests/               회귀 테스트 (레드플래그 미탐 방지 포함)
data/                개인 데이터 — .gitignore로 전부 제외됨
```

## 개발

```bash
python3 -m pytest tests -q
```

`src/health/triage.py` 를 고칠 때는 **반드시 회귀 테스트를 함께 추가**한다.
테스트 없는 안전 규칙 변경은 머지하지 않는다. → [04. 안전](docs/04-safety.md#8-안전-관련-코드를-고칠-때)

## 개인정보

`data/**` 는 `.gitignore` 로 전부 제외되어 있다. **건강 데이터를 커밋하지 않는다.**
어떤 에이전트도 건강 데이터를 외부 서비스로 자동 전송하지 않는다.
