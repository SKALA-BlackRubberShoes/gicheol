# 투자 판단 에이전트

투자 판단 규칙은 `main/agents/investment`와 공통 그래프 State에 연결돼 있습니다.
기술·시장·경쟁의 100점 결과를 각각 30점으로 환산하고 팀 평가
10점을 더합니다. `upstream_score_90`은 세 평가의 합계,
`judge_score_10`은 투자 판단 에이전트의 팀 평가 점수이며 두 값을 합쳐
`total_score`를 만듭니다. 평가할 수 없는 영역의 배점은 재분배하지 않습니다.

## 실행

저장소 루트에서 실행합니다.

전체 그래프 의존성은 `python3 -m pip install -r main/requirements.txt`로 설치합니다.

```bash
python3 -m main.agents.investment.example
python3 -m main.scripts.judge_investment --company-id 17 --output /tmp/judgment.json
python3 -m main.scripts.judge_investment --company-id 17 --input /tmp/analyses.json --team-model openai:gpt-4.1 --recommend-min-score 80 --output /tmp/judgment.json --handoff-output /tmp/report-input.json
python3 -m unittest discover -s main/agents/investment/tests -v
```

`--input`은 선택한 기업의 `company_id`가 포함된 JSON State입니다. 미입력 시
CSV 조회 결과만으로 판단하므로 `hold`가 됩니다. `BaseRAG`의 정확 조회에는
Qdrant 색인이나 OpenAI API가 필요하지 않습니다.
`--team-model`을 지정하면 팀 전용 웹 검색으로 실제 공개 페이지의 본문을 수집하고 투자 판단 에이전트가 창업자·팀을 10점 만점으로 평가합니다. 검색 모델의 생성 요약은 원문 근거로 등록하지 않습니다. 인용 URL을 열 수 없거나 회사·팀 관련 본문을 확인할 수 없으면 미확인 항목에 기록합니다. `--env-file .env`로 API 키를 로드할 수 있습니다. 팀 모델을 지정하지 않으면 입력 State의 `team_rating`을 사용하며, 둘 다
없으면 팀 점수는 `0`입니다. `--recommend-min-score`로 기본 추천 총점
80점을 변경할 수 있습니다.
`--handoff-output`을 지정하면 추천 시 다음 에이전트에 전달할
`report_payload`만 별도 JSON으로 저장합니다. 보류 시에는 같은 파일에
`null`을 써서 이전 실행의 추천 자료가 남지 않게 합니다.

## 그래프 연결

```python
from main.graph import make_investment_node, route_after_investment

judge = make_investment_node(rag, model=team_llm, team_researcher=search_team)
builder.add_node("investment_judge", judge)
builder.add_conditional_edges(
    "investment_judge", route_after_investment,
    {"report": "report_agent", "hold": "hold_handler"},
)
```

`main.graph.InvestmentState`에 판단 입력과 결과 필드를 포함했고, 회사 선택
노드는 이전 회사의 결과를 초기화합니다. `make_investment_node`는
`technical_score`, `competitor_score`, `market_evaluation`의 100점 합계·인용 ID를
각각 `technology_analysis`, `competition_analysis`, `market_analysis`로 옮깁니다.
이미 명시한 세 `*_analysis`는 우선합니다. 시장의 `investment_score_25`는 사용하지
않습니다. 판단 배점은 시장 원점수 `/100`을 `/30`으로 환산합니다.
기술·경쟁 점수표가 `status=provisional`이면 `total`에 CSV 기재 기반 잠정 점수가 포함됩니다. 판단 어댑터는 이를 실제 입력 점수로 사용하면서 고객 성과·성능·특허 권리 등의 검증 필요 사항을 `missing_items`에 남깁니다. 원래 독립 근거 점수는 각 점수표의 `verified_score`에서 확인할 수 있습니다.

기존 세 분석의 `sources`에는 판단 에이전트가 요구하는 원문, 출처 유형,
기업 소유권이 모두 들어 있지 않습니다. 변환부는 이를 근거 원장으로 승격하지
않습니다. 검증한 `evidence_registry`와 `current_candidate.eligibility`가 있으면
판단 근거와 신뢰도 표시에 활용합니다. 누락만으로 추천을 막지는 않습니다.
팀 평가 모델이나 `team_rating`이 없으면 팀 점수는 0/10입니다.
중대한 위험은 담당 분석에서 `critical_risks`로 명시해야 합니다.
기존 분석의 상세 결과는 변환된 분석의 `breakdown`에 보존됩니다.
`Evidence` 필드는 `schemas.py`에 정의돼 있습니다.
팀 조사 함수는 `team_info`, `evidence`, `missing_items`, `conflicts`를 반환합니다.
기본 CLI와 전체 CSV 실행에서는 `TeamWebResearcher`가 이 함수를 제공합니다. 외부 페이지에서 가져온 원문 단락과 URL은 `evidence_registry`와 판단 결과의 팀 평가 근거에 저장됩니다. 근거가 없으면 팀 점수를 임의로 올리지 않습니다.

기본 추천 기준은 총점 80 이상, 기술 18·시장 19.5·경쟁 18·팀 0 이상입니다.
점수 부족·상충·확인된 부적격·중대한 미해결 위험은 `hold`로 분기합니다.
근거와 적격성 정보가 부족한 항목은 `missing_items`와 낮은 신뢰도로 표시합니다.
`invest`는 후속 투자 검토 추천이며 `report_payload`를 반환합니다.
`report_payload.source_outputs`에는 기술 요약·점수, 시장 평가, 경쟁 비교·점수의
원본 출력이 모두 들어갑니다. 보고서 작성 노드는 이 필드를 포함한
`state["report_payload"]`를 입력으로 읽으면 됩니다. 보류 시 동일한 원본은
`hold_payload.source_outputs`에 보존됩니다.
`report_payload`에는 `company_id`, 기업 CSV 원본인 `company_data`,
세 평가와 팀 점수를 포함한 `evaluation`, 검증 근거 원장도 함께 들어갑니다.
점수 기준을 넘더라도 자료 상충, 확인된 부적격 또는 중대한 미해결 위험이 있으면
`report_payload`는 `null`이고 `hold_payload`에 보류 사유와 원본 자료가 남습니다.
