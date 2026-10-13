# V2 실행 통합 계약

주현 구현은 동욱의 SDD 1.0 DTO와 Supervisor를 사용해 실제 파일 저장, SQLite 재개, 오류 처리와 CLI를 연결한다. 사업 판단이나 공통 DTO를 별도로 정의하지 않는다.

## 시작 커밋과 소유권

- 원격 main 기준: `ae83a9735966a38c90a22ab807bea64ecf378392`
- 공통 계약 의존 커밋: `2fb927d` (`feature/V2/dongwook/supervisor`)
- 구현 브랜치: `feature/V2/juhyeon/runtime-integration`
- 최초 구현 시 통합 브랜치가 없어 동욱 커밋을 직접 부모로 사용했다. 현재 PR #19의 병합 대상은 `integration/V2/supervisor-neutral`이다.
- #26의 실제 보고서 통합 검증을 위해 웅희 PR #20의 `454722d`를 merge commit `23d9ab7`로 가져왔다. 기존 구현과 이력을 재사용하며, PR #16·#20이 선행 의존성이다.
- 동욱의 contracts, State, 정책, 그래프, wrapper는 수정하지 않았다. 신규 package `__init__.py`, 공통 환경과 테스트 구성은 주현 소유다.

## 서비스 등록

`services_factory`에 `module:function`을 설정한다. factory는 다음 키워드 인수를 받아 동기 또는 비동기로 `ServiceBundle`을 반환한다.

```python
def create_services(*, runtime, store, llm):
    # 각 담당자의 실제 구현을 같은 DTO 계약으로 조립한다.
    return ServiceBundle(
        workers={
            "eligibility": eligibility,
            "technology_maturity": technology,
            "market": market,
            "stakeholders": stakeholders,
            "domain_application": domain,
            "writer": writer,
            "evaluator": evaluator,
        },
        evidence_resolver=resolver,
        assess_evidence=assessor,
        publish_worker=publish,
        ingest=ingest,
        retrieve=retrieve,
        mock=False,
    )
```

위 코드는 연결 모양이며 아직 없는 서비스의 구현을 뜻하지 않는다. 기본 factory는 필요한 담당 구현을 명시하고 실패한다. 이전 투자 에이전트를 중립 평가 worker로 자동 대체하지 않는다.

| 서비스 | 입력 | 반환 | 구현 담당 |
| --- | --- | --- | --- |
| eligibility | EligibilityRequest | EligibilityResult | 수영 |
| technology_maturity | ResearchRequest | PerspectiveResult | 수현 |
| market, stakeholders, domain_application | ResearchRequest | PerspectiveResult | 수영 |
| evidence_resolver.resolve | evidence ID 목록 | EvidenceBundle | 검색·근거 서비스 |
| assess_evidence | SufficiencyRequest | SufficiencyVerdict 목록 | 효진 |
| writer | WriterRequest | DraftReport | 웅희 |
| evaluator | EvaluatorRequest | EvalVerdict | 효진 |
| publish | PublishRequest | PublishResult | 웅희 |
| ingest | runtime, corpus_manifest 키워드 | JSON 직렬화 가능한 결과 | 수현 |
| retrieve | query, top_k 키워드 | RetrievalResult | 수현 |

모든 worker와 도구는 async이다. `query`는 query_id, query, perspective, language 및 회사·도메인 등의 검색 조건이며, gold의 `relevant_source_ids`는 런타임이 제거한 뒤 retrieve에 전달한다. 반환 receipt의 query ID와 본문이 요청과 일치해야 한다. 실제 rank 1–5로 Hit Rate@5와 MRR@5를 계산하며 순위가 6 이상인 항목을 상위 5개로 재번호화하지 않는다. 20개 질의·관점별 5개·한영 각 10개 조건을 검사하고 전체·관점별 지표와 p50/p95 질의 지연을 기록한다. 메모리 지표는 현재 프로세스 수명 동안의 최대 RSS로 표시하며 색인 시간은 ingest에서 별도로 측정한다.

