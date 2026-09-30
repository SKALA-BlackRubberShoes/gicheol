# 기업 검색 모듈 사용법

`main/rag/company/baseRAG.py`는 CSV의 기업 정보 조회와 의미 검색을 제공하는 공용 모듈입니다. 기업 선정과 기술·시장·경쟁사·신뢰성 평가는 호출하는 노드가 담당합니다. 프로젝트 루트는 `/Users/skala/workspace/skalaDay48-50/blackrubbershoes`입니다. 아래 명령과 예제는 이 폴더에서 실행합니다.

## 폴더 구조와 import

```text
blackrubbershoes/
├── main/
│   ├── paths.py
│   └── rag/
│       ├── embeddings.py
│       └── company/
│           ├── __init__.py
│           ├── baseRAG.py
│           ├── data.py
│           ├── models.py
│           ├── requirements-rag.txt
│           └── docs/rag-usage.md
├── docs/data/base/raw/thevc_startups_30_updated.csv
└── compose.qdrant.yml
```

`main/rag/company/__init__.py`에서 공개 클래스를 내보내므로 `from main.rag.company import BaseRAG, CompanyFilter`로 가져옵니다. 기본 CSV는 프로젝트 루트를 기준으로 찾습니다. 공개 클래스명은 `BaseRAG`입니다. `main/`에는 `__init__.py`를 두지 않고 Python의 네임스페이스 패키지로 사용합니다. 프로젝트 루트에서 실행하면 같은 import가 동작합니다.

## 설치와 서버 실행

Python 3.11 이상을 사용합니다. 개발 가상환경은 Python 3.12.14이며 런타임 의존성 버전은 [requirements-rag.txt](../requirements-rag.txt)에 고정되어 있습니다.

현재 버전: `openai==3.20.0`, `pydantic==2.13.5`, `qdrant-client==1.19.1`, `tiktoken==0.14.0`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r main/rag/company/requirements-rag.txt

docker compose -p company-rag -f compose.qdrant.yml up -d
curl --fail http://localhost:6333/collections
```

`main/rag/company` 폴더에서 설치할 때는 프로젝트 가상환경을 활성화한 상태에서 `python -m pip install -r requirements-rag.txt`를 실행하면 됩니다. 그래프 실행과 아래 import 예제는 프로젝트 루트 기준입니다.

Qdrant는 `http://localhost:6333`에서 실행되고 named volume에 데이터를 보존합니다. 기업 정보만 조회할 때는 서버와 OpenAI 키가 필요하지 않습니다. 의미 검색에는 OpenAI `text-embedding-3-small`을 사용하며 기본 출력은 512차원입니다. 문서 색인과 검색 질문 임베딩 시 외부 API를 호출합니다.

```bash
export OPENAI_API_KEY="발급받은_API_키"
```

모듈은 `.env`를 자동으로 읽지 않습니다. 실제 애플리케이션 프로세스의 환경변수에 키를 설정하세요. 에이전트도 같은 Compose 네트워크에서 실행한다면 생성자에 `qdrant_url="http://qdrant:6333"`을 전달합니다.

반환 객체는 일반 Pydantic 모델이고, `CompanyFilter`는 입력값을 그대로 보관하는 dataclass입니다. 공통 `strict=True`·`extra="forbid"` 설정은 사용하지 않습니다. ID·금액·필터 조건·벡터 차원 등 조회 정확성에 필요한 검증은 해당 처리 단계에서 수행합니다.

## 필요한 값만 조회하기

```python
from main.rag.company import BaseRAG

rag = BaseRAG()  # main/paths.py에서 저장소 루트를 기준으로 docs/data/base/raw/thevc_startups_30_updated.csv 사용
try:
    print(rag.get_field("1", "funding_latest_won"))
    # 155000000000: 원 단위 정수

    print(rag.get_fields("1", ["company_name", "funding_total_won"]))
    # {'company_name': '홀리데이로보틱스', 'funding_total_won': 172500000000}

    print(rag.get_field("1", "투자 유치 금액 (최근)", raw=True))
    # 1550억원: 원문 문자열

    print(rag.get_fields("1", ["홈페이지", "대표제품", "서비스"], raw=True))
    print(rag.get_field("13", "funding_latest_won"))  # None: 결측
    print(rag.get_field("13", "투자 유치 금액 (최근)", raw=True))  # NULL: 문자열

    company = rag.get_company("17")
    if company is not None:
        print(company.company_name)  # 카본식스
        print(company.source.csv_path, company.source.record_number)
finally:
    rag.close()
```

