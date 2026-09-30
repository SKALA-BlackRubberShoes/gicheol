# 기술·경쟁 에이전트 PDF RAG

`CompanyPDFRAG`는 `docs/data/technology_competition/processed/`의 PDF 텍스트를 임베딩하고 회사별로 검색한다. 기술·경쟁 에이전트의 직접 호출과 그래프 노드에 기본 연결되어 있다. 전체 그래프와 CSV 순위 평가도 같은 노드를 사용하므로 자동 적용된다.

## 실행

저장소 루트에서 기존 프로젝트 의존성을 설치하고 실행한다.

```bash
python -m main.scripts.run_agents \
  --company-id 17 --competitor-id 14 --env-file .env \
  --output outputs/company_17_vs_14_pdf_rag_live.json
```

최초 실행은 PDF 청크를 `text-embedding-3-small` 512차원으로 임베딩한다. 검색과 평가에는 실제 OpenAI API 호출이 필요하다. 기존 `OPENAI_API_KEY`를 사용하며 `.env`를 출력하거나 결과 파일에 저장하지 않는다.

벡터는 `outputs/rag/company_pdf_index.json`에 저장한다. 이후 문서·출처 metadata·임베딩 설정의 해시가 같으면 문서 임베딩을 재사용한다. 질문 임베딩은 프로세스 내에서 재사용한다. 이 소규모 인덱스는 로컬 코사인 검색이므로 별도의 Qdrant 서버를 요구하지 않는다. 기존 기업 의미 검색과 시장 RAG의 Qdrant 설정은 별개다.

## 검색 방식

| 에이전트 | 검색 항목 |
|---|---|
| 기술 | 고객 문제·제품 적용, AI 모델·학습·추론, 성능 시험, 개발·배포 단계 |
| 경쟁 | 제품 차별성, 동등 조건 성능, 기술·데이터 방어력, 도입 위험 |

- CSV 정확 조회 결과에 해당 회사의 PDF만 더한다. `company_ids`와 `company_names`가 모두 일치해야 한다.
- 평가 항목마다 관련 페이지 2개를 검색해 중복을 제거한다. 회사 자료가 4쪽 이하면 전체를 함께 전달한다.
- 청크로 검색하되 주변 시험 조건을 잃지 않도록 페이지 본문을 전달한다. 긴 페이지는 검색 지점 주변 최대 7,000자를 전달한다.
- 일반 기술 논문과 투자 동향 보고서는 기업 기술·경쟁 점수 근거에서 제외한다. 검색 유사도는 투자 점수로 환산하지 않는다.
- 해당 회사 PDF가 없으면 `company_pdf_available=false`로 남기고 기존 CSV 평가를 사용한다. API 오류나 잘못된 색인은 자료 없음으로 숨기지 않고 오류로 알린다.
- 현재 회사별 PDF는 카본식스(17), 리얼월드(14)에 한정된다. 다른 회사를 평가하려면 corpus에 해당 회사 자료와 이름 metadata를 추가하고 `prepare_corpus.py`로 다시 추출한다.

## 점수와 출처

LLM이 항목별 0~5점, 판단 이유, 근거 ID, 한계를 제출한다. 코드가 인용 ID·회사 소유권·성능 자료 자격을 검사하고 각 항목에 5를 곱해 합산한다. 회사별 고정 점수나 최소 점수는 없다.

1. 기존 필수 근거 조건을 충족한 점수가 있으면 우선 사용한다(`basis=qualified_evidence`).
2. 나머지는 PDF 내용에 따른 문서 평가를 사용한다(`basis=llm_pdf_assessment`). 문서가 뒷받침하는 0점도 CSV의 더 높은 점수보다 우선한다.
3. PDF 판단 근거가 없는 항목만 CSV 해석 점수로 보완한다(`basis=llm_csv_assessment`).

PDF 문서 평가의 차별성은 제품·대상 작업·구현 방식의 차이를 평가한다. 직접 성능 우위를 뜻하지 않는다. 성능 항목은 이 경로로도 필수 조건을 우회할 수 없다. 특히 제품 소개의 '하루 이내 모델 생성 가능' 같은 주장은 측정 실험으로 처리하지 않는다. 기술 보고서의 실제 실험과 단순 홍보 주장을 `measurement_basis`로 구분한다.

`verified_score`는 호환성을 위해 유지한 기존 필수 근거 점수다. 독립 검증기관 인증을 의미하지 않으며 저자 자체 실험일 수 있다. 문서 해석 점수가 포함되면 `status=provisional`이다. 기술·경쟁 자료에서 확인되지 않은 부분은 계속 0점일 수 있다.

각 결과의 `rag_retrieval`에는 회사, 항목별 검색어, 전달한 PDF ID가 있다. `sources`는 실제 인용된 문서 ID·페이지·URL·자료 유형·요약 여부를 담는다. `rag_assessment_scores`에는 문서 기반 판단과 한계가 남고, 최종 점수는 `technical_score.total` / `competitor_score.total`이다.

## 테스트와 주입

```bash
python -m unittest discover -s main/agents/common/tests -v
python -m unittest discover -s main/agents/technology/tests -v
```

테스트에서는 가상 임베더·모델·PDF 검색기를 사용해 외부 API 호출 없이 회사 분리, 캐시 갱신, 배선, 근거 검증, 성능 조건을 검사한다. 실제 점수 검증은 위 실행 명령으로 별도로 수행한다.

`run_agent(..., pdf_rag=custom)` 또는 `make_technology_node(..., pdf_rag=custom)` / `make_comparison_node(..., pdf_rag=custom)`으로 검색기를 바꿀 수 있다. 직접 호출에서 기존 `search_company`를 명시하면 그 근거 주입 경로를 우선하며 자동 PDF 조회를 생략한다.
