# 지정 경쟁사 비교 사용법

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

경쟁사는 호출자가 `competitor_ids`에 CSV ID 문자열로 지정합니다. 자동 경쟁사 검색은 포함하지 않습니다.
중복 회사와 대상 회사 자신의 ID는 비교 대상으로 허용하지 않습니다.
노드는 `competitor_comparison`, `competitor_score` 갱신값만 반환하고 입력 State는 수정하지 않습니다.
`model=`에는 모델 식별자 또는 채팅 모델 객체를 전달합니다.

직접 호출은 `from main.agents.competition import run_agent`로 가져와
`run_agent({"company": "대상 기업명", "competitors": ["경쟁사 기업명"]}, rag, model=llm)`을 사용합니다.
기본 근거는 이름의 CSV 정확 조회입니다. 추가 근거는 `search_company`, `evidence_adapter`로 전달합니다.
직접 호출은 전달한 딕셔너리를 갱신합니다.

회사 한 행은 `CSV-{company_id}` 출처 하나입니다. 제공한 근거가 없으면 직접 성능 우위나 법적 위험을 확정하지 않습니다.
점수는 네 항목의 0~5점에 각각 5를 곱합니다. 근거가 부족한 항목은 `rating=0`, `points=0`으로 처리하고 합계에도 0점을 반영합니다. 이때 `status=insufficient_evidence`로 근거 부족을 표시합니다.

기술·경쟁 결과를 함께 저장하는 명령:

```bash
python -m main.scripts.run_agents --company-id 17 --competitor-id 14 --output outputs/company_17_vs_14.json
```

`--competitor-id`를 반복해 여러 회사를 지정할 수 있습니다.
`examples/reports/company_17_vs_14.json`은 구조 정리 전 생성된 참고 결과이며 현재 코드의 검증 결과가 아닙니다.
