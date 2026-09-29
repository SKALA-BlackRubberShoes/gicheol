# main/baseRAG/baseRAG.py 구현 계획

상태: 2026-09-29 구현·코드 검토·전체 테스트 60개 및 실제 OpenAI 평가 완료. 설치·연동 방법과 평가 결과는 [사용 안내](../../../main/baseRAG/docs/rag-usage.md)를 따른다. 테스트는 기능별로 `test_base_rag.py`, `test_embeddings.py`, `test_base_rag_qdrant.py`, `test_docker_persistence.py`에 나누었다. 아래 테스트 이름·단계 순서는 설계 당시의 작업 분류다.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 팀원이 import하는 공용 CSV 검색·조회 모듈을 OpenAI text-embedding-3-small과 로컬 Docker Qdrant 서버로 구현한다. 기업 선택 노드는 별도 작업이며 State에는 회사 식별 정보로 company_id만 전달한다.

**Architecture:** CSV 한 행을 회사 한 문서로 변환하고 OpenAI API에서 512차원 벡터를 생성한다. Qdrant는 별도 Docker 컨테이너와 named volume으로 실행한다. 고정 컬렉션 companies_small_512가 있으면 차원만 확인해 재사용하고 CSV 변경은 build_index(rebuild=True)로 반영한다. 정확한 ID·숫자 조회는 검증한 CSV 원본에서 처리한다.

**Tech Stack:** Python 3.11 이상, csv/Decimal/uuid/threading/typing 표준 라이브러리, Pydantic, openai, tiktoken, qdrant-client, Docker Compose. 테스트는 pytest·가짜 임베더 및 실제 Docker Qdrant 통합 검증. 생성 LLM·LangChain·LangGraph는 이 모듈의 필수 의존성이 아니다.

**Spec:** [공통 기업 RAG 모듈 설계](../specs/2026-09-29-company-rag-design.md)

