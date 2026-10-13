# 수현 V2 RAG와 기술 조사 연결

SDD 1.0과 동욱 공통 계약 커밋 `2fb927db25d02e1bda5bdade02a9e25977a3b289`를 사용한다. 수현 코드는 기존 투자 판단 경로와 분리된 namespace 모듈이며 공통 DTO, State, dependency, CLI, package 등록 파일을 수정하지 않는다.

## 조립 순서

1. 주현의 실행별 `ArtifactStore`와 명시적인 회사 ID·이름·별칭, `Domain`, 기준일을 주입한다. store는 `load`와 `verify`에서 실제 bytes SHA와 경로를 확인하고, `save`를 원자적으로 구현해야 한다.
2. `legacy_corpus_documents`로 기존 기술 자료를 읽거나 `DocumentInput`을 직접 지정한다. 파생 요약은 기본 제외한다. 모든 PDF 전체 페이지를 사전 파싱해 동일 SHA 파일은 한 번만 계산하고 실제 200페이지 한도를 검사한다. `ingest_documents`는 `Corpus`와 페이지·발췌 범위·원본 SHA·manifest를 반환한다. CSV 회사 ID와 별칭을 대조한다.
3. `SentenceTransformerEmbeddings`에 고정된 모델 revision과 cache/device를 명시한다. 기본은 bge-m3 dense 1024차원이다. E5 base는 768차원이며 query/passage prefix를 적용한다. 두 모델을 비교할 때 동일 청크가 두 tokenizer 모두 400 tokens 이하가 되도록 `count_tokens`를 제공한다. 모델 로드는 자동 대체하지 않는다.
4. `NeutralRetriever(corpus=..., embedding=..., store=..., client=...)`를 만들고 `await initialize()`한다. Qdrant 로컬 또는 서버 client를 주입한다. collection은 `neutral_v2_suhyeon_{index_revision}`이며 기존 collection의 차원·모델·revision·정규화·corpus hash·chunk count가 다르면 거절한다. 기존 512차원 OpenAI 색인과 혼용하지 않는다.
5. `NeutralResearchTools(retriever=..., search_provider=...)`를 만든다. 선택적 `LegacySearchProvider`는 기존 `OpenAIWebSearch`를 URL 발견에만 사용하고 HTTP 본문을 별도 확보한다. `classify_source`로 출처 유형·발행처·독립 publisher group·기업 통제 여부를 제공한다. 알 수 없는 날짜는 null로 유지한다. source metadata가 없는 검색 snippet으로 Evidence를 만들지 않는다.
6. `StoredEvidenceResolver`에 store, 회사·domain, source/evidence ArtifactRef registry를 주입한다. 기술 worker에 같은 resolver를 전달하고 공통 `workers['technology_maturity']`에 `TechnologyWorker`를 등록한다. 다른 관점 에이전트에 `NeutralResearchTools`를 재사용할 수 있다.

```python
from qdrant_client import QdrantClient
from main.agents.supervisor.contracts import Domain
from main.rag.neutral.ingestion import ingest_documents, legacy_corpus_documents
from main.rag.neutral.embeddings import SentenceTransformerEmbeddings
from main.rag.neutral.retrieval import NeutralRetriever
from main.rag.neutral.tools import NeutralResearchTools, StoredEvidenceResolver
from main.agents.perspectives.technology import TechnologyWorker

domain = Domain.PHYSICAL_AI_ROBOTICS
documents = legacy_corpus_documents('docs/data/technology_competition')
corpus = await ingest_documents(documents, store=store, corpus_version='1.0')
embedding = SentenceTransformerEmbeddings(cache_folder=model_cache)
retriever = NeutralRetriever(corpus=corpus, embedding=embedding, store=store,
                            client=QdrantClient(path=index_directory))
await retriever.initialize()
resolver = StoredEvidenceResolver(store=store, company_id='14', domain=domain,
                                 source_refs=retriever.source_refs,
                                 evidence_refs=retriever.evidence_refs)
technology_worker = TechnologyWorker(
    tools=NeutralResearchTools(retriever=retriever), store=store, llm=llm,
    model=research_model, company_id='14', company_name='리얼월드',
    domain=domain, corpus_version=corpus.corpus_version, resolver=resolver,
)
```

store, llm, research_model, model_cache, index_directory는 주현 runtime에서 제공한다. 예시는 투자가 아닌 중립 조사 worker만 조립한다. 전체 Supervisor 연결은 `docs/v2/dongwook/interfaces.md`를 따른다.

## 저장과 검증 경계

