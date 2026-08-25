"""준비도(Readiness) 점수 — 오늘 몸이 부하를 감당할 수 있는가.

0~100. 개인 베이스라인 대비 z-점수의 가중합을 시그모이드로 눌러 만든다.
결측 지표는 가중치에서 제외하고 남은 가중치를 재정규화하므로,
웨어러블이 없어도 주관 체크인만으로 (신뢰도는 낮지만) 값이 나온다.

이 점수는 **의료적 진단이 아니다.** 운동 강도를 정하고 회복이 필요한
날을 놓치지 않기 위한 내비게이션 지표다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from . import baseline as bl
from .schema import DailyRecord
from .store import Profile

# 지표별 가중치. 합이 1이 되도록 두되, 결측 시 재정규화한다.
WEIGHTS: dict[str, float] = {
    "vitals.hrv_rmssd_ms": 0.26,     # 자율신경 회복의 가장 민감한 대리지표
    "vitals.resting_hr": 0.18,
    "sleep.total_min": 0.16,
    "sleep.efficiency_pct": 0.10,
    "subjective.energy": 0.10,
    "subjective.soreness": 0.08,
    "subjective.stress": 0.07,
    "subjective.mood": 0.05,
}

SLOPE = 1.1
OFFSET = 0.75

BANDS = [
    (80, "GREEN", "고강도 훈련 가능"),
    (65, "AMBER", "중강도 유지 — 계획대로, 무리는 금물"),
    (45, "CAUTION", "회복 위주 — 가벼운 유산소/모빌리티"),
    (0, "RED", "휴식 — 오늘은 부하를 주지 않는다"),
]


@dataclass
class Readiness:
    date: str
    score: float
    band: str
    advice: str
    confidence: float                     # 0~1, 사용된 가중치 비율
    contributors: list[tuple[str, float, float]] = field(default_factory=list)
    # (지표 라벨, z, 점수 기여분)
    flags: list[str] = field(default_factory=list)
    acwr: float | None = None
    sleep_debt_min: float | None = None

    def summary(self) -> str:
        conf = "높음" if self.confidence >= 0.7 else "보통" if self.confidence >= 0.4 else "낮음"
        return f"준비도 {self.score:.0f}/100 [{self.band}] — {self.advice} (신뢰도 {conf})"


def _band(score: float) -> tuple[str, str]:
    for threshold, name, advice in BANDS:
        if score >= threshold:
            return name, advice
    return BANDS[-1][1], BANDS[-1][2]


def sleep_debt(history: Sequence[DailyRecord], need_min: float, days: int = 7) -> float | None:
    """최근 days일 누적 수면 부채(분). 양수 = 부족."""
    vals = bl.series(list(history)[-days:], "sleep.total_min")
    if not vals:
        return None
    return sum(need_min - v for v in vals)


def compute(
    history: Sequence[DailyRecord],
    today: DailyRecord,
    profile: Profile | None = None,
) -> Readiness:
    """history는 today 이전의 기록(베이스라인용), today는 평가 대상."""
    profile = profile or Profile()
    metrics = bl.compute(history, today)

    weighted = 0.0
    used = 0.0
    contributors: list[tuple[str, float, float]] = []

    for path, w in WEIGHTS.items():
        m = metrics.get(path)
        if m is None or m.z is None:
            continue
        # 방향 보정: 낮을수록 좋은 지표는 부호를 뒤집는다.
        signed = m.z * (m.direction if m.direction != 0 else 1)
        # z를 ±3으로 클리핑. 웨어러블 이상치 하나가 점수를 지배하지 않게.
        signed = max(-3.0, min(3.0, signed))
        weighted += w * signed
        used += w
        contributors.append((m.label, m.z, w * signed))

    if used == 0:
        # 점수는 못 내지만 여기서 끝내면 안 된다. 수면부채·ACWR 같은
        # '베이스라인이 필요 없는' 절대 지표는 여전히 경고할 수 있다.
        r = Readiness(
            date=today.date,
            score=50.0,
            band="UNKNOWN",
            advice="데이터가 부족해 점수 산출 불가 — 체크인부터 하세요",
            confidence=0.0,
        )
        _attach_flags(r, history, today, metrics, profile)
        return r

    normalized = weighted / used                     # 대략 -3 ~ +3
    # 보정 상수의 의미:
    #   SLOPE  z 1.0 차이가 점수 약 20점 차이로 벌어지도록 하는 기울기
    #   OFFSET z=0(딱 평소인 날)이 68점 = AMBER 중앙에 오도록 하는 절편.
    #          이게 없으면 평범한 날이 CAUTION으로 떨어져 경보 피로를 부른다.
    score = 100.0 * bl.sigmoid(normalized * SLOPE + OFFSET)
    band, advice = _band(score)

    r = Readiness(
        date=today.date,
        score=round(score, 1),
        band=band,
        advice=advice,
        confidence=round(used / sum(WEIGHTS.values()), 2),
        contributors=sorted(contributors, key=lambda c: c[2]),
    )

    _attach_flags(r, history, today, metrics, profile)
    return r


def _attach_flags(
    r: Readiness,
    history: Sequence[DailyRecord],
    today: DailyRecord,
    metrics: dict[str, bl.Metric],
    profile: Profile,
) -> None:
    """점수와 별개로 반드시 보여줘야 할 맥락. 점수가 UNKNOWN이어도 붙는다."""
    window = list(history) + [today]

    r.acwr = bl.acwr(window)
    if r.acwr is not None and r.acwr > 1.5:
        r.flags.append(f"훈련부하 급증(ACWR {r.acwr:.2f}) — 부상 위험 구간, 이번 주 볼륨 10~20% 감량 권장")
    elif r.acwr is not None and r.acwr < 0.8:
        r.flags.append(f"훈련부하 저하(ACWR {r.acwr:.2f}) — 체력 유지 위해 점진적 재개 권장")

    r.sleep_debt_min = sleep_debt(window, profile.sleep_need_min)
    if r.sleep_debt_min is not None and r.sleep_debt_min > 180:
        r.flags.append(
            f"7일 누적 수면부채 {r.sleep_debt_min / 60:.1f}시간 — 취침 30분 앞당기기 우선"
        )

    for path in ("vitals.hrv_rmssd_ms", "sleep.total_min", "subjective.energy"):
        if bl.missingness(history, path) > 0.5:
            r.flags.append(
                f"데이터 결측: 최근 2주 '{bl.label_for(path)}' 절반 이상 비어있음 — 수집 경로 점검 필요"
            )
