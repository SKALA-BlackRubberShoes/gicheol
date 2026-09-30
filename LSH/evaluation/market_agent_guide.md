# 시장성 평가 모듈 사용 및 연동 가이드

## 1. 이 모듈이 하는 일

시장성 평가 모듈은 기업 한 곳의 `company_id`를 입력받아 다음 결과를 만든다.

1. CSV에서 기업 기본정보 조회
2. 평가할 시장을 한 문장으로 정의
3. 공통 시장 보고서 PDF 검색
4. 기업별 최신 웹 자료 검색
5. 핵심 평가항목 5개를 각각 1~5점으로 평가
6. 시장성 총점 `/100`과 투자평가 반영점수 `/25` 계산

시장성 평가 기준은 [market_scoring_core.md](market_scoring_core.md)를 따른다.

## 2. 전체 흐름

```text
company_id
    │
    ├─ BaseRAG ─────────── 기업명·제품·서비스·분야 조회
    │
    ├─ 검색 계획 ───────── 시장 정의 + PDF/웹 검색어 생성
    │
    ├─ MarketRAG ───────── 공통 산업 보고서 근거 검색
    │
    ├─ OpenAIWebSearch ─── 고객·계약·도입·가격·ROI 최신 검색
    │
    ├─ 평가 LLM ────────── 5개 항목을 1~5점으로 판단
    │
    └─ scoring.py ──────── /100 및 /25 점수 계산
```

PDF 임베딩은 최초 준비 또는 PDF 변경 시에만 수행한다. 기업 30개를 평가할 때
매번 PDF를 다시 임베딩하지 않는다.

## 3. 파일별 역할

| 파일 | 역할 |
|---|---|
| `main/baseRAG/baseRAG.py` | 기존 30개 기업 CSV 조회. 변경하지 않음 |
| `LSH/marketRAG/marketRAG.py` | PDF 추출·청킹·Qdrant 색인·검색 |
| `LSH/marketEvaluation/schemas.py` | 검색 계획, 근거, 항목별 평가, 최종 결과 형식 |
| `LSH/marketEvaluation/scoring.py` | 5개 점수의 고정 가중합 계산 |
| `LSH/marketEvaluation/web_search.py` | 기업별 최신 웹 근거 수집 |
| `LSH/marketEvaluation/market_agent.py` | 전체 조사·평가 흐름 조립 |
| `LSH/marketEvaluation/market_node.py` | LangGraph State 연결 |
| `LSH/scripts/build_market_index.py` | PDF 색인 준비 명령 |
| `LSH/scripts/evaluate_market_company.py` | 기업 한 곳을 직접 실행하는 예제 |
| `LSH/market/sources.json` | PDF 문서명·기관·연도·URL 메타데이터 |
| `LSH/market/raw/*.pdf` | 시장성 평가용 공통 보고서 6개 |

## 4. 설치

저장소 루트에서 실행한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r LSH/marketRAG/requirements-market.txt
```

이 requirements 파일은 BaseRAG 의존성을 먼저 읽은 다음 `pypdf`를 추가한다.

## 5. 환경변수

모듈은 `.env`를 자동으로 읽지 않는다. 실행 프로세스에 환경변수를 설정한다.

```bash
export OPENAI_API_KEY="발급받은_API_키"

# 선택 사항. 생략하면 코드의 기본 모델을 사용한다.
export OPENAI_WEB_SEARCH_MODEL="gpt-5-mini"
export OPENAI_MARKET_MODEL="gpt-5-mini"
```

API 키를 코드, PDF 메타데이터, Git 커밋에 넣지 않는다.

## 6. Qdrant 시작

기존 BaseRAG와 같은 Qdrant 서버를 사용하되 컬렉션은 분리한다.

```bash
docker compose -p company-rag -f compose.qdrant.yml up -d
curl --fail http://localhost:6333/collections
```

| 컬렉션 | 내용 |
|---|---|
| `companies_small_512` | 기존 기업 CSV 문서 |
| `market_reference_pdf_512` | 시장 보고서 PDF 청크 |

## 7. PDF 색인 준비

최초 한 번:

```bash
python -m LSH.scripts.build_market_index
```

PDF 또는 `LSH/market/sources.json`을 수정한 경우:

```bash
python -m LSH.scripts.build_market_index --rebuild
```

`--rebuild`는 기존 시장 컬렉션을 교체한다. 그래프 실행 중에는 재생성하지 않는다.

## 8. 기업 한 곳 평가

CSV의 회사 ID가 `1`인 기업을 평가하는 예시다.

```bash
python -m LSH.scripts.evaluate_market_company 1
```

출력 예시 구조:

```json
{
  "company_id": "1",
  "company_name": "홀리데이로보틱스",
  "market_definition": "이 기업은 ... 판매한다.",
  "criteria": [
    {
      "criterion": "customer_willingness",
      "score": 4,
      "reason": "...",
      "source_ids": ["P001", "W001"]
    }
  ],
  "evidence": [
    {
      "source_id": "P001",
      "source_type": "pdf",
      "title": "World Robotics 2026 - Service Robots",
      "page": 4,
      "excerpt": "..."
    }
  ],
  "market_score_100": 71.0,
  "investment_score_25": 17.75,
  "market_risks": ["..."]
}
```

`P`로 시작하는 ID는 PDF, `W`로 시작하는 ID는 웹 출처다.

## 9. LangGraph 연결

시장성 노드는 `company_id`만 읽고 `market_evaluation`만 반환한다.

```python
from main.baseRAG import BaseRAG
from LSH.marketEvaluation import (
    MarketEvaluationAgent,
    OpenAIMarketEvaluationBackend,
    OpenAIWebSearch,
    make_market_node,
)
from LSH.marketRAG import MarketRAG

