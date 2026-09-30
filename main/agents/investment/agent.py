"""Physical AI / Robotics 투자 판단 전용 에이전트: 기술30·시장30·경쟁30·팀10.

Python 3.11+. 기본 판단은 표준 라이브러리만 사용한다.
앞선 세 에이전트의 최종100점 점수를 각각30점 배점으로 환산한다.
리스크는 별도 배점 없이 판정/보고서에 반영한다.

입력 계약
---------
current_candidate: {id, name, eligibility, team_info, ...기업 소개/투자 현황}
technology_analysis / market_analysis / competition_analysis:
    {summary, score: 0~100 또는 None, max_score: 100, reason, evidence_ids,
     evidence: [Evidence, ...], missing_items: [...], conflicts: [...],
     critical_risks: [{description, evidence_ids, resolved}, ...]}
market_analysis에는 market_definition을 전달한다. 세부 평가표는 breakdown에
담아도 된다. 앞선 에이전트가 계산한 score를 0.3배 하여 합산한다.
evidence_registry: {근거 ID: Evidence}; 전체 기업에서 ID가 유일해야 한다.
적격성: {is_private: bool|None, stage: Seed/Series A/B/C|None,
         has_exited: bool|None, evidence_ids: [...]}

팀 평가와 자료 수집
------------------
기존 team_info와 근거를 우선 사용한다. 부족하면 team_researcher를 연결한다.
검색 제공자/API 키를 임의로 선택하지 않는다. 연결하지 않으면 기존 자료만 사용한다.
team_researcher(request) -> {team_info: dict, evidence: [Evidence, ...],
                           missing_items: [...], conflicts: [...]}
request: company, existing_evidence, queries, as_of_date, attempt, missing_items.
동명이인/다른 회사 인물을 구분하고 실제 읽은 원문의 URL/발췌만 반환해야 한다.
LLM은 팀만 평가한다. 전문성4 + 핵심 역할 구성2 + 실행 경험4 = 10점이다.
10년 몰입은 점수를 강제하지 않고 정성 메모로 남긴다.
웹/문서 검색은 기업별 retry_count를 포함해 최대2회이다.

연결 예시
---------
    node = make_investment_judge_node(llm, team_researcher=search_team)
    builder.add_node("investment_judge", node)
    builder.add_conditional_edges("investment_judge", route_after_investment,
                                 {"report": "report_agent", "hold": "hold_handler"})

사전 팀 평가가 있으면 investment_judge_node(state)로 실행할 수 있다.
team_rating: {score: 0~10 또는 None, max_score: 10, reason, evidence_ids}
LLM 없이 수집만 실행하면 자동 팀 평가를 하지 않는다. 미평가 팀 점수는0/10으로 계산한다.

운영 제안값: 총80+, 기술18/30·시장19.5/30·경쟁18/30·팀0/10 이상.
state.decision_policy에서 recommend_min_score와 minimum_scores를 변경할 수 있다.
점수 미완성/기준 미달/상충/확인된 부적격/중대한 미해결 위험은 보류한다.
근거·적격성 자료 부족은 점수를 보존하고 재검토를 요청하되 단독 보류 사유가 아니다.
팀은 회사 자체 자료만 있으면 최대4/10(이전2/5 증거 규칙 환산)을 적용한다.

출력: current_evaluation, scorecard, total_score, decision, decision_reasons,
next_action, report_payload, hold_payload, research_requests, evidence_registry,
needs_research, retry_available, retry_count, team_research_log, updated_candidate.
report_payload에는 기업 정보·세 분석·팀 조사·점수·판정·근거가 포함된다.
원문이 주장을 지지하는지의 의미 검증은 담당 분석/팀 평가자의 책임이다.
가상 자료는 결과에도 표시한다. invest는 후속 투자 검토 추천이다.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
from typing import Any, Literal

from main.agents.common.llm import resolve_chat_model

from .schemas import Evidence, InvestmentJudgeState, TeamResearcher
from .scoring import (
    WEIGHTS, UPSTREAM_MAX_SCORE, ANALYSIS_KEYS, LABELS, TEAM_WEIGHTS,
    DEFAULT_POLICY, MAX_RESEARCH, _unique, _strings, _score, _merge_evidence,
    _prepare_input, _policy, _usable_refs, _team_rating, _eligibility,
    _research_requests, judge_investment, investment_judge_node,
    route_after_investment,
)
from .prompts import TEAM_JUDGMENT_PROMPT, TEAM_JUDGMENT_SCHEMA


def _has_team_data(state: InvestmentJudgeState) -> bool:
    info = state["current_candidate"].get("team_info", {})
    if not isinstance(info, dict) or not info:
        return False
    refs = info.get("evidence_ids", [])
    return bool(refs) and all(r in state["evidence_registry"] and
                             state["evidence_registry"][r]["company_id"] == state["current_candidate"]["id"]
                             for r in refs)


def _lookup_team(state: InvestmentJudgeState, researcher: TeamResearcher, issues: list[str]) -> InvestmentJudgeState:
    working = deepcopy(state)
    candidate = working["current_candidate"]
    info = candidate.get("team_info", {})
    founders = info.get("founders", []) if isinstance(info, dict) else []
    names = [x if isinstance(x, str) else x.get("name", "") for x in founders if isinstance(x, (str, dict))]
    queries = [f"{candidate['name']} 창업자 대표 로봇 AI 경력 팀 제품 현장 도입",
               *[f"{candidate['name']} {name} 경력 제품 출시 현장 도입" for name in names if name],
               *[f"{candidate['name']} {issue}" for issue in issues]]
    request = {"company": deepcopy(candidate), "as_of_date": working["as_of_date"],
               "existing_evidence": deepcopy(working["evidence_registry"]), "queries": _unique(queries),
               "attempt": working["retry_count"] + 1, "missing_items": issues[:]}
    working["retry_count"] += 1
    try:
        result = researcher(request)
    except (OSError, RuntimeError) as exc:
        working.setdefault("team_missing_items", []).append(f"창업자·팀: 자료 수집 실패 ({type(exc).__name__})")
        working["team_research_log"].append({"attempt": request["attempt"], "queries": request["queries"],
                                            "status": "failed", "evidence_ids": []})
        return working
    if not isinstance(result, dict) or set(result) - {"team_info", "evidence", "missing_items", "conflicts"}:
        raise ValueError("팀 수집 결과에는 team_info·evidence·missing_items·conflicts만 반환하세요.")
    added = result.get("evidence", [])
    _merge_evidence(working["evidence_registry"], added)
    new_info = result.get("team_info", {})
    if not isinstance(new_info, dict):
        raise ValueError("수집한 team_info는 dict여야 합니다.")
    old_info = info if isinstance(info, dict) else {}
    merged = {**old_info, **deepcopy(new_info)}
    merged["research_history"] = [*deepcopy(old_info.get("research_history", [])),
                                  {"attempt": request["attempt"], "team_info": deepcopy(new_info),
                                   "evidence_ids": [e["id"] for e in added]}]
    merged["evidence_ids"] = _unique(_strings(old_info.get("evidence_ids", []), "기존 팀 근거") +
                                    _strings(new_info.get("evidence_ids", []), "새 팀 근거") + [e["id"] for e in added])
    candidate["team_info"] = merged
    working["team_missing_items"] = _strings(result.get("missing_items", []), "수집 missing_items")
    working["team_conflicts"] = _unique(working.get("team_conflicts", []) + _strings(result.get("conflicts", []), "수집 conflicts"))
    working["team_research_log"].append({"attempt": request["attempt"], "queries": request["queries"],
                                        "status": "collected" if added else "no_evidence",
                                        "evidence_ids": [e["id"] for e in added]})
    return working


def make_investment_judge_node(llm: Any = None, *, team_researcher: TeamResearcher | None = None,
                               research_mode: Literal["if_missing", "always", "off"] = "if_missing") -> Callable:
    """기존 모델·팀 웹검색 함수를 연결한다.

    if_missing: 기존 팀 자료가 없거나 LLM이 팀 근거 부족을 확인하면 수집한다.
    always: 첫 라운드에 자료를 보완한다. off: 기존 자료만 사용한다.
    검색 함수는 실제 웹검색/원문 읽기 또는 프로젝트 RAG 함수를 감싸서 제공한다.
    LangChain 구조화 출력 참고:
    https://reference.langchain.com/python/langchain-core/language_models/chat_models/BaseChatModel/with_structured_output
    """
    if research_mode not in ("if_missing", "always", "off"):
        raise ValueError("research_mode는 if_missing/always/off여야 합니다.")
    if llm is None and team_researcher is None:
        return investment_judge_node
    model = (resolve_chat_model(llm).with_structured_output(deepcopy(TEAM_JUDGMENT_SCHEMA))
             if llm is not None else None)

    def node(state: InvestmentJudgeState) -> dict[str, Any]:
        working = _prepare_input(state)
        if _eligibility(working, [])[0] == "excluded":
            return judge_investment(working)
        if team_researcher is not None and research_mode != "off" and working["retry_count"] < MAX_RESEARCH and (
            research_mode == "always" or not _has_team_data(working)):
            working = _lookup_team(working, team_researcher, ["창업자·팀: 경력·역할·실행 경험 근거 확인"])
        while True:
            assessment = None
            if model is not None:
                payload = {k: working.get(k) for k in ("as_of_date", "current_candidate", *ANALYSIS_KEYS,
                                                       "evidence_registry", "team_missing_items", "team_conflicts")}
                assessment = model.invoke([{"role": "system", "content": TEAM_JUDGMENT_PROMPT},
                                           {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
            result = judge_investment(working, team_judgment=assessment)
            team_issues = [r["issue"] for r in result["research_requests"] if r["target_agent"] == "investment_judge"]
            if (model is None or not team_issues or result["current_evaluation"]["eligibility_status"] == "excluded"
                    or team_researcher is None or research_mode == "off" or working["retry_count"] >= MAX_RESEARCH):
                return result
            working = _lookup_team(working, team_researcher, team_issues)

    return node
