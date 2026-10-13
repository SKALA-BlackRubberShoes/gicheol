# 수현 개발 로그 01

2026년 10월 13일, SDD 1.0과 V2 역할 분담에 따라 문서 수집, 오픈소스 임베딩, 검색과 원문 근거 추출, 기술 성숙도 worker를 구현했다. 수현 브랜치는 `feature/V2/suhyeon/rag-technology`이며 동욱 계약 커밋 `2fb927db25d02e1bda5bdade02a9e25977a3b289`를 기준으로 한다.

## 구현 결과

| 영역 | 구현 내용 |
| --- | --- |
| 수집 | 실제 PDF 전체 페이지 합 200 제한, SHA 중복 제거, 암호화·손상·잘못된 page count/hash를 저장 전에 거절 |
| 청킹 | 기본 400 tokens·60 overlap, 페이지 경계와 UTF-8 문자·절대 위치 보존, 후보 tokenizer 입력 길이도 확인 |
| 임베딩 | bge-m3 dense와 multilingual-e5-base의 고정 revision, 정규화·차원 기록, E5 query/passage prefix, 입력 초과 거절 |
| 색인 | 수현 전용 Qdrant collection, corpus·모델·revision·차원·청크 manifest 검증, 기존 collection 덮어쓰기 금지 |
| 검색 | 회사·도메인·기준일 filter, 실제 ranked hits/receipt, empty·source_inaccessible·error 구분, 미래 자료 제외 |
| 원문 | PDF 원본 bytes와 추출 페이지, HTTP 원본 bytes와 본문 보존, 정확한 quote/hash/페이지/본문 위치 검증 |
| 기술 worker | 공통 ResearchRequest→PerspectiveResult, tech.principle·tech.validation, 구체적 gap과 한계, 원문 근거 저장 |
| 경계 | supported와 충분성은 자체 확정하지 않음, 다른 관점 worker 직접 호출 없음, 필요 시 cross-view Gap 반환 |

기존 기술 자료의 원본 PDF 5개를 검증한 결과 실제 140페이지·462청크이며 모든 청크는 원문 페이지의 정확한 부분 문자열이다. 기업 자료와 회사 ID·별칭을 대조하고 여러 기업이 함께 실린 Startup Recipe PDF는 카본식스의 4페이지 발췌 범위만 검색한다. 다른 개발 주체의 배경 논문에는 회사 ID를 부여하지 않는다. 생성된 공식 웹 요약 PDF는 기본 수집에서 제외한다.

## 개발과 리뷰

공통 Source·Evidence·ResearchRequest·PerspectiveResult·RetrievalReceipt와 async Protocol을 재사용했다. 공통 DTO, State, CLI, dependency, package 등록 파일을 새로 정의하거나 변경하지 않았다. 담당 모듈·프롬프트·전용 테스트와 docs만 추가했고 공통 evidence.py에는 V2 원문 검사 helper만 확장했다.

독립 리뷰에서 실제 재현된 웹 Source 재기록 시 HttpUrl 직렬화 오류, 정책상 제외된 결과의 접근 실패 오분류, retrieved_at으로 동일 색인의 identity가 변하는 문제, 유실·변조된 원문을 일반 query 오류로 처리하는 문제를 수정했다. 같은 질의의 실제 receipt와 회사·도메인·corpus·기준일을 대조하고, 웹에서 PDF를 얻었어도 문서 RAG 검색이 비었으면 critical gap을 유지하도록 보완했다. index mismatch, unavailable domain, artifact integrity 오류는 공통 영구 오류 envelope로 반환한다.

Source와 manifest 저장 경로에는 내용 hash를 반영해 재수집이 기존 artifact 참조를 덮어쓰지 않도록 했다. 공통 store가 JSON object만 받으므로 PDF bytes는 base64 envelope에 원본 PDF SHA와 별도로 보존한다. Source.sha256는 JSON artifact hash로 유지해 공통 계약과 일치한다.

## 검증 결과

- 수현 단위·acceptance: **126개 통과**. 실제 201페이지 거절, 200페이지 PDF와 persistent Qdrant 검색, PDF 근거·receipt, revision·회사·도메인·날짜·페이지·인용 검사, snippet 거절, 원문 유실·변조, LLM 출력 경계를 확인했다.
- 동욱 계약·Supervisor 테스트와 기존 공통 근거·기업 RAG 회귀를 함께 실행: **306개 통과, 13.35초**. RuntimeWarning은 오류로 처리했다.
- 실제 임베딩 두 후보를 같은 193페이지·526청크·20질의로 실행했다. 각 관점 5개, 한국어·영어 각 10개이며 두 실행의 corpus hash와 chunk manifest hash가 일치한다.
- 실제 회사 소유권을 유지한 별도 E5 검색 smoke도 통과했다. 140페이지·483개 E5 tokenizer 청크에서 리얼월드와 카본식스의 실제 원문 근거를 분리하고, 미등록 기업의 빈 결과, 미준비 도메인 거절, 미래 자료 제외, 모델 revision 불일치를 확인했다. 기본 tokenizer 검증의 462청크와 이 E5 청크 수는 tokenizer가 달라 구분한다. 실제 결과는 production-retrieval-smoke.json에 저장했다.

```bash
python -m pytest -q -W error::RuntimeWarning \
  tests/unit/v2/suhyeon tests/acceptance/v2/suhyeon \
  tests/unit/v2/dongwook tests/acceptance/v2/dongwook \
  main/agents/common/tests main/rag/company/tests
```

| 모델 | Hit Rate@5 | MRR@5 | 색인·모델 로드 | 질의 p50/p95 | 측정 peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| BAAI/bge-m3 | 1.00 | 1.00 | 92.65초 | 78.79 / 128.66ms | 2.23GiB |
| intfloat/multilingual-e5-base | 0.85 | 0.825 | 29.68초 | 47.19 / 100.35ms | 1.35GiB |

이번 측정의 검색 정확도에 따라 bge-m3를 기본으로 유지한다. E5는 시간과 메모리를 줄이는 명시적 대안이며 모델 선택 변경 시 새 색인을 생성해야 한다. 측정은 문서 문맥 검색용 합성 소유권을 사용하는 별도 평가 색인이다. 생산용 회사 근거로 사용할 수 없으며 원래 회사 소유권은 결과 JSON에 보존했다.

## 한계와 통합 항목

20개 정답 라벨은 원문 페이지와 quote hash에 근거해 agent가 작성한 잠정 라벨이다. 사람 검토를 통과한 정답셋이라고 주장하지 않는다. 숫자는 이 평가셋의 실측 결과이며 일반적인 성능 보장이나 실제 기업 평가 정확도를 뜻하지 않는다. E5의 도메인 적용 관점 Hit Rate@5는 0.60으로 다른 관점보다 낮다.

Diffusion Policy 원본 PDF에서 기존에도 발생한 pypdf numeric-object warning을 parse_diagnostics에 보존했다. 해당 19페이지 추출문은 기존 baseline과 일치하며 warning을 없는 것으로 숨기지 않았다.

주현은 runtime dependency·config·CLI와 source/evidence/receipt registry 재개를 연결하고, 효진은 의미적 주장 지지와 독립 출처 충분성을 검증해야 한다. 실제 LLM·Judge·Supervisor·보고서 PDF 발행의 전체 실행은 이 담당 검증과 구분한다. 공통 ResearchTools.retrieve에는 excluded_source_ids 인자가 없어 worker가 반환 근거에서 제외하며, 직접 NeutralRetriever API에서는 제외 filter를 지원한다. 연결 방법과 변경 요청은 interfaces.md에 기록했다.
