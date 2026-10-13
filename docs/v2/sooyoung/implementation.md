# 수영 V2 대상 확인과 세 관점 조사

SDD 1.0의 수영 담당인 대상 조건 확인과 시장성·이해관계자·도메인 적용 에이전트를 구현한다. 기존 투자 판단 실행 경로와 공통 DTO, Supervisor, 설정 및 의존성 파일은 수정하지 않는다. 각 worker는 주입받은 도구와 해당 요청 DTO만 사용하고 자기 결과를 반환한다.

## Git 기준

- PR #18 작업 브랜치: `feature/V2/suyeong/research-agents`
- 공통 계약 기준: `2fb927db25d02e1bda5bdade02a9e25977a3b289` (`origin/feature/V2/dongwook/supervisor`)
- 실제 RAG 연결 기준: `origin/main`의 `a0635742fa9fddcc5aa430d07dbfb80381557958`을 PR 브랜치에 병합했다. 수현의 이미 병합된 구현을 그대로 사용한다.
- 명세: `specs/sdd-spec.md` 1.0 및 [V2 역할 분담](https://chatgpt.com/space/page_930d7b5028148191828eda34d763458b)
- 기존 폴더의 작업은 보존하고 별도 worktree에서 구현했다. 작업 시작 시 통합 브랜치가 없어 준비된 동욱 계약 커밋에서 분기했다. 통합 시 동일 계약 커밋을 포함한 기준을 사용해야 한다.

## 호출 계약

| Worker | 입력 | 반환 | 고정 질문 |
| --- | --- | --- | --- |
| `EligibilityAgent` | `EligibilityRequest` | `EligibilityResult` | 비상장, Seed부터 Series C, Exit 미완료 |
| `MarketAgent` | `ResearchRequest` | `PerspectiveResult` | `market.demand`, `market.economics` |
| `StakeholdersAgent` | `ResearchRequest` | `PerspectiveResult` | `stakeholders.actors`, `stakeholders.burdens` |
| `DomainAgent` | `ResearchRequest` | `PerspectiveResult` | `domain.cases`, `domain.conditions` |

`ResearchTools`, `LLMClient`, `ArtifactStore`, `Domain` 및 모든 DTO는 `main/agents/supervisor/contracts.py`를 사용한다. Worker는 async callable이며 공통 node wrapper가 결과와 receipt를 저장한다. Supervisor만 관점 slot, gate, 시도 횟수와 repair cycle을 갱신한다.

다른 관점으로 요청할 내용은 공통 `Gap`의 `perspective`에 목표 관점을 지정해 반환한다. Worker에서 다른 관점의 결과를 읽거나 worker를 호출하지 않는다. 의미적으로 `supported`인지와 충분한지는 효진의 판정과 Supervisor의 게이트가 결정한다.

## Runtime 연결

주현의 `services.py`는 아래 네 callable을 동욱의 graph factory에 등록한다. 도구·모델·저장소의 실제 구현은 runtime에서 주입한다. `ResearchRequest.company_id`는 기업 이름이 아니라 immutable 기업 snapshot의 식별자이므로 세 관점 생성자에는 실행 입력의 기업명과 공식 URL도 함께 제공한다.

```python
from main.agents.eligibility.agent import EligibilityAgent
from main.agents.perspectives.market import MarketAgent
from main.agents.perspectives.stakeholders import StakeholdersAgent
from main.agents.perspectives.domain import DomainAgent
from main.agents.supervisor.contracts import ArtifactRef, ResearchRequest
from main.rag.neutral.tools import StoredEvidenceResolver

resolver = StoredEvidenceResolver(
    store=artifact_store, company_id=company_snapshot["company_id"],
    domain=run_input.domain,
)

common = dict(
    tools=research_tools, llm=llm_client, store=artifact_store, resolver=resolver,
    domain=run_input.domain, model=run_config.research_model,
    timeout_seconds=run_config.tool_timeout_seconds,
)
context = dict(
    company_id=company_snapshot["company_id"],
    company_name=run_input.company_name,
    official_url=str(run_input.official_url),
)
workers = {
    "eligibility": EligibilityAgent(**common),
    "market": MarketAgent(**common, **context),
    "stakeholders": StakeholdersAgent(**common, **context),
    "domain_application": DomainAgent(**common, **context),
}
```

기술 성숙도, writer, evaluator, publish는 각 담당 서비스를 추가한다. 새로운 package 디렉터리는 namespace package로 import되며 공통 `__init__.py`를 추가하지 않았다.

세 관점은 소유 파일 범위 안에서 중복을 줄이기 위해 `market.py`의 `_NeutralResearchAgent`를 공통 구현으로 상속한다. `StakeholdersAgent`와 `DomainAgent`가 시장 worker를 호출하는 구조는 아니다. 자기 관점의 고정 질문과 프롬프트만 사용하며 전체 State와 타 관점의 결과를 전달받지 않는다.

## 근거 저장과 재조사

세 관점은 한 요청당 최대 세 질의를 생성하고 도구마다 `top_k=5`, 기업·도메인·기준일 filter를 전달한다. 실제 PDF 검색 receipt의 질의, corpus version, filter, hit와 근거의 연결을 확인한다. 웹 검색 snippet은 인용하지 않고 원문 본문에서 연속된 발췌를 만들며 원본 참조를 저장소에서 검증한다. 본문을 확보하지 못한 상태와 정상적인 빈 검색 결과를 구별해 receipt와 limitations에 보존한다.

`gap_ids`, `instructions`, `query_hints`, `excluded_source_ids`, `attempt`, `prior_result_ref`를 후속 질의와 모델 입력에 반영한다. 제외된 출처, 기준일 뒤 자료, 다른 기업 근거, 잘못된 corpus·receipt, 조작된 evidence ID 및 task·attempt 불일치를 검증한다. Worker는 모델이 작성한 receipt를 실제 도구 receipt로 교체하며 결과 hash는 공통 canonical 규칙으로 계산한다.

아래는 fixture 회사의 추가 검색 요청 예시다. 실제 실행에서는 Supervisor가 task 식별자, 현재 gap과 이전 결과 참조를 채운다.

```python
request = ResearchRequest(
    task_id="fixture-market-attempt-2", company_id="acme",
    perspective="market", as_of_date="2026-10-13", attempt=2,
    corpus_version="1.0", prior_result_ref=prior_result_ref,
    gap_ids=["fixture-at16-market"],
    instructions=["시장 수요와 운영 비용을 독립 원문으로 재확인한다."],
    query_hints=["고객의 실제 도입 수요와 운영 비용 검증"],
    excluded_source_ids=["previous-corporate-source"],
)
result = await workers["market"](request)
```

`ArtifactStore`와 공유 resolver는 한 run에 묶여야 한다. 실제 연결에서는 네 worker에 동일한 `StoredEvidenceResolver`를 전달한다. Worker가 결과를 노출하기 전에 `resolver.record()`로 원문과 위치를 검증하고 저장소가 반환한 실제 `ArtifactRef`를 등록한다. `worker.source_refs`와 `worker.evidence_refs`는 공유 resolver의 registry와 동일한 사전이다. 키 이름으로 파일 경로를 추정해 복구하지 않는다. resolver 없이 독립 호출한 경우에도 worker가 저장소에서 실제로 받은 참조를 보존한다.

웹 `chunk_id`는 `{source_id}:{start}-{end}` 형식으로 전체 확보 본문의 정확한 문자 위치를 담는다. 공백 제거 후의 시작·종료 위치까지 보정하며 `text[start:end] == quote`가 실제 resolver를 통과해야 한다. `FetchedContent.text`가 20,000자까지만 제공되더라도 검증된 JSON 원본의 전체 `text`에서 발췌한다. 대상 확인의 source ID는 안정적인 hash를 사용한다. 결과 원문·receipt 저장과 State 갱신은 공통 래퍼가 수행한다.

runtime은 worker 완료 후 아래 registry 참조를 실행 메타데이터에 보존하고, 재개 시 같은 원본 저장소에서 복구한다. 모든 본문·DTO의 파일 해시는 `ArtifactStore`와 실제 resolver가 다시 검증한다.

```python
registry_ref = await artifact_store.save({
    "source_refs": {key: ref.model_dump(mode="json")
                    for key, ref in resolver.source_refs.items()},
    "evidence_refs": {key: ref.model_dump(mode="json")
                      for key, ref in resolver.evidence_refs.items()},
}, key="registries/sooyoung-evidence.json")

registry = await artifact_store.load(registry_ref)
resumed_resolver = StoredEvidenceResolver(
    store=artifact_store, company_id=company_snapshot["company_id"],
    domain=run_input.domain,
    source_refs={key: ArtifactRef.model_validate(value)
                 for key, value in registry["source_refs"].items()},
    evidence_refs={key: ArtifactRef.model_validate(value)
                   for key, value in registry["evidence_refs"].items()},
)
```

웹 출처를 새로 사용하는 실제 실행에는 store가 필요하다. store가 없는 세 관점 worker는 새 웹 본문을 인용하지 않고 저장소가 필요하다는 limitation을 반환한다. PDF 근거는 수현의 페이지 보존 검색 어댑터와 해당 근거 registry를 사용한다.

공통 `Claim.perspective`에는 eligibility 값이 없다. 대상 확인 사실은 `stakeholders` 값과 `eligibility.unlisted`, `eligibility.funding_stage`, `eligibility.exit_status` 접두어를 사용하는 저장 규칙을 명시했다. 이 claim은 `EligibilityResult`에만 들어가고 이해관계자의 고정 질문 답변으로 집계하지 않는다. 공통 스키마는 변경하지 않았다.

## 대상 조건 확인

`eligible`은 세 조건을 모두 원문으로 확인한 경우에만 반환한다. 확인된 위반이 있으면 `ineligible`, 나머지 미확인 조건이 있으면 `unknown`과 조건별 critical gap을 반환한다. 현재 상태가 변할 수 있는 비상장 여부와 투자 단계는 기준일의 명시적인 상태 확인을 요구하며, 과거 투자 유치 기사나 Exit 기사가 없다는 이유로 상태를 추정하지 않는다.

판정은 기업과 기준일이 확인된 실제 원문 문장의 조건 신호로 계산한다. 모델의 상태 주장이나 새 문구가 원문 판정을 덮어쓰지 못한다. 한국어 조사와 영어 표현, 부정문·예정 거래·인수 주체·다른 회사·조건부 상태를 구별하는 회귀 테스트를 포함한다. 지원 표현을 보수적으로 제한하므로 대상 기업과 조건을 명시한 확인 문장이 없으면 `unknown`으로 남는다. 이 경우 gap에 담긴 요청으로 원문을 추가 확보해야 한다.

대상 확인의 새 근거에는 저장소가 필수이며, 저장소가 없으면 검증 가능한 근거를 만들지 않는다. 페이지 번호가 없는 PDF 본문 fetch도 대상 조건의 확인 근거로 사용하지 않는다. PDF 자료만 있는 경우에는 수현의 페이지 보존 근거 adapter를 연결하는 후속 통합이 필요하다.

## 오류 경계

다른 기업이나 task의 결과, 근거·본문·출처·receipt의 무결성 오류를 정상적인 빈 결과로 바꾸지 않는다. 세 관점의 모델 출력은 한 번만 형식 보정을 요청하며 계속 잘못된 출력은 `MODEL_OUTPUT_INVALID`로 공통 래퍼에 전달한다. adapter가 이미 분류한 `WorkerFailure`는 분류를 유지한다. 검색·모델 인증 실패는 `CONFIG_OR_AUTH_ERROR`, 웹 본문 접근 제한은 `source_inaccessible`로 구분한다. 저장소의 파일 접근·hash 검증 오류는 산출물 무결성 오류로 처리한다.

## 검증 범위

결정론적 도구·LLM·저장소 fixture로 입력과 출력의 무결성 및 실제 Supervisor 흐름을 검증한다. 실제 검색 어댑터, 외부 모델, SQLite, PDF 발행, LangSmith 성공 실행은 각 담당 서비스 통합 후 검증할 범위다.

검증 환경은 Python 3.14.6, Pydantic 2.13.5, LangGraph 1.2.12, langchain-core 1.6.6, pytest 9.1.1이다. SQLite 후보 검증에는 runtime 요구사항의 langgraph-checkpoint-sqlite 3.1.2와 langgraph-checkpoint 4.3.0을 사용했다. 검증 환경에 필요한 패키지를 설치했으며 저장소 의존성 파일은 변경하지 않았다.

## 인수 시나리오

AT04는 실제 `EligibilityAgent`가 unknown을 반환하는 경우와 실제 `MarketAgent`의 근거가 부족한 경우를 각각 graph로 실행한다. 두 경우 모두 설정된 조사 상한까지 시도한 뒤 `incomplete`로 종료하며 writer 호출과 승인 보고서가 없음을 확인한다.

AT16의 `research_findings.json`은 시장과 도메인 적용의 두 `EvalFinding`과 기대 `Gap`을 제공한다. 실제 세 관점 worker를 초기 조사하고 시장·도메인 worker를 보완 호출해 전체 worker·모델 호출 다섯 번을 확인한다. 첫 보완 결과 수용 후 평가가 무효화되어도 나머지 gap과 immutable RepairPlan은 유지된다. 최종 `repair_cycles=1`, `evidence_epoch` 증가 2회, 보고서 revision 2 및 최신 gate를 검증한다. 이 시나리오는 최신 평가 통과와 publish 예약까지 검사하며 실제 PDF 렌더를 주장하지 않는다.

## 최종 검증 결과

2026년 10월 13일 PR #18의 병합 보류 이슈 #22·#23·#24를 수정한 뒤, 최신 main과 연결한 회귀 433건 및 기존 경로 60건이 통과했다. 최초 구현의 324건 통과에 없던 실제 resolver 연결과 날짜·단계 범위 사례를 추가했다.

| 묶음 | 결과 |
| --- | --- |
| 수영 대상 조건 확인 단위 테스트 | 70 passed |
| 수영 세 관점 단위 테스트 | 62 passed |
| 수영 AT04 두 경우와 AT16 | 3 passed |
| 실제 HTTP 본문 확보·네 worker·resolver·registry 복구 | 7 passed |
| 동욱 공통 계약·State·Supervisor·graph 회귀 | 165 passed |
| 수현 RAG·기술 조사 단위 및 AT08 회귀 | 126 passed |
| 기존 투자 판단 경로 | 50 passed |
| 기존 시장 평가 경로 | 10 passed |

PR 작업 폴더의 pytest 결과는 `433 passed, 1 skipped in 14.15s`이다. 아직 main에 없는 runtime 모듈을 요구하는 SQLite 테스트 모듈만 skip된다. 별도 검증 후보에 주현의 `9d14ce6` runtime 구현을 병합해 실제 파일 저장·SQLite 재개 테스트 네 경우를 모두 실행했다. 실제 저장소 회귀까지 함께 묶은 후보 결과는 아래에 구분한다. 기존 경로 60건은 두 별도 unittest 명령으로 실행했다.

runtime 후보에서 실제 웹 연결, 파일 저장, SQLite 복구와 runtime graph 회귀를 함께 실행한 결과는 `49 passed in 2.32s`이다. 이 가운데 웹 테스트 7건은 위 PR 묶음과 중복이다. SQLite 재개 네 경우는 모두 실행됐으며, runtime의 설정 경고와 기존 검색 adapter의 Python 3.14 deprecation 경고는 실패로 집계하지 않았다.

```bash
PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m pytest -q -W error::RuntimeWarning \
  tests/unit/v2/sooyoung tests/acceptance/v2/sooyoung \
  tests/unit/v2/dongwook tests/acceptance/v2/dongwook \
  tests/unit/v2/suhyeon tests/acceptance/v2/suhyeon

PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m unittest discover -s main/agents/investment/tests -q

PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m unittest discover -s main/agents/market/tests -q
```

## PR 병합 보류 이슈 수정

| 이슈 | 수정과 검증 |
| --- | --- |
| [#22](https://github.com/SKALA-BlackRubberShoes/gicheol/issues/22) | 네 worker가 전체 확보 본문의 정확한 문자 span을 기록하고 실제 resolver에 등록한다. Unicode 공백, 20,000자 뒤의 근거, registry JSON 재조회와 원문·위치·quote 변조 거절을 검증했다. |
| [#23](https://github.com/SKALA-BlackRubberShoes/gicheol/issues/23) | 날짜를 status assertion에 결합한다. 2025년 비상장 상태에 2026년 제품 출시가 연결되어도 현재 상태로 승인하지 않으며 unknown을 유지한다. |
| [#24](https://github.com/SKALA-BlackRubberShoes/gicheol/issues/24) | C/D와 시리즈 C 이상은 unknown, 명확한 현재 D는 ineligible이다. 과거 D·현재 C, 한영 연결 문장과 정상 Seed/A/B/C를 함께 검증했다. |

`test_runtime_sqlite_resume.py`는 실제 `FileArtifactStore`, `StoredEvidenceResolver`, Supervisor graph와 `AsyncSqliteSaver`를 사용한다. eligibility worker 직후 중단한 checkpoint와 registry를 파일에서 읽고 새 객체로 재개해 worker 재호출 없이 결과를 한 번 수용한다. eligible, 혼합 날짜 unknown, 단계 범위 unknown, 현재 D ineligible의 네 경우 모두 writer 호출이 없고 승인 보고서가 생성되지 않음을 확인했다. runtime 코드는 검증 후보에서만 병합했으며 PR에는 수영 담당 수정과 테스트만 반영한다.

검증 대상은 이 담당 기능과 관련 회귀 경로다. 전체 저장소 테스트를 모두 통과했다고 주장하지 않으며 외부 HTTP와 LLM 응답은 명시적인 결정론적 fixture다. 실제 SQLite와 파일 저장·재개 검증을 외부 모델·PDF·LangSmith 및 최종 CLI 성공 실행으로 확대 해석하지 않는다. `git diff --check`도 통과했다. 테스트가 루트 ignore 규칙에 포함되어 있어 수영의 명시적인 `.py`와 `.json` 파일만 추적하며 ignore·공통 설정 파일은 수정하지 않았다.
