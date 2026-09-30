# HarryKim 에이전트 전달 폴더

이 폴더에는 우리가 작성한 기술 요약·경쟁사 비교 에이전트와 연결 코드만 있습니다. 팀이 기존에 만든 `BaseRAG` 구현, CSV 데이터, Qdrant 설정은 포함하지 않았습니다.

## 포함 파일

- `main/agents/tech_summary_agent.py`: 기술 요약, 근거 검증, 기술 점수
- `main/agents/competitor_comparison_agent.py`: 경쟁사 비교, 근거 검증, 경쟁 점수
- `main/agents/evidence.py`, `main/agents/nodes.py`, `main/agents/__init__.py`: 팀 `BaseRAG`의 회사 ID를 두 에이전트에 연결
- `main/agents/docs/usage.md`: 사용 예시
- `run_agents.py`: 두 결과를 JSON으로 저장하는 실행 스크립트
- `requirements-agents.txt`: 에이전트가 추가로 필요로 하는 패키지
- `outputs/agent_results/company_17_vs_14.json`: 점수 기능을 넣기 전에 생성한 정성 평가 예시

각 노드는 보고서와 점수를 함께 반환합니다. `technical_score`와 `competitor_score`는 항목별 1~5점을 100점 만점으로 환산합니다. 근거가 부족한 항목은 `null`로 두며, 하나라도 미평가면 총점도 `null`입니다. 팀의 LangGraph State에는 이 두 점수 키를 추가해야 후속 투자 판단 노드로 전달됩니다.

## 팀 프로젝트에 적용

`main/baseRAG`가 있는 팀 프로젝트 루트에 `main/agents`, `run_agents.py`, `requirements-agents.txt`를 같은 상대 경로로 넣습니다. 팀 프로젝트의 기존 파일은 이 전달 폴더에 포함하지 않았습니다.

```bash
python -m pip install -r main/baseRAG/requirements-rag.txt -r requirements-agents.txt
python run_agents.py --company-id 17 --competitor-id 14 --output outputs/agent_results/company_17_vs_14.json
```

현재 `gicheol` 체크아웃 안에서 `HarryKim` 폴더를 바로 실행할 때는 저장소 루트에서 `python -m HarryKim.run_agents`에 같은 인수를 붙입니다.

실행에는 `OPENAI_API_KEY`와 기본 모델 `openai:gpt-4.1`의 접근 권한이 필요합니다. 기존 예시 JSON에는 새 점수 키가 없습니다. 코드를 다시 실행하면 두 점수 키가 함께 저장됩니다. 예시의 `locator`에는 실행 당시의 로컬 CSV 경로가 들어 있으며, 새 실행에서는 해당 팀 프로젝트의 경로가 기록됩니다.
