# 웅희 V2 중립 보고서·PDF 구현

기존 `SKALA-BlackRubberShoes/gicheol` 저장소의 별도 worktree와
`feature/V2/woonghee/report-pdf` 브랜치에서 구현했다. 기준은 SDD 1.0과 역할 분담 Page다.
원격에 `integration/V2/supervisor-neutral`이 없어, 공통 DTO가 있는 동욱 브랜치의
`2fb927d`를 시작점으로 사용했다. 공통 DTO 코드를 복사하거나 재정의하지 않았다.
팀의 통합 시작점이 확정되면 이 브랜치의 웅희 변경 커밋만 통합한다.

## 구현한 파일과 요구사항

| 파일 | 구현 |
| --- | --- |
| main/agents/report/neutral_builder.py | async NeutralWriter, 승인 claim의 text·kind·evidence 보존, 고정 8개 section, 실제 인용 source만 REFERENCE, canonical content_hash |
| main/agents/report/neutral_renderers.py | Markdown, A4 PDF, 10페이지·SUMMARY 반 페이지·글꼴 내장·평가된 본문과 인용의 전체 순서 일치 검증 |
| main/agents/report/neutral_publish.py | 최신 gate·eval·hash·epoch·revision 재검증, 원본 파일 검증, staging PDF와 PublishResult 반환, 실패 진단 |
| main/agents/report/renderers.py | 기존 저수준 렌더 공유. 기존 public render_pdf는 5페이지 제한을 유지하고 중립 경로만 10페이지 사용 |
| main/prompts/neutral_report/writer.txt | 모델의 선택·재배열 편집을 승인 텍스트로 제한하는 전용 프롬프트 |
| tests/unit/v2/woonghee, tests/acceptance/v2/woonghee, tests/fixtures/v2/woonghee | 담당 테스트·결정론적 fixture |

FR03의 writer 입력 guard, FR08의 provenance 보존, FR11의 보고서·PDF 제약을 구현했다.
품질 Judge, 근거 의미 판정, storage, CLI, 설정·의존성, State와 그래프는 각 소유자의 영역이다.

## runtime 연결 방법

```python
from pathlib import Path
from main.agents.report.neutral_builder import NeutralWriter
from main.agents.report.neutral_publish import NeutralPublisher

workers["writer"] = NeutralWriter()
publish_worker = NeutralPublisher(
    store=store,
    evidence_resolver=immutable_evidence_resolver,
    artifact_root=Path(configured_run_artifact_root),
    font_path=configured_report_font_path,
)
```

`NeutralWriter`는 `WriterRequest -> DraftReport` async 계약이다. 기본 모드는 추출형이며
새 문장을 생성하지 않는다. 대상 판정과 네 관점의 claim 및 한계, 원문 evidence 연결을
보존한다. 관점 fact·inference에는 현재 ClaimSupport가 필요하고 대상 사실은 현행
eligibility gate로 승인한다. SUMMARY는 각 관점의 승인 claim과 한계를 담는다.
선택적으로 `NeutralWriter(llm=llm_client, model=configured_report_model)`을 사용한다.
LLMClient의 공통 generate 계약을 호출하며 반환값은 승인된 원문·한계·인용·section과
대조한다. 편집 모델은 문단을 재배열하고 SUMMARY의 기존 claim을 선택할 수 있다.
새 표현이 필요한 rewrite는 이 추출형 제약 안에서 처리하지 못하며 재평가와 별도 정책이
필요하다. 모델이 작성한 사실, 새 출처 또는 임의 document_ref는 거절한다.

Markdown은 `neutral_markdown(report)`에서 얻는다. JSON·Markdown 저장과 result receipt는
runtime/common wrapper가 맡는다. renderer는 평가된 report만 받고 내용을 고치지 않는다.
SUMMARY 또는 본문 분량 초과를 줄이기 위한 자동 요약·삭제·글자 축소는 없다.
입력 자체에 추천·순위·ROI가 있으면 upstream assessor/Judge가 거절해야 한다.
writer는 자체 의미 Judge를 만들지 않고 승인된 입력을 그대로 보존한다.

`NeutralPublisher`는 `PublishRequest -> PublishResult` async 계약이다. resolver는 반드시
gate 승인 당시 저장된 immutable source/evidence snapshot을 반환해야 한다. store의
verify는 PDF를 포함한 임의 미디어의 실제 bytes SHA와 경로 무결성을 확인해야 한다.
artifact_root는 이 store의 단일 run root와 일치해야 한다. root는 runtime의 trusted
설정이며 run_id를 파일 경로로 직접 보간하지 않는다.

