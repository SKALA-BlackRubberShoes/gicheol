# 보고서 생성 에이전트

앞선 에이전트의 분석과 투자 판단을 한글 PDF·Markdown·검증 기록 JSON으로 만든다. 구현은 `main/agents/report`, 실행 진입점은 `main/scripts/generate_report.py`, 가상 입력은 `examples/reports/report_sample_state.json`에 둔다. 점수·판정을 재계산하거나 외부 자료를 검색하지 않는다.

## 실행

저장소 루트에서 Python 3.11 이상으로 실행한다. 다른 에이전트와 같은 가상환경을 사용하며 `main/requirements.txt`가 보고서 의존성도 설치한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r main/requirements.txt

# API 키·Qdrant 없이 가상 입력으로 출력 확인
python -m main.scripts.generate_report --demo

# 추천·보류를 포함하는 투자 판단 전체 State
python -m main.scripts.generate_report --input outputs/judgment_17.json

# 추천일 때 저장된 독립 report_payload
python -m main.scripts.generate_report --input outputs/report_input_17.json
```

투자 판단 CLI의 `--handoff-output` 파일은 **보류일 때 `null`**이다. 보류 보고서는 `hold_payload`가 들어 있는 전체 `judgment_17.json`을 사용한다. 별도로 추출한 `hold_payload` 객체도 입력할 수 있지만 `null`은 입력 State가 아니다. `--demo`의 기업과 자료는 모두 가상이며 실제 투자 평가 결과가 아니다.

각 실행은 `outputs/reports/<실행 ID>/`에 아래 파일을 만든다. `--output-dir`로 상위 출력 경로를, `--name`으로 확장자 없는 제출 파일명을 지정할 수 있다.

| 파일 | 내용 |
| --- | --- |
| `RAG-Output.pdf` | 5쪽 이내 한글 보고서 |
| `RAG-Output.md` | 같은 본문의 Markdown. `final_report`로도 반환 |
| `RAG-Output.json` | 본문, 경고, 요약 방식, PDF 상태·페이지 수, `source_payload` 원본 기록 |

PDF는 macOS의 AppleMyungjo, Windows의 맑은 고딕, Linux의 Nanum TTF를 자동 탐색한다. 없으면 `--font /path/to/NanumGothic.ttf` 또는 `REPORT_FONT_PATH`로 설치된 한글 TTF를 지정한다. 폰트를 PDF에 포함하며 시스템 폰트 파일은 저장소에 복사하지 않는다.

## 입력과 보존 규칙

`adapter.py`가 공통 `InvestmentState` 또는 투자 판단의 독립 payload를 보고서 입력으로 변환한다. payload의 `company`, `company_data`, `evaluation`, `source_outputs`, `evidence_registry`를 읽고 변환 전 입력은 검증 기록 JSON의 `source_payload`에 보존한다.

기존 `results_by_company` 누적 입력과 정규화된 단일 기업 입력도 지원한다. 전체 필드는 [schemas.py](../schemas.py), 누적 입력 예시는 [가상 예제](../../../../examples/reports/report_sample_state.json)를 참고한다.

- 회사는 CSV의 문자열 `company_id`로 식별한다. 누적 결과가 명시적으로 `{}`이면 이전 기업 결과를 다시 넣지 않는다.
- 실제 투자 판단의 `invest`·`hold`를 각각 추천·보류로 표시한다. 총점, 반영점수, 원점수, 판단 사유를 보존하며 가중치를 다시 곱하거나 순위를 만들지 않는다. 현재 판단 배점은 기술 30·시장 30·경쟁 30·팀 10점이다.
- `None`은 미확인, `0`은 실제 0점으로 구분한다. 평가 실패·결측·상충 자료와 점수 합계 불일치를 경고에 남긴다.
- **출처나 적격성 정보가 없다는 이유만으로 추천을 보류로 바꾸지 않는다.** 보고서는 판단 정책을 적용하지 않으며, 연결되지 않은 근거를 표시하고 전달된 결정을 유지한다.
- 전달된 근거만 출처로 연결한다. 문서 ID나 출처 없는 문장에 저자·URL을 만들어 붙이지 않는다. 원본 평가 출력은 보존하되 확인되지 않은 출처 연결은 경고로 남긴다.
- `REFERENCE`에는 본문에서 사용한 자료만 싣는다. 동일한 근거는 한 번만 나열하고 같은 ID의 상충 자료는 오류로 처리한다. 존재하지 않는 ID는 `근거 미확인`으로 표시한다.
- `main/rag/company`에서 전달한 기업 CSV의 제품·투자 단계·투자금을 표시한다. 입력에 없는 매출이나 팀 경력은 추정하지 않는다. 출처의 CSV 첫 데이터 행은 헤더를 포함해 2다.
- 설정, 기업 또는 사용한 근거에 가상 자료 표시가 있으면 SUMMARY에도 이를 명시한다.

원문 사실 검증과 투자 판단은 앞선 에이전트의 책임이다. 보고서는 값의 전달 일관성, 출처 연결, 출력 형식을 검사한다.

참고문헌은 투자 판단의 `evidence_registry`에 더해 기술·경쟁 결과의 `sources`와 시장 결과의 `market_evaluation.evidence`에 전달된 서지정보를 보고서 안에서만 연결한다. 이 보완은 투자 판단 원장, 점수, 정책을 변경하지 않는다. `content_kind=metadata_only`는 원문이 전달되지 않은 서지정보이고, `source_excerpt`는 상위 에이전트가 제공한 발췌를 `quote`에 보존한다. `generated_summary`는 모델이 작성한 문장을 `summary`에 별도로 보존하며 `quote`로 취급하지 않는다. 참고문헌에는 생성 요약·원문 미전달 여부와 `provenance_note`를 표시한다. `is_mock=None`은 실제/가상 자료 유형 미확인으로 유지하며, `False`도 자료 진위 검증을 뜻하지 않는다.

시장 PDF 출처의 `page`는 **시장 RAG에 제공된 PDF 파일 안의 1부터 시작하는 페이지 번호**다. 발췌본이면 원 간행물에 인쇄된 쪽 번호와 다를 수 있다. 보고서는 이를 `제공 PDF p.`로 표시하고 `page=발췌/제공 PDF 파일 기준`이라는 설명을 남긴다. 인쇄 페이지 번호를 추정하거나 원 간행물 쪽 번호라고 표시하지 않는다. 기타 출처의 페이지 위치는 `제공 위치 p.`로 표시한다.

동일한 판단 사유·미확인 항목이 여러 단계에서 반복되면 본문에는 한 번 싣고 해당 단계들을 함께 표시한다. 비교표는 종합 평가를 참조하며, 판정 사유에서 제외한 중복 한계·위험은 해당 항목에 남긴다. 출처별 제목·URL·페이지·인용 번호를 유지하면서 같은 출처 설명은 관련 ID를 모아 한 번 표시한다. 위험 문장에 명시된 출처 ID도 참고문헌에 연결한다. 분석 문장·점수·서로 다른 위험이나 한계는 분량을 맞추기 위해 삭제하지 않으며, 경고 원본 목록과 입력 전체는 JSON에 보존한다.

## Python 및 LangGraph 연결

```python
import json
from pathlib import Path
from main.agents.report import generate_report

