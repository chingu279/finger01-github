---
name: daily-checkin
description: 오늘의 건강 체크인을 진행하고 브리핑을 받는다. 아침/저녁 루틴, "체크인", "오늘 기록", "오늘 어때?" 요청에 사용.
---

# 일일 체크인

## 1. 현재 상태 확인
```bash
python -m health brief
```
브리핑을 읽고 **이미 채워진 항목은 다시 묻지 않는다.**

## 2. 안전 우선
브리핑의 "1. 안전 판정"이 `긴급` 이상이면 **여기서 멈추고** `risk-triage` 에이전트로 넘긴다.
체크인 질문을 계속하지 않는다.

## 3. 부족한 것만 묻기
`checkin-interviewer` 에게 위임한다. 질문은 최대 3개.
기본: 활력 · 기분 · 스트레스 (각 1~5) + 자유 한 줄

## 4. 기록
```bash
python -m health log --source checkin \
  --set subjective.energy=<1-5> \
  --set subjective.mood=<1-5> \
  --set subjective.stress=<1-5> \
  --set 'subjective.note=<사용자 표현 그대로>'
```
증상 언급이 있으면 `--set 'subjective.symptoms=증상1,증상2'` 를 **사용자 표현 그대로** 추가한다.

## 5. 브리핑 전달
```bash
python -m health score && python -m health triage
```
`health-orchestrator` 형식으로 답한다:
- 상태 한 줄 (준비도 · 밴드 · 가장 큰 변화)
- 주의 (있을 때만)
- 오늘의 3가지 (무엇을, 얼마나, 언제)
- 내일을 위한 질문 하나

## 주의
- 숫자를 브리핑 출력에서만 인용한다. 계산하지 않는다.
- 3개를 넘기지 않는다.
- 나쁜 수치를 나무라지 않는다. 기록을 계속하게 만드는 것이 최우선이다.
