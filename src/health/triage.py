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
    "head_injury": r"머리.{0,4}(부딪|찧|박|충격|다[쳤치])|두부.{0,3}(외상|손상)|넘어[져지].{0,10}머리|낙상|head (injury|trauma)|hit my head",
    "palpitation": r"두근|심장.{0,4}(빨리|불규칙|뛰[는던])|맥박.{0,4}불규칙|부정맥|가슴.{0,4}벌렁|palpitation|irregular (heart|pulse)",
    "bruising": r"멍이.{0,4}(잘|자주|많이)|쉽게.{0,3}멍|잇몸.{0,3}출혈|코피.{0,6}(자주|멈추지)|easy brui|nosebleed",
    "myalgia": r"근육통.{0,10}(심|전신)|전신.{0,4}근육통|근육.{0,4}(약화|힘.{0,2}빠)|myalgia|muscle (pain|weakness)",
    "dark_urine": r"소변.{0,6}(진[한하]|갈색|콜라|붉)|혈뇨|dark urine|tea.?colou?red",
    "nsaid": r"이부프로펜|부루펜|나프록센|낙센|아스피린|디클로페낙|소염진통제|NSAID|ibuprofen|naproxen|diclofenac",
}

# 약물 성분·상품명 → 계열. 프로필의 medications 문자열에서 찾아낸다.
#   왜 계열이 필요한가: "릭시아나 60mg" 이라는 문자열만으로는 규칙을 쓸 수 없다.
#   항응고제를 먹는 사람에게 머리 외상은 무증상이어도 응급이고, 맥박 조절
#   약물을 먹는 사람에게 서맥은 다른 의미를 갖는다.
MED_CLASS_PATTERNS: dict[str, str] = {
    #  사람은 "릭시아나정 60mg" 이 아니라 "릭시30mg" 이라고 적는다.
    #  축약형까지 잡되, 아래 unclassified_medications() 가 진짜 안전망이다.
    "anticoagulant": r"릭시아나|릭시\s*\d|에독사반|자렐토|자렐|리바록사반|엘리퀴스|엘리퀴|아픽사반|"
                     r"프라닥사|프라닥|다비가트란|와파린|쿠마딘|"
                     r"edoxaban|rivaroxaban|apixaban|dabigatran|warfarin",
    "antiplatelet": r"아스피린|플라빅스|클로피도그렐|브릴린타|티카그렐러|프라수그렐|"
                    r"aspirin|clopidogrel|ticagrelor|prasugrel",
    "antiarrhythmic": r"멀택|드로네다론|아미오다론|코다론|소탈롤|플레카이니드|프로파페논|리듬온|"
                      r"dronedarone|amiodarone|sotalol|flecainide|propafenone",
    "beta_blocker": r"콘코르|비소프롤롤|메토프롤롤|베타록|아테놀롤|카베딜롤|딜라트렌|프로프라놀롤|인데놀|"
                    r"bisoprolol|metoprolol|atenolol|carvedilol|propranolol",
    "rate_limiting_ccb": r"딜티아젬|헤르벤|베라파밀|이솝틴|diltiazem|verapamil",
    "statin": r"크레스토|로수바스타틴|리피토|아토르바스타틴|심바스타틴|조코|피타바스타틴|리바로|"
              r"rosuvastatin|atorvastatin|simvastatin|pitavastatin",
    "insulin_or_sulfonylurea": r"인슐린|란투스|투제오|글리메피리드|아마릴|글리클라지드|디아미크롱|"
                               r"insulin|glimepiride|gliclazide|glipizide",
    #  아래는 지금 걸린 규칙이 없는 계열들이다. 그래도 등록해 두는 이유:
    #  분류에 실패했다는 경고를 "정말 모르는 약"에만 띄우기 위해서다.
    #  넥시움 같은 흔한 약까지 매일 경고하면 진짜 경고를 무시하게 된다.
    "ppi": r"넥시움|에스오메프라졸|란스톤|란소프라졸|파리에트|라베프라졸|오메프라졸|덱실란트|"
           r"esomeprazole|lansoprazole|rabeprazole|omeprazole|pantoprazole",
    "acetaminophen": r"타이레놀|아세트아미노펜|써스펜|acetaminophen|paracetamol|tylenol",
    "arb_or_acei": r"로사르탄|코자|발사르탄|디오반|텔미사르탄|올메사르탄|칸데사르탄|라미프릴|에날라프릴|"
                   r"losartan|valsartan|telmisartan|olmesartan|candesartan|ramipril|enalapril",
    "thyroid": r"신지로이드|씬지로이드|레보티록신|levothyroxine|synthroid",
    "supplement": r"비타민|오메가|영양제|유산균|프로바이오틱|마그네슘|칼슘|철분|"
                  r"vitamin|omega|probiotic|magnesium",
}

