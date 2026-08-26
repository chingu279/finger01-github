"""개인 베이스라인과 이탈(deviation) 계산.

핵심 사상: **절대값이 아니라 나 자신의 평소 대비 변화**를 본다.
안정시 심박 58bpm이 누구에게는 정상이고 누구에게는 경보다.
그래서 모든 판단의 기준선은 인구집단 정상범위가 아니라
직전 28일의 나 자신이다.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence

from .schema import DailyRecord

# 베이스라인을 계산할 지표들: (점 경로, 표시명, 방향, 표준편차 하한)
#   direction=+1 → 높을수록 좋음, -1 → 낮을수록 좋음, 0 → 중립
#
#   sd_floor 가 왜 필요한가:
#   매일 "활력 3"이라고만 답하면 표준편차가 0이 되고, 그러면 오늘 1로
#   떨어져도 z 가 정의되지 않아 **아무 신호도 못 잡는다**. 리커트 척도나
#   반올림된 웨어러블 값에서 실제로 잘 일어나는 일이다.
#   그래서 지표별로 '이 정도 변화는 의미 있다'는 최소 폭을 하한으로 둔다.
TRACKED: list[tuple[str, str, int, float]] = [
    ("vitals.hrv_rmssd_ms", "HRV(rMSSD)", +1, 3.0),
    ("vitals.hrv_sdnn_ms", "HRV(SDNN)", +1, 5.0),
    ("vitals.resting_hr", "안정시 심박", -1, 1.5),
    ("vitals.spo2_pct", "산소포화도", +1, 0.5),
    ("vitals.afib_burden_pct", "심방세동 부담", -1, 1.0),
    ("vitals.walking_hr_avg", "보행 심박", -1, 2.0),
    ("vitals.body_temp_c", "체온", 0, 0.2),
    ("vitals.weight_kg", "체중", 0, 0.3),
    ("sleep.total_min", "총 수면", +1, 20.0),
    ("sleep.efficiency_pct", "수면 효율", +1, 2.0),
    ("sleep.deep_min", "깊은 수면", +1, 8.0),
    ("sleep.latency_min", "입면 지연", -1, 4.0),
    ("activity.steps", "걸음 수", +1, 500.0),
    ("subjective.energy", "활력", +1, 0.5),
    ("subjective.mood", "기분", +1, 0.5),
    ("subjective.stress", "스트레스", -1, 0.5),
    ("subjective.soreness", "근육통", -1, 0.5),
    ("subjective.pain_nrs", "통증(NRS)", -1, 1.0),
]

MIN_SAMPLES = 5           # 이보다 적으면 베이스라인을 신뢰하지 않는다
SIGNIFICANT_Z = 1.5       # "평소와 다르다"고 부를 최소 z


@dataclass
class Metric:
    path: str
    label: str
    direction: int
    n: int
    mean: float | None = None
    sd: float | None = None          # 하한이 적용된 유효 표준편차
    sd_raw: float | None = None      # 실제 관측 표준편차(진단용)
    latest: float | None = None
    z: float | None = None

    @property
    def reliable(self) -> bool:
        return self.n >= MIN_SAMPLES and self.sd is not None and self.sd > 0

    @property
    def deviation(self) -> str:
        """방향을 고려한 이탈 판정: better / worse / normal / unknown."""
        if self.z is None or not self.reliable or abs(self.z) < SIGNIFICANT_Z:
            return "normal" if self.reliable else "unknown"
        if self.direction == 0:
            return "shifted"
        return "better" if self.z * self.direction > 0 else "worse"

    def describe(self) -> str:
        if self.latest is None:
            return f"{self.label}: 기록 없음"
        if not self.reliable:
            return f"{self.label}: {_fmt(self.latest)} (표본 {self.n}일 — 베이스라인 형성 중)"
        arrow = {"better": "▲좋음", "worse": "▼주의", "shifted": "◆변동", "normal": "· 평소"}[
            self.deviation
        ]
        return (
            f"{self.label}: {_fmt(self.latest)} "
            f"(평소 {_fmt(self.mean)}±{_fmt(self.sd)}, z={self.z:+.1f}) {arrow}"
        )


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"


def series(history: Sequence[DailyRecord], path: str) -> list[float]:
    out: list[float] = []
    for rec in history:
        v = rec.get_path(path)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return out


def compute(
    history: Sequence[DailyRecord],
    today: DailyRecord | None = None,
    tracked: Sequence[tuple[str, str, int, float]] = TRACKED,
) -> dict[str, Metric]:
    """history(오늘 제외 권장)로 베이스라인을 만들고 today의 z를 계산.

    today를 주지 않으면 history의 마지막 날을 최신값으로 본다.
    """
    if today is None and history:
        today, history = history[-1], history[:-1]

    out: dict[str, Metric] = {}
    for path, label, direction, sd_floor in tracked:
        vals = series(history, path)
        m = Metric(path=path, label=label, direction=direction, n=len(vals))
        if vals:
            m.mean = statistics.fmean(vals)
            m.sd_raw = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            m.sd = max(m.sd_raw, sd_floor)
        if today is not None:
            v = today.get_path(path)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                m.latest = float(v)
        if m.reliable and m.latest is not None and m.mean is not None and m.sd:
            m.z = (m.latest - m.mean) / m.sd
        out[path] = m
    return out


def acwr(history: Sequence[DailyRecord], acute_days: int = 7, chronic_days: int = 28) -> float | None:
    """급성:만성 훈련부하 비율(ACWR).

    최근 7일 평균부하 / 최근 28일 평균부하. 0.8~1.3이 안전 구간으로 흔히
    인용되며, 1.5를 넘으면 부상 위험이 올라간다고 본다.
    (ACWR은 스포츠과학계에서 논쟁이 있는 지표다. 절대 기준이 아니라
     '지난주에 갑자기 늘렸는가'를 보는 신호로만 쓴다.)
    """
    if len(history) < acute_days:
        return None
    loads = [rec.activity.training_load() for rec in history]
    acute = statistics.fmean(loads[-acute_days:])
    chronic = statistics.fmean(loads[-chronic_days:])
    if chronic <= 0:
        return None
    return acute / chronic


def trend(history: Sequence[DailyRecord], path: str, window: int = 7) -> float | None:
    """최근 window일 평균 - 그 이전 window일 평균. 양수면 상승 추세."""
    vals = series(history, path)
    if len(vals) < window * 2:
        return None
    recent = statistics.fmean(vals[-window:])
    prior = statistics.fmean(vals[-window * 2 : -window])
    return recent - prior


def label_for(path: str) -> str:
    for p, label, _, _ in TRACKED:
        if p == path:
            return label
    return path


def missingness(history: Sequence[DailyRecord], path: str, days: int = 14) -> float:
    """최근 days일 중 해당 지표가 비어있는 비율. 데이터 품질 감시용."""
    if days <= 0:
        return 1.0
    recent = list(history)[-days:]
    if not recent:
        return 1.0
    present = len(series(recent, path))
    return 1.0 - present / len(recent)


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))
