"""로컬 웹 UI 서버.

왜 로컬인가
----------
건강 데이터는 개인정보보호법상 민감정보다. 어딘가에 호스팅하는 순간
전송·저장·인증·유출 책임이 전부 생긴다. 이 서버는 **127.0.0.1 에만 바인딩**해서
데이터가 이 기계를 떠나지 않게 한다. 오프라인에서도 돌고, 계정도 없다.

왜 웹인가
--------
CLI 체크인은 Enter 를 두 번 누르면 입력이 씹힌다. 매일 해야 하는 일에서
그런 마찰은 곧 기록 중단이다. 리커트 항목을 버튼 한 번으로 받으면
Enter 를 쓸 일이 아예 없어진다.

표준 라이브러리만 쓴다. 의존성을 추가하면 6개월 뒤 안 돌아간다.
"""

from __future__ import annotations

import json
import socket
import threading
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import baseline as bl
from . import checkin as ck
from . import readiness as rd
from . import triage as tg
from .schema import DailyRecord
from .store import Store

UI_DIR = Path(__file__).resolve().parent / "webui"

# 브라우저가 보낼 수 있는 요청 크기 상한. 로컬이라도 무한정 읽지 않는다.
MAX_BODY = 256 * 1024


def _today() -> str:
    return date.today().isoformat()