`BaseRAG()`의 기본 CSV 경로는 현재 실행 디렉터리와 무관하게 모듈 위치를 기준으로 정해집니다. 다른 파일은 `BaseRAG(csv_path)`로 지정합니다.

`get_field`는 값 하나, `get_fields`는 요청한 키만 포함하는 dict를 반환합니다. `get_fields`의 목록은 비어 있지 않아야 하고 중복 키를 허용하지 않습니다. 요청 순서를 유지하며, 하나라도 없는 필드이면 부분 결과 없이 오류를 냅니다.

`raw=False`는 아래의 정규화 키를 사용합니다. `raw=True`는 정확한 CSV 컬럼명으로 조회하며 `NULL`, `해당 없음`, 이모지 등을 원문 그대로 보존합니다. 대표제품·서비스·홈페이지·인증/자격·추가 CSV 컬럼은 raw 조회로 접근합니다.

| 정규화 키 | 값 형식 |
| --- | --- |
| `company_id`, `company_name` | 문자열 |
| `location`, `sector`, `subsector`, `technology`, `product_type`, `funding_stage` | 문자열 또는 `None` |
| `funding_latest_won`, `funding_total_won` | 원 단위 정수 또는 `None` |
| `employees`, `employees_change`, `patent_count` | 정수 또는 `None` |
| `funding_latest_date` | `YYYY-MM-DD` 문자열 또는 `None` |
| `company_age_years` | 년 단위 실수 또는 `None`; 기준일은 알 수 없음 |

`get_company`는 `CompanyRecord` 전체를 반환하며 `raw`, `values`, `content`, `source`를 포함합니다. 출처의 `record_number`는 헤더를 포함하므로 첫 기업은 2번입니다. 반환 객체를 수정해도 모듈 내부 원본은 바뀌지 않습니다. 현재 CSV에는 매출 컬럼이 없습니다.

## 조건 조회와 의미 검색

```python
from main.rag.company import BaseRAG, CompanyFilter

rag = BaseRAG("docs/data/base/raw/thevc_startups_30_updated.csv")
try:
    filters = [
        CompanyFilter(field="sector", op="eq", value="물류/유통"),
        CompanyFilter(field="funding_total_won", op="gte", value=5_000_000_000),
    ]
    # API·색인 없이 조건에 맞는 전체 회사를 조회합니다.
    for company in rag.list_companies(filters=filters):
        print(company.company_id, company.company_name)

    # 의미 검색 전 명시적으로 준비합니다. 기존 컬렉션은 재사용합니다.
    print("서버 문서 수:", rag.build_index())
    hits = rag.retrieve(
        "물류 자동화 로봇",
        n_results=5,
        filters=[CompanyFilter(field="sector", op="eq", value="물류/유통")],
        exclude_company_id="7",
    )
    for hit in hits:
        print(hit.company.company_id, hit.company.company_name, hit.score)
finally:
    rag.close()
```

필터는 정규화 키를 사용하고 여러 조건을 AND로 결합합니다. 연산자는 `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `is_null`, `not_null`입니다. 범위 비교는 숫자·날짜에만 적용하며, 결측을 찾을 때는 `CompanyFilter(field="patent_count", op="is_null")`처럼 value를 생략합니다. 결측은 `ne`를 포함한 일반 비교에서 제외됩니다.

`list_companies()`는 전체 결과를 ID 문자열 사전순으로 반환합니다. 투자액 순위·무작위 선정은 이 전체 후보에서 처리하세요. `retrieve`는 CSV에 필터를 먼저 적용하고 유사도 상위 후보를 반환합니다. `company_id="17"`로 검색 대상을 제한할 수도 있습니다. 이미 정한 기업의 값만 필요하면 `get_field`/`get_fields`를 사용하면 됩니다.

검색 점수는 유사도이며 투자 점수나 정답 확률이 아닙니다. 관련 없는 질문에도 가까운 후보가 나올 수 있고, 의미 검색 top-k에서 투자액을 비교했다고 전체 기업 중 최대값이 되지는 않습니다. 검색 품질과 실측 결과는 별도로 평가해야 합니다.

## 여러 노드에 같은 인스턴스 전달하기

같은 프로세스에서는 조립부가 객체 하나를 만들고 노드에 전달합니다. 각 노드가 새 `BaseRAG`를 만들 필요는 없습니다. 아래처럼 반환한 함수를 LangGraph의 노드로 등록할 수 있습니다.

```python
from typing import TypedDict
from main.rag.company import BaseRAG, Scalar

