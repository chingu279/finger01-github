"""일별 건강 레코드 스키마.

설계 원칙
---------
1. 모든 필드는 optional. 사람은 매일 모든 걸 기록하지 않는다.
   결측을 정상 상태로 취급하고, 있는 것만으로 계산한다.
2. 단위를 필드명에 박아둔다(`_ms`, `_min`, `_mg`). 단위 혼동은
   건강 데이터에서 가장 흔한 버그다.
3. 주관 지표는 1~5(리커트) 또는 0~10(NRS)로 통일한다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import date as _date
from typing import Any

LIKERT_FIELDS = ("mood", "energy", "stress", "soreness", "focus")


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """None 과 빈 컬렉션을 제거해 저장 파일을 사람이 읽을 수 있게 유지.

    정리한 뒤에도 비어 있는 하위 블록은 통째로 뺀다 — `"vitals": {}` 가
    스무 줄씩 쌓이면 파일을 눈으로 훑을 수 없다.
    """
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None or v == [] or v == {}:
            continue
        if isinstance(v, dict):
            nested = _clean(v)
            if not nested:
                continue
            out[k] = nested
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            out[k] = [_clean(item) for item in v]     # 운동 목록 등
        else:
            out[k] = v
    return out


@dataclass
class Sleep:
    """수면. 웨어러블 또는 수기 기록."""

    bedtime: str | None = None          # "23:40"
    waketime: str | None = None         # "07:05"
    total_min: float | None = None
    efficiency_pct: float | None = None  # 잠든 시간 / 침대에 있던 시간
    latency_min: float | None = None     # 눕고 나서 잠들기까지
    deep_min: float | None = None
    rem_min: float | None = None
    awakenings: int | None = None


@dataclass
class Vitals:
    resting_hr: float | None = None      # bpm, 기상 직후
    hrv_rmssd_ms: float | None = None    # 부교감 활성 대리지표 (Oura/Whoop/폴라)
    hrv_sdnn_ms: float | None = None     # Apple Health 가 주는 유일한 HRV.
    #  rMSSD 와 SDNN 은 다른 값이다. SDNN 은 측정 구간 전체의 변동을 보므로
    #  보통 rMSSD 보다 크고, 두 값을 한 칸에 섞으면 베이스라인이 조용히
    #  망가진다 — 값이 튄 게 아니라 기기가 바뀐 것뿐인데 알 길이 없어진다.
    spo2_pct: float | None = None
    body_temp_c: float | None = None
    bp_systolic: float | None = None
    bp_diastolic: float | None = None
    weight_kg: float | None = None
    body_fat_pct: float | None = None
    blood_glucose_mgdl: float | None = None

    # ── 부정맥 관련 (Apple Watch AFib History / ECG / 심박 알림) ──
    #  심방세동이 있는 사람에게는 이 값들이 HRV·안정시심박보다 중요하다.
    #  그리고 AF 중의 HRV 는 자율신경이 아니라 부정맥을 재는 값이라,
    #  이 필드들이 없으면 준비도가 정반대로 나온다(readiness.hrv_usable 참고).
    afib_burden_pct: float | None = None      # 그날 심방세동으로 보낸 시간 비율
    irregular_rhythm_events: int | None = None  # 불규칙 심박 알림 횟수
    ecg_afib: bool | None = None              # 그날 ECG 중 심방세동 판정이 있었는가
    ecg_readings: int | None = None
    high_hr_events: int | None = None         # 안정 시 고심박 알림
    low_hr_events: int | None = None          # 서맥 알림 (맥박 조절 약물 복용 시 중요)
    walking_hr_avg: float | None = None
    vo2max: float | None = None


@dataclass
class Workout:
    type: str = ""                       # "run", "strength", "yoga" ...
    duration_min: float = 0.0
    rpe: float | None = None             # 자각 운동강도 1~10 (Borg CR10)
    avg_hr: float | None = None
    distance_km: float | None = None

    def load(self) -> float:
        """세션 훈련부하 = 시간(분) x RPE (Foster sRPE).

        RPE가 없으면 평균심박으로 대략 추정하고, 그것도 없으면
        중강도(RPE 5)로 가정한다. 과대평가보다 과소평가가 위험하므로
        결측 시 보수적으로 5를 쓴다.
        """
        rpe = self.rpe
        if rpe is None and self.avg_hr is not None:
            rpe = max(1.0, min(10.0, (self.avg_hr - 60.0) / 12.0))
        if rpe is None:
            rpe = 5.0
        return self.duration_min * rpe


@dataclass
class Activity:
    steps: int | None = None
    active_kcal: float | None = None
    stand_hours: int | None = None
    workouts: list[Workout] = field(default_factory=list)

    def training_load(self) -> float:
        return sum(w.load() for w in self.workouts)


@dataclass
class Subjective:
    """대화형 체크인으로 수집. 웨어러블이 못 보는 층."""

    mood: int | None = None       # 1(최악) ~ 5(최고)
    energy: int | None = None     # 1 ~ 5
    stress: int | None = None     # 1(없음) ~ 5(극심)
    soreness: int | None = None   # 1 ~ 5
    focus: int | None = None      # 1 ~ 5
    pain_nrs: int | None = None   # 0 ~ 10 통증 숫자평가척도
    pain_site: str | None = None
    symptoms: list[str] = field(default_factory=list)
    note: str | None = None


@dataclass
class Intake:
    kcal: float | None = None
    protein_g: float | None = None
    carb_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    sodium_mg: float | None = None
    water_ml: float | None = None
    caffeine_mg: float | None = None
    last_caffeine_at: str | None = None   # "16:30" — 수면 상관분석의 핵심 변수
    alcohol_units: float | None = None
    last_meal_at: str | None = None
    meals: list[str] = field(default_factory=list)


@dataclass
class Adherence:
    """치료·습관 순응도. 개입이 실행됐는지를 기록해야 효과를 평가할 수 있다."""

    meds_taken: list[str] = field(default_factory=list)
    meds_missed: list[str] = field(default_factory=list)
    meditation_min: float | None = None
    therapy_session: bool | None = None
    plan_completed: list[str] = field(default_factory=list)
    plan_skipped: list[str] = field(default_factory=list)


@dataclass
class Context:
    """환경 맥락. 컨디션 변화의 교란변수를 설명해준다."""

    pm25: float | None = None
    pm10: float | None = None
    temp_c: float | None = None
    humidity_pct: float | None = None
    pollen: str | None = None
    travel: bool | None = None
    menstrual_phase: str | None = None    # follicular / ovulation / luteal / menses


@dataclass
class DailyRecord:
    date: str
    sleep: Sleep = field(default_factory=Sleep)
    vitals: Vitals = field(default_factory=Vitals)
    activity: Activity = field(default_factory=Activity)
    subjective: Subjective = field(default_factory=Subjective)
    intake: Intake = field(default_factory=Intake)
    adherence: Adherence = field(default_factory=Adherence)
    context: Context = field(default_factory=Context)
    sources: list[str] = field(default_factory=list)   # 데이터 출처 추적

    # ── 직렬화 ────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return _clean(asdict(self))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DailyRecord:
        rec = cls(date=d.get("date", _date.today().isoformat()))
        rec.sleep = _build(Sleep, d.get("sleep"))
        rec.vitals = _build(Vitals, d.get("vitals"))
        rec.subjective = _build(Subjective, d.get("subjective"))
        rec.intake = _build(Intake, d.get("intake"))
        rec.adherence = _build(Adherence, d.get("adherence"))
        rec.context = _build(Context, d.get("context"))
        act = d.get("activity") or {}
        rec.activity = _build(Activity, {k: v for k, v in act.items() if k != "workouts"})
        rec.activity.workouts = [_build(Workout, w) for w in act.get("workouts", [])]
        rec.sources = list(d.get("sources", []))
        return rec

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def is_empty(self) -> bool:
        """date/sources 말고는 아무 값도 없는 레코드.

        빈 파일이 남으면 `status` 의 연속 기록일이 부풀어 게이트가
        거짓으로 통과한다 — 기록하지 않은 날을 기록한 날로 세게 된다.
        """
        payload = self.to_dict()
        return not (set(payload) - {"date", "sources"})

    # ── 편의 접근자 ───────────────────────────────────────────
    def get_path(self, path: str) -> Any:
        """"sleep.total_min" 같은 점 경로로 값 읽기."""
        cur: Any = self
        for part in path.split("."):
            cur = getattr(cur, part, None) if not isinstance(cur, dict) else cur.get(part)
            if cur is None:
                return None
        return cur

    def set_path(self, path: str, value: Any) -> None:
        """"sleep.total_min=420" 형태의 부분 갱신을 지원."""
        parts = path.split(".")
        cur: Any = self
        for part in parts[:-1]:
            cur = getattr(cur, part)
        leaf = parts[-1]
        if not hasattr(cur, leaf):
            raise KeyError(f"알 수 없는 필드: {path}")
        setattr(cur, leaf, value)

    def merge(self, other: DailyRecord) -> DailyRecord:
        """other 의 값이 있는 필드만 덮어쓴다(부분 업데이트)."""
        for f in fields(self):
            if f.name in ("date", "sources"):
                continue
            mine, theirs = getattr(self, f.name), getattr(other, f.name)
            for sub in fields(theirs):
                v = getattr(theirs, sub.name)
                if v is None or v == [] :
                    continue
                setattr(mine, sub.name, v)
        self.sources = sorted(set(self.sources) | set(other.sources))
        return self


def _build(cls: type, d: dict[str, Any] | None) -> Any:
    d = d or {}
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})
