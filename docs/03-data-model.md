# 03. 데이터 모델과 수집 경로

## 수집의 제1원칙

> **자동 > 반자동 > 수동.** 수동 기록은 반드시 끊긴다. 문제는 언제 끊기느냐다.

하루 기록 시간이 90초를 넘으면 그 항목은 없는 것과 같다. 항목을 늘리기 전에
"이 데이터가 실제로 어떤 결정을 바꾸는가"를 물어야 한다. 답이 없으면 수집하지 않는다.

## 4개 층

| 층 | 데이터 | 수집 | 지연 | 비고 |
|---|---|---|---|---|
| **연속 생체** | HR, HRV, SpO2, 수면단계, 걸음, 체온 | 웨어러블 자동 | 수시간 | 밀도는 높으나 주관 상태를 모름 |
| **간헐 측정** | 체중, 혈압, 혈당, 체성분 | 가정용 기기 | 즉시~1일 | 측정 조건 통제가 정확도를 좌우 |
| **주관 보고** | 기분, 활력, 스트레스, 통증, 증상 | 대화형 체크인 | 즉시 | **가장 예측력이 높고 가장 잘 끊긴다** |
| **임상** | 검진, 혈액검사, 진단, 처방 | 문서 파싱 | 수개월 | 다른 층이 못 보는 것을 본다 |

여기에 **맥락 층**(날씨·미세먼지·생리주기·여행·업무 부하)이 붙는다.
맥락은 그 자체로 건강 지표가 아니지만, **교란변수를 설명해 오귀인을 막는다.**
"HRV가 떨어졌다"와 "출장 3일차라 HRV가 떨어졌다"는 완전히 다른 결론을 낳는다.

## 스키마

`src/health/schema.py` 의 `DailyRecord` 가 하루 한 개 파일에 대응한다.

```
data/daily/2026-08-25.json
├── sleep       bedtime, waketime, total_min, efficiency_pct, latency_min, deep_min, rem_min, awakenings
├── vitals      resting_hr, hrv_rmssd_ms, spo2_pct, body_temp_c, bp_systolic/diastolic,
│               weight_kg, body_fat_pct, blood_glucose_mgdl
├── activity    steps, active_kcal, workouts[{type, duration_min, rpe, avg_hr, distance_km}]
├── subjective  mood, energy, stress, soreness, focus (1~5) · pain_nrs (0~10) · symptoms[] · note
├── intake      kcal, protein_g, water_ml, caffeine_mg, last_caffeine_at, alcohol_units, meals[]
├── adherence   meds_taken[], meds_missed[], meditation_min, plan_completed[], plan_skipped[]
├── context     pm25, pm10, temp_c, humidity_pct, travel, menstrual_phase
└── sources     ["apple-health", "checkin"]   ← 출처 추적
```

### 설계에서 지킨 세 가지

**1. 모든 필드가 optional이다.**
사람은 매일 모든 걸 기록하지 않는다. 결측을 예외가 아니라 정상 상태로 취급한다.
계산은 있는 값으로만 하고, 신뢰도(`confidence`)로 그 사실을 드러낸다.
**결측을 0으로 채우는 것이 이 도메인에서 가장 위험한 버그다** — 안 잔 것과 기록 안 한 것은 다르다.

**2. 단위를 필드명에 박았다.**
`total_min`, `hrv_rmssd_ms`, `caffeine_mg`. 건강 데이터 사고의 대부분은 단위 혼동이다.
특히 HRV는 rMSSD와 SDNN이 스케일이 달라 섞으면 베이스라인이 통째로 망가진다.

**3. `upsert` 는 병합이다.**
웨어러블이 아침에 수면을 넣고, 저녁에 체크인이 기분을 넣는다.
덮어쓰기면 한쪽이 지워진다. `Store.upsert()` 는 값이 있는 필드만 갱신한다.

## 소스별 연결 방법