class CompanyState(TypedDict, total=False):
    company_id: str
    finance_data: dict[str, Scalar]
    technology_data: dict[str, Scalar]

def make_nodes(rag: BaseRAG):
    def finance_node(state: CompanyState):
        return {"finance_data": rag.get_fields(
            state["company_id"], ["funding_latest_won", "funding_total_won"]
        )}

    def technology_node(state: CompanyState):
        return {"technology_data": rag.get_fields(
            state["company_id"], ["대표제품", "서비스", "기술"], raw=True
        )}

    return finance_node, technology_node

rag = BaseRAG("docs/data/base/raw/thevc_startups_30_updated.csv")
try:
    # 의미 검색을 하는 노드가 있다면 여기서 rag.build_index()를 호출합니다.
    finance_node, technology_node = make_nodes(rag)
    chosen_id = "17"  # 별도 기업 선택 노드가 결정한 ID의 예
    if rag.get_company(chosen_id) is None:
        raise ValueError("선택한 기업이 CSV에 없습니다.")

    state: CompanyState = {"company_id": chosen_id}
    print(finance_node(state))
    print(technology_node(state))
finally:
    # 실제 그래프에서는 모든 노드 실행이 끝난 뒤 닫습니다.
    rag.close()
```

회사 식별 정보는 State의 `company_id`로 전달하고, 후속 노드가 필요한 자료를 조회해 각자의 키로 반환합니다. `rag.current_company_id` 같은 공용 선택 상태를 두지 않습니다. State에 분석 결과를 추가하는 것은 가능합니다. `add_messages`는 메시지 목록용 reducer이며 일반 dict까지 자동으로 병합하지는 않습니다. 이 모듈 자체는 LangGraph를 설치하거나 그래프를 생성하지 않습니다.

## 재사용·CSV 수정·종료

컬렉션 이름은 `companies_small_512`입니다. 기본 `build_index()`는 컬렉션이 없으면 만들고, 있으면 차원만 확인한 뒤 문서를 재임베딩하지 않고 재사용합니다. 반환값과 `doc_count`는 서버 문서 수이며 CSV 행 수와 비교하지 않습니다. 파일·본문 변경도 자동 감지하지 않습니다.

CSV를 수정했다면 그래프 실행을 멈추고 **준비 담당 프로세스 하나에서** 아래와 같이 재생성합니다.

```python
from main.rag.company import BaseRAG

rag = BaseRAG()  # main/paths.py에서 저장소 루트를 기준으로 docs/data/base/raw/thevc_startups_30_updated.csv 사용
try:
    print(rag.build_index(rebuild=True))
finally:
    rag.close()