**Reference:** [market_rag.py](https://github.com/Eye-Reading/rag-agent/blob/main/main/searchCorp/agents/rag/market_rag.py). 클래스·retrieve·doc_count와 Qdrant 서버 연결 구조를 참고한다. PDF 적재를 CSV로, 로컬 BGE-M3/KoE5 임베딩을 OpenAI API로 변경한다.

## Global Constraints

- 임베딩은 text-embedding-3-small, dimensions=512이며 문서와 질문에 동일 설정을 사용한다. E5 접두사는 사용하지 않는다.
- Qdrant는 qdrant/qdrant:v1.19.1, 포트 127.0.0.1:6333:6333, named volume qdrant_data:/qdrant/storage로 구성한다. 호스트 Python 기본 URL은 http://localhost:6333이다.
- 기본 API 공개 기능은 get_company, get_field, get_fields, list_companies, build_index, retrieve, doc_count, close다. select_company·기업 평가·웹 수집은 구현하지 않는다.
- company_id는 CSV의 문자열 값을 유지한다. 빈 값·중복은 오류이며 행 번호로 재발급하지 않는다. 회사의 전체 데이터나 선정 결과를 State에 넣도록 요구하지 않는다.
- 기본 CSV는 docs/data/base/raw/thevc_startups_30_updated.csv이며 모듈 위치를 기준으로 해석한다. 다른 CSV 경로를 인수로 지정할 수 있다. 기존 데이터 파일은 수정하지 않는다. 원문과 정규화 값을 함께 보존한다.
- import와 생성자에서 API나 Qdrant를 호출하지 않는다. get_company/get_field/get_fields/list_companies는 OpenAI 키·Qdrant 없이 동작한다. 항목 조회는 요청한 값만 반환하고 정규화 값/원문, 회사 없음/항목 없음/결측을 구분한다.
- 한 준비 프로세스가 색인을 생성하고 완료 후 에이전트가 사용한다. 재생성은 그래프를 멈추고 수행한다. 여러 프로세스의 동시 색인 생성은 첫 버전 범위에서 제외한다.
- 컬렉션 이름은 companies_small_512로 고정한다. build_index(rebuild=False)는 존재·차원만 확인해 재사용하고, rebuild=True는 CSV를 다시 읽어 같은 컬렉션을 전체 재생성한다. 파일 해시·본문 버전 관리·전체 데이터 비교는 구현하지 않는다.
- close는 클라이언트만 정리하고 Qdrant 데이터·서버를 유지한다. 기본 실행 구성에 메모리 모드나 서버 장애 시 자동 대체를 넣지 않는다.
- main/baseRAG/requirements-rag.txt와 main/baseRAG/docs/rag-usage.md는 팀원의 설치·사용을 위해 Git에 남긴다. tests/ 전체는 /tests/ 규칙으로 제외하고 로컬에서 검증하며 강제 추가하지 않는다. 런타임 의존성과 개발 도구 pytest를 구분한다.
- 테스트 파일은 사용자 요청에 따라 로컬에만 유지한다. 구현을 사용자 지정 경로 /Users/skala/workspace/skalaDay48-50/blackrubbershoes로 옮겼으며 CSV는 변경하지 않았다. 이동 당시 이 폴더에는 .git이 없었다.

## Review Focus

- 행 재정렬·삭제·동일 본문 기업이 company_id를 바꾸면 State가 다른 회사를 가리킨다. Task 1에서 ID 안정성과 정확 조회를 검증한다.
- 기존 컬렉션의 차원이 다르면 검색할 수 없다. Task 3에서 차원 불일치를 ValueError로 알리고 명시적 재생성을 안내한다. CSV 변경 자동 검증은 제외한다.
- Docker 재시작 후 재임베딩하거나 close에서 인덱스를 지우면 공용 서버의 이점을 잃는다. Task 3과 5에서 영속화·API 재호출 0회를 검증한다.
- OpenAI 응답 순서·차원 오류와 API/서버 장애를 후보 없음으로 숨기면 잘못된 판단으로 이어진다. Task 2와 3에서 응답/실패 계약을 검증한다.
- 재생성 실패 후 준비 완료로 표시하면 잘못된 검색이 생긴다. Task 3에서 실패 시 준비 상태 해제와 재실행 안내를 검증한다. 미완성 컬렉션 자동 감지·복구는 구현하지 않는다.

## 선택 근거

Qdrant는 벡터와 회사 payload/필터, 여러 프로세스의 서버 접근, Docker 볼륨 영속화를 제공하며 참고 코드와 연결하기 좋다. 30행에서 검색 속도나 품질이 다른 DB보다 낫다고 전제하지 않는다. Chroma도 서버/필터 기능을 제공한다. FAISS는 공용 서비스·메타데이터 계층을 더 구성해야 하며, pgvector는 이미 PostgreSQL을 쓰는 경우 특히 유리하다. 자세한 비교와 공식 출처는 설계 문서를 따른다.

512차원은 작은 출력으로 시작하는 초기 평가 설정이다. 기본 1,536차원으로 자동 전환하지 않는다. API 단가는 차원 축소만으로 내려가지 않는다. CacheBackedEmbeddings는 첫 버전에 추가하지 않고, 기존 컬렉션의 벡터를 재사용한다.

## 현재 데이터 검사 — 완료, 모듈 구현 테스트와 별개

2026-09-29 `docs/data/base/raw/thevc_startups_30_updated.csv`를 읽어 30행·20컬럼·고유 ID 30개·빈 ID 0개·중복 ID 0개를 확인했다. 현재 ID는 문자열 1~30이며 카본식스는 17이다. 매출 컬럼은 없다.

기존 transform.mjs의 초기 ID 부여는 index+1이다. 기존 CSV의 순서를 바꿔 변환 스크립트로 ID를 재발급하면 안정성이 깨질 수 있다. RAG 로더는 ID를 절대 재발급하지 않는다. 신규 회사 ID 발급과 과거 ID 재사용 방지는 데이터 준비 단계의 별도 책임이다. 기존 CSV는 수정하지 않는다.

## 생성할 파일

| 파일 | 책임 |
| --- | --- |
| main/baseRAG/baseRAG.py | CSV/ID 검증, 타입·조회, OpenAI 임베딩, Qdrant 연결·검색·재사용 |
| compose.qdrant.yml | 로컬 Qdrant 서비스·포트·영속 볼륨 정의 |
| tests/test_base_rag.py | 로컬 검증용 단위 테스트; Git 제외 |
| tests/test_base_rag_qdrant.py | 로컬 Docker 통합 검증용 테스트; Git 제외 |
| main/baseRAG/requirements-rag.txt | 팀원 설치용 런타임 의존성·검증 버전; Git에 포함 |
| main/baseRAG/docs/rag-usage.md | 설치·시작/종료, 환경변수, import/State 예제, 데이터 갱신, 평가 결과; Git에 포함 |
| .env.example | 실제 값 없는 OPENAI_API_KEY 설정 예시 |
| .gitignore | /tests/, .env, Python/테스트 캐시 제외; main/baseRAG/requirements-rag.txt, main/baseRAG/docs/rag-usage.md, .env.example은 추적 |

모듈은 .env를 자동 로드하지 않는다. 애플리케이션 환경에서 OPENAI_API_KEY를 설정한다. Qdrant URL은 생성자 인수로 전달한다. Compose의 .env 해석과 Python 환경변수 로드를 혼동하지 않도록 사용 문서에 설명한다. 별도 앱 API 서버·CLI·기업 선택/평가 에이전트 파일은 만들지 않는다.

## Task 1: CSV 고유 ID와 조회 계약

**Files:** Create .gitignore, main/baseRAG/baseRAG.py, tests/test_base_rag.py, main/baseRAG/requirements-rag.txt.

**Interfaces:** 설계의 SourceRef, CompanyRecord, CompanyFilter, SearchHit, EmbeddingBackend, BaseRAG 생성자와 get_company/get_field/get_fields/list_companies. get_field(company_id, field, *, raw=False)는 값 하나, get_fields(company_id, fields, *, raw=False)는 요청한 키만 포함한 dict다. 오류는 RAGDataError, RAGCompanyNotFoundError, RAGUnknownFieldError, RAGProviderError, RAGStoreError, RAGIndexNotReady, RAGClosedError. 기본 생성자 설정은 model_name=text-embedding-3-small, dimensions=512, qdrant_url=http://localhost:6333, embeddings=None다.

- [x] **Step 0 — Git 제외 설정.** 테스트 파일 생성 전에 .gitignore에 /tests/, .env, __pycache__/, .pytest_cache/를 추가한다. main/baseRAG/requirements-rag.txt와 main/baseRAG/docs/rag-usage.md는 제외하지 않는다. 생성 후 git check-ignore로 테스트만 제외되는지 확인한다. 테스트는 로컬에 유지하고 실행한다.
- [x] **Step 1 — 데이터 실패 테스트.** `test_load_and_normalize`에서 BOM·20컬럼·원문 보존 및 1550억원→155_000_000_000, +9→9, 2.6년차→2.6, NULL→None을 검증한다. 잘못된 금액/날짜·누락 컬럼은 레코드·필드가 있는 RAGDataError다. 값이 없는 특허를 0으로 바꾸지 않는다.
- [x] **Step 2 — ID 실패 테스트.** `test_rejects_blank_or_duplicate_ids`에 빈 문자열·NULL·앞뒤 공백 제거 후 중복을 넣는다. `test_ids_survive_reorder_delete_update`로 순서 변경·삭제·값 수정 후 기존 ID를 유지하는지 확인한다. ID 17/14에 같은 본문을 넣어도 정확 조회가 구분되는지 확인한다.
- [x] **Step 2a — 항목 조회 실패 테스트.** `test_field_projection`에서 ID 1의 최근/누적 투자액이 각각 155_000_000_000원/172_500_000_000원이고 get_fields 결과에 요청한 키만 순서대로 있는지 확인한다. raw=True의 정확한 CSV 컬럼명은 원문 1550억원을 반환한다. `test_field_missing_cases`에서 ID 13의 정상 필드 결측 None, 없는 회사 RAGCompanyNotFoundError, 없는 매출 필드 RAGUnknownFieldError, 일부만 유효한 목록의 전체 실패를 구분한다. 빈/중복/잘못된 타입의 fields도 검증한다.
- [x] **Step 3 — 실패 확인.** `python -m pytest tests/test_base_rag.py -k 'load or ids or field' -q`를 실행해 미구현 계약으로 실패하는지 확인한다.
- [x] **Step 4 — 로더·타입·조회 구현.** 생성자는 CSV 스냅샷을 검증해 저장한다. ID를 키로 원문 raw/정규화 values/SourceRef를 구성하고 반환 객체는 복사한다. list_companies는 필터를 AND로 적용한 전체 결과를 ID 사전순으로 반환한다. get_company의 미등록 ID는 None이며 필터의 없는 필드·잘못된 타입/날짜는 ValueError다.
- [x] **Step 4a — 항목 조회 구현.** get_fields는 인수 형식·회사 존재 여부·요청한 모든 키를 검증한 뒤 raw=False이면 values, raw=True이면 raw에서 값만 복사한다. 미등록 회사와 미지원 필드에는 각각의 오류를 내고 결측만 None으로 반환한다. 원문 NULL은 raw=True에서 그대로 유지한다. get_field는 같은 검증 로직으로 하나의 값만 꺼낸다. ID·원문 전체·source를 자동으로 추가하지 않는다. 이 두 함수는 모델/서버에 접근하지 않는다.
- [x] **Step 5 — 통과 및 숫자 검증.** 위 테스트와 `test_filters_and_financial_values`를 실행한다. OpenAI 키가 없고 Qdrant가 꺼져 있어도 전체/항목 조회가 동작하고 네트워크 호출이 0회인지 확인한다. 실제 CSV의 최근 투자액 최대는 ID 1·155_000_000_000원, 물류/유통 분야 최대 누적은 ID 7·17_500_000_000원인지 검증한다. 매출 필드를 만들어 내지 않는다. 이는 자료 검증이며 기업 선정 함수를 추가하는 작업이 아니다.

## Task 2: OpenAI 임베딩 어댑터

**Files:** Modify main/baseRAG/baseRAG.py, tests/test_base_rag.py, main/baseRAG/requirements-rag.txt; Create .env.example.

**Interfaces:** EmbeddingBackend의 model_name/dimensions/embed_documents/embed_query. 기본 OpenAI 어댑터는 요청 모델·차원을 고정 설정에서 읽고 문서와 질문에 동일하게 적용한다. 주입 객체 설정 불일치는 생성 시 ValueError다.

- [x] **Step 1 — 실패 테스트.** SDK 클라이언트를 가짜로 교체해 `test_openai_request_contract`에서 model·dimensions=512·encoding_format=float와 배치 최대 16개를 검증한다. `test_embedding_response_validation`에서 응답 index 순서 복원, 누락/중복 index, 개수·차원·NaN·0벡터 오류를 검사한다. `test_provider_errors`에서 키 부재·인증·쿼터·통신 실패를 RAGProviderError로 구분한다.
- [x] **Step 2 — 실패 확인.** `python -m pytest tests/test_base_rag.py -k 'openai or embedding or provider' -q`를 실행한다.
- [x] **Step 3 — 최소 어댑터 구현.** OpenAI 클라이언트는 실제 임베딩 호출 시 초기화한다. 환경의 OPENAI_API_KEY를 사용하고 timeout=30.0, max_retries=2를 전달한다. tiktoken으로 빈 입력·8,192토큰 초과를 거부하고 길이 초과를 조용히 절단하지 않는다. 응답 벡터를 검증하며 외부 API 오류를 검색 결과 없음으로 바꾸지 않는다.
- [x] **Step 4 — 통과 확인.** 위 테스트와 `test_lookup_without_api_key`, `test_no_e5_prefix`, `test_token_limit`를 통과시킨다. 단위 테스트에서는 실제 OpenAI 호출을 하지 않는다. 실제 키가 없는 .env.example과 .gitignore를 확인한다.

## Task 3: Docker Qdrant와 단순한 인덱스 재사용

**Files:** Create compose.qdrant.yml, tests/test_base_rag_qdrant.py; Modify main/baseRAG/baseRAG.py, tests/test_base_rag.py.

**Interfaces:** build_index(*, rebuild=False)->int, retrieve(query, n_results=5, *, company_id=None, filters=None, exclude_company_id=None)->list[SearchHit], doc_count, close. 내부 상수 COLLECTION_NAME은 companies_small_512다. SourceRef는 CSV 경로·레코드 번호만 저장한다. Qdrant payload는 company_id/content/raw/values이며 해시·모델/본문 버전 필드는 두지 않는다.

- [x] **Step 1 — 서버 구성.** 설계의 YAML 그대로 compose.qdrant.yml을 만든다. `docker compose -f compose.qdrant.yml config --quiet`로 확인한 뒤 up -d로 시작하고 /collections 응답·볼륨·포트 바인딩을 확인한다.
- [x] **Step 2 — 실패 테스트.** 가짜 임베더와 실제 서버로 `test_build_and_reuse_without_embedding`, `test_dimension_mismatch`, `test_explicit_rebuild_reloads_csv`, `test_rebuild_failure`, `test_query_scope_and_exclusion`을 작성한다. 재사용 시 문서 임베딩 0회, CSV 변경만으로 자동 재생성하지 않음, rebuild=True 후 본문/금액 갱신과 삭제 기업 제거를 확인한다. 테스트는 COLLECTION_NAME 상수를 고유 test_ 이름으로 교체하고 해당 테스트 컬렉션만 정리한다.
- [x] **Step 3 — 실패 확인.** `python -m pytest tests/test_base_rag_qdrant.py -q`를 실행한다. 서버 미기동은 통합 검증 미실행으로 표시한다.
- [x] **Step 4 — 기본 생성·재사용 구현.** QdrantClient(url=qdrant_url, timeout=10)로 연결한다. rebuild=False이고 컬렉션이 있으면 벡터 차원만 확인해 재사용한다. 차원 불일치는 ValueError와 rebuild=True 안내다. 없으면 현재 원본 문서를 임베딩하고 COSINE 컬렉션을 생성해 upsert(wait=True)한다. 반환값은 서버 문서 수이며 CSV 행 수와 비교하지 않는다.
- [x] **Step 5 — 명시적 재생성 구현.** rebuild=True는 csv_path를 다시 읽어 ID/값 검증과 문서 임베딩을 먼저 마친 뒤 같은 컬렉션만 삭제·재생성한다. 성공하면 객체의 원본도 갱신한다. 삭제 전에 실패하면 기존 컬렉션을 유지하고, 삭제/적재 실패 후에는 준비 상태를 해제해 오류와 rebuild=True 재시도 안내를 낸다. 자동 복원·부분 적재 감지·버전별 인덱스는 추가하지 않는다.
- [x] **Step 6 — retrieve 구현.** 전체 CSV 원본에 필터·company_id·제외 조건을 적용하고 적격 ID 집합을 Qdrant company_id MatchAny 조건으로 전달한다. 현 규모에서는 적격 수만큼 점수를 받아 유사도 내림차순·ID 사전순으로 정렬한 뒤 n_results개를 반환한다. 결과는 현재 객체의 원본과 연결한다. 후보 0개면 API 호출 없이 빈 목록, 잘못된 입력은 ValueError, build 전은 RAGIndexNotReady다. 미등록 ID 반환은 RAGStoreError와 재생성 안내로 처리하며 전체 내용 비교는 하지 않는다.
- [x] **Step 7 — 통과 확인.** 통합 테스트와 `test_invalid_retrieve_input`, `test_store_failure`를 실행한다. 정상 재사용·차원 불일치·명시적 재생성·서버 실패를 확인하며 제외한 자동 검증 기능을 테스트 요구로 되살리지 않는다.

## Task 4: 공유 객체와 START 노드 연동 계약

**Files:** Modify main/baseRAG/baseRAG.py, tests/test_base_rag.py, tests/test_base_rag_qdrant.py; Create main/baseRAG/docs/rag-usage.md.

**Interfaces:** 같은 프로세스는 준비 객체 하나를 공유한다. 여러 프로세스는 동일 CSV·설정·서버를 사용하며 하나가 먼저 생성한 다음 나머지가 build_index()로 재사용한다. 재생성 이후 다른 프로세스는 새 객체를 만들어 CSV를 다시 읽는다. State에는 company_id 문자열만 전달한다.

- [x] **Step 1 — 실패 테스트.** `test_import_and_constructor_no_network`, `test_shared_reads_keep_company_identity`, `test_returned_records_do_not_mutate_store`, `test_close_keeps_server_data`를 작성한다. close 반복, close 후 RAGClosedError, doc_count의 준비 전/종료 후 0, 준비 후 서버 장애 시 RAGStoreError를 검증한다.
- [x] **Step 2 — 실패 확인.** `python -m pytest tests/test_base_rag.py -k 'import or shared or mutate or close or doc_count' -q`를 실행한다.
- [x] **Step 3 — 공유/수명 구현.** 객체의 build/close와 네트워크 클라이언트 사용을 lock으로 보호한다. 선택 중인 회사 전역 상태를 두지 않는다. close는 클라이언트 연결만 닫고 서버·컬렉션은 유지한다. 기본으로 생성한 클라이언트 자원은 모듈이 관리하고 주입 임베더의 자원은 호출자가 관리한다.
- [x] **Step 4 — 팀 연동 문서.** from main.baseRAG import BaseRAG→생성→build_index→노드 주입→close 예제를 작성한다. 선택 노드가 ID 존재 확인 후 {"company_id": chosen_id}를 반환하고 다음 노드가 get_field/get_fields로 필요한 항목만 받는 예제를 우선 보여준다. 원본 전체와 근거가 필요할 때 get_company를 사용한다. raw=False의 정규화 키·원 단위와 raw=True의 정확한 CSV 컬럼명·문자열을 구분하고 매출 컬럼 부재/결측/미등록 ID 처리를 안내한다. list_companies의 전체 후보로 순위/무작위를 처리하고 retrieve는 의미 후보 검색임을 설명한다. 프롬프트 해석·기업 선택 자체는 구현하지 않는다.
- [x] **Step 5 — 공용 연결 확인.** 두 객체가 같은 CSV와 컬렉션을 사용해 같은 ID를 조회/검색하고 두 번째 객체는 문서를 재임베딩하지 않는지 확인한다. 여러 프로세스에서도 CSV를 일치시키고 재생성 중 검색을 멈추는 운영 순서를 문서화한다. 프로세스 간 버전 검증이나 동시 재생성 잠금은 추가하지 않는다.

## Task 5: 재시작 검증과 실제 검색 평가

**Files:** Modify main/baseRAG/requirements-rag.txt, main/baseRAG/docs/rag-usage.md, tests/test_base_rag_qdrant.py.

- [x] **Step 1 — 재시작과 볼륨 검증.** 전용 통합 검증용 Compose 프로젝트/볼륨에 가짜 임베딩으로 적재한 뒤 down(볼륨 유지)→up -d를 실행한다. 새 객체가 기존 컬렉션을 재사용하고 문서 임베딩 호출 0회인지 확인한다. 일상 종료 문서에 down -v를 넣지 않는다.
- [x] **Step 2 — 기능 전체 검증.** 로컬 tests/에서 `python -m pytest tests/test_base_rag.py tests/test_base_rag_qdrant.py -q`를 실행한다. 서버 이미지와 함께 검증한 런타임 직접 의존성 버전을 main/baseRAG/requirements-rag.txt에 기록한다. main/baseRAG/docs/rag-usage.md에 `python -m pip install -r main/baseRAG/requirements-rag.txt`와 실행 절차를 안내하고 pytest는 개발 검증용으로 구분한다. 실제 CSV 불변과 고유 ID 30개를 다시 확인한다. 테스트는 Git 제외이고 의존성 파일·사용 문서는 추적 가능한지 확인한다.
- [x] **Step 3 — 실제 OpenAI 평가.** OPENAI_API_KEY가 준비된 환경에서 text-embedding-3-small, dimensions=512로 아래 12개 질문의 검색을 평가한다. 같은 문서·k=5에서 질문별 검색 ID·Recall@5·색인/검색 시간·입력 토큰을 기록한다. 기대 ID가 두 개인 질문은 둘 다 정답으로 계산한다. 키/쿼터가 없으면 실제 모델 검증을 미실행으로 표시한다.
- [x] **Step 4 — 품질과 한계 기록.** 근거 없는 '양자컴퓨팅 신약 기업'에도 가까운 후보가 나올 수 있음을 확인한다. 유사도는 투자 가치나 정답 확률이 아니다. 512 품질이 부족하면 문서/질문을 점검하고 필요할 때만 다른 차원 비교 실험을 제안한다. 큰 차원으로 자동 전환하지 않는다.
- [x] **Step 5 — 실행 안내 완료.** Docker 시작/연결/일상 종료, 호스트 localhost와 Compose 서비스명 차이, OPENAI_API_KEY를 설명한다. 평소 build_index(), CSV 수정 후 그래프를 멈추고 build_index(rebuild=True)로 전체 재생성하는 예제를 넣는다. 변경 자동 감지와 이전 인덱스 복원이 없으며, 재생성 실패 후에는 rebuild=True로 재시도해야 함을 알린다.

| 의미 검색 질문 | 기대 ID |
| --- | --- |
| 사람이 입고 보행을 돕는 로봇 | 21 |
| 방울토마토 수확을 자동화하는 로봇 | 22 |
| 로봇을 활용한 수직농장 | 28 |
| 제조사가 달라도 같은 API로 로봇 제어 | 24 |
| 농기계 자율주행 솔루션 | 12 |
| 라이다 기반 산업 현장 공간지능 | 30 |
| 4D 이미징 레이더 애플리케이션 | 27 |
| 건설현장 자재를 새벽에 배송하는 로봇 | 6 |
| 물류로봇용 고정밀 자율주행 소프트웨어 | 13 |
| 중력보상장치와 토크센서 기반 협동로봇 | 18 |
| 산업용로봇을 위한 AI 파운데이션 모델 | 14, 17 |
| 제조업 부품 조립용 휴머노이드 | 1 |

## 완료 조건

ID 고유성·정확 조회·전체 조건 조회·OpenAI 의미 검색·Docker 영속화·컬렉션 재사용·공유 객체 수명 계약이 구현되고 검증되어야 한다. START 직후 별도 선택 노드가 전달한 company_id로 후속 노드가 같은 회사를 조회해야 한다. 제품 코드와 Compose·의존성·사용 문서를 작성했고 전체 테스트 60개가 통과했다. 실제 OpenAI 512차원 평가의 Recall@5는 12개 질문 모두 1.0이었고 top1은 8/12였다. 컨테이너 재생성과 프로세스 간 재사용도 검증했다. 원본 CSV는 변경하지 않았다. tests/는 Git 제외이며 구현 파일·의존성·사용 문서는 추적 가능하다.
