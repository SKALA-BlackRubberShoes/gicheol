"""근거 검증, 영역별 배점, 적격성 및 투자 판정을 계산합니다."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import math
from typing import Any, Literal
from zoneinfo import ZoneInfo

from .schemas import Evidence, InvestmentJudgeState
from .handoff import build_handoff_payload

WEIGHTS = {"technology": 30, "market": 30, "competitive_advantage": 30, "team": 10}
UPSTREAM_MAX_SCORE = 100
ANALYSIS_KEYS = {"technology_analysis": "technology", "market_analysis": "market",
                 "competition_analysis": "competitive_advantage"}
LABELS = {"technology": "제품·기술력", "market": "시장성",
          "competitive_advantage": "경쟁 우위", "team": "창업자·팀"}
TEAM_WEIGHTS = {"domain_expertise": 4, "role_coverage": 2, "execution": 4}
DEFAULT_POLICY = {"recommend_min_score": 80,
                  "minimum_scores": {"technology": 18, "market": 19.5,
                                     "competitive_advantage": 18, "team": 0}}
MAX_RESEARCH = 2


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{name}은 비어 있지 않은 문자열 목록이어야 합니다.")
    return value


def _score(value: Any, maximum: float, name: str) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"{name}은 0~{maximum:g} 범위의 유한한 숫자 또는 None이어야 합니다.")
    return float(value)


def _merge_evidence(registry: dict[str, Evidence], items: list[Evidence]) -> None:
    if not isinstance(items, list):
        raise ValueError("evidence는 목록이어야 합니다.")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("각 근거는 dict여야 합니다.")
        for key in ("id", "document_id", "chunk_id", "title", "publisher", "url", "collected_at", "excerpt"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"근거 메타데이터 {key}가 필요합니다.")
        for key in ("company_id", "published_at", "data_year", "page"):
            if key not in item:
                raise ValueError(f"근거 {key}가 필요합니다. 미확인 시 None을 사용하세요.")
        if item["company_id"] is not None and (not isinstance(item["company_id"], str) or not item["company_id"].strip()):
            raise ValueError("근거 company_id는 기업 ID 또는 None이어야 합니다.")
        if item.get("source_type") not in ("company", "customer", "official", "independent"):
            raise ValueError("근거 source_type이 올바르지 않습니다.")
        if item["page"] is not None and (type(item["page"]) is not int or item["page"] < 1):
            raise ValueError("page는 1 이상의 정수 또는 None이어야 합니다.")
        if "is_mock" in item and type(item["is_mock"]) is not bool:
            raise ValueError("is_mock은 bool이어야 합니다.")
        if item["id"] in registry and registry[item["id"]] != item:
            raise ValueError(f"동일 근거 ID의 내용이 다릅니다: {item['id']}")
        registry[item["id"]] = deepcopy(item)


def _prepare_input(state: InvestmentJudgeState) -> InvestmentJudgeState:
    working = deepcopy(state)
    candidate = working.get("current_candidate")
    if not isinstance(candidate, dict) or any(not isinstance(candidate.get(k), str) or not candidate[k].strip()
                                              for k in ("id", "name")):
        raise ValueError("current_candidate에 기업 id와 name이 필요합니다.")
    count = working.get("retry_count", 0)
    if type(count) is not int or not 0 <= count <= MAX_RESEARCH:
        raise ValueError("retry_count는 기업별0~2 정수여야 합니다.")
    working["retry_count"] = count
    as_of = working.get("as_of_date", datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat())
    try:
        if not isinstance(as_of, str) or date.fromisoformat(as_of).isoformat() != as_of:
            raise ValueError
    except ValueError:
        raise ValueError("as_of_date는 YYYY-MM-DD 형식이어야 합니다.") from None
    working["as_of_date"] = as_of
    registry: dict[str, Evidence] = {}
    for ref, item in working.get("evidence_registry", {}).items():
        if ref != item.get("id"):
            raise ValueError("evidence_registry 키와 근거 ID가 다릅니다.")
        _merge_evidence(registry, [item])
    for field in ANALYSIS_KEYS:
        result = working.get(field)
        if result is not None:
            if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
                raise ValueError(f"{field}는 summary와100점 만점 score를 담은 dict여야 합니다.")
            if result.get("max_score") != UPSTREAM_MAX_SCORE or type(result.get("max_score")) not in (int, float):
                raise ValueError(f"{field}.max_score는100이어야 합니다.")
            if "score" not in result:
                raise ValueError(f"{field}.score가 필요합니다. 평가 불가 시 None을 사용하세요.")
            _score(result["score"], UPSTREAM_MAX_SCORE, f"{field}.score")
            _merge_evidence(registry, result.get("evidence", []))
    _merge_evidence(registry, working.get("team_evidence", []))
    working["evidence_registry"] = registry
    working.setdefault("team_research_log", [])
    return working


def _policy(state: InvestmentJudgeState) -> dict[str, Any]:
    override = state.get("decision_policy", {})
    if not isinstance(override, dict) or set(override) - {"recommend_min_score", "minimum_scores"}:
        raise ValueError("decision_policy에는 recommend_min_score와 minimum_scores만 설정하세요.")
    policy = deepcopy(DEFAULT_POLICY)
    policy["recommend_min_score"] = override.get("recommend_min_score", policy["recommend_min_score"])
    floor = override.get("minimum_scores", {})
    if not isinstance(floor, dict) or set(floor) - WEIGHTS.keys():
        raise ValueError("minimum_scores의 평가영역이 올바르지 않습니다.")
    policy["minimum_scores"].update(floor)
    if _score(policy["recommend_min_score"], 100, "추천 총점 기준") is None:
        raise ValueError("추천 기준에 None을 사용할 수 없습니다.")
    for key, value in policy["minimum_scores"].items():
        if _score(value, WEIGHTS[key], f"{key} 최소점수") is None:
            raise ValueError("최소점수에 None을 사용할 수 없습니다.")
    return policy


def _usable_refs(refs: list[str], key: str, state: InvestmentJudgeState, missing: list[str]) -> list[str]:
    usable = []
    company_id = state["current_candidate"]["id"]
    for ref in _unique(_strings(refs, f"{key}.evidence_ids")):
        item = state["evidence_registry"].get(ref)
        if item is None:
            missing.append(f"{LABELS.get(key, key)}: 미등록 근거 {ref}")
        elif key in ("technology", "team", "eligibility") and item["company_id"] != company_id:
            missing.append(f"{LABELS.get(key, key)}: 해당 기업의 근거가 아님 ({ref})")
        elif key in ("market", "risk") and item["company_id"] not in (company_id, None):
            missing.append(f"{LABELS.get(key, key)}: 다른 기업의 근거 사용 ({ref})")
        else:
            usable.append(ref)
    return usable


def _team_rating(rating: dict[str, Any] | None, state: InvestmentJudgeState,
                 missing: list[str]) -> dict[str, Any]:
    if rating is None:
        missing.append("창업자·팀: 전문성·역할 구성·실행 경험 평가 자료 필요")
        return {"score": 0.0, "max_score": 10, "reason": "평가 자료 없음: 0점 처리", "evidence_ids": []}
    if not isinstance(rating, dict) or not isinstance(rating.get("reason"), str) or not rating["reason"].strip():
        raise ValueError("팀 평가의 reason이 필요합니다.")
    normalized = deepcopy(rating)
    submitted_refs = rating.get("evidence_ids", [])
    refs = _usable_refs(submitted_refs, "team", state, missing)
    if "criteria" in rating:
        criteria = rating["criteria"]
        if not isinstance(criteria, dict) or set(criteria) != TEAM_WEIGHTS.keys():
            raise ValueError("팀 criteria에 domain_expertise·role_coverage·execution이 필요합니다.")
        parts, checked = [], {}
        for key, maximum in TEAM_WEIGHTS.items():
            item = criteria[key]
            if not isinstance(item, dict) or "score" not in item or not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ValueError(f"팀 세부 평가 {key}의 score와 reason이 필요합니다.")
            score = _score(item["score"], maximum, key)
            item_submitted_refs = item.get("evidence_ids", [])
            item_refs = _usable_refs(item_submitted_refs, "team", state, missing)
            if score is None:
                missing.append(f"창업자·팀: {key} 점수 미확인, 0점 처리")
            if item_submitted_refs and not item_refs:
                score = 0.0
                missing.append(f"창업자·팀: {key} 잘못된 근거 ID로 0점 처리")
            if not item_refs:
                missing.append(f"창업자·팀: {key} 검증 근거 보완 필요")
            checked[key] = {"score": score, "max_score": maximum, "reason": item["reason"], "evidence_ids": item_refs}
            refs.extend(item_refs)
            parts.append(score or 0.0)
        score = round(sum(parts), 2)
        normalized["criteria"] = checked
    else:
        if rating.get("max_score") != 10 or type(rating.get("max_score")) not in (int, float) or "score" not in rating:
            raise ValueError("사전 팀 평가에는 score와 max_score=10이 필요합니다.")
        score = _score(rating["score"], 10, "팀 점수")
    refs = _unique(refs)
    if score is None:
        score = 0.0
        missing.append("창업자·팀: 점수 미확인, 0점 처리")
    if submitted_refs and not refs:
        score = 0.0
        missing.append("창업자·팀: 잘못된 근거 ID로 0점 처리")
    if not refs:
        missing.append("창업자·팀: 검증 근거 보완 필요")
    submitted = score
    if score > 4 and (not refs or all(state["evidence_registry"][r]["source_type"] == "company" for r in refs)):
        score = 4.0
        normalized["reason"] += " / 외부 검증 근거가 없어 최대4/10 적용"
        missing.append("창업자·팀: 고객·공식·독립 근거 추가 필요")
    normalized.update(score=score, max_score=10, evidence_ids=refs, uncapped_score=submitted)
    return normalized


def _eligibility(state: InvestmentJudgeState, missing: list[str]) -> tuple[str, list[str]]:
    info = state["current_candidate"].get("eligibility", {})
    if not isinstance(info, dict):
        raise ValueError("eligibility는 dict여야 합니다.")
    original = info.get("evidence_ids", [])
    refs = _usable_refs(original, "eligibility", state, missing)
    stage_ok = info.get("stage") in ("Seed", "Series A", "Series B", "Series C")
    if (info.get("is_private") is False or info.get("has_exited") is True
        or (info.get("stage") is not None and not stage_ok)) and refs:
        return "excluded", refs
    if info.get("is_private") is True and info.get("has_exited") is False and stage_ok and refs and len(refs) == len(original):
        return "eligible", refs
    missing.append("적격성: 비상장·Seed~Series C·미Exit 정보/근거 확인 필요")
    return "unverified", refs


def _research_requests(name: str, issues: list[str]) -> list[dict[str, str]]:
    requests = []
    for issue in _unique(issues):
        if issue.startswith("창업자·팀"):
            owner = "investment_judge"
        elif issue.startswith(("적격성", "eligibility")):
            owner = "startup_discovery"
        elif issue.startswith("제품·기술력"):
            owner = "technology"
        elif issue.startswith(("시장", "고객", "초기 고객", "성공 시")):
            owner = "market"
        elif issue.startswith(("경쟁", "위험", "risk")):
            owner = "competition"
        else:
            owner = "analysis_owner"
        requests.append({"target_agent": owner, "issue": issue, "query": f"{name}: {issue}"})
    return requests


def judge_investment(state: InvestmentJudgeState, *, team_judgment: dict[str, Any] | None = None) -> dict[str, Any]:
    """앞선100점 평가×3을 각30점으로 환산하고 팀10점을 더한다."""
    working = _prepare_input(state)
    policy = _policy(working)
    missing = _strings(working.get("team_missing_items", []), "team_missing_items")[:]
    conflicts = _strings(working.get("team_conflicts", []), "team_conflicts")[:]
    if team_judgment is not None:
        required = {"team_rating", "judgment_notes", "commitment_note", "missing_items", "conflicts"}
        if not isinstance(team_judgment, dict) or set(team_judgment) != required:
            raise ValueError(f"팀 판단 필드는 {sorted(required)}이어야 합니다.")
        working["team_rating"] = deepcopy(team_judgment["team_rating"])
        working["judgment_notes"] = _strings(team_judgment["judgment_notes"], "judgment_notes")
        working["commitment_note"] = team_judgment["commitment_note"]
        missing.extend(_strings(team_judgment["missing_items"], "팀 missing_items"))
        conflicts.extend(_strings(team_judgment["conflicts"], "팀 conflicts"))
    working.setdefault("team_rating", working["current_candidate"].get("team_rating"))
    notes = _strings(working.get("judgment_notes", []), "judgment_notes")
    commitment = working.get("commitment_note", "장기 몰입 의지는 공개자료만으로 확정할 수 없음; 인터뷰 필요")
    if not isinstance(commitment, str) or not commitment.strip():
        raise ValueError("commitment_note는 비어 있지 않은 문자열이어야 합니다.")
    scorecard, risks, refs = {}, [], []
    for field, key in ANALYSIS_KEYS.items():
        analysis = working.get(field)
        if analysis is None:
            scorecard[key] = {"score": None, "max_score": 30, "source_score": None,
                              "source_max_score": UPSTREAM_MAX_SCORE,
                              "reason": "분석 미수신", "evidence_ids": []}
            missing.append(f"{LABELS[key]}: 에이전트의100점 평가 결과 필요")
            continue
        reason = analysis.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{field}.reason이 필요합니다.")
        source_score = _score(analysis["score"], UPSTREAM_MAX_SCORE, field)
        score = None if source_score is None else round(source_score * WEIGHTS[key] / UPSTREAM_MAX_SCORE, 2)
        usable = _usable_refs(analysis.get("evidence_ids", []), key, working, missing)
        if score is None or not usable:
            missing.append(f"{LABELS[key]}: 점수/근거 보완 필요")
        # 부족한 근거는 재검토 항목으로 남기고 원점수는 보존한다.
        if score is not None and score > 12 and usable and all(working["evidence_registry"][r]["source_type"] == "company" for r in usable):
            missing.append(f"{LABELS[key]}: 높은 점수의 고객·공식·독립 근거 또는 담당자의 점수 재검토 필요")
        scorecard[key] = {"score": score, "max_score": 30,
                          "source_score": source_score, "source_max_score": UPSTREAM_MAX_SCORE,
                          "reason": reason,
                          "evidence_ids": usable, "submitted_evidence_ids": deepcopy(analysis.get("evidence_ids", []))}
        refs.extend(usable)
        missing.extend(_strings(analysis.get("missing_items", []), f"{field}.missing_items"))
        conflicts.extend(_strings(analysis.get("conflicts", []), f"{field}.conflicts"))
        for risk in analysis.get("critical_risks", []):
            if not isinstance(risk, dict) or not isinstance(risk.get("description"), str) or not risk["description"].strip() or type(risk.get("resolved")) is not bool:
                raise ValueError("중대한 위험에는 description·evidence_ids·resolved(bool)가 필요합니다.")
            risk_refs = _usable_refs(risk.get("evidence_ids", []), "risk", working, missing)
            if not risk_refs:
                missing.append(f"위험 근거 확인 필요: {risk['description']}")
            risks.append({**deepcopy(risk), "evidence_ids": risk_refs, "evidence_verified": bool(risk_refs)})
            refs.extend(risk_refs)
    team = _team_rating(working["team_rating"], working, missing)
    scorecard["team"] = team
    refs.extend(team["evidence_ids"])
    market_definition = (working.get("market_analysis") or {}).get("market_definition")
    locked = working.get("market_definition")
    if not isinstance(market_definition, str) or not market_definition.strip():
        missing.append("시장 정의: 지역·고객·업무·제품 확인 필요")
    elif locked is not None and locked != market_definition:
        conflicts.append("시장 정의: 기존 평가 중 정의와 현재 분석의 정의가 상충함")
    else:
        locked = market_definition
    eligibility, eligibility_refs = _eligibility(working, missing)
    refs = _unique(refs + eligibility_refs)
    missing, conflicts = _unique(missing), _unique(conflicts)
    upstream_scores = [scorecard[key]["score"] for key in ("technology", "market", "competitive_advantage")]
    upstream_score_90 = None if any(x is None for x in upstream_scores) else round(sum(upstream_scores), 2)
    judge_score_10 = scorecard["team"]["score"]
    total = None if upstream_score_90 is None or judge_score_10 is None else round(upstream_score_90 + judge_score_10, 2)
    critical = [r for r in risks if not r["resolved"]]
    failures = [f"{LABELS[k]} {scorecard[k]['score']:g}/{WEIGHTS[k]}: 최소 {floor:g}점 미달"
                for k, floor in policy["minimum_scores"].items()
                if scorecard[k]["score"] is not None and scorecard[k]["score"] < floor]
    if total is not None and total < policy["recommend_min_score"]:
        failures.insert(0, f"총점 {total:g}/100: 추천 기준 {policy['recommend_min_score']:g}점 미달")
    if eligibility == "excluded":
        decision, category = "hold", "평가대상 외"
    elif critical:
        decision, category = "hold", "중대한 위험 미해결"
    elif total is None:
        decision, category = "hold", "점수 산정 불가"
    elif conflicts:
        decision, category = "hold", "자료 상충"
    elif failures:
        decision, category = "hold", "추천 점수 기준 미충족"
    else:
        decision, category = "invest", "추천 조건 충족"
    reasons = [category, *failures, *missing, *conflicts, *[r["description"] for r in critical]]
    if eligibility == "excluded":
        reasons.append("비상장·Seed~Series C·미Exit 조건 미충족")
    if decision == "invest":
        reasons.append("총점·영역별 최소점수 조건 충족")
    mock = any(working["evidence_registry"][r].get("is_mock", False) for r in refs)
    confidence = "낮음" if mock or total is None or missing or conflicts else "보통" if any(
        entry["evidence_ids"] and all(working["evidence_registry"][r]["source_type"] == "company" for r in entry["evidence_ids"])
        for entry in scorecard.values()) else "높음"
    evaluation = {
        "candidate_id": working["current_candidate"]["id"], "as_of_date": working["as_of_date"],
        "decision": decision, "decision_category": category, "reason": "; ".join(reasons),
        "score": total, "upstream_score_90": upstream_score_90,
        "judge_score_10": judge_score_10, "scorecard": deepcopy(scorecard), "confidence": confidence,
        "evidence_ids": refs, "missing_items": missing, "conflicts": conflicts, "critical_risks": risks,
        "eligibility_status": eligibility, "is_mock": mock, "retry_count": working["retry_count"],
        "judgment_notes": notes, "commitment_note": commitment,
        "analysis": {**{k: deepcopy(working.get(k)) for k in ANALYSIS_KEYS},
                     "team_info": deepcopy(working["current_candidate"].get("team_info", {})),
                     "team_rating": deepcopy(team), "market_definition": locked},
        "evaluation_policy": deepcopy(policy), "team_research_log": deepcopy(working["team_research_log"]),
    }
    requests = _research_requests(working["current_candidate"]["name"], missing + conflicts) if eligibility != "excluded" else []
    payload = build_handoff_payload(working, evaluation, requests, policy)
    return {
        "current_evaluation": evaluation, "scorecard": deepcopy(scorecard), "total_score": total,
        "upstream_score_90": upstream_score_90, "judge_score_10": judge_score_10,
        "decision": decision, "decision_reasons": reasons + notes,
        "team_rating": deepcopy(team), "commitment_note": commitment,
        "missing_items": missing, "conflicts": conflicts, "market_definition": locked,
        "evidence_registry": working["evidence_registry"], "used_reference_ids": refs,
        "needs_research": bool(requests), "research_requests": requests,
        "retry_available": bool(requests) and working["retry_count"] < MAX_RESEARCH,
        "retry_count": working["retry_count"], "team_research_log": deepcopy(working["team_research_log"]),
        "current_candidate": deepcopy(working["current_candidate"]),
        "updated_candidate": deepcopy(working["current_candidate"]),
        "next_action": "report" if decision == "invest" else "hold",
        "report_payload": payload if decision == "invest" else None,
        "hold_payload": payload if decision == "hold" else None,
    }


def investment_judge_node(state: InvestmentJudgeState) -> dict[str, Any]:
    return judge_investment(state)


def route_after_investment(state: dict[str, Any]) -> Literal["report", "hold"]:
    decision = state.get("decision", (state.get("current_evaluation") or {}).get("decision"))
    if decision not in ("invest", "hold"):
        raise ValueError("투자 판단의 decision(invest/hold)이 필요합니다.")
    return "report" if decision == "invest" else "hold"
