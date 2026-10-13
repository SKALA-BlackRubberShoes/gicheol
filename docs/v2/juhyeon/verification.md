# 주현 V2 검증 기록

검증일: 2026-10-13. 기준 계약은 `feature/V2/dongwook/supervisor`의 `2fb927db25d02e1bda5bdade02a9e25977a3b289`이며, 구현 브랜치는 `feature/V2/juhyeon/runtime-integration`이다.

## 환경과 결과

macOS, Python 3.11.15의 별도 가상환경에서 검사했다. 전체 의존성 66개를 `requirements-v2.lock`에 고정했고 `uv pip check`에서 호환성을 확인했다. 주요 버전은 langgraph 1.2.15, langgraph-checkpoint-sqlite 3.1.2, langchain 1.4.3, pydantic 2.13.5, pytest 9.1.1이다.

최종 명령은 저장소 루트에서 실행한 `python -m pytest -q tests main`이다.

| 범위 | 결과 | 해석 |
| --- | --- | --- |
| V2 tests | 324 passed | 공통 계약 165개와 이번 저장·실행·복구 관련 159개 |
| 기존 main 테스트 | 176 passed, 2 failed, 61 subtests passed | 아래 2개는 변경 전에도 재현됨 |
| 전체 | 500 passed, 2 failed, 61 subtests passed | 전체 테스트가 모두 통과한 것은 아님 |
| 기존 실패 2개 제외 실행 | 176 passed, 2 deselected, 61 subtests passed | 기존 경로 회귀 확인 |
| 의존성 / 문법 / diff | 통과 | 패키지 호환성, compileall, git diff --check |

CI는 V2 테스트 전체와 기존 테스트를 실행하되, 변경 전부터 실패하는 아래 2개만 명시적으로 제외한다. 로컬 통과는 GitHub Actions 실행 성공을 뜻하지 않는다.

## 변경 전부터 존재하는 실패

부모 커밋 `2fb927d`를 별도 디렉터리에 추출해 같은 가상환경으로 아래 두 테스트를 실행했고, 두 실패가 동일하게 재현됐다. 다른 담당의 기존 보고서 코드는 이번 범위에서 수정하지 않았다.

1. `main/agents/report/tests/test_agent.py::ReportAgentTests::test_page_overflow_raises_and_leaves_failed_audit_and_complete_markdown`
2. `main/agents/report/tests/test_content.py::ContentTests::test_repeated_decision_limitations_are_printed_once_with_both_owners`

## 실제로 확인한 동작

- 실제 파일·bytes SHA, 경로 이동·symlink 거부, 누락·변조 참조 거부, 원자적 저장 실패 처리.
- 같은 task/input receipt의 재사용, terminal receipt 변경 거부, transient 결과의 후속 완료 저장.
- 실제 AsyncSqliteSaver의 저장·닫기·재연결과 공통 DTO 복원. pickle은 사용하지 않는다.
- subprocess의 `os._exit`를 이용한 중단, receipt 저장 후 checkpoint 전 중단, manifest commit 직전·직후 중단과 재개. AT09·AT15·AT19의 저장 복구 경계를 확인했다.
- 결정 로그 저장 후 재개 시 시각만 다른 동일 결정은 재사용하고 의미가 다른 결정은 거부한다.
- AT11 오류 주입으로 429 Retry-After, 5xx·네트워크 오류, 401·403 즉시 실패, timeout·deadline·공통 retry budget을 확인했다. 구조화 결과 교정 1회도 같은 budget을 소비한다.
- 충분성 assessor의 예산 소진은 `incomplete / BUDGET_EXHAUSTED`로 보존한다.
- 실제 PDF 파싱·합계 200페이지 제한, immutable config snapshot, corpus·prompt·font·index hash 변경 거부, 설정 우선순위와 입력 run_config 불일치 거부.
- 실제 filesystem·SQLite와 수정하지 않은 Supervisor 그래프를 연결해 mock 업무 서비스의 완료·중단·재개를 확인했다.
- 검색 평가는 gold 정답 ID를 검색 adapter에 넘기지 않으며 실제 rank 기준으로 HitRate@5·MRR@5와 관점별 지표를 계산한다.
- AT13 패키징 도구는 완료 manifest, 같은 run/revision, live trace 상태, PNG 파싱·hash, 필수 경로 증빙을 검사하고 허용 파일만 ZIP으로 저장한다.
- 새 CLI 5개 명령의 도움말과 기존 `main.py --help`가 동작한다. 잘못된 입력은 exit 2, 미설정 기본 모델은 명시적 설정 오류 exit 1로 종료된다.

## 검증하지 않은 범위와 후속 연결

실제 모델·RAG·LangSmith 전송·한국어 보고서의 종단 간 실행은 수행하지 않았다. 테스트의 mock 서비스·가짜 transport·빈 PDF는 런타임 경계 검사용이다. 실제 AT12 성능 수치, AT13 제출 완료, AT01–19 전체 live 통과나 최종 평가 보고서를 의미하지 않는다.

각 담당의 실제 worker·resolver·assessor·writer·publisher와 ingest/retrieve를 service factory로 연결하고 모델 ID, credential, 고정 embedding revision, corpus, prompt, 한글 font를 설정해야 한다. 이후 정상·재조사·품질 보완의 실제 LangSmith 화면과 최대 10페이지 한글 PDF를 같은 run/revision으로 검증해야 한다. 이번에는 실제 제출 ZIP 생성이나 메시지 전송을 하지 않았다.

현재 공통 Supervisor의 충분성 assessor 경로는 401·403을 재시도 없이 failed로 종료하지만 세부 오류 코드를 `MODEL_OUTPUT_INVALID`로 변환한다. 일반 worker 경로는 `CONFIG_OR_AUTH_ERROR`를 보존한다. assessor 오류 코드 통일은 동욱·효진과의 후속 계약 조정 사항이며, 공통 코드를 무단 수정하지 않았다.

작업 중 원격에서 확인한 웅희의 `feature/V2/woonghee/report-pdf` (`454722d`)는 아직 병합하지 않았다. 다른 담당의 구현 완료·브랜치 푸시 여부를 이번 런타임 검증 결과로 대신 판단하지 않는다. 연결 방법은 [통합 인터페이스](../integration/interfaces.md)를 따른다.
