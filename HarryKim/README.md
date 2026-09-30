# HarryKim 에이전트 전달 폴더

이 폴더에는 우리가 작성한 기술 요약·경쟁사 비교 에이전트와 연결 코드만 있습니다. 팀이 기존에 만든 `BaseRAG` 구현, CSV 데이터, Qdrant 설정은 포함하지 않았습니다.

## 포함 파일

- `main/agents/tech_summary_agent.py`: 기술 요약과 근거 검증
- `main/agents/competitor_comparison_agent.py`: 경쟁사 비교와 근거 검증
- `main/agents/evidence.py`, `main/agents/nodes.py`, `main/agents/__init__.py`: 팀 `BaseRAG`의 회사 ID를 두 에이전트에 연결
- `main/agents/docs/usage.md`: 사용 예시
- `run_agents.py`: 두 결과를 JSON으로 저장하는 실행 스크립트
- `requirements-agents.txt`: 에이전트가 추가로 필요로 하는 패키지
- `outputs/agent_results/company_17_vs_14.json`: 실제 실행 결과 예시

## 팀 프로젝트에 적용

`main/baseRAG`가 있는 팀 프로젝트 루트에 `main/agents`, `run_agents.py`, `requirements-agents.txt`를 같은 상대 경로로 넣습니다. 팀 프로젝트의 기존 파일은 이 전달 폴더에 포함하지 않았습니다.

```bash
python -m pip install -r main/baseRAG/requirements-rag.txt -r requirements-agents.txt
python run_agents.py --company-id 17 --competitor-id 14 --output outputs/agent_results/company_17_vs_14.json
```

실행에는 `OPENAI_API_KEY`와 기본 모델 `openai:gpt-4.1`의 접근 권한이 필요합니다. 예시 JSON은 `gicheol`의 CSV를 사용해 생성한 결과로, `locator`에는 실행 당시의 로컬 CSV 경로가 들어 있습니다. 새 실행에서는 해당 팀 프로젝트의 경로가 기록됩니다.
