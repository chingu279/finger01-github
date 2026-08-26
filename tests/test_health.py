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


# ── 결측 경보 (경보 피로 방지) ─────────────────────────────────

def test_no_missing_flags_in_first_week():
    """시작 첫 주에는 모든 지표가 정의상 결측이다. 여기서 경보를 내면
    사용자는 첫날부터 경보를 무시하는 법을 배운다."""
    hist = [make(i + 1, subjective__energy=3) for i in range(4)]
    r = rd.compute(hist, make(6, subjective__energy=3), Profile())
    assert not any("결측" in f for f in r.flags)


def test_untracked_never_collected_metric_is_not_flagged():
    """웨어러블이 없어 HRV를 한 번도 수집한 적 없다면 '결측'이 아니다."""
    hist = [make(i + 1, subjective__energy=3, sleep__total_min=440) for i in range(14)]
    p = Profile(tracking=["subjective.energy", "sleep.total_min"])
    r = rd.compute(hist, make(20, subjective__energy=3, sleep__total_min=440), p)
    assert not any("HRV" in f for f in r.flags)


def test_broken_collection_path_is_flagged():
    """잘 들어오던 지표가 끊기면 그건 반드시 알려야 한다."""
    hist = [make(i + 1, vitals__hrv_rmssd_ms=50, subjective__energy=3) for i in range(10)]
    hist += [make(i + 11, subjective__energy=3) for i in range(12)]   # HRV 12일간 두절
    r = rd.compute(hist, make(25, subjective__energy=3), Profile())
    assert any("HRV" in f and "결측" in f for f in r.flags)


def test_tracked_metric_is_flagged_even_if_never_arrived():
    """매일 기록하기로 해놓고 안 하는 항목은 경보 대상이다."""
    hist = [make(i + 1, subjective__energy=3) for i in range(14)]
    p = Profile(tracking=["subjective.energy", "vitals.resting_hr"])
    r = rd.compute(hist, make(20, subjective__energy=3), p)
    assert any("안정시 심박" in f for f in r.flags)


# ── 체크인 입력 처리 ───────────────────────────────────────────

def test_sleep_accepts_hours_or_minutes():
    from health.checkin import _duration

    assert _duration("7.5") == 450.0      # 사람은 시간으로 생각한다
    assert _duration("450") == 450.0      # 웨어러블은 분으로 준다
    assert _duration("7h") == 420.0


def test_profile_tracking_defaults_to_five_items():
    from health.checkin import DEFAULT_TRACKING, PROMPTS

    assert len(DEFAULT_TRACKING) == 5     # 10개를 고르면 2주 안에 그만둔다
    assert all(path in PROMPTS for path in DEFAULT_TRACKING)


def test_every_prompt_writes_to_a_real_record_field():
    """프롬프트 경로가 스키마와 어긋나면 체크인이 런타임에 죽는다.
    복합 프롬프트(vitals.bp)는 키가 아니라 targets 가 실제 경로다."""
    from health.checkin import PROMPTS

    rec = make(1)
    for prompt in PROMPTS.values():
        for target in prompt.targets:
            rec.set_path(target, 1)       # KeyError 가 나면 실패


def test_every_numeric_prompt_has_a_physiological_range():
    """숫자 항목에 범위가 없으면 오타 하나가 베이스라인을 밀어버린다."""
    from health.checkin import PROMPTS, RANGES

    for key, p in PROMPTS.items():
        if p.kind in ("number", "duration", "nrs"):
            for target in p.targets:
                assert target in RANGES, f"{key} → {target} 에 생리학적 범위가 없습니다"


# ── Phase 0 게이트 ─────────────────────────────────────────────

def test_streak_counts_consecutive_days():
    from health.cli import _streak

    dates = ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22",
             "2026-08-23", "2026-08-24", "2026-08-25"]
    n, done = _streak(dates, "2026-08-25")
    assert (n, done) == (7, True)


def test_streak_survives_an_unfinished_today():
    """아침 9시에 아직 체크인을 안 했다고 연속 기록이 끊긴 것으로 세면
    사람은 좌절하고 그만둔다."""
    from health.cli import _streak

    dates = ["2026-08-23", "2026-08-24"]
    n, done = _streak(dates, "2026-08-25")
    assert n == 2 and done is False


def test_streak_breaks_on_a_real_gap():
    from health.cli import _streak

    dates = ["2026-08-19", "2026-08-20", "2026-08-24", "2026-08-25"]
    n, _ = _streak(dates, "2026-08-25")
    assert n == 2


def test_streak_is_zero_with_no_records():
    from health.cli import _streak

    assert _streak([], "2026-08-25") == (0, False)


# ── 복합 프롬프트 · 목표 프리셋 ────────────────────────────────

def test_blood_pressure_is_one_question_two_fields():
    """사람은 "수축기"와 "이완기"를 따로 생각하지 않는다.
    질문을 둘로 쪼개면 체크인이 길어지고, 길어지면 끊긴다."""
    from health.checkin import PROMPTS

    p = PROMPTS["vitals.bp"]
    rec = make(1)
    p.apply(rec, p.parse("128/82"))
    assert rec.vitals.bp_systolic == 128
    assert rec.vitals.bp_diastolic == 82


def test_blood_pressure_accepts_common_separators():
    from health.checkin import _bp

    assert _bp("120/80") == (120, 80)
    assert _bp("120-80") == (120, 80)
    assert _bp("120 80") == (120, 80)
    with pytest.raises(ValueError):
        _bp("120")


def test_wearable_owners_are_not_asked_for_auto_collected_metrics():
    """웨어러블이 채우는 항목을 매일 손으로 묻는 것이
    체크인이 길어지는 가장 흔한 이유다."""
    from health.checkin import WEARABLE_COVERED, suggest_tracking

    picked = suggest_tracking(["sleep", "fitness"], has_wearable=True)
    assert not (set(picked) & WEARABLE_COVERED)

    without = suggest_tracking(["sleep"], has_wearable=False)
    assert "sleep.total_min" in without


def test_suggestion_respects_the_five_item_cap():
    from health.checkin import MAX_TRACKING, GOAL_PRESETS, suggest_tracking

    picked = suggest_tracking(list(GOAL_PRESETS), has_wearable=False)
    assert len(picked) <= MAX_TRACKING