```

`rebuild=True`는 CSV를 다시 읽고 검증·임베딩을 마친 뒤 같은 컬렉션을 삭제·재생성합니다. 성공하면 그 객체의 원본 조회 데이터도 갱신됩니다. 다른 프로세스의 객체는 새로 만들어 같은 CSV를 읽고 `build_index()`로 재사용하세요. 기존 company_id를 유지하며 행 순서로 재발급하지 않습니다.

CSV 검증·임베딩 준비 실패 시 기존 인덱스는 유지됩니다. 삭제·적재 단계에서 실패하면 이전 인덱스를 자동 복원하지 않으며 해당 객체는 검색 준비 상태를 해제합니다. 원인을 해결한 뒤 `rebuild=True`로 재시도하세요. 재생성 중 다른 프로세스의 검색·동시 재생성은 지원하지 않습니다.

`close()`는 반복 호출해도 안전하며 클라이언트만 닫습니다. 서버·컬렉션·볼륨은 유지합니다. 일반 서버 종료 명령은 다음과 같습니다.

```bash
docker compose -p company-rag -f compose.qdrant.yml down
```

`down -v`는 저장된 볼륨까지 삭제하므로 일상 종료에 사용하지 않습니다.

## 결측과 오류 구분

| 상황 | 동작 |
| --- | --- |
| `get_company`에 없는 ID | `None` |
| 항목 조회에 없는 ID | `RAGCompanyNotFoundError` |
| 없는 필드·매출 요청·raw/정규화 키 혼동 | `RAGUnknownFieldError` |
| 존재하는 필드의 빈 값·`NULL` | 정규화 조회는 `None`; raw 조회는 원문 |
| CSV 형식·숫자·날짜 오류, 빈/중복 ID | `RAGDataError` |
| 잘못된 필터·입력 형식·컬렉션 차원 불일치 | `ValueError` |
| OpenAI 키·인증·쿼터·통신·벡터 응답 오류 | `RAGProviderError` |
| Qdrant 통신·작업·검색 결과 오류 | `RAGStoreError` |
| build 전 또는 재생성의 저장 단계 실패 후 검색 | `RAGIndexNotReady` |
| 종료한 객체의 메서드 호출 | `RAGClosedError` |

검색 대상이 없으면 `[]`를 반환합니다. 서버/API 실패를 빈 검색 결과로 바꾸지는 않습니다. `doc_count`는 준비 전·종료 후 0이고, 준비 후 서버 장애는 오류로 알립니다.

## 개발 검증

`requirements-rag.txt`는 런타임 설치용입니다. pytest는 로컬 개발 검증에만 사용합니다.

```bash
python -m pip install pytest
# 오프라인 단위 테스트
python -m pytest tests/test_base_rag.py tests/test_embeddings.py -q
# 실행 중인 Qdrant와 별도 테스트 컨테이너의 재시작까지 검증
RAG_TEST_QDRANT_URL=http://localhost:6333 RAG_TEST_DOCKER=1 python -m pytest tests/ -q
```

`tests/` 전체는 Git에서 제외되므로 위 명령은 로컬 테스트 파일이 있는 환경에서 실행합니다. 서버 테스트는 해당 환경변수를 지정하지 않으면 건너뜁니다. 위 테스트들은 OpenAI를 실제 호출하지 않습니다. 팀 설치용 `requirements-rag.txt`와 이 문서는 Git에 포함합니다. 실제 OpenAI 검색 품질·호출 시간은 기능 테스트 통과와 별도로 확인합니다.

## 검증 기록 — 2026-09-29

단위 테스트, Docker Qdrant 통합 테스트, 별도 검증 프로젝트의 볼륨 유지 `down` → `up`, 새 프로세스의 컬렉션 재사용을 확인했습니다. 재사용 시 문서 임베딩 호출은 0회였습니다.

실제 `text-embedding-3-small` 512차원으로 현재 CSV의 30개 문서를 색인하고 아래 12개 질문을 평가했습니다. 초기 색인은 4.114초·문서 입력 3,494토큰, 12개 질문은 합계 252토큰·평균 검색 0.189초였습니다. 해당 환경의 1회 측정값입니다.

| 질문 | 기대 ID | 검색 상위 5개 ID, 순서대로 | Recall@5 |
| --- | --- | --- | --- |
| 사람이 입고 보행을 돕는 로봇 | 21 | 4, 21, 1, 25, 10 | 1.0 |
| 방울토마토 수확을 자동화하는 로봇 | 22 | 22, 4, 15, 26, 8 | 1.0 |
| 로봇을 활용한 수직농장 | 28 | 16, 28, 5, 24, 4 | 1.0 |
| 제조사가 달라도 같은 API로 로봇 제어 | 24 | 24, 8, 16, 11, 10 | 1.0 |
| 농기계 자율주행 솔루션 | 12 | 12, 5, 19, 4, 11 | 1.0 |
| 라이다 기반 산업 현장 공간지능 | 30 | 30, 25, 29, 28, 21 | 1.0 |
| 4D 이미징 레이더 애플리케이션 | 27 | 27, 26, 14, 19, 25 | 1.0 |
| 건설현장 자재를 새벽에 배송하는 로봇 | 6 | 6, 16, 4, 1, 3 | 1.0 |
| 물류로봇용 고정밀 자율주행 소프트웨어 | 13 | 19, 13, 4, 25, 5 | 1.0 |
| 중력보상장치와 토크센서 기반 협동로봇 | 18 | 18, 16, 20, 23, 3 | 1.0 |
| 산업용로봇을 위한 AI 파운데이션 모델 | 14, 17 | 17, 14, 26, 11, 4 | 1.0 |
| 제조업 부품 조립용 휴머노이드 | 1 | 3, 1, 22, 21, 11 | 1.0 |

평균 Recall@5는 1.0이며, 기대 ID가 2개인 질문도 둘 다 상위 5개에 포함됐습니다. 최상위 결과가 기대 ID였던 질문은 8/12개입니다. 이는 CSV 설명에서 작성한 작은 평가표의 결과로, 일반적인 한국어 검색 성능을 보장하지 않습니다. `양자컴퓨팅 신약 기업`처럼 근거가 없는 질문에도 후보가 반환됐으므로 점수만으로 사실이나 조건 충족 여부를 판단하지 마세요.