`assess_evidence`에는 원래 deadline과 공통 기술 retry budget이 적용된다. SufficiencyRequest 전체 hash로 안정적인 span ID를 만들고, assessor 결과의 gate 판정·evidence ID·gap ID를 별도 event로 남긴다. 최종 근거 gate 권한은 Supervisor에 있다.

## 설정 및 색인 manifest

설정은 `load_runtime_config`가 YAML < 환경변수 < CLI override 순으로 읽으며 경로는 저장소 루트 기준이다. 공통 RunConfig를 재사용하고 runtime wrapper에는 경로·서비스 설정만 추가한다. `runtime_config_from_snapshot`은 저장된 비밀 아닌 설정을 유지하며 자격 증명만 환경변수에서 다시 읽는다. 모델 ID, 실제 한글 TTF와 고정된 40자리 embedding revision SHA를 임의로 채우지 않는다. 기본 설정은 담당 서비스·prompt·색인을 등록하기 전 명시적으로 실패한다.

입력 JSON의 선택 필드 `run_config`는 생략하거나, 적용된 YAML·환경변수·CLI 설정의 공통 RunConfig와 같아야 한다. 다르면 입력 오류(exit 2)로 거절하며 입력 설정을 조용히 덮어쓰지 않는다.

preflight는 실제 PDF 페이지 합계(동일 SHA 중복 제외 최대 200), 암호화·파싱, 입력 SHA·페이지, 도메인, prompt·font bytes, SQLite 쓰기와 Qdrant 차원을 확인한다. 색인 manifest는 `status: completed`, `normalized: true`, `collection`, `corpus_hash`, `embedding_model`, `embedding_revision`, `embedding_dimension`, `index_version`을 포함해야 한다. corpus_hash는 preflight가 반환한 CorpusDocument JSON 목록의 canonical_hash다. 새 ingest는 기존 collection을 덮어쓰지 않는다. 명시적인 오프라인 테스트는 `network_skipped`로 표시하며 live 검증 성공으로 표시하지 않는다.

## 저장과 완료 공개

`FileArtifactStore(root, run_id=...)`는 shared ArtifactStore Protocol을 구현한다. `save/load/verify/find_receipt/save_receipt/find_manifest/commit_manifest`는 계약과 같은 async 호출이며 bytes와 문자열 hash를 혼용하지 않는다. binary는 추가 helper `save_bytes(data, key=..., media_type=...)`로 저장한다.

파일은 임시 파일, flush/fsync, 원자적 rename, 디렉터리 fsync 순으로 기록한다. 경로 이동·symlink·변경된 SHA를 거부하고 receipt의 task와 입력 hash, payload·result 참조를 재검증한다. worker의 결과 재사용과 Supervisor의 결과 수용은 구분한다. 외부 API가 한 번만 호출된다는 보장은 하지 않는다.

보고서 renderer는 `staging/report-{revision}.pdf`를 저장하고 검증 정보를 반환한다. 저장소는 gate·draft·evaluation hash와 epoch, 실제 PDF를 검사하고, 그 평가 완료 DraftReport에서 `report.md`·`report.json`, 같은 EvalVerdict에서 `evaluation.json`을 원자적으로 저장한다. Markdown은 웅희의 `neutral_markdown`을, JSON은 공통 DTO의 canonical JSON을 사용한다. `Manifest.artifacts`의 `report_md`·`report_json`·`evaluation_json`에 bytes SHA 참조를 연결하고 모두 다시 검증한 뒤 `manifest.json`을 마지막에 commit한다.

완료 전 파일은 승인된 산출물로 제공되지 않는다. `approved_artifact_paths(run_id, report_revision)`는 3개 export·PDF·manifest의 검증된 경로를 반환하고, 기존 `approved_report_path`도 같은 완료 검증을 통과한 PDF만 반환한다. 파일 하나라도 누락·변조되면 공개가 차단된다. 기존 export 없는 완료 manifest를 읽을 때도 무결성 오류로 거절하며, 승인되지 않은 파일을 자동 보충하지 않는다. summary 분량·한글 글꼴·본문 의미 검증은 웅희와 효진의 책임이며, PDF 파싱 성공으로 이 기준을 대체하지 않는다.