base_rag = BaseRAG()
market_rag = MarketRAG()
market_rag.build_index()

agent = MarketEvaluationAgent(
    base_rag=base_rag,
    market_rag=market_rag,
    web_search=OpenAIWebSearch(),
    evaluation_backend=OpenAIMarketEvaluationBackend(),
)
market_node = make_market_node(agent)
```

팀의 전체 State 예시:

```python
class InvestmentState(TypedDict, total=False):
    company_id: str
    competitor_evaluation: dict
    technology_evaluation: dict
    market_evaluation: dict
    investment_evaluation: dict
```

그래프에는 다음과 같이 등록한다.

```python
graph.add_node("market_evaluation", market_node)
```

경쟁사 비교, 기술 요약, 시장성 평가 노드는 같은 `company_id`를 받아 병렬로
실행할 수 있다. 투자 판단 노드는 세 결과가 모두 모인 뒤 실행한다.

## 10. 30개 기업 실행 시 주의점

- PDF 인덱스는 루프 밖에서 한 번만 준비한다.
- `BaseRAG`, `MarketRAG`, 웹 검색기와 평가 백엔드는 가능한 한 한 인스턴스를 재사용한다.
- 각 기업 결과는 `company_id`와 함께 저장한다.
- 투자 추천 여부와 무관하게 30개 기업을 모두 평가한 뒤 순위를 계산한다.
- 최고점 기업 보고서는 30개 평가가 모두 끝난 다음 생성한다.

## 11. 현재 간소화 범위

이번 버전은 핵심 평가표 구현에 집중한다. 다음 기능은 의도적으로 제외했다.

- 최소 증거 규칙에 따른 점수 상한 자동 조정
- N/A와 추가 조사 분기
- 시장성 등급
- 신뢰도 등급
- 근거 부족 시 자동 재검색 루프

출처 ID의 존재 여부와 5개 평가항목의 중복·누락은 코드에서 검증한다. 이는
평가기준 추가가 아니라 JSON 결과가 깨지지 않도록 보장하는 기본 데이터 검증이다.

## 12. 오프라인 테스트

OpenAI와 Qdrant를 호출하지 않는 테스트다.

```bash
python -m unittest discover -s LSH/marketEvaluation/tests -v
```

테스트 범위:

- PDF 6개가 페이지 정보를 가진 청크로 변환되는지
- 문서 6개와 텍스트 페이지 90개가 모두 포함되는지
- 5개 항목의 가중점수가 정확한지
- 가짜 PDF·웹·LLM으로 전체 에이전트 흐름이 동작하는지
- LangGraph 노드가 `market_evaluation`만 반환하는지

## 13. 오류가 발생할 때

| 오류 | 확인할 것 |
|---|---|
| `Set OPENAI_API_KEY` | 실행 셸에 API 키가 export됐는지 확인 |
| Qdrant 연결 오류 | Docker 컨테이너와 6333 포트 확인 |
| `Call build_index()` | 그래프 실행 전에 시장 PDF 색인 준비 |
| sources.json 관련 오류 | PDF 파일명과 manifest의 `file_name` 일치 여부 확인 |
| unknown source ID | 평가 LLM이 제공된 `Pxxx`, `Wxxx` 외 ID를 생성했는지 확인 |
| 컬렉션 차원 불일치 | `build_market_index.py --rebuild` 실행 |
