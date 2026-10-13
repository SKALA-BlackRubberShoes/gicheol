# 수영 V2 대상 확인과 세 관점 조사

SDD 1.0의 수영 담당인 대상 조건 확인과 시장성·이해관계자·도메인 적용 에이전트를 구현한다. 기존 투자 판단 실행 경로와 공통 DTO, Supervisor, 설정 및 의존성 파일은 수정하지 않는다. 각 worker는 주입받은 도구와 해당 요청 DTO만 사용하고 자기 결과를 반환한다.

## Git 기준

- 작업 브랜치: `feature/V2/sooyoung/research-agents`
- 공통 계약 기준: `2fb927db25d02e1bda5bdade02a9e25977a3b289` (`origin/feature/V2/dongwook/supervisor`)
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

common = dict(
    tools=research_tools, llm=llm_client, store=artifact_store,
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

`ArtifactStore`는 한 run에 묶여야 한다. sources와 evidence DTO는 각각 `sources/{safe_id}.json`, `evidence/{safe_id}.json`에 저장한다. 세 관점의 ID는 200자 이하 안전한 ASCII 식별자이면 그대로 사용하고, 다른 ID는 `canonical_hash(identifier)`를 파일명으로 사용한다. 대상 확인은 `eligibility-source-...`, `eligibility-evidence-...`의 안전한 hash 식별자를 만든다. `EvidenceResolver.resolve()`는 같은 규칙으로 DTO를 조회하고 source의 `content_ref`까지 검증해야 한다. 결과 원문·receipt 저장과 State 갱신은 공통 래퍼가 수행한다.

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

검증 환경은 Python 3.14.6, Pydantic 2.13.5, LangGraph 1.2.12, langchain-core 1.6.6, pytest 9.1.1이다. pytest는 검증 환경에 설치했으며 저장소 의존성 파일은 변경하지 않았다.

## 인수 시나리오

AT04는 실제 `EligibilityAgent`가 unknown을 반환하는 경우와 실제 `MarketAgent`의 근거가 부족한 경우를 각각 graph로 실행한다. 두 경우 모두 설정된 조사 상한까지 시도한 뒤 `incomplete`로 종료하며 writer 호출과 승인 보고서가 없음을 확인한다.

AT16의 `research_findings.json`은 시장과 도메인 적용의 두 `EvalFinding`과 기대 `Gap`을 제공한다. 실제 세 관점 worker를 초기 조사하고 시장·도메인 worker를 보완 호출해 전체 worker·모델 호출 다섯 번을 확인한다. 첫 보완 결과 수용 후 평가가 무효화되어도 나머지 gap과 immutable RepairPlan은 유지된다. 최종 `repair_cycles=1`, `evidence_epoch` 증가 2회, 보고서 revision 2 및 최신 gate를 검증한다. 이 시나리오는 최신 평가 통과와 publish 예약까지 검사하며 실제 PDF 렌더를 주장하지 않는다.

## 최종 검증 결과

2026년 10월 13일 실행한 최종 자동 테스트는 총 324건 모두 통과했다. 집중 리뷰 중 재현한 부정문·조건부 문장·다른 기업 발췌·URL 불일치 문제도 회귀 테스트로 보완했다.

| 묶음 | 결과 |
| --- | --- |
| 수영 대상 조건 확인 단위 테스트 | 48 passed |
| 수영 세 관점 단위 테스트 | 48 passed |
| 수영 AT04 두 경우와 AT16 | 3 passed |
| 동욱 공통 계약·State·Supervisor·graph 회귀 | 165 passed |
| 기존 투자 판단 경로 | 50 passed |
| 기존 시장 평가 경로 | 10 passed |

루트에서 아래 명령으로 담당 테스트와 공통 회귀 264건을 함께 실행한다. 기존 경로 60건은 두 별도 unittest 명령으로 실행했다. 동일한 focused 테스트 재실행은 위 총수에 중복 집계하지 않았다.

```bash
PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m pytest -q -W error::RuntimeWarning \
  tests/unit/v2/sooyoung tests/acceptance/v2/sooyoung \
  tests/unit/v2/dongwook tests/acceptance/v2/dongwook

PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m unittest discover -s main/agents/investment/tests -q

PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m unittest discover -s main/agents/market/tests -q
```

검증 대상은 이 담당 기능과 관련 회귀 경로다. 전체 저장소 테스트를 모두 통과했다고 주장하지 않으며 실제 외부 검색·모델·PDF·LangSmith 및 최종 CLI 연결은 서비스 통합 후 검증해야 한다. `git diff --check`도 통과했다. 테스트가 루트 ignore 규칙에 포함되어 있어 수영의 명시적인 `.py`와 `.json` 파일만 추적하며 ignore·공통 설정 파일은 수정하지 않았다.
