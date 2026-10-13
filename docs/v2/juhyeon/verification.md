# 주현 V2 검증 기록

검증일: 2026-10-13. 기준 계약은 `feature/V2/dongwook/supervisor`의 `2fb927db25d02e1bda5bdade02a9e25977a3b289`이며, 구현 브랜치는 `feature/V2/juhyeon/runtime-integration`이다.

## #25·#26 수정 후 검증

같은 PR [#19](https://github.com/SKALA-BlackRubberShoes/gicheol/pull/19)에 후속 수정한다. 웅희 PR #20의 `454722d`를 `23d9ab7`로 병합해 실제 Writer·Publisher로 검사했다.

| 검사 | 결과 | 범위 |
| --- | --- | --- |
| `python -m pytest -q tests` | 403 passed, 16.85초 | 기존 324개 + 보고서 의존성 40개 + 이번 회귀 39개 |
| 기존 main 회귀 | 176 passed, 2 deselected, 61 subtests passed | 아래 기존 실패 2개만 제외한 CI와 동일 명령 |
| 문법·diff | 통과 | compileall, git diff --check |

- **#25:** 파일에 provider 호출 예약을 먼저 기록해 동일 task/input 예산을 재사용한다. 실제 wrapper의 외부 timeout, transient receipt 저장 직후 취소, SQLite·파일 저장소·그래프 재생성 후에도 provider 총 3회(최초 1회 + 기술 재시도 2회)를 확인했다. 업무 attempt는 증가하지 않는다. 정상적인 다중 논리 호출과 성공 결과 재사용, 공유 retry/교정 제한, 교정 중 취소, 인증·최종 schema 오류 고정, 손상된 ledger 및 저장 실패 시 추가 호출 차단을 검사했다.
- **#26:** 실제 NeutralWriter·NeutralPublisher·한글 PDF와 파일 저장소·SQLite·변경하지 않은 Supervisor를 연결했다. 같은 평가 완료 DraftReport의 본문·인용·Markdown·JSON, EvalVerdict JSON과 manifest 참조가 일치한다. 3개 export 각각의 저장 실패·직후 변조, manifest 직전·직후 중단 및 재개, 5개 공개 파일 각각의 완료 후 누락·변조를 포함해 19개가 통과했다.
- 패키징 성공 경로는 archive writer를 캡처 함수로 교체한 구조 테스트로 확인했다. 5개 산출물 bytes와 제출 인덱스 SHA가 일치하며 실제 제출 ZIP이나 live 증빙을 만들지 않았다.
- 동시 재개 검사에서 드러난 macOS 잠금 파일 최초 생성 ENOENT는 동일한 안전한 디렉터리 핸들·플래그로 1회만 재시도한다. 수정 전 160회 거래 중 15회 재현했고, 수정 후 같은 검사에서 실패가 없었다.

GitHub CI의 최종 원격 결과는 PR #19 Checks와 워크스페이스 `주현_개발로그_01`에 기록한다. 테스트 제외를 추가하지 않았다. 연구·Judge 판단과 LLM transport는 fixture이며, 실제 모델·RAG·LangSmith의 업무 실행 성공을 뜻하지 않는다.

예산 보존은 수정 버전으로 시작한 실행에 적용한다. 이전 버전이 기록하지 않은 호출 횟수는 복원할 수 없다. 3개 export가 없는 과거 완료 manifest는 새 무결성 검사에서 거절하며 자동 마이그레이션하지 않는다.

## 초기 구현 검증 (3061e38)

macOS, Python 3.11.15의 별도 가상환경에서 검사했다. 전체 의존성 66개를 `requirements-v2.lock`에 고정했고 `uv pip check`에서 호환성을 확인했다. 주요 버전은 langgraph 1.2.15, langgraph-checkpoint-sqlite 3.1.2, langchain 1.4.3, pydantic 2.13.5, pytest 9.1.1이다.

초기 구현 검증 명령은 저장소 루트에서 실행한 `python -m pytest -q tests main`이다. #25·#26 수정 이후 결과는 아래 별도 기록을 따른다.

| 범위 | 결과 | 해석 |
| --- | --- | --- |
| V2 tests | 324 passed | 공통 계약 165개와 이번 저장·실행·복구 관련 159개 |
| 기존 main 테스트 | 176 passed, 2 failed, 61 subtests passed | 아래 2개는 변경 전에도 재현됨 |
| 전체 | 500 passed, 2 failed, 61 subtests passed | 전체 테스트가 모두 통과한 것은 아님 |
| 기존 실패 2개 제외 실행 | 176 passed, 2 deselected, 61 subtests passed | 기존 경로 회귀 확인 |
| 의존성 / 문법 / diff | 통과 | 패키지 호환성, compileall, git diff --check |

CI는 V2 테스트 전체와 기존 테스트를 실행하되, 변경 전부터 실패하는 아래 2개만 명시적으로 제외한다. 로컬 통과는 GitHub Actions 실행 성공을 뜻하지 않는다.

## CI 환경 보완

최초 PR 검사 [38349799316](https://github.com/SKALA-BlackRubberShoes/gicheol/actions/runs/38349799316)에서 V2 324개는 통과했으나, Ubuntu runner에 한글 TTF가 없어 기존 보고서 검사가 `Korean PDF font unavailable`로 실패했다. 로컬에는 폰트가 설치돼 있어 드러나지 않은 CI 의존성 누락이었다.

워크플로에 `fonts-nanum` 설치와 파일 존재 확인을 추가하고, 기존 테스트 단계에 `REPORT_FONT_PATH=/usr/share/fonts/truetype/nanum/NanumGothic.ttf`를 명시한다. 폰트 누락 오류를 무시하거나 추가 테스트를 제외하지 않는다. V2 설정 테스트에 영향을 주지 않도록 해당 환경변수는 기존 테스트 단계에만 적용한다. 수정 이후 원격 검사 결과는 PR #19의 Checks를 기준으로 확인한다.

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

실제 모델·RAG·LangSmith 전송을 포함한 업무 자료의 종단 간 실행은 수행하지 않았다. 초기 테스트의 mock 서비스·가짜 transport·빈 PDF는 런타임 경계 검사용이다. #26 추가 회귀는 실제 Writer·Publisher·한글 PDF를 사용하되 연구·Judge 결정은 fixture다. 실제 AT12 성능 수치, AT13 제출 완료, AT01–19 전체 live 통과나 최종 평가 보고서를 의미하지 않는다.

각 담당의 실제 worker·resolver·assessor·writer·publisher와 ingest/retrieve를 service factory로 연결하고 모델 ID, credential, 고정 embedding revision, corpus, prompt, 한글 font를 설정해야 한다. 이후 정상·재조사·품질 보완의 실제 LangSmith 화면과 최대 10페이지 한글 PDF를 같은 run/revision으로 검증해야 한다. 이번에는 실제 제출 ZIP 생성이나 메시지 전송을 하지 않았다.

현재 공통 Supervisor의 충분성 assessor 경로는 401·403을 재시도 없이 failed로 종료하지만 세부 오류 코드를 `MODEL_OUTPUT_INVALID`로 변환한다. 일반 worker 경로는 `CONFIG_OR_AUTH_ERROR`를 보존한다. assessor 오류 코드 통일은 동욱·효진과의 후속 계약 조정 사항이며, 공통 코드를 무단 수정하지 않았다.

웅희의 `feature/V2/woonghee/report-pdf` (`454722d`)는 #26 회귀를 위해 merge commit `23d9ab7`로 반영했다. PR #19는 동욱 #16·웅희 #20을 의존성으로 포함하며 공통 계약이나 기존 보고서 구현을 새로 작성하지 않았다. 연결 방법은 [통합 인터페이스](../integration/interfaces.md)를 따른다.
