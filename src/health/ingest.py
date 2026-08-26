"""웨어러블·헬스앱 내보내기 파일을 일별 레코드로 정규화한다.

지금은 Apple Health(`export.xml`)만 지원한다. 어댑터를 하나 더 붙이는
비용이 작도록 파싱과 정규화를 분리해 두었다.

이 모듈에서 사고가 나는 지점은 파싱이 아니라 **의미론**이다.
같은 이름의 지표가 기기마다 다른 값을 뜻하고, 같은 날의 걸음이 두 기기에서
두 번 들어오며, 자정을 넘긴 수면이 어느 날에 속하는지가 자명하지 않다.
아래 주석의 대부분은 그 함정들에 대한 것이다.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .checkin import RANGES
from .schema import DailyRecord, Workout

# ── 단위 변환 ─────────────────────────────────────────────────
LB_TO_KG = 0.45359237
MMOL_TO_MGDL = 18.0182          # 혈당


def _to_c(value: float, unit: str) -> float:
    return (value - 32) / 1.8 if "F" in unit else value


def _to_kg(value: float, unit: str) -> float:
    return value * LB_TO_KG if "lb" in unit.lower() else value


def _to_pct(value: float, unit: str) -> float:
    """Apple 은 SpO2 를 unit="%" 에 0.97 같은 **분율**로 내보낸다.

    이걸 그대로 저장하면 SpO2 0.97% 가 되어 트리아지가 매일 응급을 띄운다.
    """
    return value * 100 if value <= 1.0 else value


def _to_mgdl(value: float, unit: str) -> float:
    return value * MMOL_TO_MGDL if "mmol" in unit.lower() else value


# ── Apple Health 레코드 타입 → 우리 스키마 ─────────────────────
#   (스키마 경로, 하루 집계 방식, 단위 변환기)
#   집계 방식이 지표마다 다르다는 게 핵심이다. 걸음을 평균 내거나
#   체중을 합산하면 조용히 말이 안 되는 값이 저장된다.
QUANTITY_MAP: dict[str, tuple[str, str, Any]] = {
    "HKQuantityTypeIdentifierRestingHeartRate":
        ("vitals.resting_hr", "mean", None),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN":
        # Apple 이 내보내는 유일한 HRV 는 SDNN 이다. rMSSD 칸에 넣지 않는다.
        ("vitals.hrv_sdnn_ms", "mean", None),
    "HKQuantityTypeIdentifierOxygenSaturation":
        ("vitals.spo2_pct", "mean", _to_pct),
    "HKQuantityTypeIdentifierBodyMass":
        ("vitals.weight_kg", "last", _to_kg),
    "HKQuantityTypeIdentifierBodyFatPercentage":
        ("vitals.body_fat_pct", "last", _to_pct),
    "HKQuantityTypeIdentifierBodyTemperature":
        ("vitals.body_temp_c", "mean", _to_c),
    "HKQuantityTypeIdentifierAppleSleepingWristTemperature":
        ("vitals.body_temp_c", "mean", _to_c),
    "HKQuantityTypeIdentifierBloodPressureSystolic":
        ("vitals.bp_systolic", "last", None),
    "HKQuantityTypeIdentifierBloodPressureDiastolic":
        ("vitals.bp_diastolic", "last", None),
    "HKQuantityTypeIdentifierBloodGlucose":
        ("vitals.blood_glucose_mgdl", "mean", _to_mgdl),
    "HKQuantityTypeIdentifierStepCount":
        ("activity.steps", "sum_dedup", None),
    "HKQuantityTypeIdentifierActiveEnergyBurned":
        ("activity.active_kcal", "sum_dedup", None),
    "HKQuantityTypeIdentifierDietaryCaffeine":
        ("intake.caffeine_mg", "sum", None),
    "HKQuantityTypeIdentifierDietaryWater":
        ("intake.water_ml", "sum", None),
    "HKQuantityTypeIdentifierWalkingHeartRateAverage":
        ("vitals.walking_hr_avg", "mean", None),
    "HKQuantityTypeIdentifierVO2Max":
        ("vitals.vo2max", "mean", None),
    "HKQuantityTypeIdentifierAtrialFibrillationBurden":
        # iOS 16+ '심방세동 이력'. 하루 중 AF 로 보낸 시간 비율.
        # Apple 은 여기서도 분율(0.12)로 내보낸다.
        ("vitals.afib_burden_pct", "mean", _to_pct),
}

# 개수를 세는 이벤트형 레코드. 값이 아니라 발생 횟수가 신호다.
EVENT_MAP: dict[str, str] = {
    "HKCategoryTypeIdentifierIrregularHeartRhythmEvent": "vitals.irregular_rhythm_events",
    "HKCategoryTypeIdentifierHighHeartRateEvent": "vitals.high_hr_events",
    "HKCategoryTypeIdentifierLowHeartRateEvent": "vitals.low_hr_events",
}

# ECG 판정에서 심방세동을 뜻하는 값들
ECG_AFIB_VALUES = {
    "HKElectrocardiogramClassificationAtrialFibrillation",
    "AtrialFibrillation",
    "심방세동",
}

ASLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisAsleep",            # iOS 15 이하
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
}
IN_BED = "HKCategoryValueSleepAnalysisInBed"
AWAKE = "HKCategoryValueSleepAnalysisAwake"

# 수면 구간을 하나의 '밤'으로 묶는 최대 간격. 낮잠과 밤잠을 합치면
# 총 수면이 부풀고, 너무 짧게 잡으면 새벽 각성마다 밤이 쪼개진다.
SLEEP_SESSION_GAP = timedelta(hours=3)


def parse_ts(raw: str) -> datetime | None:
    """"2026-08-25 07:01:00 +0900" 형태를 파싱한다.

    오프셋이 붙어 있으므로 기록된 현지 시각을 그대로 쓴다 — 여행 중의
    기록을 집 타임존으로 옮기면 그날의 수면이 엉뚱한 날짜로 간다.
    """
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Interval:
    start: datetime
    end: datetime
    value: str
    source: str = ""

    @property
    def minutes(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds() / 60)


def merged_minutes(intervals: list[Interval]) -> float:
    """겹치는 구간을 합집합으로 눌러서 분을 센다.

    아이폰과 애플워치가 같은 밤을 각각 기록하면 구간이 겹친다. 그냥 더하면
    7시간 잔 밤이 14시간이 된다. 실제 11년치 내보내기에서 17.6시간짜리
    '수면'이 나온 원인이 이것이었고, 16시간을 안 넘긴 밤들은 범위 검증도
    통과해 **조용히 부풀려진 채** 저장됐다 — 그쪽이 훨씬 위험하다.
    """
    if not intervals:
        return 0.0
    spans = sorted((i.start, i.end) for i in intervals)
    total = 0.0
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)      # 겹침 — 하나로 합친다
        else:
            total += (cur_end - cur_start).total_seconds() / 60
            cur_start, cur_end = start, end
    total += (cur_end - cur_start).total_seconds() / 60
    return max(0.0, total)


def merged_spans(intervals: list[Interval]) -> list[tuple[datetime, datetime]]:
    """겹침을 합친 구간 목록. 각성 '횟수'를 세려면 개수가 필요하다."""
    if not intervals:
        return []
    spans = sorted((i.start, i.end) for i in intervals)
    out = [spans[0]]
    for start, end in spans[1:]:
        if start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


@dataclass
class IngestReport:
    days: int = 0
    first: str | None = None
    last: str | None = None
    records_seen: int = 0
    by_metric: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rejected: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    workouts: int = 0
    split_nights: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"적재 {self.days}일 ({self.first} ~ {self.last})",
                 f"원본 레코드 {self.records_seen:,}건 · 운동 {self.workouts}건"]
        if self.by_metric:
            lines.append("지표별 채워진 날:")
            for k, v in sorted(self.by_metric.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {k:<26} {v}일")
        if self.rejected:
            lines.append(f"범위 밖이라 저장하지 않은 값 {len(self.rejected)}건:")
            lines += [f"  {r}" for r in self.rejected[:10]]
            if len(self.rejected) > 10:
                lines.append(f"  … 외 {len(self.rejected) - 10}건")
        if self.split_nights:
            lines.append(
                f"수면 세션이 둘 이상 잡힌 날 {len(self.split_nights)}일 — 긴 쪽만 썼습니다:")
            lines += [f"  {n}" for n in self.split_nights[:5]]
            if len(self.split_nights) > 5:
                lines.append(f"  … 외 {len(self.split_nights) - 5}일")
            lines.append("  (시계를 밤중에 벗으셨다면 실제 수면이 더 길 수 있습니다)")
        if self.gaps:
            lines.append("기록이 비어 있는 구간:")
            lines += [f"  {g}" for g in self.gaps]
        return "\n".join(lines)


def iter_elements(path: Path) -> Iterator[ET.Element]:
    """export.xml 을 스트리밍으로 읽는다.

    이 파일은 수백 MB 에서 GB 단위까지 간다. 통째로 트리에 올리면
    메모리가 터지므로, 다 쓴 엘리먼트는 즉시 버린다.
    """
    context = ET.iterparse(str(path), events=("start", "end"))
    _, root = next(context)
    for event, elem in context:
        if event != "end":
            continue
        if elem.tag in ("Record", "Workout", "Electrocardiogram"):
            yield elem
        elem.clear()
        root.clear()     # 루트에 쌓이는 참조까지 끊어야 실제로 해제된다


def parse_apple(path: Path, since: str | None = None) -> tuple[dict[str, DailyRecord], IngestReport]:
    rep = IngestReport()
    # 스칼라 지표: date -> path -> 값 목록
    scalars: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    # 출처별 합산이 필요한 지표: date -> path -> source -> 합
    per_source: dict[str, dict[str, dict[str, float]]] = \
        defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    sleep_intervals: list[Interval] = []
    workouts: dict[str, list[Workout]] = defaultdict(list)
    events: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ecg: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for elem in iter_elements(path):
        rep.records_seen += 1
        start = parse_ts(elem.get("startDate", ""))
        if start is None:
            continue
        day = start.date().isoformat()
        if since and day < since:
            continue

        if elem.tag == "Electrocardiogram":
            ecg[day]["count"] += 1
            if (elem.get("classification") or "") in ECG_AFIB_VALUES:
                ecg[day]["afib"] += 1
            continue

        if elem.tag == "Workout":
            dur = float(elem.get("duration") or 0)
            if (elem.get("durationUnit") or "min").startswith("h"):
                dur *= 60
            dist = elem.get("totalDistance")
            workouts[day].append(Workout(
                type=(elem.get("workoutActivityType") or "")
                     .replace("HKWorkoutActivityType", "").lower() or "workout",
                duration_min=round(dur, 1),
                distance_km=float(dist) if dist else None,
            ))
            rep.workouts += 1
            continue

        rtype = elem.get("type", "")

        if rtype == "HKCategoryTypeIdentifierSleepAnalysis":
            end = parse_ts(elem.get("endDate", ""))
            if end:
                sleep_intervals.append(Interval(
                    start, end, elem.get("value", ""), elem.get("sourceName", "")))
            continue

        event_target = EVENT_MAP.get(rtype)
        if event_target:
            events[day][event_target] += 1
            continue

        mapped = QUANTITY_MAP.get(rtype)
        if mapped is None:
            continue
        target, how, convert = mapped
        try:
            value = float(elem.get("value", ""))
        except ValueError:
            continue                     # 문자열 값(예: 심전도 분류) — 수치 지표가 아니다
        if convert:
            value = convert(value, elem.get("unit", ""))

        if how == "sum_dedup":
            per_source[day][target][elem.get("sourceName", "?")] += value
        elif how == "sum":
            scalars[day][target].append(value)
        else:
            scalars[day][f"{target}|{how}"].append(value)

    # ── 조립 ──────────────────────────────────────────────────
    records: dict[str, DailyRecord] = {}

    def rec_for(d: str) -> DailyRecord:
        if d not in records:
            records[d] = DailyRecord(date=d, sources=["apple-health"])
        return records[d]

    for day, targets in scalars.items():
        r = rec_for(day)
        for key, values in targets.items():
            if not values:
                continue
            target, _, how = key.partition("|")
            if how == "mean":
                v = sum(values) / len(values)
            elif how == "last":
                v = values[-1]
            else:
                v = sum(values)
            _set_checked(r, target, v, rep)

    for day, targets in per_source.items():
        r = rec_for(day)
        for target, by_source in targets.items():
            # 아이폰과 애플워치가 같은 걸음을 각각 기록한다. 합치면 두 배가
            # 되므로, 가장 많이 잡은 출처 하나만 쓴다.
            _set_checked(r, target, max(by_source.values()), rep)

    for day, ws in workouts.items():
        rec_for(day).activity.workouts.extend(ws)

    for day, counts in events.items():
        r = rec_for(day)
        for target, n in counts.items():
            _set_checked(r, target, n, rep)

    for day, counts in ecg.items():
        r = rec_for(day)
        _set_checked(r, "vitals.ecg_readings", counts["count"], rep)
        if counts["count"]:
            _set_checked(r, "vitals.ecg_afib", counts["afib"] > 0, rep)

    for day, block in _assemble_sleep(sleep_intervals, rep).items():
        if since and day < since:
            continue
        r = rec_for(day)
        for target, v in block.items():
            _set_checked(r, target, v, rep)

    # 값이 범위 밖이라 전부 거부된 날은 레코드가 껍데기만 남는다.
    # 그대로 저장하면 기록하지 않은 날이 기록한 날로 셈해진다.
    records = {d: r for d, r in records.items() if not r.is_empty()}

    _finish_report(records, rep)
    return records, rep


def _set_checked(rec: DailyRecord, target: str, value: Any, rep: IngestReport) -> None:
    """생리학적 범위를 벗어난 값은 저장하지 않고 보고한다.

    웨어러블 오작동 값 하나가 28일 베이스라인을 통째로 밀어버린다.
    조용히 버리지도 않는다 — 무엇이 왜 빠졌는지 알아야 수집 경로를 고친다.
    """
    if isinstance(value, float):
        value = round(value, 1)
    lo, hi = RANGES.get(target, (float("-inf"), float("inf")))
    if isinstance(value, (int, float)) and not lo <= value <= hi:
        rep.rejected.append(f"{rec.date} {target}={value} (허용 {lo:g}~{hi:g})")
        return
    if target == "activity.steps":
        value = int(value)
    rec.set_path(target, value)
    rep.by_metric[target] += 1


def _assemble_sleep(
    intervals: list[Interval], rep: IngestReport | None = None
) -> dict[str, dict[str, float | str]]:
    """수면 구간들을 '밤' 단위로 묶어 하루치 수면 지표를 만든다.

    귀속 규칙: 자정을 넘긴 수면은 **기상한 날**에 넣는다. 사람은
    "어젯밤 몇 시간 잤나"가 아니라 "오늘 몇 시간 자고 일어났나"로
    컨디션을 판단하기 때문이다.
    """
    if not intervals:
        return {}
    intervals.sort(key=lambda i: i.start)

    sessions: list[list[Interval]] = [[intervals[0]]]
    for iv in intervals[1:]:
        if iv.start - max(x.end for x in sessions[-1]) > SLEEP_SESSION_GAP:
            sessions.append([iv])
        else:
            sessions[-1].append(iv)

    out: dict[str, dict[str, float | str]] = {}
    for session in sessions:
        asleep = [i for i in session if i.value in ASLEEP_VALUES]
        in_bed = [i for i in session if i.value == IN_BED]
        if not asleep:
            continue                     # 침대에만 있었던 구간은 수면이 아니다

        total = merged_minutes(asleep)
        if total < 60:
            continue                     # 낮잠·오탐 — 밤잠으로 세지 않는다

        wake = max(i.end for i in asleep)
        day = wake.date().isoformat()
        block: dict[str, float | str] = {
            "sleep.total_min": round(total, 1),
            "sleep.waketime": wake.strftime("%H:%M"),
        }

        deep = merged_minutes([i for i in asleep if i.value.endswith("Deep")])
        rem = merged_minutes([i for i in asleep if i.value.endswith("REM")])
        if deep:
            block["sleep.deep_min"] = round(deep, 1)
        if rem:
            block["sleep.rem_min"] = round(rem, 1)

        awakenings = sum(
            1 for start, end in merged_spans([i for i in session if i.value == AWAKE])
            if (end - start).total_seconds() >= 60
        )
        if awakenings:
            block["sleep.awakenings"] = awakenings

        bed_start = min(i.start for i in (in_bed or asleep))
        block["sleep.bedtime"] = bed_start.strftime("%H:%M")

        if in_bed:
            bed_min = merged_minutes(in_bed)
            if bed_min > 0:
                block["sleep.efficiency_pct"] = round(min(100.0, total / bed_min * 100), 1)
            latency = (min(i.start for i in asleep) - bed_start).total_seconds() / 60
            if latency >= 0:
                block["sleep.latency_min"] = round(latency, 1)

        # 같은 날에 두 세션이 잡히면 긴 쪽을 그날의 밤으로 본다.
        # 낮잠을 밤잠에 더하지 않기 위해서다 — 수면을 부풀려 "충분히 잤다"고
        # 말하는 쪽이 짧게 잡는 쪽보다 위험하다.
        # 다만 조용히 버리지는 않는다: 시계를 밤중에 벗어 밤이 쪼개진 경우라면
        # 실제 수면이 더 길다. 그 사실을 보고해 사용자가 판단하게 한다.
        if day in out:
            if rep is not None and day not in rep.split_nights:
                rep.split_nights.append(day)
            if out[day]["sleep.total_min"] >= block["sleep.total_min"]:
                continue
        out[day] = block
    return out


def _finish_report(records: dict[str, DailyRecord], rep: IngestReport) -> None:
    if not records:
        return
    days = sorted(records)
    rep.days, rep.first, rep.last = len(days), days[0], days[-1]

    # 결측 구간을 보고한다. 조용히 넘어가면 '데이터가 없는 것'과
    # '그날 아무 일도 없던 것'을 구별할 수 없다.
    from datetime import date as _d

    cur = _d.fromisoformat(days[0])
    end = _d.fromisoformat(days[-1])
    have = set(days)
    gap_start = None
    while cur <= end:
        iso = cur.isoformat()
        if iso not in have:
            gap_start = gap_start or iso
        elif gap_start:
            rep.gaps.append(f"{gap_start} ~ {(cur - timedelta(days=1)).isoformat()}")
            gap_start = None
        cur += timedelta(days=1)
    if gap_start:
        rep.gaps.append(f"{gap_start} ~ {end.isoformat()}")


PARSERS = {"apple": parse_apple}
