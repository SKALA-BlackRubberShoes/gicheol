# 수현 V2 오픈소스 dense 임베딩 비교

2026-10-13 실제 CPU 실행 결과다. 기본값은 SDD의 BAAI/bge-m3를 유지한다. 이번 소규모 평가에서는 BGE가 관련 문서를 더 자주 첫 번째에 반환했고, E5는 색인·질의 시간과 메모리 사용이 작았다. 이 결과를 기업 기술 우열이나 일반 검색 성능으로 확대해 해석하지 않는다.

**정답 라벨은 사람이 검토한 라벨이 아니다.** 저장소의 실제 PDF 페이지·발췌·SHA를 확인하여 에이전트가 작성한 20개 질의다. 네 관점 각 5개, 한국어·영어 각 10개이며 `agent_curated_pending_human_review`로 명시했다. 사람이 라벨을 검토·승인하기 전까지 수치와 MVP 목표 통과 여부는 잠정값이다. 사람 라벨 요구사항은 아직 충족하지 않았다.

## 모델과 입력 계약

| 항목 | BGE-M3 | Multilingual E5 Base |
| --- | --- | --- |
| 모델 ID | `BAAI/bge-m3` | `intfloat/multilingual-e5-base` |
| 고정 revision | `5617a9f61b028005a4858fdac845db406aefb181` | `d128750597153bb5987e10b1c3493a34e5a4502a` |
| dense 차원 | 1024 | 768 |
| 모델 입력 한도 | 8192 tokens | 512 tokens |
| 입력 접두사 | 없음 | 모든 언어에 `query: ` / `passage: ` |
| 정규화 | `normalize_embeddings=True`, L2 검증 | 동일 |
| Qdrant 거리 | Cosine | Cosine |

실제 Hugging Face 모델 API에서 revision SHA를 조회한 뒤 해당 revision 파일만 다운로드했다. 모델이 설치되지 않았거나 가중치·차원·출력이 잘못되면 실패하며 다른 모델/API로 조용히 대체하지 않는다. BGE의 이 revision은 dense 가중치를 `pytorch_model.bin`으로 제공하여 추가 다운로드했다. `trust_remote_code=False`이며 최신 격리 Torch 런타임으로 실행했다.

