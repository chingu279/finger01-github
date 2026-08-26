"""로컬 파일 저장소.

개인 건강 데이터는 기본적으로 **로컬 평문 JSON**으로만 둔다.
클라우드 동기화·암호화는 선택지로 열어두되 기본값은 아니다
(docs/04-safety.md 참고).

레이아웃
    data/profile.json          사용자 프로필(정적, 저빈도 변경)
    data/daily/YYYY-MM-DD.json 일별 레코드
    data/events.jsonl          에이전트 행동 로그(추가 전용, 감사용)
    data/experiments.json      진행 중/종료된 N-of-1 실험
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .schema import DailyRecord


def data_dir() -> Path:
    """HEALTH_DATA_DIR 로 위치를 바꿀 수 있다(여러 사용자/테스트 격리)."""
    root = os.environ.get("HEALTH_DATA_DIR")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[2] / "data"


@dataclass
class Profile:
    """정적 프로필. 모든 개입의 안전 경계를 정의한다."""

    name: str = "user"
    birth_year: int | None = None
    sex: str | None = None                       # "male" | "female" | "other"
    height_cm: float | None = None
    timezone: str = "Asia/Seoul"
    goals: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)      # 기저질환
    medications: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    contraindications: list[str] = field(default_factory=list)  # 금기 운동/식이
    sleep_need_min: float = 450.0                # 개인 수면 요구량(기본 7.5h)
    resting_hr_known: float | None = None
    max_hr: float | None = None
    clinician_note: str | None = None
    emergency_contact: str | None = None

    # Phase 0 에서 정하는 값들
    tracking: list[str] = field(default_factory=list)   # 매일 물을 항목 (비면 기본 세트)
    reviewed_at: str | None = None                      # 프로필을 실제로 검토한 시각
    #  왜 reviewed_at 이 필요한가: conditions=[] 는 "기저질환 없음"과
    #  "아직 안 채움"을 구별하지 못한다. 게이트는 목록의 길이가 아니라
    #  사람이 실제로 훑어봤는지를 봐야 한다.

    def tracked(self) -> list[str]:
        from .checkin import DEFAULT_TRACKING

        return list(self.tracking) if self.tracking else list(DEFAULT_TRACKING)

    @property
    def age(self) -> int | None:
        if self.birth_year is None:
            return None
        return date.today().year - self.birth_year

    def estimated_max_hr(self) -> float | None:
        if self.max_hr:
            return self.max_hr
        if self.age is None:
            return None
        # Tanaka 공식: 208 - 0.7 x 나이 (전통적 220-나이보다 오차가 작다)
        return 208 - 0.7 * self.age

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Profile:
        from dataclasses import fields as _fields

        names = {f.name for f in _fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or data_dir()
        self.daily_dir = self.root / "daily"

    # ── 프로필 ────────────────────────────────────────────────
    @property
    def profile_path(self) -> Path:
        return self.root / "profile.json"

    def load_profile(self) -> Profile:
        if not self.profile_path.exists():
            return Profile()
        return Profile.from_dict(json.loads(self.profile_path.read_text("utf-8")))

    def save_profile(self, p: Profile) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_path.write_text(
            json.dumps(p.to_dict(), ensure_ascii=False, indent=2), "utf-8"
        )

    # ── 일별 레코드 ───────────────────────────────────────────
    def path_for(self, d: str) -> Path:
        return self.daily_dir / f"{d}.json"

    def load(self, d: str) -> DailyRecord | None:
        p = self.path_for(d)
        if not p.exists():
            return None
        return DailyRecord.from_dict(json.loads(p.read_text("utf-8")))

    def load_or_new(self, d: str) -> DailyRecord:
        return self.load(d) or DailyRecord(date=d)

    def save(self, rec: DailyRecord) -> Path:
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        p = self.path_for(rec.date)
        p.write_text(rec.to_json(), "utf-8")
        return p

    def upsert(self, rec: DailyRecord) -> Path | None:
        """부분 레코드를 기존 파일에 병합 저장. 수집 에이전트의 기본 쓰기 경로.

        내용이 하나도 없으면 파일을 만들지 않는다. 빈 파일은 `status` 의
        연속 기록일을 부풀려 게이트를 거짓으로 통과시킨다.
        """
        existing = self.load(rec.date)
        merged = existing.merge(rec) if existing else rec
        if merged.is_empty():
            return None
        return self.save(merged)

    def history(self, end: str | None = None, days: int = 28) -> list[DailyRecord]:
        """end(포함) 이전 days 일의 레코드를 날짜 오름차순으로 반환. 없는 날은 건너뜀."""
        end_d = date.fromisoformat(end) if end else date.today()
        out: list[DailyRecord] = []
        for i in range(days - 1, -1, -1):
            d = (end_d - timedelta(days=i)).isoformat()
            rec = self.load(d)
            if rec:
                out.append(rec)
        return out

    def all_dates(self) -> list[str]:
        if not self.daily_dir.exists():
            return []
        return sorted(p.stem for p in self.daily_dir.glob("*.json"))

    def checkin_dates(self) -> list[str]:
        """사람이 직접 체크인한 날.

        웨어러블 적재는 하루 만에 수천 일을 채운다. 그걸 '연속 기록'으로
        세면 Phase 0 게이트가 거짓으로 통과한다 — 게이트가 보려는 것은
        웨어러블 보유가 아니라 **매일 기록하는 습관**이다.
        """
        out = []
        for d in self.all_dates():
            rec = self.load(d)
            if rec and "checkin" in rec.sources:
                out.append(d)
        return out

    # ── 이벤트 로그(감사 추적) ─────────────────────────────────
    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def log_event(self, kind: str, agent: str, payload: dict[str, Any]) -> None:
        """에이전트가 무엇을 언제 왜 했는지 남긴다.

        이 로그가 없으면 '개입이 효과가 있었나'를 나중에 절대 평가할 수 없다.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": kind,
            "agent": agent,
            **payload,
        }
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def events(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        if not self.events_path.exists():
            return iter(())
        def _gen() -> Iterator[dict[str, Any]]:
            with self.events_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    if kind is None or ev.get("kind") == kind:
                        yield ev
        return _gen()

    # ── 실험 ──────────────────────────────────────────────────
    @property
    def experiments_path(self) -> Path:
        return self.root / "experiments.json"

    def load_experiments(self) -> list[dict[str, Any]]:
        if not self.experiments_path.exists():
            return []
        return json.loads(self.experiments_path.read_text("utf-8"))

    def save_experiments(self, xs: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.experiments_path.write_text(
            json.dumps(xs, ensure_ascii=False, indent=2), "utf-8"
        )
