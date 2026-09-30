"""각 노드가 공유하는 State 키입니다. 값은 보고서별로 덮어씁니다."""

from typing import TypedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from main.agents.investment.agent import InvestmentJudgeState


class InvestmentState(InvestmentJudgeState, total=False):
    prompt: str
    company_id: str | None
    message: str | None
    competitor_ids: list[str]
    technology_summary: dict | None
    technical_score: dict | None
    competitor_comparison: dict | None
    competitor_score: dict | None
    market_evaluation: dict | None
    company_data: dict | None
    final_report: str | None
    report_pdf_path: str | None
    report_markdown_path: str | None
    report_json_path: str | None
    report_page_count: int | None
    report_warnings: list[str]
    report_status: str | None


def reset_judge_state() -> dict:
    """회사 재선택 시 투자 판단 입력과 결과가 섞이지 않도록 초기화합니다."""
    return {
        "current_candidate": None,
        "technology_analysis": None,
        "market_analysis": None,
        "competition_analysis": None,
        "evidence_registry": {},
        "team_rating": None,
        "team_evidence": [],
        "team_missing_items": [],
        "team_conflicts": [],
        "judgment_notes": [],
        "commitment_note": "장기 몰입 의지는 공개자료만으로 확정할 수 없음; 인터뷰 필요",
        "market_definition": None,
        "as_of_date": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
        "retry_count": 0,
        "team_research_log": [],
        "decision_policy": {},
        "decision": None,
        "current_evaluation": None,
        "scorecard": None,
        "total_score": None,
        "upstream_score_90": None,
        "judge_score_10": None,
        "decision_reasons": [],
        "missing_items": [],
        "conflicts": [],
        "used_reference_ids": [],
        "needs_research": False,
        "research_requests": [],
        "retry_available": False,
        "updated_candidate": None,
        "next_action": None,
        "report_payload": None,
        "hold_payload": None,
        "company_data": None,
        "final_report": None,
        "report_pdf_path": None,
        "report_markdown_path": None,
        "report_json_path": None,
        "report_page_count": None,
        "report_warnings": [],
        "report_status": None,
    }


def new_request_state(
    prompt: str, *, competitor_ids: list[str] | None = None
) -> InvestmentState:
    """새 사용자 요청을 시작할 때 이전 회사의 평가가 섞이지 않는 State를 만듭니다."""
    return InvestmentState(
        prompt=prompt,
        company_id=None,
        message=None,
        competitor_ids=list(competitor_ids or []),
        technology_summary=None,
        technical_score=None,
        competitor_comparison=None,
        competitor_score=None,
        market_evaluation=None,
        **reset_judge_state(),
    )
