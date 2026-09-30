# 기술 요약 사용법

프로젝트 루트에서 `python -m pip install -r main/agents/technology/requirements.txt`로 설치합니다.
`BaseRAG`는 호출자가 한 번 만들어 공유하고 앱 종료 시 닫습니다.

```python
from main.rag.company import BaseRAG
from main.graph import make_technology_node

rag = BaseRAG()
try:
    node = make_technology_node(rag, model="openai:gpt-4.1")
    updates = node({"company_id": "17"})
    print(updates["technology_summary"])
    print(updates["technical_score"])
finally:
    rag.close()
```

`model=`에는 LangChain 모델 식별자 또는 호출자가 만든 채팅 모델을 전달합니다.
문자열 모델을 생성할 때는 LangChain과 해당 제공자 설정이 필요합니다.
노드는 회사 ID만 읽고 `technology_summary`, `technical_score` 갱신값만 반환합니다.
CSV 정확 조회에는 Qdrant 색인이 필요하지 않습니다. LLM 실행에는 API 설정이 필요합니다.

회사명 기반 직접 호출은 `from main.agents.technology import run_agent`로 가져와
`run_agent({"company": "CSV의 기업명"}, rag, model=llm)`을 실행합니다.
기본 근거는 CSV 이름의 정확 조회 결과입니다. 추가 근거는 `search_company`와 `evidence_adapter`로 주입합니다.
직접 호출은 전달한 딕셔너리를 갱신하며, 그래프 노드는 입력 State를 수정하지 않습니다.

`schemas.py`는 결과 형식, `prompts.py`는 프롬프트, `scoring.py`는 기술 점수와 인용 자격을 담당합니다.
회사 소개 CSV는 독립적인 고객 증언·성능 검증 자료가 아닙니다. 필수 근거가 부족하면 해당 점수와 합계는 `null`이며 0점으로 해석하지 않습니다.
기술 점수는 네 항목의 1~5점에 각각 5를 곱해 100점 만점으로 계산합니다.
