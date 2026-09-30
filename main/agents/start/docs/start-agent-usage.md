# StartAgent 사용법

`StartAgent`는 사용자 조건에 맞는 회사 하나를 선택해 후속 노드에 전달합니다. 투자 적합성을 평가하는 노드는 아니며, 회사 ID는 CSV의 문자열 값을 그대로 사용합니다. 아래 명령과 예제는 프로젝트 루트에서 실행합니다.

## 설치와 준비

Python 3.11 이상을 사용합니다. [requirements-startagent.txt](../requirements-startagent.txt)는 공용 BaseRAG 의존성도 함께 설치합니다. requirements와 이 문서는 `main/agents/start` 폴더에 있습니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r main/agents/start/requirements-startagent.txt

docker compose -p company-rag -f compose.qdrant.yml up -d
export OPENAI_API_KEY="발급받은_API_키"
export START_AGENT_MODEL="팀에서_정한_채팅_모델명"
```

`START_AGENT_MODEL`에는 실제 사용할 채팅 모델명을 지정합니다. 노드는 모델명을 고정하지 않으며, 호출자가 만든 `BaseRAG`와 `BaseChatModel` 객체를 받습니다. `.env`를 자동으로 읽지 않으므로 환경변수는 실행 프로세스에 설정합니다. 공용 RAG 설정과 CSV 변경 후 색인 준비는 [BaseRAG 사용법](../../../rag/company/docs/rag-usage.md)을 참고하세요.

## 직접 호출

```python
import os

from langchain_openai import ChatOpenAI
from main.rag.company import BaseRAG
from main.agents.start import StartAgent

rag = BaseRAG()
try:
    # 의미 검색 전에 앱 초기화 시 한 번 준비합니다. 기존 컬렉션은 재사용합니다.
    rag.build_index()
    llm = ChatOpenAI(model=os.environ["START_AGENT_MODEL"])
    node = StartAgent(rag=rag, llm=llm)

    result = node.invoke({"prompt": "누적 투자액이 가장 큰 회사 하나 골라줘"})
    print(result)
finally:
    # 전체 앱 또는 그래프 실행이 끝난 뒤 닫습니다.
    rag.close()
```

공개 API는 `StartAgent(rag: BaseRAG, llm: BaseChatModel)`과 `invoke(state: dict, config=None) -> dict`입니다. `prompt`는 비어 있지 않은 문자열이어야 합니다. 필요하면 `invoke(state, config=config)`로 LangChain 실행 설정을 전달할 수 있습니다.

| 결과 | 반환값 |
| --- | --- |
| 선택 성공 | `{"company_id": "1", "message": None}` — ID는 예시 |
| 조건에 맞는 회사 없음 | `{"company_id": None, "message": "조건에 해당하는 회사가 없습니다. 조건을 다시 입력해주세요."}` |
| 미지원 선택 조건 | `{"company_id": None, "message": "현재 CSV에 매출 컬럼이 없어 이 조건으로 회사를 선택할 수 없습니다. 조건을 다시 입력해주세요."}` — 설명은 조건에 따라 다릅니다 |

노드는 입력 State를 직접 수정하지 않고 `company_id`와 `message`의 갱신값만 반환합니다. 재호출 시 성공은 이전 메시지를 지우고, 무결과는 이전 회사 ID를 지웁니다. 다른 State 필드는 전체 그래프가 유지합니다.

## LangGraph 연결

전체 그래프 의존성은 `python -m pip install -r main/requirements.txt`로 설치합니다. LangGraph가 설치된 전체 앱에서 위 예제의 직접 호출 부분을 다음 코드로 바꿉니다. 준비된 `rag`와 `llm`은 여러 노드가 함께 사용할 수 있습니다.

```python
from langgraph.graph import END, START, StateGraph
from main.graph import InvestmentState, new_request_state, make_start_node

builder = StateGraph(InvestmentState)
builder.add_node("start_agent", make_start_node(node))
builder.add_edge(START, "start_agent")
builder.add_edge("start_agent", END)
graph = builder.compile()

# input()은 전체 그래프를 실행하는 진입 파일에서 받습니다.
prompt = input("회사 선택 조건을 입력하세요: ").strip()
result = graph.invoke(new_request_state(prompt))
print(result["message"] or result["company_id"])
```

이 예제는 회사 선택 후 종료합니다. 후속 분석 노드를 연결할 때는 `company_id is None`이면 종료하고, ID가 있으면 다음 노드로 보내도록 조건 분기를 구성합니다. `company_id`와 `message`는 일반 State 필드이며 메시지 목록용 `add_messages`를 적용하지 않습니다. 노드 내부에는 `input()`이나 터미널 실행 루프가 없습니다.

## 선택 규칙과 오류

숫자·날짜·범주 조건은 전체 CSV에 먼저 적용하며 여러 조건은 AND로 결합합니다. 최고·최저값과 랜덤 선택은 Python이 처리하며, 최고·최저값이 같은 후보가 여러 개면 모델이 CSV 근거로 투자 검토 우선순위를 판단합니다. 최고·최저 비교에서 결측값은 제외하고, 후보의 비교값이 모두 없으면 데이터 부족 오류를 알립니다. 의미 조건이나 일반 추천은 조건 해석 1회와 후보 검토·선택 최대 1회로 처리합니다. 동률 검토에는 모델 호출이 한 번 추가될 수 있습니다. 검색 유사도는 투자 점수가 아니며, 의미 조건 충족 여부는 후보 설명을 바탕으로 LLM이 판단합니다.

현재 CSV의 업력은 1.3~4.8년이므로 `업력 10년 이상인 회사 하나 골라줘`는 무결과를 반환합니다. 업력은 CSV 값을 사용하며 현재 날짜로 재계산하지 않습니다.

정상 조회 후 조건에 맞는 회사가 없으면 무결과 안내를 반환합니다. CSV에 없는 매출 조건이나 지원하지 않는 OR·정렬 조건은 `unsupported_reason`의 이유와 함께 `company_id=None`을 반환합니다. 전체 그래프의 `start → prompt` 분기로 이어져 같은 터미널 실행에서 다시 입력할 수 있습니다. 매출 조건을 누적 투자액 등 다른 항목으로 대체하지 않습니다.

빈 입력, 잘못된 모델 응답, CSV와 색인의 후보 불일치, OpenAI·Qdrant 장애는 기존대로 예외를 전달합니다. 실행 오류 전체를 사용자 입력 문제나 무결과로 처리하지 않습니다.

그래프의 `make_start_node`는 재선택할 때 기존 기술·경쟁·시장 보고서를 지웁니다. 이전 선택과 다른 회사를 고르면 이전 `competitor_ids`도 비우므로 비교 전에 경쟁사를 다시 지정해야 합니다. `StartAgent.invoke()` 자체의 반환값은 기존처럼 `company_id`, `message` 두 키입니다.
