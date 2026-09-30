"""투자 판단 에이전트의 입력 형식과 실행 예시. 모든 자료는 가상 자료다.

실행 (프로젝트 루트): python -m main.agents.investment.example
LLM 없이 동작하므로 이 예시에서는 사용자가 사전에 평가한 team_rating을 넣는다.
실제 LLM 사용 시 team_rating을 생략하고 make_investment_judge_node(llm)를 사용한다.
조원은 탐색의 current_candidate/evidence_registry와 세 영역의100점 평가를 제공하면 된다.
"""
from __future__ import annotations

import json

from .agent import investment_judge_node


def sample_state():
    def evidence(ref, source="customer"):
        return {
            "id": ref, "document_id": "document-" + ref, "chunk_id": "chunk-" + ref,
            "company_id": "demo", "title": "가상 검증 자료", "publisher": "가상 기관",
            "url": "https://example.test/" + ref, "published_at": "2026-09-01",
            "data_year": 2026, "collected_at": "2026-09-30", "page": 1,
            "excerpt": "테스트용 가상 내용이며 실제 도입·매출·경력이 아님",
            "source_type": source, "is_mock": True,
        }

    def analysis(score, ref):
        return {"summary": "가상 분석 결과", "score": score, "max_score": 100,
                "reason": "담당 에이전트가100점 만점으로 계산한 가상 점수", "evidence_ids": [ref],
                "evidence": [evidence(ref)], "missing_items": [], "conflicts": []}

    market = analysis(80, "market")
    market["market_definition"] = "국내 제조공장에 반복 조립을 자동화하는 로봇을 판매한다."
    return {
        "as_of_date": "2026-09-30", "retry_count": 0,
        "current_candidate": {
            "id": "demo", "name": "가상 로보틱스 기업",
            "eligibility": {"is_private": True, "stage": "Seed", "has_exited": False,
                            "evidence_ids": ["eligibility"]},
            "team_info": {"founders": ["가상 창업자"],
                          "expertise": "가상의 로봇 개발·현장 도입 경력",
                          "roles": "하드웨어, 소프트웨어, 현장 운영",
                          "evidence_ids": ["team"]},
        },
        "technology_analysis": analysis(90, "technology"),
        "market_analysis": market,
        "competition_analysis": analysis(80, "competition"),
        "evidence_registry": {"eligibility": evidence("eligibility", "official"),
                              "team": evidence("team", "official")},
        "team_rating": {"score": 8, "max_score": 10, "reason": "투자 판단 담당자가 사전 평가한 가상 팀 점수",
                        "evidence_ids": ["team"]},
        "commitment_note": "예시에서는 장기 몰입 의지를 판단하지 않음; 실제 평가 시 인터뷰 필요",
    }


if __name__ == "__main__":
    result = investment_judge_node(sample_state())
    print("가상 자료 실행 예시 — 실제 기업의 투자 판단이 아닙니다.")
    print(json.dumps({key: result[key] for key in (
        "decision", "total_score", "scorecard", "decision_reasons", "missing_items",
        "needs_research", "retry_available", "next_action")}, ensure_ascii=False, indent=2))
