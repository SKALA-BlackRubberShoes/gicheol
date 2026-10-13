# 효진 개발 로그 01

2026년 10월 13일, V2 역할 분담의 효진 영역인 주장 의미 검증, 네 관점 근거 충분성, 중립 보고서 품질 평가를 로컬 브랜치 `codex/v2-hyojin-evidence-quality`에 구현했다. 기준은 Supervisor SDD 1.0과 동욱의 공통 계약 커밋 `2fb927db25d02e1bda5bdade02a9e25977a3b289`이다. 기존 역할 분담·명세 Page는 수정하지 않았다.

## 구현 결과

| 파일 | 동작 |
| --- | --- |
| main/agents/quality/claim_support.py | 실제 원문과 quote를 대조하고, 기업·시점·해시·추론 조건을 확인한 뒤 독립 Judge로 supported/partial/unsupported를 판정한다. 장애는 unverified다. |
| main/agents/quality/sufficiency.py | 관점별 지원 주장 2개·필수 질문·독립 그룹 2개 및 외부 출처·전체 그룹 3개·한계·critical gap·기술 PDF와 검증 receipt를 검사하고 ClaimSupport를 저장한다. |
| main/agents/quality/evaluator.py | 보고서 인용·중립성·편향·네 관점·참고문헌을 형식 검사하고 Judge 네 점수를 평가한다. 모든 finding과 report hash/revision/epoch를 반환한다. |
| main/agents/quality/rubrics.py | rubric·응답 검증·provenance 병합·최신 support 대조·async timeout·공통 LLMClient 연결을 제공한다. |
| main/prompts/quality/ | 주장·충분성·보고서 평가 프롬프트를 분리하고 입력 자료를 비신뢰 데이터로 처리한다. |

공통 DTO와 enum, State, GateCertificate, RepairPlan은 새로 정의하거나 갱신하지 않았다. 다른 담당자의 파일, README, 의존성, CLI, 설정과 기존 투자 판단 경로는 변경하지 않았다. runtime의 저장·원문·검색 receipt·LLM adapter를 주입한다.

## 검토에서 보완한 실패 사례

회사 원문과 외부 재배포의 hash가 같으면 하나의 provenance로 계산하고, 재배포를 독립 외부 검증으로 인정하지 않는다. 주장에 실제 사용하지 않은 출처를 숫자 채우기에 쓰지 않는다. 같은 claim을 두 질문에 연결해도 지원 주장 수는 하나다.

eligibility의 critical gap·무관한 주장·기준일 이후 근거는 모든 관점의 충분성을 막는다. 상충의 양쪽 원문과 warning gap을 보존한다. 동일 gap의 critical 판정과 서로 다른 query_hints가 덮어써지지 않도록 병합한다.

Judge 예외·timeout·schema 오류, bool 점수·범위 밖 점수·누락 criterion·존재하지 않는 finding 참조는 pass로 바꾸지 않는다. 지원 claim에 연결되지 않은 반대 근거도 원문을 확보하지 못하면 편향 검사를 실패시킨다. 추천 문구는 본문과 section 제목 모두 검사한다.

## 검증 결과

Python 3.12.14·Pydantic 2.13.5 환경에서 동욱 contracts.py를 /tmp에서 읽어 사용한 단위 테스트 46개와 인수 테스트 7개, 합계 53개가 통과했다. 공통 DTO의 필드·enum·해시 및 LLMClient.generate에 전달하는 엄격한 response model도 검증했다. fixture용 dict factory 검증은 별도로 수행했다.

| 시나리오 | quality 경계 결과 |
| --- | --- |
| AT05 | 홍보문 재배포를 독립 출처로 세지 않음. 유효 citation ID라도 무관한 발췌이면 지원 및 충분성 실패. |
| AT06 | 충분한 근거와 높은 Judge 점수에서도 추천·우열·순위 표현은 중립성 실패. repair_kind=rewrite이고 research finding 없음. |
| AT07 | 도메인 핵심 주장 근거 부족은 target_perspective=domain_application의 research finding으로 반환. |
| AT14 | 보고서 변경 후 새 report_hash·revision·evidence_epoch로 평가를 바인딩. |
| AT16 | 시장·도메인 research와 중립성 rewrite를 모두 보존하며 최상위 repair_kind=research. |
| AT17 | 변경된 result/claim/evidence hash에 과거 지원·충분성 판정을 재사용하지 않음. |

위 AT07·14·16·17은 quality 반환 계약과 fixture 검증이다. Supervisor의 실제 epoch 증가·gate 무효화·하나의 repair cycle·publish 거절을 포함한 전체 통합 인수 결과로 표기하지 않는다.

## 통합 전달

`docs/v2/hyojin/interfaces.md`에 서비스 생성·공통 factory/hash·LLMClient·원문 및 receipt 검증·REFERENCE 연결 방식을 기록했다. `tests/fixtures/v2/hyojin/handoff_cases.json`에는 저장 support, 충분성 verdict, 통과 평가, rewrite 전용 평가, 복수 research finding과 보고서 바인딩 변경을 제공한다.

주현은 공통 계약·namespace package·runtime·ArtifactStore·LLMClient를 연결하고 RunConfig.rubric_version을 `neutral-quality-1.0`으로 맞춰야 한다. 수현은 실제 원문 로더와 저장된 검색 receipt 검증을 제공한다. 동욱은 assessor/evaluator를 Supervisor에 등록하고 gate·State·repair 정책을 검증한다. 웅희는 REFERENCE evidence 연결을 보존한 writer와 publish를 연결한다.

현재 브랜치는 로컬 작업이며 원격 push·PR은 수행하지 않았다. 실제 모델의 의미 판정 정확도, 실제 RAG, PDF와 LangSmith 증빙 및 전체 그래프 통합은 해당 서비스 연결 후 확인해야 한다. 테스트의 의미 Judge와 저장·검색은 명시적인 deterministic fixture다.