state = json.loads(Path("outputs/judgment_17.json").read_text(encoding="utf-8"))
result = generate_report(state)
print(result["report_pdf_path"])
```

공통 노드 팩토리는 `main.graph`에서 가져온다. `main.agents.report.make_report_node`도 사용할 수 있다.

```python
from langgraph.graph import END
from main.graph import make_report_node

# builder는 팀이 조립하는 기존 StateGraph
builder.add_node("report", make_report_node(use_llm=False))
builder.add_edge("investment", "report")
builder.add_edge("report", END)
```

위 연결은 추천·보류 모두 보고서로 보내는 예시다. `investment`는 실제 노드 이름으로 바꾼다. 추천만 보낼 때는 투자 판단 이후 조건부 분기를 사용한다. 전체 에이전트를 실행하는 그래프의 조립은 별도로 필요하다.

노드는 기존 분석·판단을 변경하지 않고 다음 결과만 반환한다. 공통 `InvestmentState`에 이 필드가 선언되어 있다.

| 반환 필드 | 내용 |
| --- | --- |
| `final_report` | Markdown 본문 |
| `report_pdf_path`, `report_markdown_path`, `report_json_path` | 생성 파일의 절대 경로 |
| `report_page_count` | 실제 PDF 페이지 수 |
| `report_warnings` | 결측·출처·일관성 확인 항목 |
| `report_status` | `completed` 또는 `completed_with_warnings` |

## 선택적 LLM 요약

기본 실행은 모델을 호출하지 않는다. `--llm`을 지정하면 `ChatPromptTemplate | model.with_structured_output(...)` 체인으로 SUMMARY에 넣을 **원문 문장 ID만** 선택한다. 새 문장·점수·판정을 생성하지 않는다.

```bash
python -m main.scripts.generate_report --input outputs/judgment_17.json \
  --llm --env-file .env --model YOUR_AVAILABLE_MODEL_ID
```

`OPENAI_API_KEY`와 사용 가능한 모델 ID(`--model` 또는 `REPORT_MODEL`)가 필요하다. `.env`는 자동 로드하지 않으며 `--env-file`로 지정했을 때 읽는다. 모델 호출이나 ID 검증이 실패하면 기본 원문 요약으로 복구하고 대체 사실을 기록한다.

## 출력 제한과 검증

내부 흐름은 `prepare_report → compose_summary → export_report`다. 첫 챕터는 `SUMMARY`, 마지막 챕터는 `REFERENCE`이며 사업 아이디어, 시장·경쟁·위험, 팀 구성, 투자 판단, 한계점을 담는다.

제목·부제·SUMMARY는 첫 페이지에 함께 배치한다. SUMMARY 제목을 포함한 실제 높이가 가용 페이지의 절반을 넘거나 PDF 전체가 5쪽을 넘으면 실패한다. 초과 내용을 삭제하거나 읽기 어려운 크기로 줄이지 않는다. PDF 실패 시 검토용 Markdown과 `pdf_status=failed`인 JSON을 남기고 CLI는 종료 코드 1을 반환한다. 잘못된 입력, 상충 출처 ID, 글꼴·저장 오류도 성공으로 처리하지 않는다.

```bash
python -m unittest discover -s main/agents/report/tests -v
```

입력 변환·불변성, 점수/판정 보존, 결측과 0, 출처 충돌·미사용 자료, 가상 자료 표시, 모델 대체 흐름, 한글 PDF, SUMMARY·5쪽 제한, 표 줄바꿈과 기존 파일 보존을 검사한다. 실제 모델을 호출하는 전체 서비스 검증은 별도다.

| 구현 파일 | 역할 |
| --- | --- |
| `schemas.py` | 보고서 입력 모델과 정규화 계약 |
| `adapter.py` | 공통 State·투자 판단 payload 변환 |
| `content.py` | 목차·비교표·인용·한계점 조립 |
| `agent.py` | 보고서 LangGraph와 Python API |
| `renderers.py` | Markdown·한글 PDF와 물리적 분량 검사 |
| `main/scripts/generate_report.py` | CLI 진입점 |