공통 store는 JSON object 저장만 지원하므로 원본 PDF bytes를 base64로 보존한 JSON envelope에 원본 PDF SHA와 실제 페이지 텍스트를 함께 넣는다. `Source.sha256`는 JSON `content_ref`의 bytes hash이며 원본 PDF hash와 구별한다. `original_file_id`는 원본 `.pdf` 식별자를 보존해 Supervisor의 실제 PDF gate와 연결한다. PDF 페이지와 청크 문자는 원문 추출 결과의 실제 위치와 일치해야 한다.

기술 worker는 실제 RAG 검색을 최대 3개 query로 수행하며 `ResearchRequest.query_hints`를 우선한다. `excluded_source_ids`는 최종 근거에서 제외한다. 이전 결과에 빈 결과만 있었으면 동일 query set 반복을 거절하고, 실제 일시 오류 receipt가 있을 때만 같은 query 재시도를 허용한다. request와 result의 task·company·perspective·attempt·corpus를 보존하고 `tech.principle`, `tech.validation`을 사용한다.

회사·domain·날짜 filter, 실제 ranked hits와 `success`, `empty`, `source_inaccessible`, `error`를 receipt에 남긴다. 알려진 미래 날짜는 제외하고 알 수 없는 날짜를 꾸며내지 않는다. 원문 접근에 실패해도 검색 snippet을 실제 본문으로 승격하지 않는다. 웹 Evidence의 chunk ID는 본문 문자 위치를, PDF는 실제 page number를 보존한다.

`StoredEvidenceResolver.record/resolve`는 참조와 원문 페이지·본문 위치·quote hash를 확인한다. runtime는 resolver의 `source_refs`와 `evidence_refs`, retrieval `receipt_refs`, corpus/index manifest refs를 실행 registry에 저장하고 재개 시 그대로 주입해야 한다. 변경된 같은 Source/Evidence ID를 덮어쓰지 않는다.

원문 일치 검사는 주장 지지를 판정하지 않는다. worker는 `ClaimSupport`나 supported/sufficient/pass를 발급하지 않는다. 효진의 검증 서비스가 의미적 지지와 독립 출처 충분성을 판정하며 Supervisor가 gate를 갱신한다. 다른 관점이 필요하면 Gap을 반환하고 해당 worker를 직접 호출하지 않는다.

## 주현과 다른 담당자에게 필요한 연결

- runtime dependency에 `sentence-transformers`, `torch`, `transformers`, `qdrant-client`, `pypdf`, `httpx`, `tiktoken`을 고정하고 실제 설치 환경을 lockfile에 반영한다. 이번 브랜치는 공통 dependency 파일을 수정하지 않았다.
- config에 선택 모델의 정확한 revision·dimension·정규화·collection/index revision과 tokenizer 설정을 기록한다. 후보를 바꿀 때 새 색인을 생성한다.
- 외부 PDF fetch를 사용하면 `LegacySearchProvider.initial_pdf_pages`에 초기 corpus 전체 페이지 수를 주입하고, 재개 시 이미 확보한 PDF 풀의 페이지 수를 포함해 200페이지 한도를 유지한다. 새 provider 생성으로 기존 페이지 예산을 초기화하지 않는다.
- 웹 source classification은 실제 publisher group을 지정한다. 같은 발행처의 재배포 URL을 독립 출처로 세지 않는다.
- SearchProvider의 기존 동기 검색 backend는 자체 network timeout을 갖는다. runtime가 모든 async 도구와 LLM await의 timeout·재시도·deadline을 적용한다.
- 공통 ResearchTools.retrieve 계약에는 excluded_source_ids 인자가 없다. worker는 반환 근거에서 제외하며 NeutralRetriever의 직접 API는 제외 filter를 지원한다. 통합 시 공통 계약 확장이 필요하면 동욱이 반영한다.
- 사람에게 정답 문서를 검토받아 검색 평가 fixture의 `label_provenance`를 승인된 상태로 갱신한다. agent가 작성한 provisional label의 수치를 사람 검증 결과로 표기하지 않는다.

## 검증 명령

```bash
python -m pytest -q -W error::RuntimeWarning \
  tests/unit/v2/suhyeon tests/acceptance/v2/suhyeon
```

실제 후보 비교는 `docs/v2/suhyeon/run-embedding-benchmark.py`와 `embedding-comparison.md`를 따른다. deterministic LLM과 실제 PDF/Qdrant를 사용하는 담당 acceptance 검증은 전체 runtime·실제 LLM·Judge·PDF 발행 검증과 구분한다.
