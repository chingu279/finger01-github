"""건강 데이터 CLI. 에이전트가 호출하는 도구 표면(tool surface)이기도 하다.

    python -m health init
    python -m health log --set sleep.total_min=430 --set subjective.energy=3
    python -m health log --json '{"vitals":{"resting_hr":58}}'
    python -m health score
    python -m health triage
    python -m health brief            # 에이전트에 먹일 일일 팩트시트
    python -m health weekly
    python -m health seed --days 30   # 데모용 합성 데이터
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, timedelta

from . import readiness as rd
from . import report as rp
from . import triage as tg
from .schema import DailyRecord, Workout
from .store import Profile, Store


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


def cmd_init(args, store: Store) -> int:
    p = store.load_profile()
    if store.profile_path.exists() and not args.force:
        print(f"프로필이 이미 있습니다: {store.profile_path} (덮어쓰려면 --force)")
        return 1
    store.save_profile(p)
    store.daily_dir.mkdir(parents=True, exist_ok=True)
    print(f"초기화 완료.\n프로필: {store.profile_path}")
    print("→ 나이·기저질환·복약·금기·목표를 직접 채워 넣으세요. "
          "이 값들이 모든 조언의 안전 경계가 됩니다.")
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
    s.set_defaults(func=cmd_init)

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
