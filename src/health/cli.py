"""건강 데이터 CLI. 에이전트가 호출하는 도구 표면(tool surface)이기도 하다.

저장소 루트의 `./health` 셸 진입점으로 부르는 것을 기본으로 한다
(PYTHONPATH 설정 불필요, macOS 의 python3 도 알아서 찾는다).

    ./health init -i
    ./health checkin
    ./health status
    ./health log --set sleep.total_min=430 --set subjective.energy=3
    ./health score / triage / brief / weekly
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta

from . import checkin as ck
from . import readiness as rd
from . import report as rp
from . import triage as tg
from .schema import DailyRecord, Workout
from .store import Profile, Store


def cmd_name() -> str:
    """안내 문구에 쓸 호출 이름.

    셸 진입점(./health)이 HEALTH_CMD 를 넘겨준다. 직접 모듈로 실행했다면
    현재 인터프리터에 맞춰 만든다 — macOS 에는 `python` 이 없고 `python3`
    만 있어서, 문구에 `python` 이라고 적어두면 그대로 따라 쳤을 때 실패한다.
    """
    override = os.environ.get("HEALTH_CMD")
    if override:
        return override
    exe = "python3" if sys.version_info[0] == 3 else "python"
    return f"{exe} -m health"


def _today() -> str:
    return date.today().isoformat()


def _coerce(v: str):
    """CLI 문자열을 적절한 타입으로. "3" -> 3, "4.5" -> 4.5, "a,b" -> ["a","b"]"""
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null", ""):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    if "," in v:
        return [s.strip() for s in v.split(",") if s.strip()]
    return v


PROFILE_QUESTIONS: list[tuple[str, str, str]] = [
    # (필드, 질문, 힌트)  — 순서가 곧 중요도다. 안전 경계부터 묻는다.
    ("name",          "이름/호칭",              "브리핑에서 부를 이름"),
    ("birth_year",    "태어난 해",              "예: 1988. 최대심박·참고범위 추정에 쓰입니다"),
    ("sex",           "성별",                   "male / female / other (참고범위 판정용)"),
    ("height_cm",     "키(cm)",                 ""),
    ("conditions",    "기저질환",               "쉼표로 구분. 없으면 Enter"),
    ("medications",   "복약 중인 약",           "이름+용량+빈도. 건강기능식품 포함. 없으면 Enter"),
    ("allergies",     "알레르기",               "약물·음식. 없으면 Enter"),
    ("contraindications", "피해야 할 운동/식이", "의사가 제한한 것, 부상 이력 등"),
    ("goals",         "목표",                   "구체적일수록 좋습니다. 예: 평일 7시간 수면"),
    ("emergency_contact", "비상연락처",         "응급 판정 시 안내에 쓰입니다"),
    ("clinician_note", "주치의 지시사항",       "최근 진료에서 받은 목표·주의사항"),
]

LIST_FIELDS = {"conditions", "medications", "allergies", "contraindications", "goals"}


def _fill_profile(p: Profile) -> Profile:
    print("\n프로필을 채웁니다. 이 값들이 모든 조언의 안전 경계가 됩니다.")
    print("모르거나 해당 없으면 Enter 로 건너뜁니다. (q = 중단)\n")
    for field_name, question, hint in PROFILE_QUESTIONS:
        cur = getattr(p, field_name)
        shown = ", ".join(cur) if isinstance(cur, list) else cur
        was = f"  [현재: {shown}]" if shown else ""
        if hint:
            print(f"  {hint}")
        try:
            raw = input(f"  {question}{was}\n  › ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n중단했습니다.")
            raise ck.Aborted
        print()
        if not raw:
            continue
        if raw.lower() == "q":
            raise ck.Aborted
        if field_name in LIST_FIELDS:
            setattr(p, field_name, [s.strip() for s in raw.split(",") if s.strip()])
        elif field_name == "birth_year":
            try:
                p.birth_year = int(raw)
            except ValueError:
                print("    ↑ 숫자가 아니라 건너뜁니다")
        elif field_name == "height_cm":
            try:
                p.height_cm = float(raw)
            except ValueError:
                print("    ↑ 숫자가 아니라 건너뜁니다")
        else:
            setattr(p, field_name, raw)

    p.tracking = _choose_tracking(p)
    p.reviewed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return p


def _prompt(text: str, default: str = "") -> str:
    try:
        return input(text).strip() or default
    except (EOFError, KeyboardInterrupt):
        return default


def _choose_tracking(p: Profile) -> list[str]:
    """매일 물을 항목을 목표와 기기 보유 여부로 제안하고 확인받는다.

    자유 입력으로 받으면 사람은 거의 항상 너무 많이 고른다. 제안을
    기본값으로 두고 바꾸고 싶을 때만 손대게 하는 편이 훨씬 잘 지켜진다.
    """
    print("\n매일 기록할 항목을 정합니다.\n")

    print("  웨어러블(스마트워치·수면링)을 쓰시나요?")
    print("  쓰신다면 수면·안정시심박·걸음은 자동 수집에 맡기고 매일 묻지 않습니다.")
    has_wearable = _prompt("  y/n [y] › ", "y").lower().startswith("y")

    print("\n  목표를 고르세요 (번호를 쉼표로, 예: 1,2,4)")
    keys = list(ck.GOAL_PRESETS)
    for i, key in enumerate(keys, 1):
        label, paths = ck.GOAL_PRESETS[key]
        print(f"    {i}. {label}")
    raw = _prompt("  › ")
    goals = [keys[int(n) - 1] for n in raw.replace(" ", "").split(",")
             if n.isdigit() and 1 <= int(n) <= len(keys)]

    suggested = ck.suggest_tracking(goals, has_wearable)
    print(f"\n  제안: {', '.join(suggested)}")
    if has_wearable:
        print("  (수면·안정시심박·걸음은 Phase 2의 wearable-ingest 가 채웁니다)")
    print(f"\n  그대로 쓰려면 Enter. 직접 정하려면 쉼표로 입력하세요.")
    print(f"  가능한 항목: {', '.join(sorted(ck.PROMPTS))}")
    raw = _prompt("  › ")
    if not raw:
        return suggested

    chosen = [t.strip() for t in raw.split(",") if t.strip() in ck.PROMPTS]
    unknown = [t.strip() for t in raw.split(",") if t.strip() and t.strip() not in ck.PROMPTS]
    if unknown:
        print(f"  ⚠ 알 수 없는 항목은 제외했습니다: {', '.join(unknown)}")
    if len(chosen) > ck.MAX_TRACKING:
        print(f"  ⚠ {len(chosen)}개를 고르셨습니다. {ck.MAX_TRACKING}개를 넘으면 "
              "2주 안에 기록이 끊깁니다 — status 게이트에서 걸립니다.")
    return chosen or suggested


def cmd_init(args, store: Store) -> int:
    exists = store.profile_path.exists()
    if exists and not (args.force or args.interactive):
        print(f"프로필이 이미 있습니다: {store.profile_path}")
        print(f"내용을 채우려면: {cmd_name()} init --interactive")
        return 1

    p = store.load_profile() if exists else Profile()
    if args.interactive:
        try:
            p = _fill_profile(p)
        except ck.Aborted:
            print("저장하지 않고 종료합니다.")
            return 130

    store.save_profile(p)
    store.daily_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n저장: {store.profile_path}")
    if p.reviewed_at:
        print(f"매일 기록할 항목 {len(p.tracked())}개: {', '.join(p.tracked())}")
        print(f"\n다음: {cmd_name()} checkin")
    else:
        print("→ 나이·기저질환·복약·금기·목표를 채워야 합니다.")
        print(f"   대화형으로: {cmd_name()} init --interactive")
    return 0


def cmd_log(args, store: Store) -> int:
    d = args.date or _today()
    rec = DailyRecord(date=d)

    if args.json:
        payload = json.loads(args.json)
        payload["date"] = d
        rec = DailyRecord.from_dict(payload)

    for pair in args.set or []:
        if "=" not in pair:
            print(f"형식 오류: {pair} (key=value 여야 합니다)", file=sys.stderr)
            return 2
        k, v = pair.split("=", 1)
        try:
            rec.set_path(k.strip(), _coerce(v.strip()))
        except (AttributeError, KeyError) as e:
            print(f"알 수 없는 필드: {k} ({e})", file=sys.stderr)
            return 2

    if args.workout:
        for spec in args.workout:
            # "run:45:7" = 종류:분:RPE
            parts = spec.split(":")
            rec.activity.workouts.append(Workout(
                type=parts[0],
                duration_min=float(parts[1]) if len(parts) > 1 else 0.0,
                rpe=float(parts[2]) if len(parts) > 2 and parts[2] else None,
            ))

    if args.source:
        rec.sources.append(args.source)

    path = store.upsert(rec)
    store.log_event("log", args.source or "cli", {"date": d, "fields": args.set or []})
    print(f"저장: {path}")
    return 0


def cmd_checkin(args, store: Store) -> int:
    d = args.date or _today()
    profile = store.load_profile()
    rec = store.load_or_new(d)

    print(f"\n체크인 — {d}")
    if not profile.reviewed_at:
        print("⚠ 프로필이 아직 검토되지 않았습니다. 안전 경계 없이 조언이 나갑니다.")
        print(f"  먼저: {cmd_name()} init --interactive\n")

    try:
        rec, seconds, filled = ck.run(rec, profile.tracked())
    except ck.Aborted:
        print("\n중단했습니다. 여기까지는 저장되지 않았습니다.")
        return 130
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        return 130

    rec.sources = sorted(set(rec.sources) | {"checkin"})
    store.save(rec)
    store.log_event("checkin", "cli", {"date": d, "seconds": seconds, "filled": filled})

    print(f"\n저장 ({seconds:.0f}초, {filled}개 기록)")
    if seconds > 90:
        print("⚠ 90초를 넘었습니다. 항목을 줄이거나 자동 수집으로 옮기세요 "
              "— 이 속도로는 2주 안에 기록이 끊깁니다.")

    # 체크인 직후 안전 판정을 보여준다. 자유 서술도 레드플래그 스캔 대상이다.
    history = [r for r in store.history(end=d, days=29) if r.date != d]
    res = tg.evaluate(history, rec, profile)
    if res.findings:
        print("\n" + res.render())
    r = rd.compute(history, rec, profile)
    print(f"\n{r.summary()}")
    for f in r.flags:
        print(f"  ⚑ {f}")
    return int(res.severity)


def _streak(dates: list[str], today: str) -> tuple[int, bool]:
    """연속 기록일과 '오늘 기록 여부'.

    아침 9시에 아직 체크인을 안 했다고 연속 기록이 끊긴 것으로 세면
    사람은 좌절한다. 그래서 오늘이 비어 있으면 어제부터 센다.
    """
    have = set(dates)
    done_today = today in have
    cursor = date.fromisoformat(today)
    if not done_today:
        cursor -= timedelta(days=1)
    n = 0
    while cursor.isoformat() in have:
        n += 1
        cursor -= timedelta(days=1)
    return n, done_today


def cmd_status(args, store: Store) -> int:
    """Phase 0 게이트 판정판. 통과 조건을 감이 아니라 숫자로 본다."""
    today = args.date or _today()
    profile = store.load_profile()
    dates = store.all_dates()
    streak, done_today = _streak(dates, today)

    recent = [d for d in dates if d > (date.fromisoformat(today) - timedelta(days=14)).isoformat()]
    coverage = len(recent) / 14

    times = [e["seconds"] for e in store.events("checkin") if isinstance(e.get("seconds"), (int, float))]
    median = sorted(times)[len(times) // 2] if times else None

    tracked = profile.tracked()
    checks: list[tuple[bool | None, str, str]] = [
        (
            bool(profile.reviewed_at),
            "프로필 검토",
            f"검토 {profile.reviewed_at[:10]}" if profile.reviewed_at
            else f"미검토 — {cmd_name()} init --interactive",
        ),
        (
            len(tracked) <= 5,
            f"측정 항목 {len(tracked)}개",
            "5개 이하" if len(tracked) <= 5 else "5개를 넘으면 2주 안에 끊깁니다",
        ),
        (
            streak >= 7,
            f"연속 기록 {streak}일",
            "7일 달성" if streak >= 7 else f"{7 - streak}일 남음",
        ),
        (
            None if median is None else median <= 90,
            "체크인 소요 시간",
            f"측정된 체크인 없음 — {cmd_name()} checkin"
            if median is None else f"중앙값 {median:.0f}초 (기준 90초)",
        ),
    ]

    print(f"\nPhase 0 게이트 — {today}")
    print("─" * 52)
    for ok, label, detail in checks:
        mark = "…" if ok is None else ("✓" if ok else "✗")
        print(f"  {mark} {label:<20} {detail}")
    print("─" * 52)
    print(f"  기록된 날 {len(dates)}일 · 최근 14일 기록률 {coverage * 100:.0f}%"
          f" · 오늘 {'완료' if done_today else '미기록'}")

    if profile.conditions or profile.medications:
        print(f"  안전 경계: 기저질환 {len(profile.conditions)}건 · 복약 {len(profile.medications)}건")

    passed = all(c[0] for c in checks)
    print()
    if passed:
        print("  Phase 0 통과. 다음은 Phase 1 — 에이전트 4개(orchestrator, checkin,")
        print("  risk-triage, vitals-analyst)를 붙이고 21일 데이터를 모읍니다. docs/02-roadmap.md")
    else:
        nxt = next((c for c in checks if not c[0]), None)
        if nxt:
            print(f"  다음 할 일: {nxt[1]} — {nxt[2]}")
    return 0 if passed else 1


def cmd_score(args, store: Store) -> int:
    d = args.date or _today()
    today = store.load(d)
    if today is None:
        print(f"{d} 기록 없음.")
        return 1
    history = [r for r in store.history(end=d, days=29) if r.date != d]
    r = rd.compute(history, today, store.load_profile())
    if args.json:
        print(json.dumps({
            "date": r.date, "score": r.score, "band": r.band,
            "advice": r.advice, "confidence": r.confidence, "flags": r.flags,
            "acwr": r.acwr, "sleep_debt_min": r.sleep_debt_min,
            "contributors": [{"label": l, "z": round(z, 2), "impact": round(i, 3)}
                             for l, z, i in r.contributors],
        }, ensure_ascii=False, indent=2))
    else:
        print(r.summary())
        for lbl, z, imp in r.contributors:
            bar = "#" * int(abs(imp) * 40)
            print(f"  {lbl:<14} z={z:+5.1f}  {'+' if imp >= 0 else '-'}{bar}")
        for f in r.flags:
            print(f"  ⚑ {f}")
    return 0


def cmd_triage(args, store: Store) -> int:
    d = args.date or _today()
    today = store.load(d)
    if today is None:
        print(f"{d} 기록 없음.")
        return 1
    history = [r for r in store.history(end=d, days=29) if r.date != d]
    res = tg.evaluate(history, today, store.load_profile())
    if args.json:
        print(json.dumps({
            "date": d,
            "severity": res.severity.name,
            "blocks_exercise": res.blocks_exercise,
            "findings": [{"severity": f.severity.name, "code": f.code,
                          "message": f.message, "action": f.action} for f in res.findings],
        }, ensure_ascii=False, indent=2))
    else:
        print(res.render())
    # 종료코드로 심각도를 노출 → 훅/스크립트에서 분기하기 쉽다
    return int(res.severity)


def cmd_brief(args, store: Store) -> int:
    print(rp.daily_brief(store, args.date or _today()))
    return 0


def cmd_weekly(args, store: Store) -> int:
    print(rp.weekly_review(store, args.date or _today()))
    return 0


def cmd_seed(args, store: Store) -> int:
    """재현 가능한 합성 데이터. 실제 사람의 값이 아니라 시연·테스트용이다."""
    import random

    rng = random.Random(args.seed)
    end = date.fromisoformat(args.date) if args.date else date.today()
    for i in range(args.days - 1, -1, -1):
        d = end - timedelta(days=i)
        # 주중/주말 리듬 + 완만한 추세를 흉내낸다
        weekend = d.weekday() >= 5
        wave = math.sin(i / 5.0)
        rec = DailyRecord(date=d.isoformat(), sources=["seed"])
        rec.sleep.total_min = round(rng.gauss(430 + (35 if weekend else 0) + wave * 12, 32))
        rec.sleep.efficiency_pct = round(min(98, max(70, rng.gauss(88 + wave * 2, 4))), 1)
        rec.sleep.deep_min = round(max(20, rng.gauss(70, 15)))
        rec.sleep.latency_min = round(max(2, rng.gauss(16, 7)))
        rec.vitals.resting_hr = round(rng.gauss(57 - wave * 1.5, 2.5), 1)
        rec.vitals.hrv_rmssd_ms = round(max(15, rng.gauss(48 + wave * 5, 8)), 1)
        rec.vitals.spo2_pct = round(min(99, rng.gauss(97, 0.8)), 1)
        rec.vitals.weight_kg = round(rng.gauss(70, 0.5), 1)
        rec.activity.steps = int(max(1500, rng.gauss(8200 if not weekend else 5500, 2500)))
        rec.subjective.energy = max(1, min(5, round(rng.gauss(3.4 + wave * 0.3, 0.8))))
        rec.subjective.mood = max(1, min(5, round(rng.gauss(3.5, 0.8))))
        rec.subjective.stress = max(1, min(5, round(rng.gauss(2.8 - wave * 0.3, 0.9))))
        rec.subjective.soreness = max(1, min(5, round(rng.gauss(2.4, 0.9))))
        rec.intake.caffeine_mg = round(max(0, rng.gauss(180, 70)))
        rec.intake.water_ml = round(max(500, rng.gauss(1900, 500)))
        if not weekend and rng.random() < 0.6:
            rec.activity.workouts.append(Workout(
                type=rng.choice(["run", "strength", "cycling"]),
                duration_min=round(rng.gauss(45, 12)),
                rpe=round(min(10, max(2, rng.gauss(6, 1.5))), 1),
            ))
        store.upsert(rec)
    print(f"{args.days}일치 합성 데이터를 {store.daily_dir} 에 생성했습니다.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="health", description="개인 건강 관리 에이전트 코어 CLI")
    p.add_argument("--data-dir", help="데이터 루트(기본: 저장소의 data/ 또는 $HEALTH_DATA_DIR)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="프로필/디렉터리 초기화")
    s.add_argument("--force", action="store_true")
    s.add_argument("-i", "--interactive", action="store_true", help="대화형으로 프로필 채우기")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("checkin", help="대화형 일일 체크인 (Phase 0 의 기본 루프)")
    s.add_argument("--date")
    s.set_defaults(func=cmd_checkin)

    s = sub.add_parser("status", help="Phase 0 게이트 판정판")
    s.add_argument("--date")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("log", help="일별 레코드 기록/갱신")
    s.add_argument("--date")
    s.add_argument("--set", action="append", metavar="경로=값",
                   help='예: --set sleep.total_min=430 --set subjective.energy=3')
    s.add_argument("--json", help="부분 레코드 JSON")
    s.add_argument("--workout", action="append", metavar="종류:분:RPE")
    s.add_argument("--source", help="데이터 출처 태그 (예: apple-health, checkin)")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("score", help="준비도 계산")
    s.add_argument("--date"); s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_score)

    s = sub.add_parser("triage", help="레드플래그 판정 (종료코드=심각도 0~3)")
    s.add_argument("--date"); s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("brief", help="일일 팩트시트(에이전트 입력용)")
    s.add_argument("--date"); s.set_defaults(func=cmd_brief)

    s = sub.add_parser("weekly", help="주간 리뷰 팩트시트")
    s.add_argument("--date"); s.set_defaults(func=cmd_weekly)

    s = sub.add_parser("seed", help="데모용 합성 데이터 생성")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--date"); s.add_argument("--seed", type=int, default=42)
    s.set_defaults(func=cmd_seed)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from pathlib import Path

    store = Store(Path(args.data_dir) if args.data_dir else None)
    return args.func(args, store)


if __name__ == "__main__":
    raise SystemExit(main())
