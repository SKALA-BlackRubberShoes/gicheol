# 기술 요약·경쟁사 비교 노드

`main/agents`는 선택된 기업 ID를 공용 `BaseRAG`의 CSV 행에 연결합니다. 조립부가 `BaseRAG` 객체를 한 번 만들고 두 노드에 전달합니다. `company_id`와 `competitor_ids`는 CSV의 ID 문자열입니다.

```python
from main.baseRAG import BaseRAG
from main.agents import make_technology_node, make_comparison_node

rag = BaseRAG()
try:
    technology_node = make_technology_node(rag)
    comparison_node = make_comparison_node(rag)

    state = {"company_id": "17", "competitor_ids": ["14"], "finance_data": {}}
    state.update(technology_node(state))
    state.update(comparison_node(state))
    print(state["technology_summary"])
    print(state["competitor_comparison"])
finally:
    rag.close()
```

LangGraph에서는 두 반환 함수를 그대로 노드로 등록합니다. 각 노드는 각각 `technology_summary`, `competitor_comparison` 키만 반환합니다. 입력 `state`를 직접 수정하지 않습니다. `company` 또는 `company_name`도 State에 있다면 `company_id`가 가리키는 이름과 일치해야 합니다.

```python
graph.add_node("technology", make_technology_node(rag))
graph.add_node("comparison", make_comparison_node(rag))
```

`main/baseRAG`가 있는 팀 프로젝트 루트에 이 폴더의 `main/agents`, `run_agents.py`, `requirements-agents.txt`를 같은 상대 경로로 넣습니다. 그 프로젝트 루트에서 기존 BaseRAG 의존성과 에이전트 의존성을 설치합니다.

```bash
python -m pip install -r main/baseRAG/requirements-rag.txt -r requirements-agents.txt
```

두 결과를 JSON 파일로 저장하려면 프로젝트 루트에서 실행합니다.

```bash
python run_agents.py --company-id 17 --competitor-id 14 --output outputs/agent_results/company_17_vs_14.json
```

한 회사를 더 비교하려면 `--competitor-id`를 반복합니다. 두 에이전트의 결과 검증이 모두 끝나야 파일이 저장됩니다.

기본 생성 모델은 `openai:gpt-4.1`이며 실제 실행에는 해당 모델의 접근 권한과 `OPENAI_API_KEY`가 필요합니다. 다른 LangChain 모델 이름은 두 `make_*_node`의 `model=` 인수로 전달할 수 있습니다. `BaseRAG.get_company()`로 이미 선택한 행을 정확 조회하므로 이 노드에는 Qdrant 색인과 `build_index()`가 필요하지 않습니다. 앞선 기업 선정 노드에서 `retrieve()`를 쓴다면 그 노드에서는 기존 `BaseRAG` 문서대로 색인을 먼저 준비해야 합니다.

각 기업의 CSV 한 행은 `CSV-{company_id}` 근거 하나로 변환됩니다. `sources`의 `locator`는 CSV 경로와 레코드 번호입니다. 자료의 실제·가상 여부가 확인되지 않아 `data_label`은 출처 미확인으로 표시합니다. 이 CSV에 고객 증언, 시험 조건, 모델 학습 방식 또는 독립적인 성능 비교가 없다면 그런 결론은 확인 불가로 두어야 합니다. 결과 문장이 원문에서 실제로 뒷받침되는지는 사용자가 검토해야 합니다.

기존 `run_agent()`는 회사명과 검색 결과 목록을 전달하는 고급 연결 지점으로 남겨 두었습니다. 팀의 선택된 회사 흐름에는 위 노드 함수를 사용합니다.