# 계열은 알지만 아직 걸린 규칙이 없는 것들. 프로필 표시에서 구분해 준다.
CLASSES_WITHOUT_RULES = {"ppi", "acetaminophen", "arb_or_acei", "thyroid", "supplement"}


def unclassified_medications(profile: Profile) -> list[str]:
    """계열을 알아내지 못한 약 목록.

    이 약들에 대해서는 복약 기반 규칙이 **하나도 걸리지 않는다.**
    항응고제를 "릭시30mg" 이라고만 적으면 '머리 외상 = 응급' 규칙이
    조용히 사라진다. 규칙이 빠진 것보다 빠졌다는 걸 말하지 않는 쪽이
    훨씬 위험하므로, 분류 실패를 반드시 드러낸다.
    """
    out: list[str] = []
    for med in profile.medications:
        if not any(re.search(pattern, med, re.I)
                   for pattern in MED_CLASS_PATTERNS.values()):
            out.append(med)
    return out


def med_classes(profile: Profile) -> set[str]:
    """프로필의 복약 목록에서 약물 계열을 추출한다."""
    text = " ".join(profile.medications).lower()
    found = {name for name, pattern in MED_CLASS_PATTERNS.items()
             if re.search(pattern, text, re.I)}
    if found & {"antiarrhythmic", "beta_blocker", "rate_limiting_ccb"}:
        found.add("rate_control")      # 맥박을 낮추는 약을 먹고 있다
    if found & {"anticoagulant", "antiplatelet"}:
        found.add("bleeding_risk")
    return found


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
    unknown = unclassified_medications(profile)
    if unknown:
        out.append(Finding(
            Severity.MONITOR, "unclassified_medication",
            f"계열을 알 수 없는 약이 있습니다: {', '.join(unknown)}",
            "성분명을 함께 적어주세요 (예: '릭시아나(에독사반) 60mg'). "
            "그래야 항응고제·맥박조절제 같은 계열별 안전 규칙이 작동합니다 "
            "— 지금 이 약들에는 복약 기반 규칙이 하나도 걸려 있지 않습니다",
            ["프로필 복약 목록"],
        ))

    if today.adherence.meds_missed:
        out.append(Finding(
            Severity.ROUTINE if len(today.adherence.meds_missed) > 1 else Severity.MONITOR,
            "med_missed",
            f"복약 누락: {', '.join(today.adherence.meds_missed)}",
            "임의 중단·이중 복용은 위험합니다. 처방의나 약사에게 대처법을 확인하세요", [],
        ))
    return out


def _rule_medications(history, today, profile) -> list[Finding]:
    """복약 계열이 바꾸는 판정들.

    같은 증상도 무슨 약을 먹고 있느냐에 따라 긴급도가 달라진다.
    이 규칙들은 **상향만** 한다 — 어떤 약을 먹는다고 위험을 낮추지 않는다.
    """
    out: list[Finding] = []
    classes = med_classes(profile)
    t = _text(today)
    v = today.vitals

    if "bleeding_risk" in classes:
        if _has(t, "head_injury"):
            out.append(Finding(
                Severity.EMERGENCY, "anticoag_head_injury",
                "항응고·항혈소판제 복용 중 머리 외상이 기록되었습니다",
                f"지금 증상이 없어도 즉시 응급실. 두개내 출혈은 수 시간~수일 뒤에 "
                f"나타날 수 있습니다. {HOTLINES['emergency']}",
                ["복약 계열: 출혈 위험"],
            ))
        if _has(t, "bruising"):
            out.append(Finding(
                Severity.ROUTINE, "anticoag_bruising",
                "항응고·항혈소판제 복용 중 멍·잇몸출혈·코피가 기록되었습니다",
                "처방의에게 알리고 다음 진료 때 확인하세요. 임의로 약을 거르지 마세요",
                ["복약 계열: 출혈 위험"],
            ))
        if _has(t, "nsaid"):
            out.append(Finding(
                Severity.ROUTINE, "anticoag_nsaid",
                "항응고·항혈소판제와 소염진통제(NSAID)를 함께 쓰면 출혈 위험이 올라갑니다",
                "복용 전 약사나 처방의에게 확인하세요", ["복약 계열: 출혈 위험"],
            ))

    if "rate_control" in classes:
        hr = v.resting_hr
        symptomatic = _has(t, "syncope") or _has(t, "dyspnea") or "어지" in t
        if hr is not None and hr < 45:
            out.append(Finding(
                Severity.URGENT if symptomatic else Severity.ROUTINE,
                "bradycardia_on_rate_control",
                f"맥박 조절 약물 복용 중 안정시 심박 {hr:.0f}bpm",
                "처방의 확인이 필요합니다. 어지럼·실신·호흡곤란이 동반되면 당일 진료. "
                "임의로 약을 중단하지 마세요",
                ["복약 계열: 맥박 조절"],
            ))
        if v.low_hr_events:
            out.append(Finding(
                Severity.ROUTINE, "low_hr_alerts",
                f"저심박 알림 {v.low_hr_events}회 (맥박 조절 약물 복용 중)",
                "알림 기록을 다음 진료 때 보여주세요", ["웨어러블 알림"],
            ))

    if "statin" in classes and _has(t, "myalgia"):
        sev = Severity.URGENT if _has(t, "dark_urine") else Severity.ROUTINE
        out.append(Finding(
            sev, "statin_myopathy_watch",
            "스타틴 복용 중 근육통" + (" + 짙은 소변" if _has(t, "dark_urine") else ""),
            "근육 손상 여부 확인이 필요합니다. 짙은 소변이 동반되면 당일 진료. "
            "임의 중단 말고 처방의와 상의하세요",
            ["복약 계열: 스타틴"],
        ))

    return out