[BGE 공식 모델 카드](https://huggingface.co/BAAI/bge-m3)는 다국어·긴 입력과 dense 표현을 지원하며 질의 instruction을 요구하지 않는다. [E5 공식 모델 카드](https://huggingface.co/intfloat/multilingual-e5-base)는 768차원과 query/passage 접두사 및 512-token 입력 제한을 명시한다. [SentenceTransformer 공식 API](https://sbert.net/docs/package_reference/sentence_transformer/model.html)의 revision·normalization 설정을 사용했다.

페이지 경계를 보존한 동일한 **193페이지·526청크**를 두 모델에 입력했다. 주 tokenizer는 고정 E5의 fast tokenizer이며 400-token 청크·60-token overlap을 사용한다. 추가로 두 모델의 tokenizer에서 passage 접두사·special tokens까지 포함하여 각 청크가 400 이하임을 확인했다. 잘린 입력을 모델에 넘기지 않는다.

전체 저장소 PDF 풀은 200페이지를 넘는다. 생성된 카본식스 웹 요약 노트와 37페이지 OpenVLA 논문을 평가 풀에서 제외하고, 기술 원문 4개와 시장 원문 6개의 **실제 전체 페이지**를 센 결과가 193페이지다. 일부 페이지에서 회사 발췌만 검색하더라도 PDF 전체 페이지 수를 줄여 계산하지 않는다.

두 실행의 corpus hash는 `21e776d16980509b685feab30bad2eacecf62fee3ed91ae52f3d08fa09a8ec51`, chunk manifest hash는 `1ad6f912d0e45059e4b7355db7b7ad7b06c2f735a2686a6499eaecafedb3cb36`으로 동일하다. 모델별 색인은 별도 `neutral_v2_suhyeon_{index_revision}` collection에 생성했다.

## 실제 측정값

| 모델 | Hit Rate@5 | MRR@5 | 색인 시간 | 질의 p50 | 질의 p95 | 측정 peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BGE-M3 | 1.000 | 1.000 | 92.646초 | 78.788ms | 128.663ms | 2.232 GiB |
| Multilingual E5 Base | 0.850 | 0.825 | 29.681초 | 47.187ms | 100.346ms | 1.351 GiB |

| 관점 / 질의 수 | BGE Hit@5 / MRR@5 | E5 Hit@5 / MRR@5 |
| --- | --- | --- |
| 기술 성숙도 / 5 | 1.000 / 1.000 | 1.000 / 0.900 |
| 시장성 / 5 | 1.000 / 1.000 | 0.800 / 0.800 |
| 이해관계자 / 5 | 1.000 / 1.000 | 1.000 / 1.000 |
| 도메인 적용 / 5 | 1.000 / 1.000 | 0.600 / 0.600 |

Hit Rate@5는 실제 top 5 청크 중 관련 source ID가 하나라도 있는 질의 비율이다. MRR@5는 첫 관련 청크의 **실제 rank** 역수 평균이며 누락은 0이다. 중복 source가 차지한 순위를 임의로 앞당기지 않는다. 두 모델의 전체 잠정값은 SDD 목표 0.80/0.60 이상이지만 사람 라벨 확인이 남았다. E5 누락은 M05(카본식스 투자 영문 질의), D04(컨베이어 한국어 질의), D05(단계적 제조 적용 영문 질의)다. 평가 결과를 본 뒤 질의/라벨/청크를 고쳐 재점수를 보고하지 않았다.

Python 3.12.14, macOS arm64, Torch 2.14.1, Sentence Transformers 5.1.1, Qdrant client 1.19.1, CPU 4 threads, batch 8이다. 모델 로드와 임베딩·Qdrant 쓰기를 색인 시간에 포함했고 다운로드와 PDF/tokenizer preflight는 제외했다. 질의는 1회 warmup 후 각 1회 측정하며 저장된 원문 무결성 검사와 receipt 저장을 포함한다. p95는 20개 표본에서 선형 보간했다. peak memory는 모델 로드/색인/질의 동안 50ms 간격 process RSS 최대값이며 tokenizer/corpus preflight의 최대값은 포함하지 않는다. 운영체제 전체·GPU 메모리나 전체 장비 최대 메모리로 해석하지 않는다.

## 기업 귀속과 근거 무결성

벤치마크는 도메인 문서 검색 비교를 위해 **평가 전용 가상 ID `benchmark_physical_ai`**를 사용한다. 원래 회사 ID와 출처 범위는 각 결과 JSON의 `original_source_provenance`에 보존했다. 이 평가용 corpus/collection/Source/Evidence를 실제 RLWRLD 또는 카본식스의 주장 근거로 연결하면 안 된다. 기관 통계나 다른 개발자의 실험을 특정 기업 실적으로 귀속하지 않는다.

운영 `legacy_corpus_documents()`는 원래 회사 귀속을 보존하고 생성 요약을 기본 제외한다. 기술 원문 5개는 실제 140페이지다. `company_ids=[]`인 기술 배경 자료는 저장할 수 있으나 회사 filter로 반환하지 않는다. 카본식스 투자 자료는 PDF 전체를 보존하되 원래 선언한 4쪽 `카본식스`부터 `더플레이토` 이전까지의 발췌만 회사 청크로 만든다. 별도의 실제 E5 기업 검색 검증은 `production-retrieval-smoke.json`에 기록했다. 6개 검증이 모두 통과했으며 E5 tokenizer·접두사·special-token 한도를 적용한 483청크를 새 색인에 임베딩했다. 기본 cl100k tokenizer로 수집만 검증한 462청크와 토큰화 조건이 다르며, 193페이지·526청크의 후보 비교 풀과도 구분한다.

검색 전에는 collection의 model ID/revision/dimension/normalization/corpus/chunk manifest/count와 저장된 manifest bytes를 검증한다. 회사·도메인·기준일 filter를 적용한 후 실제 결과의 payload를 준비된 corpus와 대조하고, 저장된 원문 artifact의 해당 페이지/문자 위치가 quote와 일치하는지 검사한다. 이미 수집한 파일의 유실·SHA 불일치는 `ArtifactIntegrityError`, 다른 모델이나 변경된 색인은 `IndexMismatchError`로 실패한다. 명시적인 원문 접근 불가는 `source_inaccessible` receipt이며 성공/빈 결과/오류와 구분한다. receipt에는 실제 query ID·질의 hash·rank·source/chunk ID·filter·색인 버전이 들어간다.

알려진 발행일이나 사건 시점이 기준일 이후인 자료는 제외한다. 월·연도만 알려진 자료는 날짜를 만들지 않고 `published_at=None`으로 보존한다. 이런 자료를 특정 날짜의 최신 사실이라고 승인하려면 후속 조사와 한계 표시가 필요하다. 운영 검증에서 RLWRLD/카본식스 별칭, 회사 분리, 미래 자료 제외, 빈 회사, 미준비 도메인, 다른 revision 거부와 원문 quote 검증을 확인한다.

## 실행과 통합 요청

`run-embedding-benchmark.py`를 각 모델에 대해 **새로운 작업 폴더**로 실행하면 실제 색인·질의와 결과 JSON을 만든다. 기존 동일 revision 색인을 재사용하면 색인 시간 비교로 사용할 수 없으므로 새 측정에는 새 작업 폴더가 필요하다.

```bash
PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 python docs/v2/suhyeon/run-embedding-benchmark.py \
  --model BAAI/bge-m3 --cache /path/to/pinned-model-cache \
  --work-directory /path/to/new-benchmark-work

PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 python docs/v2/suhyeon/run-embedding-benchmark.py \
  --model intfloat/multilingual-e5-base --cache /path/to/pinned-model-cache \
  --work-directory /path/to/new-benchmark-work
```

루트 의존성·잠금 파일·README·공유 설정은 이 역할 범위에서 수정하지 않았다. 주현 런타임에서 Sentence Transformers 5.1.1/Torch 2.14.1/Transformers 4.57.6/Qdrant client 1.19.1의 설치 가능성을 확인한 잠금 파일, 모델 cache 위치, EMBEDDING_MODEL/revision/dimension/normalization 설정과 새 collection 선택을 연결해야 한다. root README에는 이 비교 문서와 모델 선택 이유를 반영해야 한다. E5로 명시적 설정을 바꾸면 모델·차원·revision·corpus hash가 일치하는 새 색인을 함께 생성한다. 실제 LLM·LangSmith·최종 PDF 성공 실행은 이 임베딩 벤치마크가 증명하지 않는다.

단위 검증은 `test_embeddings.py`, `test_retrieval.py`, `test_benchmark.py`의 34건을 통과했다. 모델 로드 실패/접두사·한도/정규화, 실제 로컬 Qdrant의 회사·도메인·시간 filter, 이전 collection 보존, stale model/count/payload와 파일 유실·오염, 실제 페이지와 quote 불일치, receipt rank/hash, 질의 균형과 정답 발췌의 원본 일치를 포함한다.
