# V2 오버엔지니어링 이슈 개선

기준 검토: [동욱_V2.0 오버엔지니어링 검토](https://chatgpt.com/space/page_618422fe25708191a5647363aeaae8b8). 구현 대상은 `integration/V2/supervisor-neutral`이다.

| 이슈 | 적용 |
| --- | --- |
| 실제 서비스 조립 누락 | 기본 production factory, ingest/실행/재개/검색 평가 연결. 회사 ID·Qdrant collection을 공통 계약과 일치시킴 |
| Judge schema 메타프로그래밍 | 주장·충분성·보고서 판정을 명시적인 엄격한 Pydantic 모델로 대체 |
| quality factory/hash 과잉 주입 | 공통 DTO·canonical/semantic hash 직접 사용 |
| 사용하지 않는 Supervisor 모델 필수값 | 선택값으로 변경. 기존 snapshot의 4개 모델 fingerprint는 유지 |
| 동일 주장 중복 검증 | 원문을 먼저 확인한 후 변경 없는 supported 판정만 run 내 재사용 |

재사용 키에는 주장·근거·출처·원문·회사·기준일·Judge 모델·실제 prompt·verifier 버전과 client identity를 포함한다. 실패 판정은 저장하지 않는다. 최대 256개이며 새 프로세스에서는 빈 verifier로 시작한다. 단순 run 내 저장으로 중복 Judge 호출을 줄이며 새로운 영속 캐시 계층은 추가하지 않았다. 고정 transport 테스트에서 같은 주장 2회 검증 시 Judge 호출이 2회에서 1회로 줄었다. 실제 API 비용·지연 개선은 측정하지 않았다.

공통 State, 결정 정책, 4개 관점 gate, groundedness/neutrality/bias/coverage, immutable RepairPlan, SQLite·receipt·재시도 예산, 최종 manifest commit은 유지한다. 원문 변조·회사 불일치·색인 불일치·재개 근거 복구를 실제 파일/PDF/Qdrant로 검사한다. 외부 모델/embedding은 fixture로 표시한다.

품질 테스트 fixture도 같은 공통 DTO로 정규화한다. 별도 계약 경로 환경변수 없이 검증된 receipt의 기본 필드와 실제 서비스 입력이 일치하며, 실제 Supervisor 대상 이슈 #21 회귀와 Judge adapter 테스트가 기본 테스트 실행에서 생략되지 않는다.

전체 테스트에서 기존 투자 보고서의 아래 2개 실패는 기준 브랜치에도 있던 문제다. 이번 중립 V2 변경으로 수정하지 않았다.

- `main/agents/report/tests/test_agent.py::ReportAgentTests::test_page_overflow_raises_and_leaves_failed_audit_and_complete_markdown`
- `main/agents/report/tests/test_content.py::ContentTests::test_repeated_decision_limitations_are_printed_once_with_both_owners`

실제 모델 3개의 ID, 고정 embedding 가중치, Qdrant 서버·색인, 한글 글꼴을 설정한 live 실행과 LangSmith 제출 증빙은 아직 검증하지 않았다. 배포 방법은 [통합 계약](../integration/interfaces.md)을 참고한다.

검토 결정: 회사명·공식 URL의 기준 CSV를 소유권의 기준으로 사용한다. 이름이 다른 외부 corpus를 같은 숫자 ID로 연결하지 않는다. 현재 준비된 Physical AI 자료만 기본 factory에 사용하며, 미확인 웹 날짜·독립성을 추정하지 않는다. 이 보수적인 기준에서 원본 자료가 부족하면 보고서가 `incomplete`로 끝날 수 있다. 무거운 모델 설치·가중치는 기존 adapter의 배포 의존성으로 별도 준비하고, 가벼운 테스트 lock은 유지한다. 기존 투자 보고서 실패 2개는 범위 밖으로 남겨두므로 해당 경로에는 그 제한이 지속된다.
