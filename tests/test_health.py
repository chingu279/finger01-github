"""코어 로직 회귀 테스트.

건강 시스템에서 가장 위험한 실패는 '조용한 오작동'이다.
레드플래그를 놓치는 경로와 점수 계산의 경계 조건을 고정해둔다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from health import baseline as bl
from health import readiness as rd
from health import triage as tg
from health.schema import DailyRecord, Workout
from health.store import Profile, Store


def make(day: int, **kw) -> DailyRecord:
    r = DailyRecord(date=f"2026-01-{day:02d}")
    for path, v in kw.items():
        r.set_path(path.replace("__", "."), v)
    return r


def steady(n: int = 20, **kw) -> list[DailyRecord]:
    return [make(i + 1, **kw) for i in range(n)]


# ── 베이스라인 ────────────────────────────────────────────────

def test_baseline_needs_minimum_samples():
    hist = steady(3, vitals__hrv_rmssd_ms=50)
    m = bl.compute(hist, make(9, vitals__hrv_rmssd_ms=20))["vitals.hrv_rmssd_ms"]
    assert not m.reliable
    assert m.z is None
    assert m.deviation == "unknown"


def test_zscore_direction_is_applied():
    hist = [make(i + 1, vitals__resting_hr=55 + (i % 3)) for i in range(20)]
    metrics = bl.compute(hist, make(25, vitals__resting_hr=70))
    m = metrics["vitals.resting_hr"]
    assert m.z is not None and m.z > 2
    # 안정시 심박은 낮을수록 좋으므로 높은 z 는 'worse'
    assert m.deviation == "worse"


def test_missing_values_are_skipped_not_zeroed():
    hist = steady(10, vitals__hrv_rmssd_ms=50)
    hist.append(make(11))  # 완전 결측일
    vals = bl.series(hist, "vitals.hrv_rmssd_ms")
    assert vals == [50.0] * 10


def test_acwr_detects_spike():
    hist = []
    for i in range(28):
        r = make(i + 1)
        load = 400 if i >= 21 else 100   # 마지막 주에만 급증
        r.activity.workouts.append(Workout(type="run", duration_min=load / 5, rpe=5))
        hist.append(r)
    a = bl.acwr(hist)
    assert a is not None and a > 1.5


def test_acwr_none_without_enough_history():
    assert bl.acwr(steady(3)) is None


# ── 준비도 ────────────────────────────────────────────────────

def test_readiness_average_day_is_amber():
    hist = [make(i + 1, vitals__hrv_rmssd_ms=50 + (i % 5) - 2,
                 vitals__resting_hr=56 + (i % 3) - 1,
                 sleep__total_min=440 + (i % 7) * 5,
                 subjective__energy=3) for i in range(21)]
    today = make(25, vitals__hrv_rmssd_ms=50, vitals__resting_hr=56,
                 sleep__total_min=445, subjective__energy=3)
    r = rd.compute(hist, today, Profile())
    assert r.band == "AMBER", r.summary()
    assert 60 <= r.score <= 78


def test_readiness_bad_day_drops_to_red():
    hist = [make(i + 1, vitals__hrv_rmssd_ms=50 + (i % 5) - 2,
                 vitals__resting_hr=56 + (i % 3) - 1,
                 sleep__total_min=440 + (i % 7) * 5,
                 subjective__energy=4, subjective__soreness=2) for i in range(21)]
    today = make(25, vitals__hrv_rmssd_ms=28, vitals__resting_hr=68,
                 sleep__total_min=300, subjective__energy=1, subjective__soreness=5)
    r = rd.compute(hist, today, Profile())
    assert r.band == "RED", r.summary()


def test_readiness_reports_zero_confidence_without_data():
    r = rd.compute([], make(1), Profile())
    assert r.confidence == 0.0
    assert r.band == "UNKNOWN"
    # 데이터가 없으면 '좋다'고 말하지 않는다
    assert "불가" in r.advice


def test_readiness_confidence_scales_with_coverage():
    hist = steady(20, subjective__energy=3)
    partial = rd.compute(hist, make(25, subjective__energy=3), Profile())
    assert 0 < partial.confidence < 0.3   # 주관 지표 하나뿐


def test_sleep_debt_flag():
    hist = [make(i + 1, sleep__total_min=330) for i in range(20)]
    today = make(25, sleep__total_min=330, vitals__hrv_rmssd_ms=40)
    r = rd.compute(hist, today, Profile(sleep_need_min=450))
    assert any("수면부채" in f for f in r.flags)


# ── 트리아지(안전) ────────────────────────────────────────────

def test_chest_pain_with_sweating_is_emergency():
    r = make(1)
    r.subjective.symptoms = ["가슴 통증", "식은땀"]
    res = tg.evaluate([], r)
    assert res.severity == tg.Severity.EMERGENCY
    assert res.blocks_exercise
    assert "119" in res.render()


def test_stroke_signs_are_emergency():
    r = make(1)
    r.subjective.note = "아침에 한쪽 팔에 힘이 빠지고 말이 어눌했다"
    assert tg.evaluate([], r).severity == tg.Severity.EMERGENCY


def test_suicidal_ideation_routes_to_crisis_line():
    r = make(1)
    r.subjective.note = "요즘 죽고 싶다는 생각이 든다"
    res = tg.evaluate([], r)
    assert res.severity == tg.Severity.EMERGENCY
    assert "109" in res.render()


def test_low_spo2_is_emergency_and_borderline_is_urgent():
    a, b = make(1), make(2)
    a.vitals.spo2_pct = 88
    b.vitals.spo2_pct = 92
    assert tg.evaluate([], a).severity == tg.Severity.EMERGENCY
    assert tg.evaluate([], b).severity == tg.Severity.URGENT


def test_hypertensive_crisis_vs_ordinary_hypertension():
    a, b = make(1), make(2)
    a.vitals.bp_systolic, a.vitals.bp_diastolic = 185, 110
    b.vitals.bp_systolic, b.vitals.bp_diastolic = 145, 92
    assert tg.evaluate([], a).severity == tg.Severity.URGENT
    assert tg.evaluate([], b).severity == tg.Severity.ROUTINE


def test_severe_hypoglycemia_is_emergency():
    r = make(1)
    r.vitals.blood_glucose_mgdl = 48
    assert tg.evaluate([], r).severity == tg.Severity.EMERGENCY


def test_sustained_rhr_elevation_is_flagged():
    hist = [make(i + 1, vitals__resting_hr=55 + (i % 3)) for i in range(20)]
    hist += [make(21, vitals__resting_hr=66), make(22, vitals__resting_hr=67)]
    today = make(23, vitals__resting_hr=68)
    codes = [f.code for f in tg.evaluate(hist, today).findings]
    assert "rhr_elevated" in codes


def test_healthy_day_produces_no_flags():
    hist = [make(i + 1, vitals__resting_hr=56, vitals__hrv_rmssd_ms=50 + (i % 5),
                 vitals__spo2_pct=97, sleep__total_min=450) for i in range(20)]
    today = make(25, vitals__resting_hr=56, vitals__hrv_rmssd_ms=51,
                 vitals__spo2_pct=97, sleep__total_min=455)
    res = tg.evaluate(hist, today)
    assert res.severity == tg.Severity.MONITOR
    assert not res.blocks_exercise


def test_broken_rule_does_not_silence_other_rules(monkeypatch):
    """한 규칙이 터져도 나머지 안전망은 계속 돌아야 한다."""
    def boom(h, t, p):
        raise RuntimeError("의도적 실패")

    monkeypatch.setattr(tg, "RULES", [boom, tg._rule_vitals])
    r = make(1)
    r.vitals.spo2_pct = 85
    res = tg.evaluate([], r)
    assert res.severity == tg.Severity.EMERGENCY          # 살아남은 규칙이 잡아냄
    assert any(f.code == "rule_error" for f in res.findings)  # 실패도 감춰지지 않음


def test_profile_conditions_gate_advice():
    p = Profile(conditions=["천식"])
    r = make(1)
    r.subjective.symptoms = ["호흡곤란"]
    codes = [f.code for f in tg.evaluate([], r, p).findings]
    assert "resp_exacerbation" in codes


# ── 저장소 ────────────────────────────────────────────────────

def test_upsert_merges_partial_records(tmp_path):
    st = Store(tmp_path)
    st.upsert(make(1, sleep__total_min=420))
    st.upsert(make(1, vitals__resting_hr=57))
    rec = st.load("2026-01-01")
    assert rec is not None
    assert rec.sleep.total_min == 420      # 이전 값이 지워지지 않음
    assert rec.vitals.resting_hr == 57


def test_upsert_overwrites_only_provided_fields(tmp_path):
    st = Store(tmp_path)
    st.upsert(make(1, sleep__total_min=420, subjective__energy=2))
    st.upsert(make(1, subjective__energy=4))
    rec = st.load("2026-01-01")
    assert rec.subjective.energy == 4
    assert rec.sleep.total_min == 420


def test_roundtrip_preserves_workouts(tmp_path):
    st = Store(tmp_path)
    r = make(1)
    r.activity.workouts.append(Workout(type="run", duration_min=40, rpe=7))
    st.save(r)
    back = st.load("2026-01-01")
    assert back.activity.workouts[0].type == "run"
    assert back.activity.training_load() == pytest.approx(280)


def test_event_log_is_append_only(tmp_path):
    st = Store(tmp_path)
    st.log_event("plan", "exercise-coach", {"detail": "zone2 30min"})
    st.log_event("plan", "sleep-coach", {"detail": "취침 23:00"})
    evs = list(st.events("plan"))
    assert len(evs) == 2
    assert {e["agent"] for e in evs} == {"exercise-coach", "sleep-coach"}


def test_unknown_field_is_rejected():
    r = make(1)
    with pytest.raises(KeyError):
        r.set_path("sleep.not_a_field", 1)
