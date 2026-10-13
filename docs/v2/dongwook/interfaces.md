# 동욱 V2 연결 계약
이 브랜치는 SDD 1.0의 동욱 담당인 계약, State, Supervisor, 그래프와 노드 래퍼를 구현한다. 실제 검색·근거 의미 검증·보고서 렌더·산출물 저장·SQLite·CLI는 다른 담당자의 서비스를 주입한다.

## 기준과 검증 환경
- 브랜치: feature/V2/dongwook/supervisor
- 기준: origin/main ae83a9735966a38c90a22ab807bea64ecf378392
- 명세: specs/sdd-spec.md
- 검증 환경: Python 3.12.14, LangGraph 1.2.12, langchain-core 1.6.6, Pydantic 2.13.5, pytest 9.1.1
- 기존 의존성·설정·README·package __init__는 변경하지 않았다. 새 디렉터리는 namespace package로 import한다.

## 실행 연결
1. contracts.py의 async ArtifactStore, EvidenceResolver와 도구 Protocol을 구현한다. store 인스턴스는 단일 run의 artifact root에 연결한다.
2. RunConfig에 supervisor_model, research_model, report_model, judge_model과 실제 prompt_hash를 제공한다.
3. initialize_run(run_input, run_config, store=store)를 호출해 입력 검증을 통과한 최소 State와 immutable snapshot을 만든다.
4. Supervisor(store, evidence_resolver, assess_evidence, decision_sink=..., limits=run_config)를 만든다. 최초 config 조회에도 고정한 tool_timeout을 적용하려면 limits를 제공한다. 생략 시 최초 조회는 기본 30초와 State의 남은 deadline으로 제한한다.
5. build_supervisor_graph(supervisor=..., workers=..., publish_worker=..., store=..., run_config=..., checkpointer=...)에 async 호출을 지원하는 SQLite checkpointer를 명시적으로 주입한다. 테스트에서만 InMemorySaver를 사용한다.
6. graph.ainvoke(state, {"configurable": {"thread_id": state["thread_id"]}})로 시작한다. recursion_limit은 RunConfig 값이 기본 적용된다.
7. resume 전에 checkpoint의 State를 읽고 validate_resume_versions(state, current_versions)를 호출한다. 원본 config를 다시 사용한다. completed/incomplete/failed 실행은 같은 run_id로 시작하지 않는다.

workers에는 eligibility, technology_maturity, market, stakeholders, domain_application, writer, evaluator를 정확히 등록한다. worker는 async 함수로 typed request 하나만 받고 자기 DTO 또는 분류된 ResultEnvelope를 반환한다. 전체 State나 다른 worker 호출 경로를 받지 않는다. publish_worker는 별도 PublishRequest를 받고 PublishResult를 반환한다.

## 서비스별 입출력
| 서비스 | 입력 | 출력 |
| --- | --- | --- |
| eligibility | EligibilityRequest | EligibilityResult |
| 네 관점 worker | ResearchRequest | PerspectiveResult |
| assess_evidence | SufficiencyRequest | 네 관점 SufficiencyVerdict 목록 |
| writer | WriterRequest | DraftReport |
| evaluator | EvaluatorRequest | EvalVerdict |
| publish_worker | PublishRequest | PublishResult |
| EvidenceResolver.resolve | evidence ID 목록 | EvidenceBundle |

assess_evidence는 ClaimSupport를 ArtifactStore에 저장하고 verdict의 claim_support_refs에 넣는다. 각 ClaimSupport의 claim_hash는 전체 Claim DTO canonical_hash, evidence_hashes는 evidence ID에서 전체 Evidence DTO canonical_hash로의 사전이다. supported 의미 검증은 실제 발췌와 주장 비교를 사용한다. 독립 출처·필수 질문·기준일·실제 PDF receipt 등 구조적 조건은 Supervisor가 별도로 검사한다.

## ArtifactStore의 필수 계약
- load(ref): 파일 경로와 실제 bytes SHA를 검증한 뒤 JSON 객체를 반환한다.
- verify(ref): 모든 미디어의 경로·존재·symlink·실제 bytes SHA를 검증한다. 원본 PDF와 text를 JSON으로 디코딩하지 않는다.
- save(payload, key=..., media_type=...): 임시 파일·flush·atomic rename으로 저장하고 실제 bytes hash가 있는 ArtifactRef를 반환한다.
- find_receipt(task_id, input_hash), save_receipt(task, result): 정확히 같은 입력의 task 결과를 저장·조회한다. payload와 결과 참조 무결성을 검증한다.
- find_manifest(run_id, report_revision): 해당 report key의 completed manifest만 반환한다. staging PDF만 존재하면 None을 반환한다.
- commit_manifest(manifest): 관련 파일과 hash, gate·평가·PDF 검증을 확인하고 completed manifest를 마지막에 원자적으로 commit한다. 동일 report key의 재실행은 동일한 완료 결과를 반환한다.

