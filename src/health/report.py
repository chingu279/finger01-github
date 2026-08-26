"""에이전트에게 넘길 '사실 브리핑'을 만든다.

에이전트가 원시 JSON을 직접 읽고 통계를 암산하게 두면 틀린다.
계산은 코드가 하고, 에이전트는 **확정된 숫자에 대한 해석과 코칭**만 한다.
이것이 이 시스템의 환각 방지 1차 방어선이다.
"""

from __future__ import annotations

from typing import Sequence

from . import baseline as bl
from . import readiness as rd
from . import triage as tg
from .schema import DailyRecord
from .store import Profile, Store

DISCLAIMER = (
    "※ 이 브리핑은 의료 진단이 아닙니다. 자기관리를 위한 참고 정보이며, "
    "증상이 걱정된다면 의료 전문가의 진료를 받으세요."
)


def daily_brief(store: Store, date: str) -> str:
    profile = store.load_profile()
    today = store.load(date)
    if today is None:
        return f"{date} 기록이 없습니다. 먼저 체크인을 진행하세요."
    history = [r for r in store.history(end=date, days=29) if r.date != date]

    r = rd.compute(history, today, profile)
    t = tg.evaluate(history, today, profile)
    metrics = bl.compute(history, today)

    lines: list[str] = [f"# 일일 건강 브리핑 — {date}", ""]

    # 안전이 항상 맨 위. 에이전트가 스크롤해서 놓칠 여지를 주지 않는다.
    lines += ["## 1. 안전 판정", t.render(), ""]
    if t.blocks_exercise:
        lines += ["> **운동 처방 금지**: 위 항목이 해소될 때까지 어떤 운동 계획도 제시하지 않는다.", ""]

    lines += ["## 2. 준비도", r.summary()]
    if r.contributors:
        # 원시 z 의 부호로 판단하면 안 된다. 안정시 심박은 낮을수록 좋아서
        # z=+1.9(평소보다 13bpm 높음)가 "받쳐준 요인"으로 뒤집혀 나온다.
        # 세 번째 값(impact)이 이미 방향 보정된 기여분이다.
        worst = [f"{lbl}(z={z:+.1f})" for lbl, z, imp in r.contributors[:3] if imp < -0.02]
        best = [f"{lbl}(z={z:+.1f})" for lbl, z, imp in reversed(r.contributors[-3:]) if imp > 0.02]
        if worst:
            lines.append(f"- 끌어내린 요인: {', '.join(worst)}")
        if best:
            lines.append(f"- 받쳐준 요인: {', '.join(best)}")
    for f in r.flags:
        lines.append(f"- ⚑ {f}")
    lines.append("")

    lines.append("## 3. 지표 상세 (개인 베이스라인 28일 대비)")
    for path, _, _, _ in bl.TRACKED:
        m = metrics.get(path)
        if m and m.latest is not None:
            lines.append(f"- {m.describe()}")
    lines.append("")

    lines.append("## 4. 오늘의 맥락")
    a = today.activity
    if a.workouts:
        for w in a.workouts:
            lines.append(f"- 운동: {w.type} {w.duration_min:.0f}분 (RPE {w.rpe or '-'}, 부하 {w.load():.0f})")
    if a.steps:
        lines.append(f"- 걸음: {a.steps:,}")
    i = today.intake
    if i.caffeine_mg:
        lines.append(f"- 카페인 {i.caffeine_mg:.0f}mg (마지막 {i.last_caffeine_at or '시각 미기록'})")
    if i.alcohol_units:
        lines.append(f"- 음주 {i.alcohol_units:g} 단위")
    if today.adherence.plan_completed:
        lines.append(f"- 계획 이행: {', '.join(today.adherence.plan_completed)}")
    if today.adherence.plan_skipped:
        lines.append(f"- 계획 미이행: {', '.join(today.adherence.plan_skipped)}")
    if today.subjective.note:
        lines.append(f"- 메모: {today.subjective.note}")
    c = today.context
    if c.pm25 is not None:
        lines.append(f"- 초미세먼지 PM2.5 {c.pm25:.0f}㎍/㎥" + (" (야외 고강도 운동 비권장)" if c.pm25 >= 36 else ""))
    lines.append("")

    lines.append("## 5. 프로필 제약 (모든 조언이 지켜야 할 경계)")
    lines.append(f"- 기저질환: {', '.join(profile.conditions) or '없음'}")
    lines.append(f"- 복약: {', '.join(profile.medications) or '없음'}")
    lines.append(f"- 금기: {', '.join(profile.contraindications) or '없음'}")
    lines.append(f"- 목표: {', '.join(profile.goals) or '미설정'}")
    if profile.clinician_note:
        lines.append(f"- 주치의 지시: {profile.clinician_note}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def weekly_review(store: Store, end_date: str) -> str:
    """주간 회고용 팩트시트. reflection-agent 의 입력."""
    profile = store.load_profile()
    history = store.history(end=end_date, days=28)
    if not history:
        return "기록이 없습니다."
    week = [r for r in history if r.date > _shift(end_date, -7)]
    prior = [r for r in history if _shift(end_date, -14) < r.date <= _shift(end_date, -7)]

    lines = [f"# 주간 리뷰 — {_shift(end_date, -6)} ~ {end_date}", ""]
    lines.append(f"## 1. 기록 충실도")
    lines.append(f"- 기록된 날: {len(week)}/7일")
    for path, label, _, _ in bl.TRACKED[:8]:
        miss = bl.missingness(week, path, days=7)
        if miss > 0.3:
            lines.append(f"- ⚠ {label} 결측률 {miss * 100:.0f}%")
    lines.append("")

    lines.append("## 2. 주요 지표 (이번 주 평균 vs 지난 주)")
    for path, label, direction, _ in bl.TRACKED:
        cur = bl.series(week, path)
        pre = bl.series(prior, path)
        if not cur:
            continue
        cur_m = sum(cur) / len(cur)
        if pre:
            pre_m = sum(pre) / len(pre)
            delta = cur_m - pre_m
            # 변화가 사실상 0인데 화살표를 붙이면 없는 추세를 읽게 만든다.
            eps = max(0.05, abs(pre_m) * 0.005)
            mark = "" if direction == 0 or abs(delta) < eps else (
                " ▲" if delta * direction > 0 else " ▼")
            lines.append(f"- {label}: {cur_m:.1f} (지난주 {pre_m:.1f}, {delta:+.1f}){mark}")
        else:
            lines.append(f"- {label}: {cur_m:.1f} (비교 불가)")
    lines.append("")

    loads = [r.activity.training_load() for r in week]
    lines.append("## 3. 훈련부하")
    lines.append(f"- 주간 총 부하: {sum(loads):.0f} (일평균 {sum(loads) / max(1, len(week)):.0f})")
    a = bl.acwr(history)
    if a is not None:
        verdict = "안전 구간" if 0.8 <= a <= 1.3 else ("급증 — 감량 권장" if a > 1.3 else "저하 — 점진적 증량")
        lines.append(f"- ACWR: {a:.2f} ({verdict})")
    lines.append("")

    lines.append("## 4. 준비도 분포")
    bands: dict[str, int] = {}
    for i, rec in enumerate(week):
        prev = [x for x in history if x.date < rec.date]
        band = rd.compute(prev, rec, profile).band
        bands[band] = bands.get(band, 0) + 1
    lines.append(
        "- " + ", ".join(f"{k} {v}일" for k, v in sorted(bands.items()))
        if bands else "- 데이터 부족"
    )
    lines.append("")

    lines.append("## 5. 개입 이행률")
    done = sum(len(r.adherence.plan_completed) for r in week)
    skipped = sum(len(r.adherence.plan_skipped) for r in week)
    total = done + skipped
    lines.append(f"- 계획 {total}건 중 {done}건 이행 ({done / total * 100:.0f}%)" if total else "- 이행 기록 없음")
    missed = [m for r in week for m in r.adherence.meds_missed]
    if missed:
        lines.append(f"- 복약 누락 {len(missed)}건: {', '.join(sorted(set(missed)))}")
    lines.append("")

    lines.append("## 6. 안전 이벤트")
    events = []
    for rec in week:
        prev = [x for x in history if x.date < rec.date]
        res = tg.evaluate(prev, rec, profile)
        for f in res.findings:
            if f.severity >= tg.Severity.ROUTINE:
                events.append(f"{rec.date}: [{tg.LABEL[f.severity]}] {f.message}")
    lines += [f"- {e}" for e in events] if events else ["- 없음"]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def _shift(date: str, days: int) -> str:
    from datetime import date as _d, timedelta

    return (_d.fromisoformat(date) + timedelta(days=days)).isoformat()