## 실행과 복구

`open_checkpointer(sqlite_path)`는 실제 AsyncSqliteSaver 연결을 관리한다. DTO와 enum 직렬화는 공통 계약 클래스만 허용하며 pickle을 켜지 않는다. CLI의 runtime.json에는 run/thread ID와 저장된 runtime/input snapshot 참조가 있다. 재개는 저장된 모델과 설정을 그대로 사용하고 원래 config 파일·corpus·prompt·index·font의 변경을 검출한다. completed/incomplete/failed 실행은 재시작하지 않는다.

기술 호출에는 deadline과 timeout, Retry-After, 기술 retry budget을 적용한다. `RetryLedger`는 동일 run/task/input에 대해 `.runtime/retries/`에 호출 예약을 lock·fsync·원자적 rename으로 기록한 뒤 provider를 호출한다. 외부 timeout, receipt 재생, SQLite 재시작이 있어도 사용한 예산을 돌려주지 않는다. 한 task가 여러 정상적인 LLM 호출을 할 수 있도록 논리 호출 순번·요청 hash를 구분하고 성공 결과는 재사용한다. 기술 재시도 최대 2회와 JSON 구조 교정 최대 1회는 task 전체가 공유한다. 같은 논리 호출은 최초 1회와 재시도 2회를 넘지 않는다. 요청 내용이 바뀌거나 ledger가 손상되면 호출을 거부하며, 401·403과 최종 schema 오류도 재개 시 다시 호출하지 않는다.

SDK 자체 retry를 끄고 adapter budget 소진을 최종 오류로 반환해 Supervisor의 재시도와 중첩하지 않는다. 업무 attempt, gate, repair cycle은 Supervisor만 갱신한다. 기존 예산 기록이 없는 과거 실행의 소비 횟수는 복원할 수 없으므로 이 보장은 수정 버전으로 시작한 실행에 적용된다.

충분성 assessor의 예산 소진은 기존 Supervisor의 timeout 계약으로 전달해 `incomplete / BUDGET_EXHAUSTED`를 유지한다. 다만 현재 동욱의 `refresh_evidence`는 timeout 외의 assessor 예외를 `MODEL_OUTPUT_INVALID`로 분류한다. 따라서 이 경로의 provider 401·403은 재시도 없이 `failed`로 종료되지만 `CONFIG_OR_AUTH_ERROR`라는 세부 코드는 보존되지 않는다. 일반 worker 경로는 인증 오류 코드를 보존하며, assessor 오류 코드 통일은 담당자 간 후속 계약 조정 사항이다.

로그에는 run/task/attempt, 결정과 gap ID, hash, 기간, 결과를 남긴다. source 본문·prompt·자격 증명을 기록하지 않는다. `mock=True` fixture trace는 실제 모델 증빙으로 사용하지 않는다.

## 제출 패키지

`docs/v2/integration/package_submission.py`는 완료 manifest, 동일 run/revision의 trace 증빙과 PNG, README·브랜치 링크를 검사하고 허용 파일만 ZIP으로 묶는다. 실제 실행·화면 내용의 진위는 별도의 live 검증과 시각 검토가 필요하다. Slack으로 자동 제출하지 않는다.

## 남은 통합 작업

웅희의 `NeutralWriter`와 `NeutralPublisher(store=store, evidence_resolver=resolver, artifact_root=store.root, font_path=runtime.report_font_path)`는 #26 회귀에서 실제 구현을 연결했다. 연구·Judge 결정은 fixture이며 실제 LLM·RAG 결과를 의미하지 않는다. 실제 서비스 factory 등록은 별도로 필요하다.

수현·수영·효진·웅희의 실제 서비스 등록, 고정 open-source 색인과 prompt, 실제 model ID·font 설정이 필요하다. 이후 AT01–19 전체 통합, 실제 RAG와 모델 성공, 재조사·품질 보완 LangSmith PNG, 최대 10페이지 한글 PDF를 같은 run과 report revision에 연결해야 한다. 현재 mock fixture 성공을 그 완료로 해석하지 않는다.
