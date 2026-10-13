# 효진 근거 검증과 품질 평가 연결 계약

SDD 1.0의 FR03·FR05·FR08을 구현한다. 공통 계약 기준은 동욱 브랜치 `feature/V2/dongwook/supervisor`의 `2fb927db25d02e1bda5bdade02a9e25977a3b289`이다. 이 브랜치에는 다른 담당자의 DTO, State, 그래프, CLI, 저장소, 공통 설정을 복제하거나 병합하지 않았다. 신규 디렉터리는 기존 동욱 구현과 같은 namespace package로 import한다.

## 서비스 입출력

| 서비스 | 입력 | 출력 |
| --- | --- | --- |
| `ClaimSupportVerifier.verify` | Claim, Source·Evidence 목록, company_id, as_of_date | 공통 ClaimSupport |
| `SufficiencyAssessor.__call__` | 공통 SufficiencyRequest | 네 관점의 공통 SufficiencyVerdict 목록 |
| `ReportEvaluator.__call__` | 공통 EvaluatorRequest | 공통 EvalVerdict |
| `LLMClientJudge.__call__` | 분리된 system prompt, evidence payload, response schema | 엄격히 검증한 Judge 응답 |

모든 I/O adapter는 async 함수이고 취소를 준수해야 한다. 호출별 timeout을 적용한다. 전체 실행 deadline·재시도·State 갱신은 공통 wrapper와 Supervisor가 맡는다. Judge 예외·timeout·형식 오류는 ClaimSupport의 unverified 또는 실패 verdict가 된다. 입력 DTO·저장소 오류는 예외를 전달하며 성공으로 대체하지 않는다.

## 공통 계약 주입

공통 브랜치가 통합된 실행 환경에서 아래와 같이 연결한다. 이 예시의 store·excerpt_loader·receipt_validator·llm_client는 담당 서비스가 제공해야 하는 실제 adapter다.

```python
from main.agents.supervisor import contracts as c
from main.agents.quality.claim_support import ClaimSupportVerifier
from main.agents.quality.sufficiency import SufficiencyAssessor
from main.agents.quality.evaluator import ReportEvaluator
from main.agents.quality.rubrics import LLMClientJudge, RUBRIC_VERSION

def contract_factory(name, payload):
    return getattr(c, name).model_validate(payload)

judge = LLMClientJudge(client=llm_client, model=run_config.judge_model)
verifier = ClaimSupportVerifier(
    judge=judge, excerpt_loader=excerpt_loader,
    contract_factory=contract_factory, hash_fn=c.canonical_hash,
    timeout_seconds=run_config.tool_timeout_seconds,
)

async def save_support(payload, *, key):
    return await store.save(payload, key=key, media_type="application/json")

assess_evidence = SufficiencyAssessor(
    verifier=verifier, judge=judge, save_claim_support=save_support,
    receipt_validator=receipt_validator, contract_factory=contract_factory,
    hash_fn=c.canonical_hash, semantic_hash_fn=c.semantic_hash,
    company_id=company_id, corpus_version=corpus_version,
    index_version=index_version, timeout_seconds=run_config.tool_timeout_seconds,
)
evaluator = ReportEvaluator(
    verifier=verifier, judge=judge, contract_factory=contract_factory,
    hash_fn=c.canonical_hash, company_id=company_id,
    judge_model=run_config.judge_model,
    timeout_seconds=run_config.llm_timeout_seconds,
)
# RunConfig.rubric_version도 RUBRIC_VERSION과 일치시킨다.
# Supervisor(..., assess_evidence=assess_evidence)
# workers["evaluator"] = evaluator
```

`LLMClientJudge`는 확정된 `LLMClient.generate(model, system_prompt, payload, response_model)`을 호출한다. Pydantic은 이 adapter를 사용할 때만 로드한다. Judge용 응답 모델만 만들며 Source·Evidence·ClaimSupport·SufficiencyVerdict·EvalVerdict의 공통 DTO는 만들지 않는다. 모델 ID와 provider 설정을 추측하지 않는다.

## 원문과 검색 receipt의 신뢰 경계

`excerpt_loader(source, evidence)`는 ArtifactStore의 raw-byte 무결성을 확인하고 지정된 원본 페이지 또는 chunk의 실제 텍스트를 반환해야 한다. Source.sha256와 content_ref.sha256를 일치시킨다. PDF의 byte hash를 추출된 텍스트 hash로 대체하지 않는다. 웹 검색 snippet, worker 요약, 임의로 합성한 quote를 반환해서는 안 된다. 품질 서비스는 quote_hash, 실제 quote 포함 여부, source·company·chunk·page·기준일을 추가 검사한다. 발행일 미상은 null로 유지한다.

`receipt_validator(receipt)`는 저장된 검색 실행 기록과 receipt를 대조하고 검증된 경우에만 실제 bool `True`를 반환한다. 성공 상태 문자열만 확인해서는 안 된다. 품질 서비스는 company 필터·corpus/index 버전·실제로 지원 주장에 사용한 PDF의 source/chunk hit를 별도로 검사한다. canonical DTO 직렬화와 저장 byte 검증을 구분한다.

