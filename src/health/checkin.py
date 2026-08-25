"""대화형 일일 체크인 — Phase 0 의 90초 게이트를 통과시키기 위한 도구.

왜 이 모듈이 필요한가
--------------------
`health log --set subjective.energy=3 --set ...` 는 하루 144자 타이핑이다.
설계상 90초 안에 끝날 수 없고, 90초를 넘으면 그 항목은 2주 안에 사라진다.
그래서 리커트 항목은 **키 한 번**으로 받고, 이미 채워진 항목은 묻지 않는다.

측정하지 않는 목표는 지켜지지 않으므로, 체크인은 자기 소요 시간을
`events.jsonl` 에 남긴다. `health status` 가 그 값으로 게이트를 판정한다.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from .schema import DailyRecord

# 프로필의 tracking 기본값. Phase 0 권장 세트 — 항목 5개.
# 10개를 고르면 2주 안에 그만둔다. 늘리는 것은 습관이 붙은 뒤에 한다.
DEFAULT_TRACKING = [
    "sleep.total_min",
    "vitals.resting_hr",
    "subjective.energy",
    "subjective.stress",
    "subjective.note",
]


@dataclass
class Prompt:
    path: str
    label: str
    kind: str                 # likert | nrs | number | duration | time | text
    hint: str = ""
    unit: str = ""
    parse: Callable[[str], Any] | None = None

    @property
    def single_key(self) -> bool:
        """리커트/NRS 는 한 글자만 받으면 되므로 Enter 없이 넘어갈 수 있다."""
        return self.kind == "likert"


def _duration(raw: str) -> float:
    """수면을 시간(7.5)으로도 분(450)으로도 받는다.

    사람은 "일곱 시간 반"으로 생각하지 "450분"으로 생각하지 않는다.
    24 미만이면 시간으로 해석한다 — 24분 잤다는 입력보다 24시간 표기 오류가
    훨씬 흔하고, 진짜 24분이면 다음 줄의 범위 경고에 걸린다.
    """
    v = float(raw.replace("h", "").replace("시간", "").strip())
    return round(v * 60, 1) if v < 24 else v


PROMPTS: dict[str, Prompt] = {
    "subjective.energy":   Prompt("subjective.energy", "활력", "likert", "1 바닥 · 3 보통 · 5 최고"),
    "subjective.mood":     Prompt("subjective.mood", "기분", "likert", "1 최악 · 3 보통 · 5 최고"),
    "subjective.stress":   Prompt("subjective.stress", "스트레스", "likert", "1 없음 · 3 보통 · 5 극심"),
    "subjective.soreness": Prompt("subjective.soreness", "근육통", "likert", "1 없음 · 5 심함"),
    "subjective.focus":    Prompt("subjective.focus", "집중력", "likert", "1 안 됨 · 5 잘 됨"),
    "subjective.pain_nrs": Prompt("subjective.pain_nrs", "통증", "nrs", "0 없음 ~ 10 최악", parse=int),
    "subjective.pain_site":Prompt("subjective.pain_site", "통증 부위", "text"),
    "subjective.note":     Prompt("subjective.note", "한 줄", "text",
                                  "어제와 달랐던 것 / 증상 / 신경 쓰이는 것 (Enter=건너뜀)"),
    "sleep.total_min":     Prompt("sleep.total_min", "잔 시간", "duration", "7.5 또는 450", "h/분", _duration),
    "sleep.bedtime":       Prompt("sleep.bedtime", "취침", "time", "23:40"),
    "sleep.waketime":      Prompt("sleep.waketime", "기상", "time", "07:05"),
    "sleep.efficiency_pct":Prompt("sleep.efficiency_pct", "수면 효율", "number", "", "%", float),
    "sleep.latency_min":   Prompt("sleep.latency_min", "입면 지연", "number", "눕고 잠들기까지", "분", float),
    "vitals.resting_hr":   Prompt("vitals.resting_hr", "안정시 심박", "number", "기상 직후", "bpm", float),
    "vitals.hrv_rmssd_ms": Prompt("vitals.hrv_rmssd_ms", "HRV", "number", "rMSSD", "ms", float),
    "vitals.weight_kg":    Prompt("vitals.weight_kg", "체중", "number", "", "kg", float),
    "vitals.bp_systolic":  Prompt("vitals.bp_systolic", "수축기 혈압", "number", "", "mmHg", float),
    "vitals.bp_diastolic": Prompt("vitals.bp_diastolic", "이완기 혈압", "number", "", "mmHg", float),
    "vitals.body_temp_c":  Prompt("vitals.body_temp_c", "체온", "number", "", "℃", float),
    "vitals.blood_glucose_mgdl": Prompt("vitals.blood_glucose_mgdl", "혈당", "number", "", "mg/dL", float),
    "vitals.spo2_pct":     Prompt("vitals.spo2_pct", "산소포화도", "number", "", "%", float),
    "activity.steps":      Prompt("activity.steps", "걸음 수", "number", "", "보", int),
    "intake.caffeine_mg":  Prompt("intake.caffeine_mg", "카페인", "number", "커피 1잔 ≈ 90", "mg", float),
    "intake.last_caffeine_at": Prompt("intake.last_caffeine_at", "마지막 카페인", "time", "16:30"),
    "intake.water_ml":     Prompt("intake.water_ml", "물", "number", "", "ml", float),
    "intake.alcohol_units":Prompt("intake.alcohol_units", "음주", "number", "소주 1잔 ≈ 1", "잔", float),
}

# 생리학적 범위. 벗어나면 저장하지 않고 되묻는다 — 오타 하나가
# 28일 베이스라인을 통째로 밀어버린다.
RANGES: dict[str, tuple[float, float]] = {
    "sleep.total_min": (0, 960),
    "sleep.efficiency_pct": (0, 100),
    "sleep.latency_min": (0, 600),
    "vitals.resting_hr": (25, 220),
    "vitals.hrv_rmssd_ms": (1, 300),
    "vitals.weight_kg": (20, 300),
    "vitals.bp_systolic": (50, 260),
    "vitals.bp_diastolic": (30, 180),
    "vitals.body_temp_c": (30, 43),
    "vitals.blood_glucose_mgdl": (20, 700),
    "vitals.spo2_pct": (50, 100),
    "activity.steps": (0, 100_000),
    "intake.caffeine_mg": (0, 2000),
    "intake.water_ml": (0, 10_000),
    "subjective.pain_nrs": (0, 10),
    "intake.alcohol_units": (0, 60),
}


class Aborted(Exception):
    """사용자가 Ctrl-C 또는 q 로 중단."""


def _read_line_key() -> str:
    """tty 가 아닐 때(파이프·스크립트·테스트)의 폴백.

    raw 모드 에코가 없으므로 줄바꿈을 직접 찍어야 출력이 뭉개지지 않는다.
    """
    try:
        raw = input()
    except EOFError:
        raise Aborted
    print()
    return raw.strip()[:1]


def _read_key() -> str:
    """리커트 답변 한 글자를 Enter 없이 받는다. tty 가 아니면 줄 단위로 폴백."""
    try:
        import termios
        import tty
    except ImportError:                      # Windows
        return _read_line_key()
    if not sys.stdin.isatty():
        return _read_line_key()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch in ("\x03", "\x04"):               # Ctrl-C / Ctrl-D
        raise Aborted
    print(ch)
    return ch


def _ask(p: Prompt, existing: Any) -> Any:
    """한 항목을 묻는다. 빈 입력이면 None(=기록 안 함)을 반환한다."""
    unit = f" ({p.unit})" if p.unit else ""
    was = f"  [기존 {existing}]" if existing is not None else ""
    hint = f"  {p.hint}" if p.hint else ""

    while True:
        if p.single_key:
            print(f"  {p.label}{unit} 1-5{hint}{was} › ", end="", flush=True)
            raw = _read_key()
        else:
            print(f"  {p.label}{unit}{hint}{was}")
            try:
                raw = input("  › ").strip()
            except EOFError:
                raise Aborted

        raw = raw.strip()
        if raw in ("", "\r", "\n"):
            return None                       # 결측은 정상이다. 캐묻지 않는다.
        if raw.lower() in ("q", "\x1b"):
            raise Aborted

        try:
            if p.kind == "likert":
                v: Any = int(raw)
                if not 1 <= v <= 5:
                    raise ValueError("1~5 사이여야 합니다")
            elif p.parse:
                v = p.parse(raw)
            else:
                v = raw
        except ValueError as e:
            print(f"    ↑ 다시 입력해 주세요 ({e})")
            continue

        lo, hi = RANGES.get(p.path, (float("-inf"), float("inf")))
        if isinstance(v, (int, float)) and not lo <= v <= hi:
            print(f"    ↑ {lo:g}~{hi:g} 범위를 벗어났습니다. 오타가 아닌지 확인해 주세요")
            continue
        return v


def run(rec: DailyRecord, tracking: list[str]) -> tuple[DailyRecord, float, int]:
    """체크인을 진행하고 (갱신된 레코드, 소요 초, 새로 채운 항목 수)를 반환.

    이미 값이 있는 항목은 묻지 않는다 — 웨어러블이 아침에 넣은 수면을
    저녁에 또 물으면 그게 마찰이다.
    """
    started = time.monotonic()
    asked = filled = 0

    for path in tracking:
        p = PROMPTS.get(path)
        if p is None:
            print(f"  (알 수 없는 항목 '{path}' — 건너뜀)")
            continue
        if rec.get_path(path) is not None:
            continue                          # 이미 채워짐
        asked += 1
        v = _ask(p, None)
        if v is not None:
            rec.set_path(path, v)
            filled += 1

    if asked == 0:
        print("  오늘 항목이 모두 채워져 있습니다.")
    return rec, round(time.monotonic() - started, 1), filled