class HealthHandler(BaseHTTPRequestHandler):
    store: Store                      # serve() 에서 주입

    server_version = "health/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # 기본 로그는 요청마다 stderr 를 채운다. 조용히 둔다.
        pass

    # ── 응답 헬퍼 ────────────────────────────────────────────
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 로컬 전용이므로 다른 출처에서 부르지 못하게 막는다.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    # ── 라우팅 ───────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                html = (UI_DIR / "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if url.path == "/api/today":
                return self._json(self.api_today())
            if url.path == "/api/brief":
                return self._json(self.api_brief(query.get("date", [_today()])[0]))
            if url.path == "/api/trend":
                days = min(365, max(7, int(query.get("days", ["30"])[0])))
                return self._json(self.api_trend(days))
            return self._error(404, "없는 경로입니다")
        except FileNotFoundError:
            self._error(500, "UI 파일을 찾을 수 없습니다")
        except Exception as e:                       # 서버가 죽으면 체크인도 죽는다
            self._error(500, f"{type(e).__name__}: {e}")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._error(413, "요청이 너무 큽니다")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/checkin":
                return self._json(self.api_save(payload))
            return self._error(404, "없는 경로입니다")
        except json.JSONDecodeError:
            self._error(400, "JSON 형식이 아닙니다")
        except Exception as e:
            self._error(500, f"{type(e).__name__}: {e}")

    # ── API 구현 ─────────────────────────────────────────────
    def api_today(self) -> dict[str, Any]:
        """오늘 물어야 할 항목과 이미 채워진 항목."""
        d = _today()
        profile = self.store.load_profile()
        rec = self.store.load_or_new(d)
        y_date = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        yesterday = self.store.load_or_new(y_date)

        fields, measurements = [], []
        for key in profile.tracked():
            p = ck.PROMPTS.get(key)
            if p is None:
                continue
            # 섭취 항목은 어제 레코드를 본다 — 아침에 답할 수 있는 것도,
            # 그 값이 설명하는 수면도 어제 것이기 때문.
            about_yesterday = ck.asks_about_yesterday(key)
            src = yesterday if about_yesterday else rec
            filled = all(src.get_path(t) is not None for t in p.targets)
            current = None
            if filled:
                values = [src.get_path(t) for t in p.targets]
                current = "/".join(f"{v:g}" if isinstance(v, float) else str(v)
                                   for v in values)
            # 프롬프트 힌트는 CLI 와 공유한다. "(Enter=건너뜀)" 같은 터미널
            # 전용 문구는 웹에서 거짓말이 되므로 걷어낸다.
            hint = p.hint.replace("(Enter=건너뜀)", "").strip()
            item = {
                "key": key, "label": p.label, "kind": p.kind,
                "hint": hint, "unit": p.unit,
                "filled": filled, "current": current,
                "about": "yesterday" if about_yesterday else "today",
            }
            # 측정값은 이미 채워져 있어도 화면에 남겨둔다 — 다시 잰 값으로
            # 고칠 수 있어야 하기 때문. 질문은 채워졌으면 다시 묻지 않는다.
            (measurements if ck.is_measurement(key) else fields).append(item)

        # 웨어러블이 이미 채워준 것 — 다시 묻지 않지만 보여는 준다
        auto = []
        tracked_targets = {t for k in profile.tracked()
                           for t in ck.PROMPTS.get(k, ck.Prompt(k, "", "")).targets}
        for path, label, _, _ in bl.TRACKED:
            if path in tracked_targets:
                continue
            v = rec.get_path(path)
            if v is not None:
                auto.append({"label": label, "value": v})

        return {
            "date": d,
            "yesterday": y_date,
            "fields": fields,
            "measurements": measurements,
            "auto": auto,
            "sources": rec.sources,
            "profile_reviewed": bool(profile.reviewed_at),
        }

    def api_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """체크인 저장. 검증은 CLI 와 같은 코드를 쓴다."""
        d = payload.get("date") or _today()
        values: dict[str, Any] = payload.get("values") or {}

        rec = self.store.load_or_new(d)
        y_date = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        yesterday = self.store.load_or_new(y_date)
        saved, rejected = 0, []

        for key, raw in values.items():
            p = ck.PROMPTS.get(key)
            if p is None or raw in (None, ""):
                continue
            try:
                value = p.parse(str(raw)) if p.parse else (
                    int(raw) if p.kind == "likert" else str(raw))
            except (ValueError, TypeError) as e:
                rejected.append({"key": key, "reason": str(e) or "형식 오류"})
                continue

            bad = None
            parts = value if isinstance(value, tuple) else (value,)
            for target, part in zip(p.targets, parts):
                lo, hi = ck.RANGES.get(target, (float("-inf"), float("inf")))
                if isinstance(part, (int, float)) and not lo <= part <= hi:
                    bad = f"{lo:g}~{hi:g} 범위를 벗어났습니다"
            if bad:
                rejected.append({"key": key, "reason": bad})
                continue

            p.apply(yesterday if ck.asks_about_yesterday(key) else rec, value)
            saved += 1

        rec.sources = sorted(set(rec.sources) | {"checkin"})
        self.store.save(rec)
        if not yesterday.is_empty():
            # 섭취는 섭취한 날의 것이다. 어제 레코드에 'checkin' 표식을
            # 붙이지 않으므로 연속 체크인 계산도 흔들리지 않는다.
            self.store.upsert(yesterday)
        self.store.log_event("checkin", "web", {
            "date": d, "seconds": payload.get("seconds"), "filled": saved,
        })

        result = self.api_brief(d)
        result["saved"] = saved
        result["rejected"] = rejected
        return result

    def api_brief(self, d: str) -> dict[str, Any]:
        """준비도·트리아지·지표를 화면이 그릴 수 있는 형태로."""
        profile = self.store.load_profile()
        today = self.store.load(d)
        if today is None:
            return {"date": d, "empty": True}

        history = [r for r in self.store.history(end=d, days=29) if r.date != d]
        r = rd.compute(history, today, profile)
        t = tg.evaluate(history, today, profile)
        metrics = bl.compute(history, today)

        rows = []
        for path, label, direction, _ in bl.TRACKED:
            m = metrics.get(path)
            if not (m and m.latest is not None):
                continue
            excluded = path in rd.HRV_PATHS and r.hrv_excluded
            rows.append({
                "path": path, "label": label, "value": m.latest,
                "mean": m.mean, "sd": m.sd, "z": m.z,
                "deviation": "excluded" if excluded else m.deviation,
                "n": m.n,
            })

        return {
            "date": d,
            "readiness": {
                "score": r.score, "band": r.band, "advice": r.advice,
                "confidence": r.confidence, "flags": r.flags,
                "acwr": r.acwr, "sleep_debt_min": r.sleep_debt_min,
                "hrv_excluded": r.hrv_excluded,
                "contributors": [{"label": l, "z": z, "impact": i}
                                 for l, z, i in r.contributors],
            },
            "triage": {
                "severity": t.severity.name,
                "label": tg.LABEL[t.severity],
                "blocks_exercise": t.blocks_exercise,
                "findings": [{"severity": f.severity.name, "code": f.code,
                              "message": f.message, "action": f.action}
                             for f in sorted(t.findings, key=lambda f: -f.severity)],
            },
            "metrics": rows,
            "profile": {
                "conditions": profile.conditions,
                "medications": profile.medications,
                "med_classes": sorted(tg.med_classes(profile)),
                "unclassified": tg.unclassified_medications(profile),
                "contraindications": profile.contraindications,
                "goals": profile.goals,
                "clinician_note": profile.clinician_note,
            },
        }

    def api_trend(self, days: int) -> dict[str, Any]:
        """지표별 시계열 + 개인 베이스라인 밴드.

        밴드(평균±표준편차)를 함께 보내는 이유: 이 시스템의 모든 판단이
        '나의 평소 대비'이므로, 그 평소가 눈에 보여야 그래프가 의미를 갖는다.
        """
        end = date.today()
        start = end - timedelta(days=days - 1)
        dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        records = {r.date: r for r in self.store.history(end=end.isoformat(), days=days)}
        profile = self.store.load_profile()

        series = []
        for path, label, direction, _ in bl.TRACKED:
            values = [records[d].get_path(path) if d in records else None for d in dates]
            present = [v for v in values if isinstance(v, (int, float))]
            if len(present) < 3:
                continue
            m = bl.compute(list(records.values()))[path]
            series.append({
                "path": path, "label": label, "direction": direction,
                "values": values, "mean": m.mean, "sd": m.sd,
            })

        # 준비도는 지표가 아니라 파생값이라 따로 계산한다
        scores: list[float | None] = []
        for d in dates:
            rec = records.get(d)
            if rec is None:
                scores.append(None)
                continue
            prior = [r for r in records.values() if r.date < d]
            res = rd.compute(prior, rec, profile)
            scores.append(res.score if res.band != "UNKNOWN" else None)

        checkins = set(self.store.checkin_dates())
        return {
            "dates": dates,
            "readiness": scores,
            "series": series,
            "checkins": [d in checkins for d in dates],
        }


def find_port(preferred: int) -> int:
    """이미 쓰는 포트면 다음 빈 포트를 찾는다. 매일 쓰는 도구가
    '주소 사용 중'으로 죽으면 안 된다."""
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"{preferred}~{preferred + 19} 사이에 빈 포트가 없습니다")


def serve(store: Store, port: int = 8765, open_browser: bool = False) -> None:
    port = find_port(port)
    handler = type("Bound", (HealthHandler,), {"store": store})
    # 127.0.0.1 고정. 0.0.0.0 으로 열면 같은 네트워크의 누구나 건강기록을 본다.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"

    print(f"체크인 화면: {url}")
    print(f"데이터: {store.root}")
    print("이 서버는 이 컴퓨터에서만 접속됩니다. 종료하려면 Ctrl-C.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료했습니다.")
    finally:
        httpd.server_close()