writer와 publish는 bool만으로 승인하지 않고 현재 GateCertificate·result hash·epoch와 ClaimSupport 및 파일을 확인한다. writer·evaluator에는 승인 당시 저장한 immutable EvidenceBundle을 전달한다. worker 경계는 실제 request를 승인된 결과·대상 조건·근거·지원 기록과 대조하며 receipt replay 전에도 같은 검사를 적용한다. evaluator의 승인 claim에는 eligibility 사실도 포함한다. 동일 ID와 동일 DTO는 deduplicate_claims로 한 번만 전달하고, 같은 ID에 서로 다른 DTO가 있으면 거부한다.

publish는 staging 검증 후 committed manifest와 PDF 참조를 검증한 뒤 completed로 전환한다. commit 응답이 timeout된 경우 동일 run_id·report_revision의 완료 manifest를 조회·검증해 재사용한다. deadline 이후 pending publish를 재개하거나 commit 직후 deadline이 지난 경우에도 이미 완료된 manifest만 유한한 tool timeout 안에서 읽고 검증한다. completion_recovery facade는 load·verify·find_manifest만 허용한다. 완료 기록이 없으면 새 렌더·commit을 시작하지 않고 BUDGET_EXHAUSTED로 종료한다. 만료된 평가, 유실 파일, 참조 무결성 오류는 새 파일을 임의 생성해 통과시키지 않는다.

## 시간 제한과 재시도
Supervisor.step(state, limits=run_config), graph factory와 각 노드 래퍼는 동일한 고정 RunConfig를 사용한다. bounded_call과 BoundedArtifactStore는 외부 호출에 min(호출 timeout, 남은 deadline)을 적용하는 facade이며 저장소 I/O를 구현하지 않는다. load·save·verify·receipt·manifest 조회·commit·decision_sink까지 tool timeout을 적용하고 worker·publish callback에는 llm_timeout을 적용한다. adapter는 async 계약과 취소를 준수해야 하며 blocking I/O는 adapter에서 처리한다.

validate_current_gate(state, store, config, now=...)는 운영에서 현재 UTC를 사용한다. 고정 시각 테스트나 standalone 호출에서 시각을 주입하려면 now를 명시한다. 근거 refresh 뒤 eligibility가 unknown이 되면 구체적인 gap ID와 지시를 담아 eligibility를 재호출하고, 시도 예산을 소진하면 ELIGIBILITY_UNKNOWN으로 종료한다.

make_abort_node(store=..., now=..., limits=run_config)는 deadline 이후에도 유한한 tool timeout 안에서 진단 기록을 시도한다. 저장 실패·timeout 시 기존 terminal status를 유지하고 node_status.abort=diagnostics_failed를 남긴다. 증거·예산 부족은 incomplete, 영구 설정·인증·산출물·PDF 오류는 failed로 분류한다.

## State와 결정 로그
State에는 snapshot 및 result 참조, 네 perspective slot, active_task·pending_result, 제어 counter·deadline·versions를 저장한다. 본문은 외부 artifact에 둔다. UTF-8 canonical JSON 32 KiB를 초과한 결과를 수용했을 때는 이전 유효 checkpoint를 복원하고 failed/STATE_TOO_LARGE로 종료한다. 전체 결과·gap은 외부 artifact에 보존하고 State를 임의로 잘라 통과시키지 않는다. 유효하지 않은 최초 State도 조용히 복구하지 않고 거부한다.

Supervisor Decision은 decisions/{decision_id}.json에 저장한다. decision_sink는 런타임에서 decisions.jsonl과 LangSmith 이벤트로 전달하는 adapter다. 실제 모델과 trace 연결 및 SQLite 구현은 주현의 runtime에서 제공한다.

## 검증 명령
저장소의 Python 환경을 활성화한 뒤 실행한다.

```bash
PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m pytest -q -W error::RuntimeWarning tests/unit/v2/dongwook tests/acceptance/v2/dongwook
```

테스트에는 DTO 경계, 동적 라우팅, writer guard, 복수 관점 repair, 마지막 허용 조사·수정에서 통과, receipt replay, 현재 hash·epoch, staging/manifest crash window를 포함한다. timeout·근거 변경·대상 사실 인용·eligibility 재시도·영구 publish 실패·State overflow의 리뷰 회귀 사례 32개와 AT19 완료 복구 사례 15개를 포함해 총 165개가 통과했다. acceptance의 renderer·store·Judge는 명시적인 deterministic fixture다. 실제 RAG·모델·PDF·LangSmith 성공 증빙은 전체 서비스 통합 후 별도로 검증한다.

기존 루트 pytest는 동일 이름의 report/investment test_adapter.py·test_agent.py로 collection 오류가 난다. 기존 테스트는 디렉터리별로 실행해 비교하며, baseline 보고서 실패 2건은 이번 브랜치의 새 실패와 구분해 기록한다. 기존 /tests/ ignore 규칙을 바꾸지 않고 이 브랜치의 테스트 파일만 명시적으로 추적한다.
