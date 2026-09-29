# main/baseRAG/baseRAG.py 공통 기업 검색 모듈 설계

상태: 2026-09-29 합의한 설계에 따라 `main/baseRAG/baseRAG.py` 구현 및 실행 검증을 진행했다. START 기업 선택 노드는 후속 작업이다. 설치와 팀 연동은 [사용 안내](../../../main/baseRAG/docs/rag-usage.md)를 따른다.

## 목적과 책임

`main/baseRAG/baseRAG.py`는 각 에이전트가 사용하는 공용 자료 모듈이다. CSV 로딩·고유 ID 검증·정확한 원본 조회·필요 항목만의 조회·조건 검색·의미 검색을 제공한다. 한 행 전체를 항상 반환할 필요는 없다. 기업 선정, 프롬프트 해석, 기술·시장·경쟁·신뢰성 평가, 투자 판단은 호출하는 에이전트가 맡는다.

사용자가 추후 구현할 START 직후 기업 선택 노드는 이 모듈에서 후보와 근거를 얻어 회사 하나를 정한다. 선택한 ID가 실제 존재하는지 확인한 뒤 State에 회사 식별 정보로 `company_id: str`만 전달한다. 다음 노드는 같은 CSV 스냅샷을 사용하는 공용 인스턴스에서 `get_company(state["company_id"])`로 필요한 데이터를 읽는다. 회사 전체 데이터·선정 결과 객체를 State에 복사하도록 요구하지 않는다.

실제 프로젝트의 입력은 `docs/data/base/raw/thevc_startups_30_updated.csv`다. 기본 경로는 모듈 위치를 기준으로 해석하며 다른 CSV 경로도 인수로 받을 수 있다. CSV를 수정하거나 ID를 새로 부여하지 않는다.

## 현재 CSV 및 ID 검증 결과

2026-09-29 실제 파일을 읽어 확인했다.

- 30행, 20컬럼, 고유 `company_id` 30개. ID 값은 현재 `1`부터 `30`이며 빈 ID 0개·중복 ID 0개다. 기업명 중복도 0개다.
- ID `17`은 카본식스, ID `14`는 리얼월드다. 두 회사의 대표제품·서비스 설명이 같아도 ID를 합치지 않는다.
- 초기 데이터 준비 스크립트는 초기 ID를 `index + 1`로 부여했다. 원본 순서를 바꾸고 해당 변환을 다시 실행하면 같은 회사의 ID가 바뀔 수 있다. 이후 정제에서는 기존 ID를 보존해야 하며 이 스크립트는 이번에 수정하지 않는다.

모듈 구현 시 모든 로드에서 아래 계약을 검사한다.

1. ID는 앞뒤 공백을 제거한 문자열로 다룬다. 빈 문자열·NULL·중복을 발견하면 해당 CSV 레코드를 포함한 `RAGDataError`로 중단한다. 공백 제거 후의 충돌도 검사한다.
2. 행 번호로 ID를 재생성하지 않는다. 정렬·삭제·숫자/본문 수정 후에도 남은 회사의 기존 ID를 보존한다. ID가 연속 번호일 필요는 없다.
3. 신규 회사의 ID 발급은 데이터 준비 단계 책임이다. 기존 ID와 겹치지 않아야 하며 삭제된 ID를 다른 회사에 재사용하지 않는다. 한 파일의 중복 검증만으로 과거 ID 재사용 여부까지 증명할 수는 없다.
4. Qdrant 내부 point ID는 `uuid5(NAMESPACE_URL, "basic-rag:" + company_id)`로 만들고 원래 `company_id`를 payload에 그대로 보관한다. 벡터 내부 키나 CSV 레코드 번호를 State의 회사 ID로 쓰지 않는다.

## 임베딩과 서버 구성

사용자 결정에 따라 **OpenAI text-embedding-3-small + 로컬 Docker Qdrant 서버**를 사용한다. 임베딩 차원은 우선 `dimensions=512`로 명시한다. 모델의 기본 출력은 1,536차원이지만 축소 출력을 지원한다. 512는 이 CSV에 최적이라고 측정한 값이 아니라 작은 출력으로 시작하기 위한 평가 설정이다. 차원 축소는 벡터 크기를 줄이며 입력 토큰당 API 요금을 줄이는 것은 아니다.