### 웨어러블 / 헬스앱
| 소스 | 경로 | 난이도 |
|---|---|---|
| Apple Health | 건강 앱 → 프로필 → 모든 건강 데이터 내보내기 → `export.xml` | 중 (XML이 크다. 스트리밍 파싱 필요) |
| 삼성 헬스 | 앱 → 설정 → 개인 데이터 다운로드 → CSV | 중 |
| Fitbit / Garmin | 계정 데이터 내보내기 또는 개발자 API | 중~상 (API는 OAuth 필요) |
| Google Fit / Health Connect | Android Health Connect API | 상 |
| Oura / Whoop | 공개 API (개인 토큰) | 하 (문서가 잘 되어 있다) |

시작은 **내보내기 파일 배치 적재**로 충분하다. 실시간 API는 Phase 3 이후에 붙인다.
주 1회 내보내기로도 이 시스템의 거의 모든 기능이 동작한다.

### 맥락 데이터 (한국)
- 대기질: 에어코리아 / 공공데이터포털 대기오염정보 API → `context.pm25`
- 날씨: 기상청 단기예보 API → `context.temp_c`, `humidity_pct`
- 둘 다 무료이고 인증키만 있으면 된다. `scripts/ingest_context.py` 로 매일 자동화한다.

### 임상 데이터
- 건강검진 결과: PDF/이미지 → `clinical-record` 에이전트가 구조화
- 국민건강보험공단 "나의건강기록" 앱에서 검진 이력·투약 이력을 내려받을 수 있다
- **저장 전 주민등록번호와 환자 식별번호는 반드시 마스킹**한다

## 파생 지표 (수집하지 않고 계산한다)

| 지표 | 계산 | 모듈 |
|---|---|---|
| z-점수 | (오늘 − 28일 평균) / max(표준편차, 하한) | `baseline.compute` |
| 준비도 | 방향 보정 z의 가중합 → 시그모이드 → 0~100 | `readiness.compute` |
| 세션 훈련부하 | 시간(분) × RPE (Foster sRPE) | `schema.Workout.load` |
| ACWR | 7일 평균부하 / 28일 평균부하 | `baseline.acwr` |
| 수면부채 | Σ(필요 수면 − 실제 수면), 7일 | `readiness.sleep_debt` |
| 결측률 | 최근 14일 중 비어있는 비율 | `baseline.missingness` |

### 표준편차 하한(sd_floor)이 왜 있는가
매일 "활력 3"이라고만 답하면 표준편차가 0이 되고, 오늘 1로 떨어져도 z가 정의되지 않아
**아무 신호도 못 잡는다.** 리커트 척도와 반올림된 웨어러블 값에서 실제로 자주 일어난다.
그래서 지표별로 "이 정도 변화는 의미 있다"는 최소 폭을 하한으로 둔다 (`baseline.TRACKED`).

## 데이터 품질 규칙

수집 단계에서 막지 못하면 분석 전체가 오염된다.

1. **생리학적 범위 밖은 저장하지 않는다.** 심박 20~220, SpO2 70~100, 체온 30~43, 수면 0~960분
2. **5σ 이상 이탈값은 저장 전 확인한다.** 웨어러블 오작동이 베이스라인을 통째로 밀어버린다
3. **타임존을 고정한다.** 자정을 넘긴 수면은 **기상한 날**에 귀속시킨다
4. **출처를 남긴다.** 나중에 "이 값은 어디서 왔나"를 물을 수 있어야 한다
5. **결측 구간을 보고한다.** 조용히 넘어가면 "데이터가 없는 것"과 "정상인 것"을 구별할 수 없다

## 보관

```
data/
├── profile.json          정적 프로필 (안전 경계)
├── daily/YYYY-MM-DD.json 일별 레코드
├── clinical/             임상 문서 (마스킹 후)
├── events.jsonl          에이전트 행동 로그 (추가 전용, 감사용)
└── experiments.json      N-of-1 실험
```

`data/**` 는 `.gitignore` 로 전부 제외되어 있다. **건강 데이터는 커밋하지 않는다.**
백업이 필요하면 로컬 암호화 아카이브를 쓴다 (docs/04-safety.md).
