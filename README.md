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
export PYTHONPATH=src          # 또는: pip install -e .

python -m health init -i       # 프로필을 대화형으로 채운다 (5분, 한 번만)
python -m health checkin       # 매일 이것 하나. 90초 안에 끝나야 한다
python -m health status        # Phase 0 게이트 통과 여부
```

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
```bash
python -m health score          # 준비도 0~100 + 기여 요인
python -m health triage         # 레드플래그 (종료코드 = 심각도 0~3)
python -m health brief          # 에이전트에 먹일 일일 팩트시트
python -m health weekly         # 주간 팩트시트
python -m health seed --days 30 # 데모용 합성 데이터 (실제 값 아님)
python -m health log --set vitals.weight_kg=70.2   # 단건 기록/보정
```

## 데이터는 어디에 사는가

`data/` 는 **이 저장소를 클론한 로컬 머신**에만 있다. `.gitignore` 로 전부 제외되어
커밋되지 않고, 원격 세션(Claude Code on the web 등)의 컨테이너는 회수되면 사라진다.
**매일의 기록은 본인 기기에서 돌려야 한다.**

`HEALTH_DATA_DIR` 로 위치를 옮길 수 있다 — 예를 들어 암호화된 볼륨 안으로:
```bash
export HEALTH_DATA_DIR=~/Vault/health-data
```
백업은 평문 클라우드 동기화 폴더가 아니라 암호화 아카이브로 한다:
```bash
tar czf - "$HEALTH_DATA_DIR" | age -p > health-$(date +%F).tar.gz.age
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
python -m pytest tests -q
```

`src/health/triage.py` 를 고칠 때는 **반드시 회귀 테스트를 함께 추가**한다.
테스트 없는 안전 규칙 변경은 머지하지 않는다. → [04. 안전](docs/04-safety.md#8-안전-관련-코드를-고칠-때)

## 개인정보

`data/**` 는 `.gitignore` 로 전부 제외되어 있다. **건강 데이터를 커밋하지 않는다.**
어떤 에이전트도 건강 데이터를 외부 서비스로 자동 전송하지 않는다.