def _rule_arrhythmia(history, today, profile) -> list[Finding]:
    """부정맥 관련 신호. 웨어러블이 주는 값과 증상을 함께 본다.

    판정은 "지금 전문가에게 알려야 하는가"까지다. 재발 여부나 시술 성공
    여부를 말하지 않는다 — 그건 홀터·심전도와 의료진의 영역이다.
    """
    out: list[Finding] = []
    v = today.vitals
    t = _text(today)
    red = _has(t, "chest_pain") or _has(t, "dyspnea") or _has(t, "syncope")

    if v.ecg_afib:
        out.append(Finding(
            Severity.URGENT if red else Severity.ROUTINE,
            "ecg_afib",
            "웨어러블 심전도에서 심방세동 판정이 기록되었습니다",
            "기록을 저장해 담당 의료진에게 전달하세요. 흉통·호흡곤란·실신이 "
            f"동반되면 즉시 {HOTLINES['emergency']}",
            ["Apple Watch ECG"],
        ))

    if v.afib_burden_pct is not None and v.afib_burden_pct > 0:
        prior = [r.vitals.afib_burden_pct for r in list(history)[-7:]
                 if r.vitals.afib_burden_pct is not None]
        avg = sum(prior) / len(prior) if prior else 0.0
        rising = v.afib_burden_pct > max(5.0, avg * 2)
        out.append(Finding(
            Severity.URGENT if red else (Severity.ROUTINE if rising else Severity.MONITOR),
            "afib_burden",
            f"심방세동 부담 {v.afib_burden_pct:.1f}%"
            + (f" (최근 7일 평균 {avg:.1f}%)" if prior else ""),
            "추세를 기록해 다음 진료 때 보여주세요"
            + (". 뚜렷이 올라간 상태라 담당의 확인을 권합니다" if rising else "")
            + (f". 흉통·호흡곤란·실신 동반 시 즉시 {HOTLINES['emergency']}" if red else ""),
            ["Apple Watch 심방세동 이력"],
        ))

    if v.irregular_rhythm_events:
        out.append(Finding(
            Severity.URGENT if red else Severity.ROUTINE,
            "irregular_rhythm_alert",
            f"불규칙 심박 알림 {v.irregular_rhythm_events}회",
            "가능하면 알림 직후 심전도를 기록하고 담당 의료진에게 전달하세요",
            ["웨어러블 알림"],
        ))

    if _has(t, "palpitation"):
        out.append(Finding(
            Severity.URGENT if red else Severity.ROUTINE,
            "palpitation",
            "두근거림·맥박 불규칙 증상이 기록되었습니다",
            "증상이 있을 때 심전도를 기록해두면 진료에 크게 도움이 됩니다. "
            f"흉통·호흡곤란·실신이 동반되면 즉시 {HOTLINES['emergency']}",
            ["증상 기록"],
        ))

    if v.high_hr_events:
        out.append(Finding(
            Severity.MONITOR, "high_hr_alerts",
            f"안정 시 고심박 알림 {v.high_hr_events}회",
            "반복되면 다음 진료 때 알리세요", ["웨어러블 알림"],
        ))
    return out


RULES: list[Callable] = [
    _rule_symptoms, _rule_vitals, _rule_deviation, _rule_profile,
    _rule_medications, _rule_arrhythmia,
]


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