def test_suggestion_always_keeps_the_free_text_line():
    """구조화된 항목이 놓치는 것의 대부분이 자유 서술에서 나오고,
    그 텍스트는 레드플래그 스캔 대상이다."""
    from health.checkin import GOAL_PRESETS, suggest_tracking

    for goal in GOAL_PRESETS:
        assert "subjective.note" in suggest_tracking([goal], has_wearable=True)


def test_suggestion_falls_back_when_no_goals_chosen():
    from health.checkin import DEFAULT_TRACKING, suggest_tracking

    assert suggest_tracking([], has_wearable=False) == DEFAULT_TRACKING


def test_goal_preset_paths_are_all_real_prompts():
    """프리셋에 오타가 있으면 체크인이 조용히 그 항목을 건너뛴다."""
    from health.checkin import GOAL_PRESETS, PROMPTS

    for goal, (label, paths) in GOAL_PRESETS.items():
        assert label
        for path in paths:
            assert path in PROMPTS, f"{goal} 프리셋의 '{path}' 가 PROMPTS 에 없습니다"


def test_checkin_does_not_reask_a_filled_composite_field():
    from health.checkin import PROMPTS, run

    rec = make(1)
    rec.vitals.bp_systolic, rec.vitals.bp_diastolic = 120, 80
    rec, seconds, filled = run(rec, ["vitals.bp"])   # 입력을 안 받아도 통과해야 한다
    assert filled == 0
    assert rec.vitals.bp_systolic == 120


# ── 실행 진입점 (이식성) ────────────────────────────────────────

REPO = Path(__file__).resolve().parents[1]


def test_shim_exists_and_is_executable():
    shim = REPO / "health"
    assert shim.exists(), "저장소 루트에 ./health 진입점이 있어야 합니다"
    assert shim.stat().st_mode & 0o111, "./health 에 실행 권한이 없습니다"


