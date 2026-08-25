"""레드플래그 트리아지 — 시스템의 안전 바닥(safety floor).

왜 LLM이 아니라 규칙 엔진인가
-----------------------------
"흉통 + 식은땀"을 놓치는 일은 있어서는 안 된다. LLM은 프롬프트, 맥락,
대화 흐름에 따라 출력이 흔들린다. 생명과 직결되는 판정은 **결정적 코드**로
먼저 돌리고, 그 결과를 에이전트에게 '반드시 전달해야 할 사실'로 주입한다.
에이전트는 이 판정을 완화하거나 무시할 수 없다.

이 모듈은 진단하지 않는다. "지금 전문가에게 가야 하는가"만 판정한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Sequence

from . import baseline as bl
from .schema import DailyRecord
from .store import Profile


class Severity(IntEnum):
    MONITOR = 0    # 기록해두고 추세를 본다
    ROUTINE = 1    # 수일 내 진료 권유
    URGENT = 2     # 24시간 내 진료 / 당일 상담
    EMERGENCY = 3  # 즉시 119 또는 응급실


LABEL = {
    Severity.MONITOR: "관찰",
    Severity.ROUTINE: "진료 권유(수일 내)",
    Severity.URGENT: "긴급(24시간 내)",
    Severity.EMERGENCY: "응급 — 즉시 119",
}

# 한국 기준 연락처. 프로필에 주치의가 있으면 그쪽을 우선 안내한다.
HOTLINES = {
    "emergency": "119 (응급의료)",
    "poison": "1339 (질병관리청 콜센터)",
    "mental": "109 (자살예방 상담전화, 24시간) / 1577-0199 (정신건강 상담)",
}

# 증상 텍스트 매칭용. 한글/영문 모두 잡는다.
_PATTERNS: dict[str, str] = {
    "chest_pain": r"흉통|가슴.{0,4}(통증|아프|답답|조[이임])|chest pain",
    "radiating": r"(팔|턱|어깨|등).{0,6}(뻗치|방사|저림|통증)|radiat",
    "diaphoresis": r"식은땀|냉한|cold sweat",
    "dyspnea": r"호흡곤란|숨.{0,3}(차|막|가쁨)|shortness of breath|dyspnea",
    "syncope": r"실신|기절|의식.{0,3}(소실|잃)|syncope|fainted",
    "stroke": r"편측|한쪽.{0,6}(마비|힘.{0,2}빠)|안면.{0,3}(마비|처짐)|언어장애|말.{0,3}어눌|발음.{0,3}(어눌|이상)|시야.{0,3}(소실|이상)|stroke|facial droop",
    "thunderclap": r"벼락.{0,3}두통|생애.{0,4}최악.{0,4}두통|갑작스[런러].{0,4}극심.{0,4}두통|thunderclap",
    "bleeding": r"토혈|각혈|혈변|흑변|혈뇨|대량.{0,3}출혈|coughing up blood|hematochezia|melena",
    "suicidal": r"자살|자해|죽고.{0,3}싶|살고.{0,3}싶지.{0,3}않|suicidal|self.?harm|end my life",
    "anaphylaxis": r"아나필락시스|목.{0,3}(붓|조[이임])|전신.{0,3}두드러기|anaphylax",
    "seizure": r"경련|발작|seizure|convulsion",
    "severe_abdo": r"복통.{0,6}(심[하한]|극심)|극심.{0,4}복통|반발압통",
}


@dataclass
class Finding:
    severity: Severity
    code: str
    message: str
    action: str
    evidence: list[str] = field(default_factory=list)

    def render(self) -> str:
        ev = f" [근거: {', '.join(self.evidence)}]" if self.evidence else ""
        return f"[{LABEL[self.severity]}] {self.message} → {self.action}{ev}"


@dataclass
class TriageResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def severity(self) -> Severity:
        return max((f.severity for f in self.findings), default=Severity.MONITOR)

    @property
    def blocks_exercise(self) -> bool:
        """URGENT 이상이면 어떤 운동 처방도 내리지 않는다."""
        return self.severity >= Severity.URGENT

    def render(self) -> str:
        if not self.findings:
            return "레드플래그 없음."
        lines = [f.render() for f in sorted(self.findings, key=lambda f: -f.severity)]
        if self.severity >= Severity.EMERGENCY:
            lines.insert(0, f"■ 지금 즉시 {HOTLINES['emergency']} 에 연락하세요.")
        return "\n".join(lines)


def _text(rec: DailyRecord) -> str:
    parts = list(rec.subjective.symptoms)
    for v in (rec.subjective.note, rec.subjective.pain_site):
        if v:
            parts.append(v)
    return " ".join(parts).lower()


def _has(text: str, key: str) -> bool:
    return re.search(_PATTERNS[key], text) is not None


# ── 규칙들 ────────────────────────────────────────────────────
# 각 규칙은 (history, today, profile) -> list[Finding]

def _rule_symptoms(history, today, profile) -> list[Finding]:
    t = _text(today)
    if not t:
        return []
    out: list[Finding] = []

    if _has(t, "chest_pain") and (_has(t, "radiating") or _has(t, "diaphoresis") or _has(t, "dyspnea")):
        out.append(Finding(
            Severity.EMERGENCY, "acs_suspect",
            "흉통에 방사통·식은땀·호흡곤란이 동반됨 — 심장 이벤트 배제가 필요합니다",
            f"즉시 {HOTLINES['emergency']}. 혼자 운전하지 말고, 아스피린 복용은 의료진 지시에 따르세요",
            ["증상 기록"],
        ))
    elif _has(t, "chest_pain"):
        out.append(Finding(
            Severity.URGENT, "chest_pain",
            "흉통이 기록됨",
            "당일 의료기관 상담. 악화·방사통·식은땀이 생기면 즉시 119",
            ["증상 기록"],
        ))

    if _has(t, "stroke"):
        out.append(Finding(
            Severity.EMERGENCY, "stroke_fast",
            "뇌졸중 의심 징후(편측 마비/안면 처짐/언어장애)",
            f"즉시 {HOTLINES['emergency']}. 증상 시작 시각을 기억해 의료진에게 전달하세요",
            ["FAST 스크리닝"],
        ))
    if _has(t, "thunderclap"):
        out.append(Finding(
            Severity.EMERGENCY, "thunderclap_headache",
            "수 초 내 최고조에 달한 극심한 두통",
            f"즉시 {HOTLINES['emergency']}", ["증상 기록"],
        ))
    if _has(t, "anaphylaxis"):
        out.append(Finding(
            Severity.EMERGENCY, "anaphylaxis",
            "아나필락시스 의심(기도 부종/전신 두드러기)",
            f"에피네프린 자가주사기가 있다면 즉시 사용 후 {HOTLINES['emergency']}", ["증상 기록"],
        ))
    if _has(t, "seizure"):
        out.append(Finding(
            Severity.EMERGENCY, "seizure",
            "경련/발작 기록", f"즉시 {HOTLINES['emergency']}", ["증상 기록"],
        ))
    if _has(t, "syncope"):
        out.append(Finding(
            Severity.URGENT, "syncope",
            "실신 또는 의식 소실",
            "당일 진료. 운전·수영·고소작업을 피하세요", ["증상 기록"],
        ))
    if _has(t, "bleeding"):
        out.append(Finding(
            Severity.URGENT, "bleeding",
            "토혈/각혈/혈변/흑변/혈뇨 등 출혈 징후",
            "당일 진료. 대량 출혈이면 즉시 119", ["증상 기록"],
        ))
    if _has(t, "severe_abdo"):
        out.append(Finding(
            Severity.URGENT, "severe_abdominal_pain",
            "극심한 복통", "당일 진료 — 급성복증 배제 필요", ["증상 기록"],
        ))
    if _has(t, "suicidal"):
        out.append(Finding(
            Severity.EMERGENCY, "suicidal_ideation",
            "자살/자해 관련 표현이 기록되었습니다. 혼자 견디지 않아도 됩니다",
            f"{HOTLINES['mental']} 에 지금 연락하세요. 위급하면 {HOTLINES['emergency']}",
            ["체크인 기록"],
        ))
    if _has(t, "dyspnea") and not any(f.code == "acs_suspect" for f in out):
        out.append(Finding(
            Severity.URGENT, "dyspnea",
            "호흡곤란 기록", "당일 진료. 안정 시에도 지속되면 즉시 119", ["증상 기록"],
        ))
    return out


def _rule_vitals(history, today, profile) -> list[Finding]:
    v = today.vitals
    out: list[Finding] = []

    if v.spo2_pct is not None:
        if v.spo2_pct < 90:
            out.append(Finding(
                Severity.EMERGENCY, "hypoxia",
                f"산소포화도 {v.spo2_pct:.0f}%", f"즉시 {HOTLINES['emergency']}",
                ["SpO2 측정값"],
            ))
        elif v.spo2_pct < 94:
            out.append(Finding(
                Severity.URGENT, "low_spo2",
                f"산소포화도 {v.spo2_pct:.0f}% (기준 94% 미만)",
                "재측정 후에도 낮으면 당일 진료 — 손이 차거나 측정 자세가 나쁘면 값이 낮게 나옵니다",
                ["SpO2 측정값"],
            ))

    if v.bp_systolic is not None and v.bp_diastolic is not None:
        if v.bp_systolic >= 180 or v.bp_diastolic >= 120:
            out.append(Finding(
                Severity.URGENT, "hypertensive_crisis",
                f"혈압 {v.bp_systolic:.0f}/{v.bp_diastolic:.0f} mmHg — 고혈압성 위기 범위",
                "5분 안정 후 재측정. 여전히 높거나 두통·흉통·시야이상이 동반되면 즉시 119",
                ["혈압 측정값"],
            ))
        elif v.bp_systolic >= 140 or v.bp_diastolic >= 90:
            out.append(Finding(
                Severity.ROUTINE, "hypertension",
                f"혈압 {v.bp_systolic:.0f}/{v.bp_diastolic:.0f} mmHg",
                "가정혈압을 1주간 아침·저녁 측정해 기록하고 진료 시 지참하세요",
                ["혈압 측정값"],
            ))
        elif v.bp_systolic < 90:
            out.append(Finding(
                Severity.URGENT, "hypotension",
                f"수축기 혈압 {v.bp_systolic:.0f} mmHg",
                "어지럼·실신이 동반되면 당일 진료", ["혈압 측정값"],
            ))

    if v.body_temp_c is not None:
        if v.body_temp_c >= 39.5:
            out.append(Finding(
                Severity.URGENT, "high_fever",
                f"체온 {v.body_temp_c:.1f}℃", "당일 진료", ["체온 측정값"],
            ))
        elif v.body_temp_c >= 38.0:
            fever_days = sum(
                1 for r in list(history)[-3:]
                if (r.vitals.body_temp_c or 0) >= 38.0
            ) + 1
            sev = Severity.URGENT if fever_days >= 3 else Severity.MONITOR
            out.append(Finding(
                sev, "fever",
                f"발열 {v.body_temp_c:.1f}℃ ({fever_days}일째)",
                "3일 이상 지속되면 진료. 오늘은 운동 중단, 수분 섭취를 늘리세요",
                ["체온 측정값"],
            ))

    if v.blood_glucose_mgdl is not None:
        g = v.blood_glucose_mgdl
        if g < 54:
            out.append(Finding(
                Severity.EMERGENCY, "severe_hypoglycemia",
                f"혈당 {g:.0f} mg/dL — 심한 저혈당",
                f"속효성 당분 15g 즉시 섭취 후 15분 뒤 재측정. 의식 저하 시 {HOTLINES['emergency']}",
                ["혈당 측정값"],
            ))
        elif g < 70:
            out.append(Finding(
                Severity.URGENT, "hypoglycemia",
                f"혈당 {g:.0f} mg/dL — 저혈당",
                "당분 15g 섭취 후 15분 뒤 재측정(15-15 규칙). 운동 금지", ["혈당 측정값"],
            ))
        elif g >= 300:
            out.append(Finding(
                Severity.URGENT, "hyperglycemia",
                f"혈당 {g:.0f} mg/dL", "당일 의료진 상담. 케톤 확인 권장", ["혈당 측정값"],
            ))
    return out


def _rule_deviation(history, today, profile) -> list[Finding]:
    """베이스라인 대비 급격한 이탈. 감염·과훈련·번아웃의 조기 신호."""
    out: list[Finding] = []
    metrics = bl.compute(history, today)

    rhr = metrics.get("vitals.resting_hr")
    if rhr and rhr.reliable and rhr.latest is not None and rhr.mean is not None:
        delta = rhr.latest - rhr.mean
        streak = 0
        for r in reversed(list(history)[-4:]):
            hr = r.vitals.resting_hr
            if hr is not None and hr - rhr.mean >= 7:
                streak += 1
            else:
                break
        if delta >= 15 or (delta >= 7 and streak >= 2):
            out.append(Finding(
                Severity.ROUTINE, "rhr_elevated",
                f"안정시 심박이 평소보다 {delta:+.0f}bpm 높음 ({streak + 1}일 연속)",
                "감염·탈수·과훈련·수면부족의 흔한 신호입니다. 오늘 고강도 운동은 중단하고, "
                "3일 이상 지속되면 진료를 고려하세요",
                [f"z={rhr.z:+.1f}"],
            ))

    hrv = metrics.get("vitals.hrv_rmssd_ms")
    if hrv and hrv.z is not None and hrv.z <= -2.0:
        out.append(Finding(
            Severity.MONITOR, "hrv_drop",
            f"HRV가 평소보다 크게 낮음 (z={hrv.z:+.1f})",
            "오늘은 회복 위주로. 카페인·음주·야간 고강도 운동을 피하세요", [],
        ))

    w = metrics.get("vitals.weight_kg")
    if w and w.reliable and w.latest is not None and w.mean is not None and w.mean > 0:
        pct = (w.latest - w.mean) / w.mean * 100
        if pct <= -5:
            out.append(Finding(
                Severity.ROUTINE, "weight_loss",
                f"체중이 평소 대비 {pct:.1f}% 감소",
                "의도하지 않은 감소라면 진료를 권합니다", [],
            ))

    pain = today.subjective.pain_nrs
    if pain is not None and pain >= 7:
        out.append(Finding(
            Severity.ROUTINE, "severe_pain",
            f"통증 NRS {pain}/10",
            "해당 부위 운동 중단. 2주 이상 지속되거나 야간통·체중감소 동반 시 진료", [],
        ))

    mood_low = [
        r.subjective.mood for r in list(history)[-13:] + [today]
        if r.subjective.mood is not None
    ]
    if len(mood_low) >= 10 and sum(1 for m in mood_low if m <= 2) >= len(mood_low) * 0.6:
        out.append(Finding(
            Severity.ROUTINE, "persistent_low_mood",
            "최근 2주간 기분 저하가 지속적으로 기록됨",
            f"우울 선별검사(PHQ-9)와 전문가 상담을 권합니다. {HOTLINES['mental']}", [],
        ))
    return out


def _rule_profile(history, today, profile) -> list[Finding]:
    """프로필의 기저질환·금기 사항이 오늘 상태와 충돌하는지."""
    out: list[Finding] = []
    conds = " ".join(profile.conditions).lower()
    t = _text(today)

    if re.search(r"천식|asthma|copd", conds) and _has(t, "dyspnea"):
        out.append(Finding(
            Severity.URGENT, "resp_exacerbation",
            "기저 호흡기질환 + 호흡곤란 — 악화 가능성",
            "구조약(속효성 기관지확장제) 사용 후 호전이 없으면 즉시 진료", ["프로필 기저질환"],
        ))
    if re.search(r"당뇨|diabet", conds) and today.vitals.blood_glucose_mgdl is None:
        out.append(Finding(
            Severity.MONITOR, "glucose_not_logged",
            "당뇨 이력이 있으나 오늘 혈당 기록이 없습니다",
            "측정값을 기록해야 운동·식이 조언의 안전 경계를 잡을 수 있습니다", [],
        ))
    if today.adherence.meds_missed:
        out.append(Finding(
            Severity.ROUTINE if len(today.adherence.meds_missed) > 1 else Severity.MONITOR,
            "med_missed",
            f"복약 누락: {', '.join(today.adherence.meds_missed)}",
            "임의 중단·이중 복용은 위험합니다. 처방의나 약사에게 대처법을 확인하세요", [],
        ))
    return out


RULES: list[Callable] = [_rule_symptoms, _rule_vitals, _rule_deviation, _rule_profile]


def evaluate(
    history: Sequence[DailyRecord],
    today: DailyRecord,
    profile: Profile | None = None,
) -> TriageResult:
    profile = profile or Profile()
    res = TriageResult()
    for rule in RULES:
        try:
            res.findings.extend(rule(history, today, profile))
        except Exception as e:  # 한 규칙의 버그가 나머지 안전망을 무너뜨리면 안 된다
            res.findings.append(Finding(
                Severity.MONITOR, "rule_error",
                f"트리아지 규칙 '{rule.__name__}' 실행 실패: {e}",
                "개발자 확인 필요 — 이 규칙은 이번 평가에서 누락되었습니다",
            ))
    return res
