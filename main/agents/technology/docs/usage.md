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
기본 근거는 CSV 이름의 정확 조회 결과와 회사별 PDF 검색 결과입니다. 추가 근거는 `search_company`와 `evidence_adapter`로 주입합니다. `search_company`를 직접 지정하면 자동 PDF 조회를 생략합니다.
직접 호출은 전달한 딕셔너리를 갱신하며, 그래프 노드는 입력 State를 수정하지 않습니다.

`schemas.py`는 결과 형식, `prompts.py`는 프롬프트, `scoring.py`는 기술 점수와 인용 자격을 담당합니다.
회사 소개 CSV는 독립적인 고객 증언·성능 검증 자료가 아닙니다. LLM이 필수 근거 조건 평가 `criterion_scores`, PDF 문서 평가 `rag_assessment_scores`, CSV 해석 `csv_assessment_scores`를 구분해 판단합니다. 필수 조건 점수가 있으면 우선 사용하고, 나머지는 PDF 판단, CSV 판단 순서로 보완합니다. 근거가 없는 항목은 0점입니다. 근거가 있는 PDF 0점은 더 높은 CSV 점수로 대체하지 않습니다.
모델이 근거 ID 없는 성능 항목을 반환하면 결과와 점수에서 제외하고 `key_unknowns`에 기록합니다.
기술 점수는 네 항목의 0~5점에 각각 5를 곱해 100점 만점으로 계산합니다.

CSV 해석 점수는 제품·서비스의 적용 대상, AI 역할의 구체성, 성능 자료, 제품 단계를 모델이 종합해 항목별 0~5점으로 정합니다. 코드에는 `개발중=1점`처럼 CSV 값과 점수를 직접 연결하는 규칙이 없습니다. 양수 점수에는 실제 CSV 컬럼명(`source_fields`)과 출처 ID가 필요하고, 없는 컬럼을 인용하면 오류로 처리합니다. 성능 지표·결과·시험 조건이 모두 없으면 성능 점수는 부여하지 않습니다.

잠정 점수가 들어간 결과는 `status=provisional`, 항목별 `basis=llm_pdf_assessment` 또는 `llm_csv_assessment`로 표시합니다. 기존 필수 조건 점수는 `verified_score`에 보존하며 독립기관 인증을 뜻하지 않습니다. 투자 판단 어댑터는 `total`과 미확인 사항을 함께 받습니다. 기본 경로는 CSV 회사명이 유일해야 합니다.

PDF 검색은 고객 문제·AI 역할·성능·성숙도별로 수행합니다. 결과의 `rag_retrieval`에서 검색어와 전달 문서를, `sources`에서 페이지와 원출처를 확인합니다. 제품 소개의 조건부 시간 주장과 실제 측정을 구분하며 `measurement_basis=controlled_experiment/field_measurement`만 성능 점수 후보가 됩니다. 최초 실행에는 임베딩 API가 필요하며 문서 벡터는 로컬에 캐시됩니다. 자세한 흐름은 [PDF RAG 사용법](../../../rag/company_pdf/README.md)을 참고하세요.
