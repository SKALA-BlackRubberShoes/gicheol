# 기술·경쟁 평가용 PDF RAG 자료

수집일: 2026-09-30. 우선 대상: CSV 기업 ID 17 카본식스, ID 14 리얼월드(RLWRLD).

**원문 PDF 5개 + 공식 웹 자료 요약 PDF 1개, 총 143쪽**을 준비했다. 페이지 텍스트와 검색용 청크를 생성하고 기술·경쟁 에이전트의 기본 평가 경로에 연결했다. [PDF RAG 사용법](../../../main/rag/company_pdf/README.md)에 실행 방법과 점수 반영 규칙이 있다.

## 자료 목록

| 문서 | 쪽수 | 용도 | 파일 / 원출처 |
|---|---:|---|---|
| RLDX-1 Technical Report v2 | 55 | 리얼월드 모델 구조·학습·성능 실험 | [PDF](raw/rlwrld_rldx1_2605.03269v2.pdf) · [arXiv](https://arxiv.org/abs/2605.03269v2) |
| 카본식스 공식 웹 자료 요약 | 3 | 제품, AI 역할, 적용 사례, 사업화 단서 | [요약 PDF](derived/carbon6_official_source_notes.pdf) · [제품](https://www.carbon6robotics.com/ko/product) · [적용 사례](https://www.carbon6robotics.com/ko/applications) · [DIPS](https://dips1000.net/com_robo_gall/102?sca=2025) |
| 2026년 6월 8–12일 주간 투자 동향 | 10 | 카본식스 투자 보도; 본문은 PDF 4쪽 | [PDF](raw/startuprecipe_20260608.pdf) · [원출처](https://startuprecipe.co.kr/wp-content/uploads/2026/06/260608_weekly_startuprecipe_v001.pdf) |
| pi0.5 v1 | 19 | 일반화 능력 평가와 RLDX 비교 모델 이해 | [PDF](raw/pi05_2504.16054v1.pdf) · [arXiv](https://arxiv.org/abs/2504.16054v1) |
| OpenVLA v3 | 37 | VLA 학습 데이터·미세조정·평가 방법 | [PDF](raw/openvla_2406.09246v3.pdf) · [arXiv](https://arxiv.org/abs/2406.09246v3) |
| Diffusion Policy, IJRR판 | 19 | 모방학습과 조작 작업 평가 방법 | [PDF](raw/diffusion_policy_ijrr_2024.pdf) · [저자 프로젝트](https://diffusion-policy.cs.columbia.edu/) |

카본식스의 공개 원문 기술 PDF는 이번 검색에서 찾지 못했다. 대신 공식 웹 자료를 짧게 재서술하여 `derived/`에 저장했다. **이 PDF는 카본식스가 발행한 사양서나 독립 검증 보고서가 아니다.** 페이지마다 원출처와 요약임을 표시했다. 작성 내용은 [JSON](derived/carbon6_source_notes.json)에서도 볼 수 있다.

## RAG 입력 파일

- [sources.json](sources.json): 출처, 버전, 기업 ID, 활용 항목, 근거의 한계.
- [processed/pages.jsonl](processed/pages.jsonl): 원문 기준 143개 페이지 레코드. `page`는 1부터 시작하는 PDF 파일 페이지 번호다.
- [processed/chunks.jsonl](processed/chunks.jsonl): 495개 청크. 페이지를 넘지 않게 최대 1,400자, 200자 중복으로 분할한 시작용 데이터다. 표 구조를 보존하는 분할은 아니다.
- [processed/validation.json](processed/validation.json): 파일 크기, SHA-256, 추출 문자 수, 페이지 수, 파서 경고.

청크는 `document_id`, `page`, `source_url`, `company_ids`, `evidence_scope`, `source_type`, `is_derived`, `limitations`를 포함한다. 저장 시 텍스트는 `page_content`, 나머지 값은 metadata로 전달하면 된다. 벡터 DB가 리스트 metadata를 지원하지 않으면 기업별 인덱스로 나누거나 기업 ID를 별도 스칼라 필드로 변환한다.

원본 투자 보고서는 여러 기업을 포함한다. `pages.jsonl`에는 원문 페이지 전체를 보관하되, `chunks.jsonl`에는 4쪽의 카본식스 문단만 추출했다. 페이지 전체에 포함된 다른 기업의 매출을 카본식스 실적으로 사용하면 안 된다.

## 검색과 평가에 적용할 구분

1. 기업 사실을 찾을 때는 해당 `company_ids`로 필터한다. 리얼월드 14, 카본식스 17이다.
2. `technical_background` 논문 3개는 평가 방법 설명용이다. 기업 ID가 비어 있으며, 그 논문의 성능을 두 회사의 실적으로 간주하지 않는다. 특히 Diffusion Policy가 카본식스의 실제 채택 구조라고 추정하지 않는다.
3. `company_technical`도 저자 자체 실험일 수 있다. 모델, 작업, 로봇, 학습 조건, 지표, 표본 수를 함께 읽는다. RLDX 보고서의 비교 모델 결과가 카본식스와의 직접 비교를 의미하지 않는다.
4. `company_description`은 제품과 활용 방식의 근거이며 정량 성능 검증과 구분한다. `company_business_context`는 투자 문맥에만 사용한다.
5. 점수 근거에는 문서 ID·PDF 페이지·출처 URL·인용 문맥을 남긴다. PDF 수나 검색 결과 수 자체를 가산점으로 사용하지 않는다.
6. 과거 평가를 재현한다면 `published_date`로 기준일 이후 자료를 제외한다. 날짜의 정밀도는 연도/월/일로 다르므로 처리 시 구분한다. 웹 요약은 수집일 당시 내용을 반영한다.

리얼월드에는 상세 논문이 있고 카본식스에는 제품 설명이 주로 있어 공개 근거의 양이 다르다. 이 차이를 곧바로 실제 기술 수준 차이로 해석하면 안 된다. 고객 검수서, 같은 조건의 성능 시험 결과, 특허 원문, 도입 규모 자료가 추가되면 평가 근거가 더 좋아진다.

## 검증 결과와 재생성

6개 모두 PDF로 열리고, 143쪽 모두 텍스트가 추출됐다. 원문 PDF의 첫 페이지와 투자 보고서 4쪽, 작성한 요약 PDF 3쪽을 렌더링하여 확인했다. 요약본 한글도 확인했다. 원문 논문의 모든 도표를 수동으로 전사·검증한 것은 아니다.

Diffusion Policy 원문에서 잘못된 숫자 객체를 0으로 처리했다는 pypdf 경고 1건이 발생했다. 파일을 수정하지 않았으며 경고를 validation.json에 남겼다. 모든 페이지에서 텍스트는 추출되지만, 수식·그래프·표 수치는 원문 화면과 대조해야 한다. 투자 보고서 1쪽은 표지라 텍스트가 짧다.

이 폴더에서 `pypdf`가 설치된 Python으로 실행한다. 네트워크나 API 키를 사용하지 않는다.

```bash
python prepare_corpus.py
```

연결 흐름은 `chunks.jsonl → OpenAI 임베딩 → 로컬 벡터 색인 → 회사별·항목별 검색 → 근거를 포함한 LLM 평가`다. 첫 실행에 색인을 만들고 이후 파일 해시가 같으면 재사용한다. 일반 기술 논문과 투자 보고서는 현재 기업 기술·경쟁 점수 검색에서 제외한다.
