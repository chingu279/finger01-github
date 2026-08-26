---
name: weekly-review
description: 주간 건강 회고 — 지난 주 결산, 개입 효과 판정, 다음 주 계획과 시스템 개선안. 매주 1회 실행하며 "주간 리뷰", "이번 주 어땠어?" 요청에 사용.
---

# 주간 리뷰

이 리뷰가 시스템을 개선시킨다. 건너뛰면 시스템은 첫 주 수준에 영원히 머문다.

## 1. 팩트 수집
```bash
./health weekly
```

## 2. 지난 주 권고 확인
```bash
tail -50 data/events.jsonl
```
지난 주에 무엇을 권했는지 확인한다. 권고를 모르면 효과를 평가할 수 없다.

## 3. 진행 중인 실험 확인
```bash
python3 -c "import sys;sys.path.insert(0,'src');from health.store import Store;import json;print(json.dumps(Store().load_experiments(),ensure_ascii=False,indent=2))"
```
종료일이 지난 실험은 `experiment-designer` 에게 판정을 맡긴다.

## 4. 회고
`reflection-agent` 에게 위임한다. 3층 회고를 요구한다:
1. 건강 결과 — 숫자로
2. 개입 효과 — 효과 있었다 / 없었다 / 판정 불가(이행 안 됨)
3. 시스템 자체 — 결측 데이터, 무시되는 조언, 경보 피로, 트리아지 오탐/미탐

## 5. 다음 주 확정
- 실험 **1개** (experiment-designer)
- 집중 **1개** (가장 병목인 영역)
- 시스템 개선 **1개** (파일·변경·이유를 구체적으로)

3개 초과 금지.

## 6. 시스템 변경을 실제로 반영할 때
사용자 승인 후 코드를 고치고, **반드시 회귀 테스트를 함께 추가**한다:
```bash
python3 -m pytest tests -q
```
테스트 없는 안전 규칙 변경은 머지하지 않는다.