def test_shim_is_valid_posix_sh():
    """zsh/bash/dash 어디서 돌지 모른다. bashism 이 들어가면 사용자 머신에서 죽는다."""
    import subprocess

    r = subprocess.run(["sh", "-n", str(REPO / "health")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_docs_never_tell_the_user_to_run_bare_python():
    """macOS 에는 `python` 이 없다. 문서의 명령을 그대로 따라 치면 실패한다
    — 실제로 이 저장소를 처음 클론한 사용자가 여기서 막혔다."""
    import re

    offenders = []
    for f in [REPO / "README.md", REPO / "CLAUDE.md",
              *(REPO / "docs").glob("*.md"), *(REPO / ".claude").rglob("*.md")]:
        in_code = False
        for i, line in enumerate(f.read_text("utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                continue
            if not in_code:
                continue          # 산문에서 단어로 언급하는 것은 문제가 아니다
            # `python3` 과 `pythonic` 은 통과, 맨 `python` 호출만 잡는다
            if re.search(r"(?<![\w.3-])python(?![\w3])", line):
                offenders.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "복사해 붙이면 macOS 에서 실패하는 명령:\n" + "\n".join(offenders)


def test_cmd_name_matches_how_it_was_invoked(monkeypatch):
    """안내 문구가 사용자가 실제로 친 명령과 어긋나면 도움이 아니라 함정이다."""
    from health.cli import cmd_name

    monkeypatch.setenv("HEALTH_CMD", "./health")
    assert cmd_name() == "./health"

    monkeypatch.delenv("HEALTH_CMD", raising=False)
    assert cmd_name() == "python3 -m health"     # 맨 python 을 안내하지 않는다


def test_core_stays_importable_on_python_39():
    """macOS 기본 python3 는 3.9 다. 3.10 전용 문법이 들어가면 임포트 단계에서 죽는다."""
    import ast

    for f in sorted((REPO / "src").rglob("*.py")):
        ast.parse(f.read_text("utf-8"), filename=str(f), feature_version=(3, 9))


def _bash_block_lines():
    """문서의 bash/sh 코드블록 안 명령 줄만 뽑는다."""
    import re

    files = [REPO / "README.md", REPO / "CLAUDE.md",
             *(REPO / "docs").glob("*.md"), *(REPO / ".claude").rglob("*.md")]
    for f in files:
        lang = None
        for i, line in enumerate(f.read_text("utf-8").splitlines(), 1):
            fence = re.match(r"\s*```(\w*)", line)
            if fence:
                lang = None if lang is not None else fence.group(1)
                continue
            if lang in ("bash", "sh", "shell") and line.strip():
                yield f, i, line


def test_shell_blocks_have_no_comments():
    """zsh 는 대화형 라인에서 `#` 를 주석으로 치지 않는다(interactive_comments 기본 off).
    주석이 붙은 명령을 붙여넣으면 `#` 와 그 뒤 단어들이 인자로 넘어가 실패한다
    — 이 저장소를 처음 클론한 사용자가 정확히 여기서 막혔다."""
    import re

    offenders = [
        f"{f.relative_to(REPO)}:{i}: {line.strip()}"
        for f, i, line in _bash_block_lines()
        if re.search(r"(^\s*#)|(\S\s+#\s)", line)
    ]
    assert not offenders, (
        "붙여넣으면 zsh 에서 깨지는 주석:\n" + "\n".join(offenders))


# ── 웨어러블 적재 (Apple Health) ────────────────────────────────

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "apple_export_sample.xml"


@pytest.fixture(scope="module")
def apple():
    from health.ingest import parse_apple

    return parse_apple(FIXTURE)


def test_spo2_fraction_is_converted_to_percent(apple):
    """Apple 은 unit="%" 에 0.97 같은 분율을 넣는다. 그대로 저장하면
    SpO2 0.97% 가 되어 트리아지가 매일 응급을 띄운다."""
    recs, _ = apple
    assert recs["2026-08-25"].vitals.spo2_pct == 97.0


def test_hrv_goes_to_sdnn_not_rmssd(apple):
    """Apple 이 주는 HRV 는 SDNN 이다. rMSSD 칸에 넣으면 기기를 바꿨을 때
    베이스라인이 조용히 망가진다."""
    recs, _ = apple
    v = recs["2026-08-25"].vitals
    assert v.hrv_sdnn_ms == 65.0          # 62, 68 의 평균
    assert v.hrv_rmssd_ms is None


def test_steps_are_deduplicated_across_sources(apple):
    """아이폰과 애플워치가 같은 걸음을 각각 기록한다. 합치면 두 배가 된다."""
    recs, _ = apple
    assert recs["2026-08-25"].activity.steps == 7700     # 15,000 이 아니다


def test_pounds_are_converted_to_kilograms(apple):
    recs, _ = apple
    assert recs["2026-08-25"].vitals.weight_kg == 70.0   # 154.3 lb


def test_sleep_across_midnight_belongs_to_the_wake_day(apple):
    """8/24 23:20 취침 → 8/25 06:45 기상. 사람은 "오늘 몇 시간 자고
    일어났나"로 컨디션을 판단한다."""
    recs, _ = apple
    assert "2026-08-24" not in recs
    sleep = recs["2026-08-25"].sleep
    assert sleep.bedtime == "23:20" and sleep.waketime == "06:45"


def test_nap_is_not_counted_as_night_sleep(apple):
    """25분 낮잠을 밤잠에 더하면 총 수면이 부풀고 수면부채가 과소평가된다."""
    recs, _ = apple
    assert recs["2026-08-25"].sleep.total_min == 415.0   # 낮잠 25분 제외


def test_sleep_stages_and_efficiency(apple):
    recs, _ = apple
    s = recs["2026-08-25"].sleep
    assert s.deep_min == 70.0 and s.rem_min == 72.0
    assert s.awakenings == 1
    assert s.efficiency_pct == pytest.approx(92.2, abs=0.1)
    assert s.latency_min == 22.0


def test_out_of_range_values_are_rejected_and_reported(apple):
    """웨어러블 오작동 값 하나가 28일 베이스라인을 통째로 민다.
    조용히 버리지도 않는다 — 무엇이 왜 빠졌는지 알아야 고칠 수 있다."""
    recs, rep = apple
    assert any("410" in r for r in rep.rejected)
    assert "2026-08-23" not in recs


def test_a_day_whose_values_were_all_rejected_creates_no_record(apple):
    """빈 파일이 남으면 status 의 연속 기록일이 부풀어 게이트가
    거짓으로 통과한다."""
    recs, _ = apple
    assert all(not r.is_empty() for r in recs.values())


def test_gaps_are_reported(apple):
    _, rep = apple
    assert any("2026-08-23" in g for g in rep.gaps)


def test_workout_is_imported(apple):
    recs, _ = apple
    w = recs["2026-08-25"].activity.workouts[0]
    assert w.type == "running" and w.duration_min == 46.5 and w.distance_km == 8.2


def test_non_numeric_records_are_skipped(apple):
    """심전도 분류 같은 문자열 값 레코드에서 죽으면 안 된다."""
    _, rep = apple
    assert rep.records_seen > 0        # 파싱이 끝까지 갔다


def test_import_does_not_erase_manual_entries(tmp_path):
    """웨어러블이 아침에 수면을 넣고 저녁에 체크인이 기분을 넣는다.
    덮어쓰기면 한쪽이 지워진다."""
    from health.ingest import parse_apple

    st = Store(tmp_path)
    manual = make(1)
    manual.date = "2026-08-25"
    manual.subjective.energy = 4
    manual.subjective.note = "종아리 뻐근"
    st.upsert(manual)

    recs, _ = parse_apple(FIXTURE)
    st.upsert(recs["2026-08-25"])

    back = st.load("2026-08-25")
    assert back.subjective.energy == 4          # 수기 기록이 남았다
    assert back.subjective.note == "종아리 뻐근"
    assert back.sleep.total_min == 415.0        # 웨어러블 값도 들어왔다


def test_empty_record_never_creates_a_file(tmp_path):
    st = Store(tmp_path)
    assert st.upsert(make(1)) is None
    assert st.all_dates() == []


def test_readiness_uses_sdnn_when_rmssd_is_absent():
    """애플워치 사용자는 SDNN 만 갖는다. 그것 때문에 준비도의 가장 큰
    기여 지표가 통째로 빠지면 안 된다."""
    base = dict(vitals__resting_hr=56, sleep__total_min=445, subjective__energy=3)
    hist = [make(i + 1, **base, vitals__hrv_sdnn_ms=60 + (i % 5) - 2) for i in range(21)]
    r = rd.compute(hist, make(25, **base, vitals__hrv_sdnn_ms=60), Profile())
    assert any("SDNN" in label for label, _, _ in r.contributors)
    assert r.confidence >= 0.6


def test_only_one_hrv_metric_is_counted():
    """둘 다 있으면 같은 신호를 두 번 세게 된다."""
    base = dict(vitals__resting_hr=56, sleep__total_min=445, subjective__energy=3)
    hist = [make(i + 1, **base, vitals__hrv_rmssd_ms=48 + (i % 4),
                 vitals__hrv_sdnn_ms=60 + (i % 4)) for i in range(21)]
    r = rd.compute(hist, make(25, **base, vitals__hrv_rmssd_ms=48,
                              vitals__hrv_sdnn_ms=60), Profile())
    hrv_used = [label for label, _, _ in r.contributors if "HRV" in label]
    assert hrv_used == ["HRV(rMSSD)"]           # rMSSD 우선, 하나만


def test_confidence_can_reach_one_with_a_single_hrv_source():
    """HRV 항목이 둘이라고 분모가 커지면 신뢰도가 1에 닿지 못한다."""
    full = dict(vitals__resting_hr=56, vitals__walking_hr_avg=78,
                sleep__total_min=445, sleep__efficiency_pct=90,
                subjective__energy=3, subjective__soreness=2, subjective__stress=2,
                subjective__mood=3)
    hist = [make(i + 1, **full, vitals__hrv_sdnn_ms=60 + (i % 5) - 2) for i in range(21)]
    r = rd.compute(hist, make(25, **full, vitals__hrv_sdnn_ms=60), Profile())
    assert r.confidence == 1.0


# ── 겹치는 수면 구간 (실제 11년치 내보내기에서 발견) ─────────────

DUAL = Path(__file__).resolve().parent / "fixtures" / "apple_export_dual_source.xml"


def test_overlapping_intervals_are_unioned_not_summed():
    from datetime import datetime as dt

    from health.ingest import Interval, merged_minutes

    def iv(h1, m1, h2, m2):
        return Interval(dt(2026, 8, 26, h1, m1), dt(2026, 8, 26, h2, m2), "x")

    assert merged_minutes([iv(1, 0, 3, 0), iv(1, 0, 3, 0)]) == 120      # 완전 중복
    assert merged_minutes([iv(1, 0, 3, 0), iv(2, 0, 4, 0)]) == 180      # 부분 겹침
    assert merged_minutes([iv(1, 0, 2, 0), iv(3, 0, 4, 0)]) == 120      # 안 겹침
    assert merged_minutes([]) == 0.0


def test_dual_source_night_is_not_double_counted():
    """아이폰과 애플워치가 같은 밤을 각각 기록한다. 단순 합산하면
    7.5시간 잔 밤이 15시간이 된다 — 실제 내보내기에서 17.6시간짜리
    '수면'이 나온 원인이다."""
    from health.ingest import parse_apple

    recs, _ = parse_apple(DUAL)
    sleep = recs["2026-08-26"].sleep
    assert sleep.total_min == 450.0        # 23:20~06:50 = 7시간 30분. 900 이 아니다


def test_dual_source_stages_and_efficiency_stay_physiological():
    from health.ingest import parse_apple

    recs, _ = parse_apple(DUAL)
    s = recs["2026-08-26"].sleep
    assert s.deep_min == 65.0
    assert s.rem_min == 75.0
    assert s.efficiency_pct <= 100.0       # 침대 시간도 합집합이어야 100%를 안 넘는다
    assert s.awakenings == 1               # 두 기기가 기록한 같은 각성은 한 번


def test_sleep_never_exceeds_the_physiological_ceiling():
    """겹침을 합치지 않으면 여기서 걸린다 — 그런데 16시간을 안 넘긴
    밤들은 범위 검증도 통과해 조용히 부풀려진 채 저장된다."""
    from health.checkin import RANGES
    from health.ingest import parse_apple

    lo, hi = RANGES["sleep.total_min"]
    for fixture in (FIXTURE, DUAL):
        recs, _ = parse_apple(fixture)
        for rec in recs.values():
            if rec.sleep.total_min is not None:
                assert lo <= rec.sleep.total_min <= hi


# ── 게이트가 웨어러블 적재로 거짓 통과하지 않는다 ────────────────

def test_streak_counts_checkins_not_imported_days(tmp_path):
    """웨어러블 적재는 하루 만에 수천 일을 채운다. 그걸 연속 기록으로
    세면 Phase 0 게이트가 거짓 통과한다."""
    st = Store(tmp_path)
    for day in range(1, 11):                     # 적재된 10일 (체크인 아님)
        r = make(day, vitals__resting_hr=57)
        r.sources = ["apple-health"]
        st.upsert(r)
    assert len(st.all_dates()) == 10
    assert st.checkin_dates() == []              # 게이트에는 하나도 안 셈된다

    r = make(11, subjective__energy=3)
    r.sources = ["checkin"]
    st.upsert(r)
    assert st.checkin_dates() == ["2026-01-11"]


def test_imported_days_still_feed_the_baseline(tmp_path):
    """게이트에서 빼는 것이지 버리는 것이 아니다 — 적재한 과거 데이터는
    베이스라인과 준비도에 그대로 쓰인다."""
    st = Store(tmp_path)
    for day in range(1, 22):
        r = make(day, vitals__resting_hr=56 + (day % 3), vitals__hrv_sdnn_ms=60 + (day % 5),
                 sleep__total_min=440 + (day % 7) * 5)
        r.sources = ["apple-health"]
        st.upsert(r)
    hist = st.history(end="2026-01-21", days=28)
    today = make(22, vitals__resting_hr=57, vitals__hrv_sdnn_ms=62, sleep__total_min=445)
    r = rd.compute(hist, today, Profile())
    assert r.band != "UNKNOWN"
    assert r.confidence >= 0.5


# ── 부정맥 · 복약 맥락 (심방세동 절제술 후 사용자 사례에서 도출) ──

# 심방세동 절제술 후에 흔한 처방 구성. 특정인의 기록이 아니라
# '이 계열들이 함께 있을 때' 규칙이 어떻게 도는지를 고정하기 위한 픽스처다.
AFIB_PROFILE = Profile(
    sex="male",
    conditions=["심방세동 (카테터 절제술)"],
    medications=["드로네다론", "에독사반", "로수바스타틴", "에스오메프라졸"],
)


def test_medication_classes_are_extracted_from_free_text():
    """'릭시아나 60mg' 이라는 문자열만으로는 규칙을 쓸 수 없다.
    계열을 알아야 머리 외상의 긴급도가 달라진다."""
    found = tg.med_classes(AFIB_PROFILE)
    assert {"anticoagulant", "antiarrhythmic", "statin"} <= found
    assert "bleeding_risk" in found and "rate_control" in found


def test_no_medication_no_class():
    assert tg.med_classes(Profile()) == set()


def test_head_injury_on_anticoagulant_is_emergency_even_without_symptoms():
    """두개내 출혈은 수 시간~수일 뒤에 나타날 수 있다.
    '지금 괜찮다'가 안전을 뜻하지 않는 대표적인 경우다."""
    r = make(1)
    r.subjective.note = "어제 넘어져서 머리를 좀 부딪혔는데 괜찮음"
    res = tg.evaluate([], r, AFIB_PROFILE)
    assert res.severity == tg.Severity.EMERGENCY
    assert res.blocks_exercise
    assert any(f.code == "anticoag_head_injury" for f in res.findings)


def test_head_injury_without_anticoagulant_does_not_fire_that_rule():
    r = make(1)
    r.subjective.note = "머리를 부딪혔다"
    codes = [f.code for f in tg.evaluate([], r, Profile()).findings]
    assert "anticoag_head_injury" not in codes


def test_nsaid_with_anticoagulant_is_flagged():
    r = make(1)
    r.subjective.note = "두통이 있어서 나프록센 먹을까 고민"
    codes = [f.code for f in tg.evaluate([], r, AFIB_PROFILE).findings]
    assert "anticoag_nsaid" in codes


def test_bradycardia_on_rate_control_escalates_when_symptomatic():
    plain, symptomatic = make(1), make(2)
    plain.vitals.resting_hr = 42
    symptomatic.vitals.resting_hr = 42
    symptomatic.subjective.note = "어지럽고 실신할 뻔했다"

    a = tg.evaluate([], plain, AFIB_PROFILE)
    b = tg.evaluate([], symptomatic, AFIB_PROFILE)
    assert any(f.code == "bradycardia_on_rate_control" for f in a.findings)
    assert b.severity >= tg.Severity.URGENT


def test_statin_myalgia_with_dark_urine_is_urgent():
    r = make(1)
    r.subjective.note = "전신 근육통이 심하고 소변이 진한 갈색"
    res = tg.evaluate([], r, AFIB_PROFILE)
    assert res.severity == tg.Severity.URGENT
    assert res.blocks_exercise


def test_ecg_afib_is_reported():
    r = make(1)
    r.vitals.ecg_afib = True
    codes = [f.code for f in tg.evaluate([], r, AFIB_PROFILE).findings]
    assert "ecg_afib" in codes


def test_afib_burden_escalates_with_red_flag_symptoms():
    quiet, red = make(1), make(2)
    quiet.vitals.afib_burden_pct = 8.0
    red.vitals.afib_burden_pct = 8.0
    red.subjective.symptoms = ["호흡곤란"]

    assert tg.evaluate([], quiet, AFIB_PROFILE).severity <= tg.Severity.ROUTINE
    assert tg.evaluate([], red, AFIB_PROFILE).severity >= tg.Severity.URGENT


def test_medication_rules_never_lower_severity():
    """복약 규칙은 상향만 한다. 어떤 약을 먹는다고 위험이 낮아지지 않는다."""
    r = make(1)
    r.subjective.symptoms = ["가슴 통증", "식은땀"]
    plain = tg.evaluate([], r, Profile())
    with_meds = tg.evaluate([], r, AFIB_PROFILE)
    assert with_meds.severity >= plain.severity == tg.Severity.EMERGENCY


# ── AF 중의 HRV 는 자율신경 지표가 아니다 ────────────────────────

def test_hrv_is_unusable_on_afib_days():
    from health.readiness import hrv_usable

    clean = make(1, vitals__hrv_rmssd_ms=50)
    assert hrv_usable(clean)

    for field, value in [("afib_burden_pct", 3.0),
                         ("irregular_rhythm_events", 1),
                         ("ecg_afib", True)]:
        r = make(2, vitals__hrv_rmssd_ms=210)
        setattr(r.vitals, field, value)
        assert not hrv_usable(r), field


def test_afib_hrv_spike_does_not_inflate_readiness():
    """AF 중에는 RR 간격이 불규칙해져 HRV 가 폭증한다. 회복이 좋아진 게
    아니라 부정맥을 재고 있는 것이다. 이걸 그대로 두면 가장 쉬어야 할 날에
    '고강도 훈련 가능'이 나온다."""
    base = dict(vitals__resting_hr=57, sleep__total_min=430, subjective__energy=3)
    hist = [make(i + 1, **base, vitals__hrv_rmssd_ms=45 + (i % 5)) for i in range(21)]

    normal = rd.compute(hist, make(25, **base, vitals__hrv_rmssd_ms=48), Profile())
    spike = rd.compute(hist, make(25, **base, vitals__hrv_rmssd_ms=210), Profile())
    afib = make(25, **base, vitals__hrv_rmssd_ms=210)
    afib.vitals.afib_burden_pct = 12.0
    masked = rd.compute(hist, afib, Profile())

    # HRV 상단 클립: 급등이 점수를 밀어 올리는 폭이 제한된다
    assert spike.score - normal.score < 12
    assert spike.band != "GREEN"
    # AF 표지가 있으면 아예 제외된다
    assert masked.score < spike.score
    assert any("심방세동" in f for f in masked.flags)


def test_afib_night_does_not_poison_the_hrv_baseline():
    """AF 하룻밤의 rMSSD 200ms 가 28일 평균을 밀어버리면, 그 뒤로 정상인
    날들이 전부 'HRV 가 낮다'로 읽힌다. 오염은 하루로 끝나지 않는다."""
    base = dict(vitals__resting_hr=57, sleep__total_min=430)
    hist = [make(i + 1, **base, vitals__hrv_rmssd_ms=50) for i in range(20)]
    bad = make(21, **base, vitals__hrv_rmssd_ms=220)
    bad.vitals.afib_burden_pct = 30.0
    hist.append(bad)

    metrics = bl.compute(rd._mask_unusable_hrv(hist), make(25, **base, vitals__hrv_rmssd_ms=50))
    m = metrics["vitals.hrv_rmssd_ms"]
    assert m.mean == 50.0                        # 220 이 평균에 섞이지 않았다
    assert abs(m.z) < 0.5                        # 정상인 날이 '낮음'으로 읽히지 않는다


def test_brief_does_not_call_an_elevated_resting_hr_a_supporting_factor():
    """안정시 심박은 낮을수록 좋다. 원시 z 의 부호로 판단하면 평소보다
    13bpm 높은 날이 '받쳐준 요인'으로 뒤집혀 나온다 — 심장 시술 후
    회복 중인 사람에게는 정반대의 신호를 주는 셈이다."""
    from health import report as rp

    st = Store(Path(__import__("tempfile").mkdtemp()))
    for i in range(20):
        st.upsert(make(i + 1, vitals__resting_hr=60 + (i % 5) - 2))
    st.upsert(make(25, vitals__resting_hr=76))

    text = rp.daily_brief(st, "2026-01-25")
    supporting = [ln for ln in text.splitlines() if "받쳐준" in ln]
    assert not any("안정시 심박" in ln for ln in supporting)
    assert any("안정시 심박" in ln for ln in text.splitlines() if "끌어내린" in ln)


def test_low_confidence_scores_are_marked_provisional():
    """지표 한두 개로 낸 점수를 단정적으로 말하면 경보 피로를 부른다."""
    hist = [make(i + 1, vitals__resting_hr=60) for i in range(20)]
    r = rd.compute(hist, make(25, vitals__resting_hr=76), Profile())
    assert r.confidence < 0.4
    assert "잠정" in r.advice


# ── 안전망이 조용히 사라지지 않게 ───────────────────────────────

def test_abbreviated_medication_names_are_still_classified():
    """사람은 '릭시아나정 60mg' 이 아니라 '릭시30mg' 이라고 적는다."""
    p = Profile(medications=["릭시30mg 매아침", "멀택정 400mg", "크레스토 매일아침"])
    assert "anticoagulant" in tg.med_classes(p)
    assert "bleeding_risk" in tg.med_classes(p)
    assert tg.unclassified_medications(p) == []


def test_unclassifiable_medication_is_surfaced_not_ignored():
    """계열을 모르면 복약 기반 규칙이 하나도 걸리지 않는다.
    규칙이 빠진 것보다 빠졌다는 걸 말하지 않는 쪽이 훨씬 위험하다."""
    p = Profile(medications=["아침약 1알", "파란약"])
    assert tg.unclassified_medications(p) == ["아침약 1알", "파란약"]

    codes = [f.code for f in tg.evaluate([], make(1), p).findings]
    assert "unclassified_medication" in codes


def test_partially_classified_list_still_warns_about_the_rest():
    p = Profile(medications=["크레스토 10mg", "이름 모르는 약"])
    assert "statin" in tg.med_classes(p)
    assert tg.unclassified_medications(p) == ["이름 모르는 약"]


def test_brief_shows_medication_classes_and_gaps(tmp_path):
    from health import report as rp

    st = Store(tmp_path)
    st.save_profile(Profile(medications=["릭시30mg", "정체불명 약"]))
    st.upsert(make(1, subjective__energy=3))
    text = rp.daily_brief(st, "2026-01-01")
    assert "인식된 계열" in text and "anticoagulant" in text
    assert "계열 미분류" in text and "정체불명 약" in text


def test_sparse_metrics_do_not_trigger_missing_alerts():
    """Apple 의 심방세동 이력은 주 단위 추정치라 매일 들어오지 않는다.
    이런 지표에 매일 '수집 경로 점검 필요'를 띄우면 경보 피로만 쌓인다."""
    hist = []
    for i in range(20):
        r = make(i + 1, vitals__resting_hr=57, subjective__energy=3)
        if i % 7 == 0:
            r.vitals.afib_burden_pct = 2.0
        hist.append(r)
    r = rd.compute(hist, make(25, vitals__resting_hr=57, subjective__energy=3), Profile())
    assert not any("심방세동 부담" in f for f in r.flags)


def test_split_night_is_reported_not_silently_dropped():
    """시계를 밤중에 벗으면 밤이 두 세션으로 쪼개진다. 긴 쪽만 쓰되
    버렸다는 사실은 알려야 사용자가 판단할 수 있다."""
    from datetime import datetime as dt

    from health.ingest import IngestReport, Interval, _assemble_sleep

    def asleep(d1, h1, m1, d2, h2, m2):
        return Interval(dt(2026, 8, d1, h1, m1), dt(2026, 8, d2, h2, m2),
                        "HKCategoryValueSleepAnalysisAsleepCore")

    rep = IngestReport()
    _assemble_sleep([asleep(25, 23, 0, 26, 3, 0),      # 4시간 자고
                     asleep(26, 7, 0, 26, 9, 30)], rep)  # 4시간 공백 뒤 2.5시간 더
    assert rep.split_nights == ["2026-08-26"]


# ── 준비도가 프로필의 금기를 넘지 않는다 ────────────────────────

CARDIAC_PROFILE = Profile(
    conditions=["심방세동"],
    contraindications=["너무 강한 운동(심장에 부담을 주는 운동)"],
)


def _green_day(**extra):
    """준비도가 GREEN 에 닿는 하루를 만든다."""
    base = dict(vitals__resting_hr=52, sleep__total_min=480, sleep__efficiency_pct=94,
                subjective__energy=5, subjective__soreness=1, subjective__stress=1,
                subjective__mood=5)
    hist = [make(i + 1, vitals__resting_hr=57, sleep__total_min=430,
                 sleep__efficiency_pct=88, subjective__energy=3,
                 subjective__soreness=3, subjective__stress=3,
                 subjective__mood=3, vitals__hrv_rmssd_ms=45 + (i % 5))
            for i in range(21)]
    return hist, make(25, **base, **extra)


def test_green_never_recommends_high_intensity_when_the_profile_forbids_it():
    """프로필이 금지한 것을 시스템이 권해서는 안 된다.
    점수는 오늘의 몸 상태고, 금기는 그 위에 있는 경계다."""
    hist, today = _green_day(vitals__hrv_rmssd_ms=50)

    free = rd.compute(hist, today, Profile())
    limited = rd.compute(hist, today, CARDIAC_PROFILE)

    assert free.band == limited.band == "GREEN"
    assert "고강도" in free.advice
    assert "고강도" not in limited.advice
    assert "중강도" in limited.advice
    assert "심장에 부담" in limited.advice        # 근거를 함께 보여준다


def test_intensity_limit_is_detected_from_conditions_too():
    """금기 칸이 비어 있어도 기저질환에서 읽어낸다."""
    assert rd.intensity_limited(Profile(conditions=["관상동맥 스텐트 삽입"]))
    assert rd.intensity_limited(Profile(conditions=["심방세동"]))
    assert rd.intensity_limited(Profile(contraindications=["무리한 운동 자제"]))
    assert rd.intensity_limited(Profile(conditions=["알레르기성 비염"])) is None


def test_lower_bands_are_unchanged_by_the_limit():
    """제한은 GREEN 문구만 바꾼다. 회복 권고까지 흔들면 안 된다."""
    hist = [make(i + 1, vitals__resting_hr=57, subjective__energy=3) for i in range(21)]
    bad = make(25, vitals__resting_hr=72, subjective__energy=1)
    assert rd.compute(hist, bad, Profile()).advice == \
           rd.compute(hist, bad, CARDIAC_PROFILE).advice


# ── 높은 HRV 가 무조건 좋은 신호는 아니다 ───────────────────────

def test_hrv_upside_is_capped():
    """HRV 가 평소보다 낮은 것은 신뢰할 만한 신호지만, 크게 높은 것은
    회복이 두 배 좋아졌다는 뜻이 아니다 — 잡음이거나 부정맥일 때가 많다."""
    base = dict(vitals__resting_hr=57, sleep__total_min=430, subjective__energy=3)
    hist = [make(i + 1, **base, vitals__hrv_rmssd_ms=45 + (i % 5)) for i in range(21)]

    mild = rd.compute(hist, make(25, **base, vitals__hrv_rmssd_ms=52), Profile())
    huge = rd.compute(hist, make(25, **base, vitals__hrv_rmssd_ms=300), Profile())
    assert huge.score - mild.score < 5       # 상단이 잘려 거의 차이가 없다

    # 하단은 그대로 — 낮은 HRV 는 끝까지 반영된다
    low = rd.compute(hist, make(25, **base, vitals__hrv_rmssd_ms=12), Profile())
    assert mild.score - low.score > 15


def test_hrv_spike_is_discarded_for_users_with_arrhythmia_history():
    """Apple 의 심방세동 이력은 주 단위 추정치라 에피소드 당일엔 비어 있다.
    표지가 없어도 이력이 있는 사람의 HRV 급등은 회복 신호로 쓰지 않는다."""
    base = dict(vitals__resting_hr=57, sleep__total_min=430, subjective__energy=3)
    hist = [make(i + 1, **base, vitals__hrv_sdnn_ms=26 + (i % 5) - 2) for i in range(21)]
    today = make(25, **base, vitals__hrv_sdnn_ms=38)      # z ≈ +2.4

    plain = rd.compute(hist, today, Profile())
    cardiac = rd.compute(hist, today, CARDIAC_PROFILE)

    assert cardiac.score < plain.score
    assert any("심전도" in f for f in cardiac.flags)
    assert not any("HRV" in label for label, _, _ in cardiac.contributors)


def test_arrhythmia_history_is_detected_from_conditions_or_medications():
    assert rd.has_arrhythmia_history(Profile(conditions=["심방세동"]))
    assert rd.has_arrhythmia_history(Profile(medications=["멀택정 400mg"]))
    assert rd.has_arrhythmia_history(Profile(conditions=["카테터 절제술 후"]))
    assert not rd.has_arrhythmia_history(Profile(conditions=["고혈압"]))


def test_walking_heart_rate_counts_toward_readiness():
    """같은 걸음에 심장이 더 일했다면 그건 부하의 직접 신호다."""
    base = dict(vitals__resting_hr=57, sleep__total_min=430, subjective__energy=3)
    hist = [make(i + 1, **base, vitals__walking_hr_avg=79 + (i % 5) - 2) for i in range(21)]

    normal = rd.compute(hist, make(25, **base, vitals__walking_hr_avg=79), Profile())
    strained = rd.compute(hist, make(25, **base, vitals__walking_hr_avg=95), Profile())
    assert strained.score < normal.score


def test_brief_does_not_praise_an_hrv_it_discarded(tmp_path):
    """준비도에서 뺀 값을 지표 상세에서 '▲좋음'이라고 적으면 같은 화면이
    서로 반대되는 말을 한다. 사용자가 뭘 믿어야 할지 모르게 만드는 출력은
    없느니만 못하다."""
    from health import report as rp

    st = Store(tmp_path)
    st.save_profile(CARDIAC_PROFILE)
    for i in range(21):
        st.upsert(make(i + 1, vitals__resting_hr=57, sleep__total_min=430,
                       vitals__hrv_sdnn_ms=26 + (i % 5) - 2))
    st.upsert(make(25, vitals__resting_hr=59, sleep__total_min=430,
                   vitals__hrv_sdnn_ms=38))

    text = rp.daily_brief(st, "2026-01-25")
    hrv_line = next(ln for ln in text.splitlines() if "HRV(SDNN)" in ln)
    assert "▲좋음" not in hrv_line
    assert "제외" in hrv_line


def test_hrv_exclusion_reason_is_structured_not_just_prose():
    base = dict(vitals__resting_hr=57, sleep__total_min=430)
    hist = [make(i + 1, **base, vitals__hrv_sdnn_ms=26 + (i % 5) - 2) for i in range(21)]

    spike = rd.compute(hist, make(25, **base, vitals__hrv_sdnn_ms=38), CARDIAC_PROFILE)
    assert spike.hrv_excluded == "spike"

    af = make(25, **base, vitals__hrv_sdnn_ms=38)
    af.vitals.afib_burden_pct = 9.0
    assert rd.compute(hist, af, CARDIAC_PROFILE).hrv_excluded == "afib"

    clean = rd.compute(hist, make(25, **base, vitals__hrv_sdnn_ms=27), Profile())
    assert clean.hrv_excluded is None


def test_blood_pressure_has_a_personal_baseline():
    """가정혈압을 매일 재는 사람에게 절대 기준(140/90)만 보여주면
    자기 범위를 벗어난 날을 놓친다."""
    hist = [make(i + 1, vitals__bp_systolic=110 + (i % 5) - 2,
                 vitals__bp_diastolic=65 + (i % 4) - 1) for i in range(20)]
    m = bl.compute(hist, make(25, vitals__bp_systolic=127, vitals__bp_diastolic=77))
    assert m["vitals.bp_systolic"].z > 2
    assert m["vitals.bp_systolic"].deviation == "worse"     # 낮을수록 좋은 방향


def test_bp_sd_floor_prevents_daily_false_alarms():
    """가정혈압은 같은 사람 안에서도 흔들린다. 하한이 없으면
    ±3mmHg 변동에 매일 '평소와 다름'이 뜬다."""
    hist = [make(i + 1, vitals__bp_systolic=110 + (i % 2)) for i in range(20)]
    m = bl.compute(hist, make(25, vitals__bp_systolic=114))["vitals.bp_systolic"]
    assert m.sd >= 5.0                      # 관측 표준편차(0.5)가 아니라 하한이 쓰인다
    assert abs(m.z) < 1.5                   # 4mmHg 차이로는 경보하지 않는다


def test_common_drugs_are_classified_even_without_rules():
    """넥시움 같은 흔한 약까지 '계열 미상'으로 매일 경고하면
    진짜 경고를 무시하게 된다."""
    p = Profile(medications=["넥시움정 40mg", "타이레놀 500mg", "비타민D"])
    assert tg.unclassified_medications(p) == []
    assert {"ppi", "acetaminophen", "supplement"} <= tg.med_classes(p)
    codes = [f.code for f in tg.evaluate([], make(1), p).findings]
    assert "unclassified_medication" not in codes


# ── 로컬 웹 UI ─────────────────────────────────────────────────

@pytest.fixture
def server(tmp_path):
    """실제 서버를 임시 포트에 띄운다. 모킹하면 라우팅·직렬화 버그를 놓친다."""
    import json as _json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from health import web

    st = Store(tmp_path)
    st.save_profile(Profile(
        reviewed_at="2026-01-01T07:00:00+09:00",
        tracking=["subjective.energy", "vitals.bp", "subjective.note"],
        conditions=["심방세동"], medications=["릭시아나(에독사반) 60mg"]))
    from datetime import date as _date, timedelta as _td

    for i in range(21):                      # 오늘로부터 거슬러 21일
        d = (_date.today() - _td(days=i + 1)).isoformat()
        rec = DailyRecord(date=d, sources=["apple-health"])
        rec.vitals.resting_hr = 57 + (i % 3) - 1
        rec.vitals.hrv_sdnn_ms = 26 + (i % 4)
        if i % 2 == 0:                       # 결측이 섞이도록
            rec.sleep.total_min = 430 + (i % 5) * 10
        st.upsert(rec)

    handler = type("Bound", (web.HealthHandler,), {"store": st})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class Client:
        base = f"http://127.0.0.1:{port}"
        store = st

        def get(self, path):
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return _json.loads(r.read())

        def raw(self, path):
            try:
                with urllib.request.urlopen(self.base + path, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        def post(self, path, payload):
            req = urllib.request.Request(
                self.base + path, method="POST",
                data=_json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return _json.loads(r.read())
            except urllib.error.HTTPError as e:
                return _json.loads(e.read())

    yield Client()
    httpd.shutdown()
    httpd.server_close()


def test_server_binds_to_loopback_only():
    """0.0.0.0 으로 열면 같은 와이파이의 누구나 건강기록을 본다."""
    src = (REPO / "src" / "health" / "web.py").read_text("utf-8")
    assert '"0.0.0.0"' not in src
    assert 'ThreadingHTTPServer(("127.0.0.1"' in src


def test_today_lists_only_unfilled_tracked_fields(server):
    d = server.get("/api/today")
    keys = [f["key"] for f in d["fields"]]
    assert keys == ["subjective.energy", "vitals.bp", "subjective.note"]
    assert all(f["filled"] is False for f in d["fields"])


def test_cli_only_hint_text_is_stripped_for_web(server):
    """'(Enter=건너뜀)' 은 터미널 얘기다. 웹에서는 거짓말이 된다."""
    note = next(f for f in server.get("/api/today")["fields"]
                if f["key"] == "subjective.note")
    assert "Enter" not in note["hint"]


def test_checkin_saves_and_returns_the_brief(server):
    res = server.post("/api/checkin", {
        "values": {"subjective.energy": "4", "vitals.bp": "127/77",
                   "subjective.note": "걸음이 적었던 날"},
        "seconds": 38})
    assert res["saved"] == 3 and res["rejected"] == []
    assert "readiness" in res and "triage" in res

    rec = server.store.load(res["date"])
    assert rec.subjective.energy == 4
    assert rec.vitals.bp_systolic == 127 and rec.vitals.bp_diastolic == 77
    assert "checkin" in rec.sources          # 게이트가 세는 표식


def test_web_uses_the_same_validation_as_the_cli(server):
    """웹에만 검증이 없으면 오타 하나가 베이스라인을 민다."""
    res = server.post("/api/checkin", {"values": {"vitals.bp": "270/77"}})
    assert res["saved"] == 0
    assert res["rejected"][0]["key"] == "vitals.bp"
    assert "범위" in res["rejected"][0]["reason"]

    res = server.post("/api/checkin", {"values": {"vitals.bp": "백이십칠"}})
    assert res["saved"] == 0 and res["rejected"]


def test_brief_marks_excluded_hrv(server):
    """준비도에서 뺀 HRV 를 화면이 '좋음'으로 그리지 않으려면
    제외 사유가 API 에 실려야 한다."""
    from datetime import date as _date

    today = _date.today().isoformat()
    rec = server.store.load_or_new(today)
    rec.vitals.hrv_sdnn_ms = 60          # 평소 26±4 대비 급등
    rec.vitals.resting_hr = 57
    server.store.upsert(rec)
    b = server.get(f"/api/brief?date={today}")
    assert b["readiness"]["hrv_excluded"] == "spike"
    hrv = next(m for m in b["metrics"] if "HRV" in m["label"])
    assert hrv["deviation"] == "excluded"


def test_trend_reports_gaps_as_null_not_zero(server):
    """결측을 0 으로 채우면 그래프가 '그날 안 잤다'고 말한다."""
    d = server.get("/api/trend?days=30")
    assert len(d["dates"]) == 30
    sleep = next(s for s in d["series"] if s["path"] == "sleep.total_min")
    assert None in sleep["values"]
    assert 0 not in [v for v in sleep["values"] if v is not None]
    assert sleep["mean"] is not None and sleep["sd"] is not None


def test_unknown_paths_and_traversal_are_refused(server):
    for path in ("/../../etc/passwd", "/api/nope", "/etc/passwd"):
        code, body = server.raw(path)
        assert code == 404, path
        assert b"root:" not in body


def test_malformed_json_does_not_crash_the_server(server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(server.base + "/api/checkin", method="POST",
                                 data=b"not json",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 400
    # 서버가 살아 있어야 한다
    assert server.get("/api/today")["fields"]


def test_ui_file_ships_with_the_package():
    from health import web

    assert (web.UI_DIR / "index.html").exists()
