# 주현 V2 저장 복구와 실행 통합

SDD 1.0의 FR09 저장·복구, FR10 기술 오류 처리, FR12 실행 환경·제출 준비를 구현한다. 동욱의 공통 DTO를 그대로 사용하고 기존 투자 CLI를 보존한다. 실제 신규 RAG·Judge·보고서 서비스는 각 담당자가 등록해야 한다.

## 설치와 실행

Python 3.11 이상 환경에서 저장소 루트 기준으로 실행한다. 검증한 전체 패키지 버전은 `requirements-v2.lock`에 고정했다.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-v2.lock
python -m main.neutral_eval --help
python -m pytest -q tests
python -m pytest -q main
```

`configs/neutral-evaluation.yaml`과 `.env.example`에서 모델·credential·font·corpus·prompt·Qdrant·service factory를 설정한다. 비어 있는 모델이나 없는 font에 임의 기본값을 넣지 않는다. 설정 우선순위는 `--set KEY=VALUE`, 환경변수, YAML이며 상대 경로는 저장소 루트다. `.env`는 `--env-file`로 명시했을 때만 읽는다.

```bash
python -m main.neutral_eval validate --config configs/neutral-evaluation.yaml --env-file .env
python -m main.neutral_eval ingest --config configs/neutral-evaluation.yaml --env-file .env
python -m main.neutral_eval run --input inputs/company.json --config configs/neutral-evaluation.yaml --env-file .env
python -m main.neutral_eval resume --run-id RUN_UUID --artifact-root outputs/juhyeon --env-file .env
python -m main.neutral_eval evaluate-retrieval --dataset tests/fixtures/retrieval-gold.json --env-file .env
```

company.json은 공통 `RunInput` 형식의 company_name, official_url, domain, as_of_date, corpus_manifest를 가진다. 입력 파일과 정답 gold는 사용자 및 담당자가 준비한다. 예시 명령의 파일이 이미 존재하거나 live 서비스가 완성됐다는 뜻은 아니다.

| 종료 코드 | 의미 |
| --- | --- |
| 0 | run 완료 또는 해당 관리 명령 성공 |
| 1 | 설정·인증·무결성 등 시스템 실패, 중단 |
| 2 | 입력 validation 실패 |
| 3 | 근거·품질·예산 부족으로 incomplete |

`validate`는 실행 전 환경 검사이며 모델의 답변 품질이나 전체 live 성공을 의미하지 않는다. `run` 성공은 committed manifest 검증까지 완료해야 한다.

## 기능과 검증 경계

| 영역 | 구현 | 검증 |
| --- | --- | --- |
| 저장 | actual bytes SHA, symlink 방지, 원자적 파일 저장 | 변경·누락·경로 이동·rename 실패 |
| receipt | 동일 task/input에 대한 결과 재사용 | 저장 직후 중단, 중복 결과 방지 |
| SQLite | 실제 비동기 SQLite saver, 제한된 DTO 복원 | 연결 종료·재연결, 프로세스 중단 |
| 발행 | 평가된 Markdown·보고서 JSON·평가 JSON·PDF 검증 후 manifest 마지막 commit | 실제 Writer/Publisher, 저장 실패·변조·중단 재개 |
| 설정 | 모델·font·PDF 총 페이지·manifest·domain·Qdrant 검사 | 버전·hash 차이 거부, 201페이지 거부 |
| 오류 | 429/5xx/network 재시도, 401/403 분류, deadline | AT11 결정론적 오류 주입 |
| LLM | 명시적 모델, task별 영속 retry 예약, 구조화 교정 1회 | 외부 timeout·receipt replay·SQLite 재개 후 총 호출 상한 |
| trace | decisions.jsonl, metadata-only LangSmith adapter | 비밀 제거, mock 표시, 중복 결정 방지 |
| 통합 | 실제 filesystem/SQLite와 동욱 그래프 결합 | mock 업무 fixture 완료·중단 후 재개 |
| 패키징 | 완료 artifact·trace metadata 결합, ZIP 허용 목록 | AT13 준비 조건 검사; 실제 live 증빙 미완료 |

실행 결과 수와 기존 경로 실패는 `verification.md`에 기록한다. #26 회귀는 실제 NeutralWriter·NeutralPublisher·한글 PDF와 파일/SQLite를 연결하며 연구·Judge 결정만 fixture다. 기존 빈 PDF fixture는 저장 경계 검사용으로 유지한다. 실제 모델/RAG·LangSmith와 업무 자료를 통한 보고서 종단 간 검증은 담당 서비스를 등록한 뒤 수행해야 한다.

연결 상세: [통합 인터페이스](../integration/interfaces.md), [동욱 계약](../dongwook/interfaces.md), [SDD](../../../specs/sdd-spec.md).
