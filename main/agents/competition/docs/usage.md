# 경쟁사 선정·비교 사용법

프로젝트 루트에서 `python -m pip install -r main/agents/competition/requirements.txt`로 설치합니다.

```python
from main.rag.company import BaseRAG
from main.graph import make_comparison_node

rag = BaseRAG()
try:
    node = make_comparison_node(rag, model="openai:gpt-4.1")
    updates = node({"company_id": "17", "competitor_ids": ["14"]})
    print(updates["competitor_comparison"])
    print(updates["competitor_score"])
finally:
    rag.close()
```

비교 노드는 호출자가 `competitor_ids`에 지정한 CSV ID 문자열을 받습니다. 자동 선정은 `main/agents/competitor_selection/`의 별도 `CompetitorSelectionAgent`가 담당하며 기존 비교·평가 로직은 그대로 사용합니다.
중복 회사와 대상 회사 자신의 ID는 비교 대상으로 허용하지 않습니다.
노드는 `competitor_comparison`, `competitor_score` 갱신값만 반환하고 입력 State는 수정하지 않습니다.
`model=`에는 모델 식별자 또는 채팅 모델 객체를 전달합니다.

직접 호출은 `from main.agents.competition import run_agent`로 가져와
`run_agent({"company": "대상 기업명", "competitors": ["경쟁사 기업명"]}, rag, model=llm)`을 사용합니다.
기본 근거는 이름의 CSV 정확 조회와 대상·경쟁사 각각의 PDF 검색 결과입니다. 추가 근거는 `search_company`, `evidence_adapter`로 전달합니다. `search_company`를 명시하면 자동 PDF 조회를 생략합니다.
직접 호출은 전달한 딕셔너리를 갱신합니다.

`condition_checks`의 여섯 항목(`task`, `metric`, `protocol`, `environment`,
`configuration`, `stage`)이 누락되거나 중복되면, 해당 경쟁사와 잘못된 항목을 알려
동일한 기업 근거로 LLM 교정을 한 번만 재요청합니다. 자료가 없는 항목은 `unverified`로
작성하도록 요청하며 코드가 임의로 판정이나 점수를 채우지 않습니다. 교정 응답에도
기존 기업·출처·점수 검증을 모두 적용합니다. 다시 누락·중복되면 구체적인 오류로
종료하고, 다른 검증 오류나 API 오류는 이 교정 재시도의 대상이 아닙니다.

회사 한 행은 `CSV-{company_id}` 출처 하나입니다. 제공한 근거가 없으면 직접 성능 우위나 법적 위험을 확정하지 않습니다.
점수는 네 항목의 0~5점에 각각 5를 곱합니다. LLM은 필수 근거 조건 평가 `criterion_scores`, 문서 평가 `rag_assessment_scores`, CSV 해석 `csv_assessment_scores`를 구분합니다. 필수 조건 점수가 있으면 우선 사용하고 나머지는 PDF, CSV 순서로 보완합니다. PDF가 뒷받침하는 0점을 더 높은 CSV 점수로 바꾸지 않습니다.

CSV 해석 점수는 대상·경쟁사의 제품과 서비스 설명, 특허 기재, 제품 단계 및 자료의 빈틈을 모델이 종합해 항목별 0~5점으로 정합니다. 코드에는 `특허 3건 이상=2점` 같은 수치별 고정 점수가 없습니다. 양수 점수에는 실제 CSV 컬럼명(`source_fields`)과 출처 ID가 필요합니다. 차별성·비교 성능은 양쪽 회사의 근거가 필요하고, 동일 조건 성능 자료가 없으면 비교 성능 점수는 부여하지 않습니다. 특허 수만으로 권리의 질이나 경쟁 우위를 확정하지 않습니다.

현재 CSV의 17번과 14번은 설명이 비슷하지만 PDF에는 제품과 구현 내용이 더 있습니다. 문서 차별성은 제품·고객 작업·구현 차이를 평가하며 직접 성능 우위를 의미하지 않습니다. 동일 조건 성능 자료가 없으면 `comparable_performance`는 계속 0점입니다. 잠정 결과는 `status=provisional`, 항목별 `basis=llm_pdf_assessment` 또는 `llm_csv_assessment`로 표시합니다. `verified_score`는 기존 필수 조건 점수이며 독립기관 인증을 의미하지 않습니다. 투자 판단에는 `total`과 미확인 사항을 전달합니다.

`rag_retrieval`에 양사별 검색 기록을, `sources`에 실제 인용 PDF의 페이지·URL을 남깁니다. 일반 기술 논문은 두 회사 실적으로 사용하지 않습니다. 자세한 검색·캐시·점수 연결은 [PDF RAG 사용법](../../../rag/company_pdf/README.md)을 참고하세요.

기술·경쟁 결과를 함께 저장하는 명령:

```bash
python -m main.scripts.run_agents --company-id 17 --competitor-id 14 --env-file .env --output outputs/company_17_vs_14_pdf_rag_live.json
```

`--competitor-id`를 반복해 여러 회사를 지정할 수 있습니다.
`examples/reports/company_17_vs_14.json`은 구조 정리 전 생성된 참고 결과이며 현재 코드의 검증 결과가 아닙니다.

## 경쟁사·비교 대상 자동 선정

선정 에이전트는 그래프 State 대신 회사 ID를 받으므로 독립 호출할 수 있습니다.

```python
from main.agents.competitor_selection import CompetitorSelectionAgent
from main.rag.company import BaseRAG

rag = BaseRAG()
try:
    rag.build_index()
    selection = CompetitorSelectionAgent(rag, model="openai:gpt-4.1").select("17")
    print(selection["competitor_ids"])
    print(selection["message"])
finally:
    rag.close()
```

같은 `sector`의 유사 기업 후보 최대 10개를 검색하고, 모델이 직접 경쟁사·대체재를 최대 3개 고릅니다. 직접 경쟁 관계가 불명확하면 동종 업종 기업 하나를 비교 대상으로 사용합니다. 동종 업종 기업이 없으면 업력 차이가 가장 작은 다른 회사 하나를 선택하며, 차이가 같으면 문자열 `company_id` 순으로 결정합니다. 업력 비교 대상은 동종 업종 경쟁사를 뜻하지 않으며 `message`에 선택 이유를 표시합니다.

`main/graph/nodes.py`의 `make_competitor_selection_node(agent)`는 `state["company_id"]`를 `agent.select()`에 전달하는 어댑터입니다. `main/graph/workflow.py`는 선정 로직 없이 실행 순서·분기·병렬 합류를 구성합니다. 전체 터미널 실행은 저장소 루트에서 `python -m main.scripts.run_graph`를 사용합니다.
