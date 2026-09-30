"""저장된 분석 State를 투자 판단 노드에 전달해 JSON으로 출력합니다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from main.agents.investment import TeamWebResearcher, make_rag_investment_judge_node
from main.agents.market import OpenAIWebSearch
from main.rag.company import BaseRAG


def _write_json(output: Path, value: object) -> None:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run investment judgment for one company")
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--input", type=Path, help="기존 분석과 검증된 근거를 담은 State JSON")
    parser.add_argument("--team-model", help="창업자·팀 10점 평가에 사용할 채팅 모델 ID")
    parser.add_argument("--env-file", type=Path, help="API 키를 읽을 .env 파일")
    parser.add_argument("--recommend-min-score", type=float, help="투자 추천 최소 총점 (기본 80)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--handoff-output", type=Path, help="추천 시 다음 에이전트용 JSON; 보류 시 null")
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("--env-file 파일이 없습니다.")
        load_dotenv(args.env_file, override=False)
    if args.handoff_output and args.handoff_output.expanduser().resolve() == args.output.expanduser().resolve():
        raise ValueError("--output과 --handoff-output은 다른 파일이어야 합니다.")

    state = json.loads(args.input.read_text(encoding="utf-8")) if args.input else {}
    if not isinstance(state, dict):
        raise ValueError("입력 JSON은 State 객체여야 합니다.")
    if "company_id" in state and state["company_id"] != args.company_id:
        raise ValueError("입력 State와 --company-id가 다릅니다.")
    state["company_id"] = args.company_id
    if args.recommend_min_score is not None:
        policy = state.setdefault("decision_policy", {})
        if not isinstance(policy, dict):
            raise ValueError("decision_policy는 객체여야 합니다.")
        policy["recommend_min_score"] = args.recommend_min_score
    rag = BaseRAG()
    team_search = OpenAIWebSearch(purpose="team") if args.team_model else None
    try:
        result = make_rag_investment_judge_node(
            rag, llm=args.team_model,
            team_researcher=TeamWebResearcher(team_search) if team_search else None,
        )(state)
    finally:
        if team_search:
            team_search.close()
        rag.close()

    _write_json(args.output, result)
    if args.handoff_output:
        _write_json(args.handoff_output, result["report_payload"])
    print(args.output.expanduser().resolve())


if __name__ == "__main__":
    main()