출처 provenance의 `publisher_group`은 원 publisher 기준으로 정규화해야 한다. 품질 서비스는 group·원문 byte hash·canonical URL·original_file_id의 연결 관계를 병합한다. 같은 홍보문을 외부 URL에 재배포해도 기업 통제 원문 그룹의 외부 검증으로 인정하지 않는다. 상충 자료 원문도 Judge에 전달하며, 승인 bundle의 원문을 확보하지 못하면 보고서 pass를 거절한다.

## 해시와 저장

ClaimSupport.claim_hash는 전체 Claim DTO의 공통 canonical_hash다. evidence_hashes는 **인용 evidence ID 전체 집합**에서 전체 Evidence DTO canonical_hash로의 사전이다. 모든 인용이 주장에 기여해야 supported로 승인한다. 일부 인용만 관련 있으면 partial이다. 텍스트·kind·limitations·evidence 집합·quote·verifier_version 변경은 과거 지원 판정을 무효화한다.

PerspectiveResult와 SufficiencyVerdict는 각자 전체 산출물의 공통 semantic_hash를 사용한다. SufficiencyVerdict.result_hash는 PerspectiveResult.result_hash가 아니다. 보고서 hash는 sections·claim_ids·citations·evidence_epoch만 포함하며 revision, document_ref, 자기 hash는 제외한다. EvalVerdict는 report_revision·report_hash·evidence_epoch를 그대로 반환한다. 최신성 및 발행 guard는 Supervisor와 publish가 대조한다.

ClaimSupport는 `claim-supports/{canonical_hash(support)}.json` key로 저장하고 실제 ArtifactRef를 verdict에 포함한다. 사람별 run artifact root와 별도 run_id를 사용해야 한다. dict factory는 테스트 전용이며 실서비스는 공통 DTO 검증 factory를 필수로 제공한다.

## 보고서와 보완 계약

section ID는 `SUMMARY`, `company_scope`, `technology_maturity`, `market`, `stakeholders`, `domain_application`, `limitations`, `REFERENCE` 순서다. 첫·마지막 title도 SUMMARY와 REFERENCE다. fact·inference 문단의 claim/evidence와 citation을 대조한다. REFERENCE 문단에는 해당 출처의 evidence_ids를 보존해 본문에서 인용한 source 집합과 정확히 비교한다. 참고문헌 문단에는 새 claim을 만들지 않고 kind=unknown을 사용할 수 있다. connective 문단에는 claim/evidence 연결을 추가하지 않는다. 수현·웅희는 이 provenance 연결을 유지해야 한다.

네 품질 점수는 groundedness·neutrality·bias_control·perspective_coverage 각각 0–4 정수다. 모든 형식 검사 통과, 네 점수 모두 3 이상, critical finding 없음의 conjunction으로 pass를 계산한다. bool·숫자 문자열·누락 항목·잘못된 finding 참조는 거절한다. Judge의 overall_pass 자기 선언을 받지 않는다.

표현·누락된 기존 자료·구성 문제는 rewrite, 승인 bundle에도 없는 주장 근거나 관점 답변은 해당 관점 research다. 복수 finding을 모두 반환하며 research가 하나라도 있으면 최상위 repair_kind=research다. 같은 gap의 critical severity와 query_hints를 병합해 보존한다. GateCertificate·State·RepairPlan은 수정하지 않는다.

## 로컬 검증

fixture factory로 외부 라이브러리나 API 없이 실행할 수 있다. LLMClient adapter 테스트 2개는 Pydantic이 있는 실제 계약 검증 환경에서 실행한다.

```bash
python3 -m unittest discover -s tests/unit/v2/hyojin -p 'test_*.py' -v
python3 -m unittest discover -s tests/acceptance/v2/hyojin -p 'test_*.py' -v
```

실제 공통 DTO 검증은 지정 커밋의 **원본 contracts.py**를 /tmp에 읽어 놓고 동일 테스트에 주입한다. 프로젝트에 공통 DTO를 복사하지 않는다.

```bash
git show 2fb927db25d02e1bda5bdade02a9e25977a3b289:main/agents/supervisor/contracts.py \
  > /tmp/hyojin-contracts-2fb927d.py
HYOJIN_CONTRACTS_PATH=/tmp/hyojin-contracts-2fb927d.py \
  python -m unittest discover -s tests/unit/v2/hyojin -p 'test_*.py' -v
HYOJIN_CONTRACTS_PATH=/tmp/hyojin-contracts-2fb927d.py \
  python -m unittest discover -s tests/acceptance/v2/hyojin -p 'test_*.py' -v
PYTHONPATH=. HYOJIN_CONTRACTS_PATH=/tmp/hyojin-contracts-2fb927d.py \
  python tests/fixtures/v2/hyojin/export_handoff.py
```

마지막 명령은 `tests/fixtures/v2/hyojin/handoff_cases.json`을 재생성한다. 통과 평가, rewrite 전용 평가, 시장·도메인 research 두 개와 neutrality rewrite 하나, 보고서 hash·revision·epoch 변경, 저장된 ClaimSupport와 충분성 verdict를 전달한다. 실제 모델·RAG·PDF·LangSmith 실행 결과가 아니다.