- `openai` Python SDK의 `embeddings.create(model="text-embedding-3-small", dimensions=512, input=..., encoding_format="float")`를 사용한다. 문서와 질문에 같은 모델·차원을 사용한다. E5의 query/passage 접두사는 사용하지 않는다.
- 색인을 새로 만들 때 회사 문서를, retrieve 때 질문을 OpenAI에 전달한다. get_company/get_field/get_fields/list_companies에는 OpenAI 호출이 없다. 생성 LLM과 기업 선정 로직은 모듈 범위에 넣지 않는다.
- `OPENAI_API_KEY`는 환경변수에서 읽는다. 키는 코드·CSV·Qdrant payload·로그에 저장하지 않는다. import/생성자에서는 OpenAI 클라이언트를 초기화하지 않고 실제 임베딩이 필요할 때 준비한다.
- 입력 길이를 tiktoken으로 검사해 빈 입력·8,192토큰 초과를 거부하며 문서를 조용히 자르지 않는다. 문서 배치는 최대 16개로 구성한다. 응답의 index로 순서를 복원하고 개수·차원·유한값·0이 아닌 노름을 검증한다.
- 호출 timeout은 30초, SDK max_retries는 2로 지정한다. 모듈에서 별도 무한 재시도를 추가하지 않는다. 인증·쿼터·통신·응답 오류는 자료 부족과 구분한다.
- 고정 컬렉션 companies_small_512가 있으면 벡터 차원만 확인하고 재사용한다. 문서 재임베딩은 하지 않는다. CSV 또는 임베딩 입력 구성을 바꾸면 build_index(rebuild=True)로 전체를 다시 만든다. 변경 자동 감지와 버전별 컬렉션은 구현하지 않는다.
- CacheBackedEmbeddings는 임베딩 결과 캐시 래퍼이며 첫 버전에는 넣지 않는다. 서버에 저장한 벡터를 재사용하는 것으로 시작하고, 변경 행이 많아져 별도 캐시의 이점이 확인되면 추가한다.

## Qdrant 선택 근거와 대안

이 CSV의 30행 규모에서 Qdrant가 다른 저장소보다 빠르거나 정확하다고 주장하지 않는다. 선택 근거는 참고 코드와의 연결, 조건 검색 API, 영속적인 공용 검색 서버 요구다.

| 선택지 | 이번 프로젝트에서의 판단 |
| --- | --- |
| Qdrant — 채택 | 벡터와 company_id·분야·금액 payload를 함께 저장하고 필터 검색 가능. Docker로 독립 실행하며 여러 Python 프로세스가 같은 컬렉션을 이용 가능. 참고 market_rag.py의 구조를 이어가기 좋음 |
| Chroma | 서버 모드와 metadata 필터를 지원하는 유효한 대안. 공유 서버라는 특성은 Qdrant만의 장점이 아님 |
| FAISS | 벡터 검색 라이브러리. 소규모 검색에는 충분하지만 원본/메타데이터 관리와 공용 서비스 계층을 애플리케이션에서 추가 구성해야 함 |
| pgvector | PostgreSQL의 SQL 조건·정렬·벡터 검색을 함께 활용. 기존 PostgreSQL이 있다면 매력적이며 현재 CSV만을 위해 새로 도입할 필요는 낮음 |

Qdrant 필터로 회사 지정·자사 제외를 처리한다. 현재 30행에서 복잡한 날짜/결측 의미를 저장소마다 중복 구현하지 않도록 공개 CompanyFilter는 먼저 검증된 원본 전체에 적용하고, 적격 company_id 집합을 Qdrant payload의 MatchAny 조건으로 전달한다. payload 인덱스·HNSW 튜닝·분산 클러스터는 첫 버전 범위에서 제외한다. 숫자 순위와 무작위 선정은 전체 후보를 받는 선택 노드의 책임이다.

## Docker 운영 계약

`compose.qdrant.yml`은 아래 구성으로 구현했다.

```yaml
services:
  qdrant:
    image: qdrant/qdrant:v1.19.1
    ports:
      - "127.0.0.1:6333:6333"
    volumes:
      - qdrant_data:/qdrant/storage
volumes:
  qdrant_data:
```

v1.19.1은 실제 Docker 연동·볼륨 유지·컨테이너 재생성을 검증한 고정 버전이다. 호스트 Python은 `http://localhost:6333`으로 연결한다. 기본 REST만 사용하므로 gRPC 6334 포트는 열지 않는다. 에이전트까지 같은 Compose 네트워크에 넣는 후속 구성에서는 주소를 `http://qdrant:6333`으로 바꾼다. 컨테이너의 localhost는 호스트/다른 컨테이너 주소가 아니다.