PDF는 먼저 `staging/neutral-render-*/report-{revision}.pdf` 후보에 렌더하고 전체 검증 후
`staging/report-{revision}-{pdf_sha256}.pdf`로 이동한다. SDD의 revision 식별에 bytes hash를
덧붙여 이전 검증 파일의 덮어쓰기를 피한다. 반환 ArtifactRef가 정확한 파일을 지정한다.
렌더 작업은 asyncio.to_thread로 실행한다. 취소 시 후보 파일이 남을 수 있으나 worker는
receipt 또는 manifest를 commit하지 않으며 최종 승인 파일로 노출하지 않는다.

worker는 `final_manifest_ref=None`을 반환한다. 동욱의 기존 publish wrapper가 주현의
`ArtifactStore.commit_manifest`를 호출하고, 완료 manifest와 PDF를 검증한 뒤 State를
completed로 바꾼다. staging 존재만으로 성공 처리하지 않는다. renderer 실패는
`diagnostics/pdf-{revision}.json`에 평가된 draft hash와 진단을 저장하고
`PdfLayoutError`를 발생시켜 wrapper의 `PDF_LAYOUT_FAILED` 처리를 사용한다.

## 설정·의존성

추가 의존성이나 공통 설정 파일 변경은 없다. 기존 report 의존성인 reportlab과 pypdf를
사용한다. REPORT_FONT_PATH에 내장 가능한 한글 TrueType 글꼴의 실제 경로를 지정한다.
neutral 경로는 글꼴을 임의 대체하지 않는다. 테스트는 REPORT_FONT_PATH 또는 기존
renderer의 로컬 한글 글꼴 후보를 사용하며 글꼴이 없으면 실패한다.

실제 검증 환경: Python 3.12.14, reportlab 4.4.9, pypdf 6.10.0,
Pydantic 2.14.1, pytest 9.1.1, LangGraph 1.2.15, langchain-core 1.6.10.
로컬 검증용 환경만 별도로 설치했고 팀 소유 lockfile은 수정하지 않았다.

## 검증 결과

```bash
PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false \
python -m pytest -q -W error::RuntimeWarning \
  tests/unit/v2/woonghee tests/acceptance/v2/woonghee \
  tests/unit/v2/dongwook tests/acceptance/v2/dongwook

PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 LANGSMITH_TRACING=false \
python -m pytest -q main/agents/report/tests/test_renderers.py
```

- 웅희 40개 + 기존 동욱 165개: 205 passed.
- 기존 저수준 renderer: 12 passed, 11 subtests passed.
- 기존 report 전체: 68 passed, 37 subtests passed, 2 failed. 수정 전 원본 checkout에서도
  동일한 두 실패를 재현했다: `test_page_overflow_raises_and_leaves_failed_audit_and_complete_markdown`,
  `test_repeated_decision_limitations_are_printed_once_with_both_owners`.
- AT12 담당 경계: 실제 한글 PDF, A4·내장 글꼴·SUMMARY 높이·본문과 인용의 일치,
  REFERENCE 집합, 6~10페이지 허용, 10페이지 초과·요약 초과·인용 불일치·폰트 실패 거절.
- AT14·AT17 담당 경계: 과거 eval revision·epoch·hash, 수정한 본문·연구 결과·eligibility,
  무효 gate와 rubric mismatch를 PDF 생성 전 거절.
- 실제 NeutralWriter와 NeutralPublisher를 동욱의 기존 Supervisor graph에 연결해
  completed manifest를 얻는 계약 테스트도 통과했다. assessor·Judge·store와 원본 자료는
  명시적인 테스트 fixture이며 실제 서비스의 완료나 live 성공을 의미하지 않는다.

시각 검증용 fixture PDF는 2페이지이며 SUMMARY 높이는 usable A4 page의 0.306113이다.
두 페이지를 Poppler로 PNG 렌더해 직접 확인했다. 한글, 인용, headings, REFERENCE,
페이지 번호가 정상 표시되고 잘림·겹침·누락 글리프가 없다.
샘플의 source·평가 판정은 fixture이므로 최종 기업 평가 또는 승인 보고서로 제공하지 않는다.

## 남은 통합 작업

주현이 services.py에 worker와 동일 root의 store/resolver를 등록하고, Markdown·JSON 저장,
font 설정과 completed manifest commit을 연결한다. 효진의 실제 근거 검증·Judge,
수현·수영의 실제 조사 결과를 사용한 live 실행과 LangSmith 증빙은 통합 후 필요하다.
이 브랜치의 테스트 통과를 전체 SDD MUST 또는 제출 패키지의 완료로 표시하지 않는다.
다른 담당 소유 파일과 Page는 수정하지 않았다.
