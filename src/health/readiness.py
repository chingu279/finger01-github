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
# HRV 는 기기에 따라 rMSSD 또는 SDNN 하나만 들어온다. 둘 다 가중치에
# 넣으면 한쪽만 있는 사람은 그만큼 신뢰도가 깎이고, 둘 다 있는 사람은
# 같은 신호를 두 번 세게 된다. 그래서 하나를 골라 쓴다.
HRV_PATHS = ("vitals.hrv_rmssd_ms", "vitals.hrv_sdnn_ms")


def hrv_usable(rec: DailyRecord) -> bool:
    """그날의 HRV 를 자율신경 지표로 쓸 수 있는가.

    심방세동 중에는 RR 간격이 불규칙해져 rMSSD·SDNN 이 폭증한다.
    회복이 좋아진 게 아니라 **부정맥을 재고 있는 것**이다. 그대로 두면
    AF 에피소드 날에 "준비도 GREEN, 고강도 훈련 가능"이 나온다 —
    정확히 반대여야 하는 상황에서.

    그래서 AF 신호가 있는 날은 HRV 를 준비도에서 빼고, 뺐다는 사실을
    사용자에게 알린다. 조용히 빼면 왜 점수가 달라졌는지 알 수 없다.
    """
    v = rec.vitals
    if v.afib_burden_pct is not None and v.afib_burden_pct > 0:
        return False
    if v.irregular_rhythm_events:
        return False
    if v.ecg_afib:
        return False
    return True


def _mask_unusable_hrv(history: Sequence[DailyRecord]) -> list[DailyRecord]:
    """베이스라인에서도 AF 날의 HRV 를 뺀다.

    AF 하룻밤의 rMSSD 200ms 가 28일 평균을 통째로 밀어버리면, 그 뒤로
    정상인 날들이 전부 "HRV 가 낮다"로 읽힌다. 오염은 하루로 끝나지 않는다.
    """
    out: list[DailyRecord] = []
    for rec in history:
        if hrv_usable(rec):
            out.append(rec)
            continue
        clone = DailyRecord.from_dict(rec.to_dict())
        clone.vitals.hrv_rmssd_ms = None
        clone.vitals.hrv_sdnn_ms = None
        out.append(clone)
    return out


def max_weight() -> float:
    """달성 가능한 최대 가중치 합. 신뢰도의 분모다.

    HRV 두 항목 중 하나만 쓰이므로 단순 합을 쓰면 모든 지표가 다 있어도
    신뢰도가 1에 닿지 못한다.
    """
    return sum(WEIGHTS.values()) - min(WEIGHTS[p] for p in HRV_PATHS)

WEIGHTS: dict[str, float] = {
    "vitals.hrv_rmssd_ms": 0.26,     # 자율신경 회복의 가장 민감한 대리지표
    "vitals.hrv_sdnn_ms": 0.26,      # rMSSD 가 없을 때의 대체 (둘 중 하나만 쓰인다)
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
    history = _mask_unusable_hrv(history)
    hrv_ok = hrv_usable(today)
    metrics = bl.compute(history, today)

    weighted = 0.0
    used = 0.0
    contributors: list[tuple[str, float, float]] = []

    # rMSSD 가 있으면 그것을, 없으면 SDNN 을 쓴다. rMSSD 쪽이 부교감
    # 활성 지표로 더 확립돼 있어 우선한다.
    hrv_in_use = next(
        (p for p in HRV_PATHS if (metrics.get(p) and metrics[p].z is not None)), None
    ) if hrv_ok else None

    for path, w in WEIGHTS.items():
        if path in HRV_PATHS and path != hrv_in_use:
            continue
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
        if not hrv_ok:
            r.flags.append(
                "심방세동 신호가 있어 오늘 HRV 를 준비도에서 제외했습니다 "
                "— AF 중의 HRV 는 자율신경이 아니라 부정맥을 반영합니다"
            )
        return r

    normalized = weighted / used                     # 대략 -3 ~ +3
    # 보정 상수의 의미:
    #   SLOPE  z 1.0 차이가 점수 약 20점 차이로 벌어지도록 하는 기울기
    #   OFFSET z=0(딱 평소인 날)이 68점 = AMBER 중앙에 오도록 하는 절편.
    #          이게 없으면 평범한 날이 CAUTION으로 떨어져 경보 피로를 부른다.
    score = 100.0 * bl.sigmoid(normalized * SLOPE + OFFSET)
    band, advice = _band(score)

    if used / max_weight() < 0.4:
        # 지표 한두 개로 낸 점수를 단정적으로 말하면 경보 피로를 부른다.
        # 점수는 그대로 두되(보수적인 쪽이 안전하다) 잠정임을 밝힌다.
        advice = f"{advice} — 다만 지표가 부족해 잠정 판정입니다"

    r = Readiness(
        date=today.date,
        score=round(score, 1),
        band=band,
        advice=advice,
        confidence=round(min(1.0, used / max_weight()), 2),
        contributors=sorted(contributors, key=lambda c: c[2]),
    )

    _attach_flags(r, history, today, metrics, profile)
    if not hrv_ok:
        r.flags.append(
            "심방세동 신호가 있어 오늘 HRV 를 준비도에서 제외했습니다 "
            "— AF 중의 HRV 는 자율신경이 아니라 부정맥을 반영합니다"
        )
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

    _flag_missing(r, history, profile)


# 결측 경보를 내기 전에 필요한 최소 관측 기간. 시작 첫 주에는
# 모든 지표가 정의상 '절반 이상 결측'이라, 이때 경보를 내면 사용자는
# 첫날부터 경보를 무시하는 법을 배운다. 그게 경보 피로의 시작이다.
MISSING_MIN_HISTORY = 7
MISSING_SEEN_THRESHOLD = 3


def _flag_missing(r: Readiness, history: Sequence[DailyRecord], profile: Profile) -> None:
    """수집이 '끊긴' 지표만 경보한다 — 애초에 수집한 적 없는 지표는 뺀다.

    두 경우만 경보 대상이다:
      · 사용자가 매일 기록하기로 정한 항목(profile.tracked())
      · 과거에 잘 들어오던 지표(3회 이상 관측)가 최근 비어버린 경우
    """
    if len(history) < MISSING_MIN_HISTORY:
        return

    expected = set(profile.tracked())
    for path, label, _, _ in bl.TRACKED:
        seen = len(bl.series(history, path))
        if path not in expected and seen < MISSING_SEEN_THRESHOLD:
            continue                      # 수집한 적 없는 지표 — 경보 대상 아님
        if path in HRV_PATHS and any(
            len(bl.series(history, other)) >= MISSING_SEEN_THRESHOLD
            for other in HRV_PATHS if other != path
        ):
            continue                      # 다른 쪽 HRV 가 들어오고 있다면 결측이 아니다
        if bl.missingness(history, path) > 0.5:
            r.flags.append(
                f"데이터 결측: 최근 2주 '{label}' 절반 이상 비어있음 — 수집 경로 점검 필요"
            )