시작 명령은 `docker compose -p company-rag -f compose.qdrant.yml up -d`, 연결 확인은 호스트에서 `curl --fail http://localhost:6333/collections`다. named volume에 데이터가 남아 컨테이너 재생성 후에도 컬렉션을 재사용할 수 있다. 일반 중지는 `docker compose -p company-rag -f compose.qdrant.yml down`이며 `down -v`는 데이터를 삭제하므로 일상 종료 절차에 넣지 않는다. 별도 앱 API 서버는 만들지 않는다.

## 참고 코드에서 가져올 구조

참조: [Eye-Reading/rag-agent의 market_rag.py](https://github.com/Eye-Reading/rag-agent/blob/main/main/searchCorp/agents/rag/market_rag.py). 해당 파일의 실행 코드 기준으로 확인했다.

| 참고 코드 | 이번 모듈 |
| --- | --- |
| MarketEvalRAG 클래스·retrieve·doc_count | BaseRAG 클래스·retrieve·doc_count |
| SentenceTransformer, BGE-M3/KoE5 1,024차원 | OpenAI text-embedding-3-small, dimensions=512 |
| QdrantClient(url=qdrant_url), 기본 http://localhost:6333 | 같은 서버 연결 방식, Docker Compose·named volume로 실행/저장 구성 명시 |
| PDF 두 페이지씩 청크 | CSV 회사 한 행을 문서 하나로 변환 |
| 생성자에서 PDF 자동 적재 | 생성자는 CSV 검증만 수행, 명시적 build_index |
| 파일명만으로 이미 적재됐다고 판단 | 고정 컬렉션의 존재·차원만 확인해 재사용하고 CSV 변경 시 rebuild=True로 전체 재생성 |
| retrieve가 본문 문자열만 반환 | 회사 ID·원본·유사도·출처 반환 |
| 미등록 모델의 차원을 1,024로 추정 | 명시한 dimensions와 응답 벡터·컬렉션 설정의 일치 검증 |

선배 코드의 기본 구성은 별도로 실행 중인 Qdrant 서버에 연결하는 방식이다. `localhost`는 같은 호스트의 서비스 주소이며 외부 클라우드를 뜻하지 않는다. 이 파일은 서버를 실행하지 않는다. Docker 또는 다른 배포 방식으로 실행했는지는 이 파일만으로 확인할 수 없다. 임베딩 계산은 SentenceTransformer가 로컬에서 수행하며 이 파일에 OpenAI 임베딩 호출은 없다.

## 데이터 표현

컬럼: `company_id`, `기업명`, `홈페이지`, `소재지역`, `대표제품`, `서비스`, `분야`, `소분야`, `기술`, `제품 형태`, `투자 유치 단계 (최근)`, `투자 유치 금액 (최근)`, `임직원 수`, `1개월전 대비 임직원 수`, `특허 수`, `투자자 분류`, `인증/자격`, `투자 유치일 (최근)`, `투자 유치 금액 (누적)`, `업력`.

한 회사의 본문은 `기업명`, `대표제품`, `서비스`, `분야`, `소분야`, `기술`, `제품 형태`로 구성한다. LLM이 만든 설명을 덧붙이지 않는다. 원문 전체는 raw에 보존하고 values에는 정규화 값을 둔다.

| 키 | 형식 |
| --- | --- |
| company_id, company_name | 문자열 |
| location, sector, subsector, technology, product_type, funding_stage | 문자열 또는 null |
| funding_latest_won, funding_total_won | 원 단위 정수 또는 null |
| employees, employees_change, patent_count | 정수 또는 null |
| funding_latest_date | ISO 날짜 문자열 또는 null |
| company_age_years | 실수 또는 null; 기준일 미상 |

금액은 Decimal로 원·만원·억원 및 쉼표를 처리한다. 빈 문자열과 NULL은 결측으로 보존하며 특허 결측을 0으로 바꾸지 않는다. 인식하지 못한 숫자·날짜는 파일·레코드·컬럼을 포함한 데이터 오류로 알린다. 업력으로 설립일을 역산하지 않는다. `해당 없음`은 NULL과 구분한다. 매출 컬럼은 없으며 새로 추정하지 않는다.

## 공개 인터페이스

타입과 클래스는 `main/baseRAG/baseRAG.py` 안에 둔다. 반환 객체는 호출자가 수정해도 내부 원본이 바뀌지 않도록 복사해 제공한다.

```python
BaseRAG(
    csv_path: str | Path = DEFAULT_CSV_PATH, *,
    model_name: str = "text-embedding-3-small",
    dimensions: int = 512,
    qdrant_url: str = "http://localhost:6333",
    embeddings: EmbeddingBackend | None = None,
) -> BaseRAG

rag.get_company(company_id: str) -> CompanyRecord | None
rag.get_field(
    company_id: str, field: str, *, raw: bool = False,
) -> str | int | float | None
rag.get_fields(
    company_id: str, fields: list[str], *, raw: bool = False,
) -> dict[str, str | int | float | None]
rag.list_companies(*, filters: list[CompanyFilter] | None = None) -> list[CompanyRecord]
rag.build_index(*, rebuild: bool = False) -> int
rag.retrieve(
    query: str, n_results: int = 5, *, company_id: str | None = None,
    filters: list[CompanyFilter] | None = None,
    exclude_company_id: str | None = None,
) -> list[SearchHit]
rag.doc_count -> int
rag.close() -> None
```

- `EmbeddingBackend`: model_name, dimensions, embed_documents(texts), embed_query(text) 인터페이스. OpenAI 어댑터와 가짜 테스트 구현을 교체할 수 있으며 주입 객체의 모델·차원이 생성자 설정과 일치해야 한다. 기본 모델은 text-embedding-3-small로 제한하고 dimensions는 1~1,536의 정수로 검증한다.
- `SourceRef`: `csv_path: str`, 헤더를 포함한 1-based `record_number: int`. 파일 해시나 본문 버전은 관리하지 않는다.
- `CompanyRecord`: `company_id: str`, `company_name: str`, `raw: dict[str,str]`, `values: dict[str,str|int|float|None]`, `content: str`, `source: SourceRef`.
- `CompanyFilter`: 정규화 field, op(`eq/ne/gt/gte/lt/lte/is_null/not_null`), value. 필터 목록은 AND로 결합한다. 범위 비교는 숫자·날짜 필드에 한정한다. 결측은 is_null을 제외한 비교에서 제외한다. 미지원 필드·연산자, 필드와 맞지 않는 value 타입·잘못된 날짜값은 ValueError다.
- `SearchHit`: `company: CompanyRecord`, `score: float`. score는 코사인 유사도이며 투자 점수나 정답 확률이 아니다.

`get_company("17")`는 이미 정한 회사를 정확히 조회한다. `list_companies`는 조건을 만족하는 전체 행을 ID 사전순으로 반환하며 top-k를 적용하지 않는다. 두 기능은 OpenAI 키·Qdrant 연결·색인 없이 사용할 수 있다. 모르는 ID는 None을 반환하고 비슷한 기업으로 대체하지 않는다.

`get_field`는 요청한 값 하나만, `get_fields`는 요청한 항목만 담은 dict를 반환한다. ID·본문·출처·다른 항목을 자동으로 덧붙이지 않는다. 근거 전체가 필요하면 get_company의 SourceRef를 사용한다. 항목 조회도 검증한 CSV 스냅샷에서 수행하며 OpenAI·Qdrant·색인 없이 동작한다.

- raw=False(기본): values의 정규화 키를 사용한다. 예: `get_field("1", "funding_latest_won")`은 원 단위 정수 `155_000_000_000`이다.
- raw=True: CSV의 정확한 컬럼명을 사용하고 원문 문자열을 그대로 반환한다. 예: `get_field("1", "투자 유치 금액 (최근)", raw=True)`는 `"1550억원"`이다. 원문 NULL 문자열도 그대로 유지한다. 추가 CSV 컬럼도 raw 조회로 접근할 수 있다.
- `get_fields("1", ["funding_latest_won", "funding_total_won"])`은 `{"funding_latest_won": 155_000_000_000, "funding_total_won": 172_500_000_000}`만 반환한다. 요청한 키 순서를 유지한다.
- 정상 필드의 실제 결측은 기본 조회에서 None이다. 예: ID 13의 funding_latest_won. 결측을 0으로 바꾸지 않는다.
- 회사 ID가 없으면 RAGCompanyNotFoundError, 선택한 raw/정규화 영역에 필드가 없으면 RAGUnknownFieldError다. 기존 get_company의 미등록 ID→None 계약은 유지하며, 항목 조회의 None은 실제 결측값만 의미하도록 구분한다.
- 매출 컬럼은 현재 없으므로 revenue_won 또는 raw의 매출액 요청은 RAGUnknownFieldError다. 유사한 투자액으로 대체하거나 매출을 추정하지 않는다.
- fields는 비어 있지 않은 문자열 목록이며 중복을 허용하지 않는다. 잘못된 인수 형식은 ValueError다. 회사 존재 여부를 확인하고 모든 필드를 검증한 뒤 반환하며, 하나라도 미지원 필드이면 부분 결과 없이 오류를 낸다. 자연어 항목명 추론이나 별칭 추측은 하지 않는다.

기존 `select_company(prompt)`는 프롬프트를 해석해 회사 하나를 결정하는 기능이었다. 이번 책임 분리에 맞춰 공용 모듈의 공개 API에서 제외한다. QueryIntent·SelectionResult·선택용 LLM 의존성도 제거한다. 기업명 지정은 `list_companies`의 `company_name eq` 필터로 정확히 찾을 수 있다.

`retrieve`는 명시적 필터를 전체 원본에 먼저 적용한 뒤 해당 회사 ID 집합으로 Qdrant 검색을 제한한다. 현 규모에서는 적격 후보 점수를 모두 받아 유사도 내림차순·ID 사전순으로 정렬하고 n_results개를 반환한다. 빈 질문이나 양수가 아닌 n_results는 ValueError, 후보 0개는 빈 목록, build 전 호출은 RAGIndexNotReady다. 유사도 상위 후보가 질문의 조건을 충족한다고 보장하지 않으며 최종 적합성 판단은 에이전트가 한다.

## 기업 선택 노드와의 계약

기업 선택 노드는 후속 작업이며 이 모듈의 구현 범위에는 포함하지 않는다. 아래는 팀 연동 계약이다.

- 특정 ID·회사명: 정확히 조회한다. ID 17/14처럼 본문이 같아도 이름 지정은 다른 회사로 바꾸지 않는다.
- 투자액 순위: list_companies로 전체 적격 집합을 얻고 정규화 금액으로 비교한다. 최근·누적 기준과 동률 처리는 선택 노드가 결정한다. 결측은 0이 아니다.
- 무작위: 전체 적격 ID 집합에서 선택한다. 재현이 필요하면 ID 정렬 후 seed를 사용한다.
- 의미 기반: retrieve로 후보를 찾고 원문 근거를 확인한다. 의미 조건과 최대 투자액을 함께 요구하면 top-k만 비교해 전체 1위라고 주장하지 않는다.
- 매출 1위는 데이터 부족, 최근/누적이 없는 투자액 요청은 기준이 모호함을 선택 노드가 처리한다.
- 선택 직후 get_company(chosen_id)가 None이 아닌지 검증하고 State에 `{"company_id": chosen_id}`를 전달한다. 이후 노드는 이 ID를 유지하며 잘못된 ID를 임의 회사로 대체하지 않는다.

## 단순한 인덱스 재사용과 재생성

실습 범위에서는 컬렉션 이름을 내부 상수 `COLLECTION_NAME = "companies_small_512"`로 고정한다. CSV 해시·모델/본문 버전 관리, 기존 전체 ID/payload/벡터 비교, 버전별 컬렉션 생성, 자동 복구를 제외한다. Qdrant payload에는 company_id·content·raw·values만 저장한다. 출처는 현재 CSV의 경로와 레코드 번호로 제공한다.

```python
rag.build_index()              # 없으면 생성, 있으면 차원만 확인해 재사용
rag.build_index(rebuild=True)  # CSV를 다시 읽고 같은 컬렉션을 전체 재생성
```

- 기본 호출: 컬렉션이 있으면 벡터 차원이 설정과 같은지 확인한다. 다르면 ValueError로 알리고 rebuild=True 사용을 안내한다. 같으면 임베딩 없이 연결하고 서버 문서 수를 반환한다. 문서 수는 반환값에 쓰며 CSV 행 수와 비교 검증하지 않는다.
- 컬렉션이 없으면 생성자가 읽어 둔 CSV의 문서를 임베딩하고 Distance.COSINE으로 생성·적재한다. 응답 벡터 검증과 upsert(wait=True)는 유지한다.
- rebuild=True: 생성 시 받은 csv_path를 다시 읽고 ID/값을 검증한 뒤 전체 문서 임베딩을 먼저 준비한다. 이후 고정 컬렉션만 삭제·재생성해 적재한다. 성공하면 현재 객체의 원본 데이터도 새 CSV로 바꾼다. 다른 컬렉션은 건드리지 않는다.
- CSV 검증이나 임베딩 준비가 실패하면 기존 컬렉션을 유지한다. 삭제/적재 단계가 실패하면 객체를 준비 전 상태로 두고 오류와 rebuild=True 재실행 안내를 반환한다. 이전 인덱스 자동 복원이나 부분 적재 자동 감지는 구현하지 않는다.
- CSV 변경을 자동으로 감지하지 않는다. CSV를 수정하거나 본문 구성·임베딩 설정을 바꾼 뒤 재생성을 생략하면 원본 조회와 벡터 검색의 내용이 달라질 수 있다. 재생성은 그래프 실행을 멈춘 상태에서 한 준비 프로세스만 수행한다.

## 공유와 수명 관리

import 시 CSV 읽기·API 호출·Qdrant 연결을 하지 않는다. 생성자는 CSV 검증과 설정 저장만 한다. 준비 담당이 build_index를 완료한 뒤 같은 프로세스의 노드에 객체 하나를 공유한다. 선택 중인 회사 상태를 모듈에 두지 않는다.

여러 프로세스에서는 준비 담당이 먼저 생성하고 다른 객체는 build_index()로 같은 컬렉션을 재사용한다. 모두 동일 CSV·모델·차원을 사용하도록 팀이 맞춘다. 재생성 후에는 다른 프로세스의 객체도 새로 만들어 CSV를 다시 읽는다. 동시 재생성이나 실행 중 데이터 교체는 지원하지 않는다.

get_company/get_field/get_fields/list_companies는 각 객체가 읽은 CSV에서 처리한다. retrieve는 Qdrant 검색 결과의 company_id로 현재 원본과 연결하며 미등록 ID가 반환되면 RAGStoreError와 재생성 안내를 낸다. 모든 내용의 최신성을 보장하는 검사로 확대하지 않는다. 반환 객체를 수정해도 내부 원본은 바뀌지 않는다.

공유 객체의 build/close와 네트워크 클라이언트 사용은 lock으로 보호한다. close는 클라이언트 연결만 닫고 컬렉션·볼륨·Docker 서버를 유지하며 반복 호출해도 안전하다. 종료 후 메서드는 RAGClosedError를 내고 doc_count는 준비 전/종료 후 0이다. 준비 후 doc_count는 서버의 문서 수를 조회하며 장애를 0으로 숨기지 않는다.

오류는 RAGDataError(CSV), RAGCompanyNotFoundError(항목 조회의 미등록 회사), RAGUnknownFieldError(미지원 항목), RAGProviderError(OpenAI/벡터 응답), RAGStoreError(Qdrant), RAGIndexNotReady(build 전), RAGClosedError(종료 후)로 구분한다. 차원 불일치는 ValueError를 사용하고 별도 컬렉션 불일치 오류 클래스는 만들지 않는다.

## 검증과 근거

팀원이 설치할 수 있도록 main/baseRAG/requirements-rag.txt와 main/baseRAG/docs/rag-usage.md는 Git 추적 대상으로 남긴다. main/baseRAG/requirements-rag.txt에는 런타임 의존성과 검증한 버전을 기록하고, 사용 문서에는 설치·실행 명령을 안내한다. tests/ 전체는 .gitignore의 /tests/ 규칙으로 제외하고 로컬 검증에만 사용한다. pytest는 개발 검증 도구이며 모듈 실행에 필요하지 않다. 테스트 파일을 강제로 Git에 추가하지 않는다.

가짜 임베더로 ID·수치·OpenAI 요청/응답 계약을 테스트하고 실제 Docker Qdrant로 네트워크 연결·재사용·재시작·프로세스 간 공유를 검증했다. 실제 text-embedding-3-small 512차원으로도 현재 CSV의 질문 12개를 평가했다. 기대 회사가 모두 상위 5개에 포함되었고 첫 결과가 기대 회사인 경우는 8/12였다. 이 작은 평가표가 일반 검색 성능을 보장하지는 않는다. 상세 결과는 사용 안내에 기록한다.

- OpenAI 임베딩: https://developers.openai.com/api/docs/guides/embeddings
- OpenAI 임베딩 생성 API: https://developers.openai.com/api/reference/resources/embeddings/methods/create
- Qdrant Docker 실행: https://qdrant.tech/documentation/quickstart/
- Qdrant 필터: https://qdrant.tech/documentation/search/filtering/
- Qdrant 버전: https://github.com/qdrant/qdrant/releases/tag/v1.19.1
- Chroma 서버: https://docs.trychroma.com/docs/run-chroma/client-server
- FAISS: https://github.com/facebookresearch/faiss
- pgvector: https://github.com/pgvector/pgvector
