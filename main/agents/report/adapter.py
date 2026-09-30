"""투자 판단 State/handoff를 보고서 입력으로 옮긴다. 재평가·검색은 하지 않는다.

판단 에이전트의 점수와 판정을 그대로 사용한다. 축약된 ``sources``를 검증된
근거 원장으로 승격하지 않으며, 전체 handoff는 source_payload에 보존한다.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from pydantic import BaseModel


_SOURCE_KEYS = (
    "technology_summary", "technical_score", "market_evaluation",
    "competitor_comparison", "competitor_score",
)
_PDF_PAGE_NOTE = "page=발췌/제공 PDF 파일 기준; 원 간행물 인쇄 페이지와 다를 수 있음"
_DIMENSIONS = {
    "technology": ("technical", "technology_analysis", "기술"),
    "market": ("market", "market_analysis", "시장"),
    "competitive_advantage": ("competition", "competition_analysis", "경쟁"),
    "team": ("team", "team_rating", "팀"),
}


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _plain(value.model_dump())
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return deepcopy(value)


def _dict(value: Any, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name}는 dict여야 합니다.")
    return value


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("사유와 근거 ID는 문자열 목록이어야 합니다.")
    return list(dict.fromkeys(item for item in value if item.strip()))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _join(items: list[str]) -> str:
    return " / ".join(dict.fromkeys(item for item in items if item))


def _risk_text(risk: Any) -> str:
    if isinstance(risk, str):
        return risk
    if not isinstance(risk, dict):
        raise ValueError("위험은 문자열 또는 dict여야 합니다.")
    parts = [_text(risk.get("description"))]
    for field, label in (("caveat", "한계"), ("adoption_impact", "도입 영향"),
                         ("scaling_impact", "확장 영향")):
        if _text(risk.get(field)):
            parts.append(f"{label}: {risk[field]}")
    if "resolved" in risk:
        parts.append("해결됨" if risk["resolved"] else "미해결")
    if "evidence_verified" in risk:
        parts.append("근거 확인" if risk["evidence_verified"] else "근거 미확인")
    return _join(parts)


def _source_identity(sources: dict, company_id: str, company_name: str) -> None:
    for field, key, expected in (
        ("technology_summary", "company", company_name),
        ("competitor_comparison", "company", company_name),
        ("market_evaluation", "company_id", company_id),
        ("market_evaluation", "company_name", company_name),
    ):
        result = _dict(sources.get(field), field)
        if key in result and result[key] != expected:
            raise ValueError(f"{field}.{key}와 보고서 기업이 다릅니다.")


def _select_payload(source: dict) -> dict:
    if "evaluation" in source and "company" in source:
        return source
    current = _dict(source.get("current_evaluation"), "current_evaluation")
    decision = source.get("decision") or current.get("decision")
    report = source.get("report_payload")
    hold = source.get("hold_payload")
    if report is not None and hold is not None:
        raise ValueError("report_payload와 hold_payload가 동시에 있습니다. 현재 판정의 payload만 전달하세요.")
    payload = report if report is not None else hold
    if payload is not None:
        payload = _dict(payload, "handoff payload")
        actual = _dict(payload.get("evaluation"), "evaluation").get("decision")
        expected = "invest" if report is not None else "hold"
        if actual != expected or decision is not None and decision != actual:
            raise ValueError("decision과 report_payload/hold_payload가 일치하지 않습니다.")
        if current and current != payload.get("evaluation"):
            raise ValueError("current_evaluation과 handoff evaluation이 다릅니다. 최신 결과를 전달하세요.")
        return payload
    if not current:
        raise ValueError("완료된 투자 판단의 report_payload, hold_payload 또는 current_evaluation이 필요합니다.")
    company = _dict(source.get("current_candidate") or source.get("updated_candidate"), "current_candidate")
    if decision != current.get("decision"):
        raise ValueError("decision과 current_evaluation.decision이 다릅니다.")
    return {
        "company_id": source.get("company_id") or company.get("id"),
        "company": company,
        "company_data": source.get("company_data") or company.get("base_rag"),
        "evaluation": current,
        "evidence_registry": source.get("evidence_registry", {}),
        "research_requests": source.get("research_requests", []),
        "evaluation_policy": current.get("evaluation_policy", source.get("decision_policy", {})),
        "source_outputs": {key: source.get(key) for key in _SOURCE_KEYS},
    }


def _validate_identity(source: dict, payload: dict) -> tuple[str, str, dict | None]:
    company = _dict(payload.get("company"), "company")
    evaluation = _dict(payload.get("evaluation"), "evaluation")
    company_id, name = payload.get("company_id"), company.get("name")
    if not isinstance(company_id, str) or not company_id.strip():
        raise ValueError("handoff company_id는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("handoff company.name이 필요합니다.")
    for label, value in (("company.id", company.get("id")),
                         ("evaluation.candidate_id", evaluation.get("candidate_id"))):
        if value != company_id:
            raise ValueError(f"기업 ID 불일치: {label}")
    for label, value in (("State.company_id", source.get("company_id")),
                         ("current_candidate.id", _dict(source.get("current_candidate"), "current_candidate").get("id")),
                         ("updated_candidate.id", _dict(source.get("updated_candidate"), "updated_candidate").get("id"))):
        if value is not None and value != company_id:
            raise ValueError(f"기업 ID 불일치: {label}")
    for key in ("current_candidate", "updated_candidate"):
        candidate = _dict(source.get(key), key)
        if candidate.get("name", name) != name:
            raise ValueError(f"기업명 불일치: {key}.name")
    record = payload.get("company_data") or company.get("base_rag")
    for label, item in (("company_data", record), ("company.base_rag", company.get("base_rag")),
                        ("State.company_data", source.get("company_data"))):
        data = _dict(item, label)
        if data.get("company_id", company_id) != company_id:
            raise ValueError(f"기업 ID 불일치: {label}")
        if data.get("company_name", name) != name:
            raise ValueError(f"기업명 불일치: {label}")
    _source_identity(_dict(payload.get("source_outputs"), "source_outputs"), company_id, name)
    _source_identity(source, company_id, name)
    return company_id, name, record


def _registry_evidence(registry: Any) -> list[dict]:
    output = []
    for key, value in _dict(registry, "evidence_registry").items():
        item = _dict(value, "Evidence")
        if item.get("id") != key:
            raise ValueError("evidence_registry 키와 근거 ID가 다릅니다.")
        output.append({
            "evidence_id": key, "title": item.get("title") or "제목 미확인",
            # source_type은 company/customer/official/independent이며 문서 형식이 아니다.
            "kind": "web", "issuer": item.get("publisher") or "",
            "url": item.get("url") or "", "published_at": item.get("published_at") or "",
            "doc_id": item.get("document_id") or "", "chunk_id": item.get("chunk_id") or "",
            "company_id": item.get("company_id") or "", "page": item.get("page"),
            "data_year": item.get("data_year"), "collected_at": item.get("collected_at") or "",
            "quote": item.get("excerpt") or "", "is_mock": item.get("is_mock"),
            "content_kind": "source_excerpt",
            "provenance_note": "투자 판단 근거 원장 제공; 보고서 단계에서 원문 진위 재검증 안 함",
        })
    return output


def _supplement_bibliography(evidence: list[dict], sources: dict, company_id: str, company_name: str) -> list[dict]:
    """실제 인용 ID의 서지정보를 보존하며 투자 판단 원장에는 쓰지 않는다."""
    catalog = {item["evidence_id"]: deepcopy(item) for item in evidence}

    def add(item: dict) -> None:
        eid = item["evidence_id"]
        if not isinstance(eid, str) or not eid.strip():
            raise ValueError("상위 분석 출처에 비어 있지 않은 ID가 필요합니다.")
        previous = catalog.get(eid)
        if previous is None:
            catalog[eid] = item
            return
        # 서로 다른 자료가 같은 ID를 쓸 때 앞의 자료로 덮어쓰지 않는다.
        for key in ("title", "url", "csv_path", "record_number", "company_id", "quote", "summary"):
            before, after = previous.get(key), item.get(key)
            if before not in (None, "") and after not in (None, "") and before != after:
                raise ValueError(f"동일 근거 ID의 서지정보가 다릅니다: {eid} ({key})")
        if (previous.get("is_mock") is not None and item.get("is_mock") is not None
                and previous["is_mock"] != item["is_mock"]):
            raise ValueError(f"동일 근거 ID의 자료 유형이 다릅니다: {eid}")
        kinds = {previous.get("content_kind"), item.get("content_kind")}
        if {"source_excerpt", "generated_summary"}.issubset(kinds):
            raise ValueError(f"동일 근거 ID의 원문/생성 요약 구분이 다릅니다: {eid}")
        for key, value in item.items():
            if previous.get(key) in (None, ""):
                previous[key] = value
        if previous.get("content_kind") in (None, "metadata_only", "unspecified"):
            previous.update(content_kind=item.get("content_kind", "metadata_only"),
                            provenance_note=item.get("provenance_note", ""))
        if item.get("kind") in ("csv", "report", "paper"):
            previous["kind"] = item["kind"]
        if item.get("kind") == "report" and _PDF_PAGE_NOTE in item.get("provenance_note", ""):
            if _PDF_PAGE_NOTE not in previous.get("provenance_note", ""):
                previous["provenance_note"] = previous.get("provenance_note", "") + "; " + _PDF_PAGE_NOTE

    for key in ("technology_summary", "competitor_comparison"):
        result = _dict(sources.get(key), key)
        for raw in result.get("sources", []):
            item = _dict(raw, f"{key}.sources")
            locator = _text(item.get("locator"))
            csv_location = re.fullmatch(r"(.+\.csv)#record=(\d+)", locator, flags=re.IGNORECASE)
            owner = company_id if item.get("company", company_name) == company_name else ""
            eid = item.get("id")
            if isinstance(eid, str) and eid.startswith("CSV-") and csv_location:
                owner = eid[4:]
            converted = {
                "evidence_id": eid, "title": item.get("title") or "제목 미확인",
                "kind": "csv" if csv_location else "web", "issuer": "",
                "url": locator if locator.startswith(("https://", "http://")) else "",
                "locator": locator, "published_at": item.get("date") or "",
                "company_id": owner, "is_mock": item.get("is_mock"),
                "content_kind": "metadata_only", "quote": "",
                "provenance_note": "상위 분석의 서지정보만 제공; 원문 미전달·진위 미검증",
            }
            if csv_location:
                converted.update(csv_path=csv_location[1], record_number=int(csv_location[2]))
            add(converted)

    market = _dict(sources.get("market_evaluation"), "market_evaluation")
    for raw in market.get("evidence", []):
        item = _dict(raw, "market_evaluation.evidence")
        content_kind = item.get("content_kind", "source_excerpt")
        if content_kind not in {"source_excerpt", "generated_summary"}:
            raise ValueError("시장 출처의 content_kind는 source_excerpt 또는 generated_summary여야 합니다.")
        if item.get("source_type") not in {"pdf", "web"}:
            raise ValueError("시장 출처의 source_type은 pdf 또는 web이어야 합니다.")
        generated = content_kind == "generated_summary"
        add({
            "evidence_id": item.get("source_id"), "title": item.get("title") or "제목 미확인",
            "kind": "report" if item["source_type"] == "pdf" else "web",
            "issuer": item.get("publisher") or "", "url": item.get("url") or "",
            "published_at": item.get("published_at") or "", "page": item.get("page"),
            "content_kind": content_kind, "quote": "" if generated else item.get("excerpt", ""),
            "summary": item.get("excerpt", "") if generated else "",
            "is_mock": item.get("is_mock"),
            "provenance_note": ("시장 에이전트 생성 요약; 원문 인용·진위 검증 아님" if generated else
                                "시장 에이전트 제공 원문 발췌; 보고서 단계에서 원문 진위 재검증 안 함")
                               + ("; " + _PDF_PAGE_NOTE if item["source_type"] == "pdf" else ""),
        })
    return list(catalog.values())


def _analysis_result(dimension: str, analysis: dict, card: dict, sources: dict) -> dict | None:
    raw_key = {"technical": "technology_summary", "market": "market_evaluation",
               "competition": "competitor_comparison"}[dimension]
    raw = _dict(sources.get(raw_key), raw_key)
    if not analysis and not raw and card.get("source_score") is None:
        return None
    paragraphs = [_text(analysis.get("summary")), _text(analysis.get("reason"))]
    ids = _strings(analysis.get("evidence_ids"))
    missing = _strings(analysis.get("missing_items")) + _strings(raw.get("key_unknowns"))
    risks = [_risk_text(item) for item in analysis.get("critical_risks", [])]
    if dimension == "technical":
        paragraphs.append(_text(raw.get("technology_summary")))
        ids += _strings(raw.get("summary_evidence_ids"))
        problem = _dict(raw.get("problem_solution"), "problem_solution")
        for field, label in (("problem", "고객 문제"), ("product_approach", "제품 접근"),
                             ("observed_outcome", "관측 결과"), ("caveat", "문제 해결 한계")):
            if _text(problem.get(field)):
                paragraphs.append(f"{label}: {problem[field]}")
        ids += _strings(problem.get("evidence_ids"))
        ai = _dict(raw.get("ai_role"), "ai_role")
        if _text(ai.get("role")):
            paragraphs.append("AI 역할: " + ai["role"] + ("; " + ai["caveat"] if _text(ai.get("caveat")) else ""))
        ids += _strings(ai.get("evidence_ids"))
        for item in raw.get("performance_validations", []):
            paragraphs.append("성능: " + _join([_text(item.get(key)) for key in ("metric", "result", "test_conditions", "caveat")]))
            ids += _strings(item.get("evidence_ids"))
    elif dimension == "market":
        definition = analysis.get("market_definition") or raw.get("market_definition")
        if _text(definition):
            paragraphs.append("시장 정의: " + definition)
        risks += _strings(raw.get("market_risks"))
        for criterion in raw.get("criteria", []):
            ids += _strings(criterion.get("source_ids"))
    else:
        for item in raw.get("comparisons", []):
            paragraphs.append("경쟁사 " + _text(item.get("competitor")) + ": " + _join([
                _text(item.get(key)) for key in ("differentiation", "comparison_conditions", "caveat")]))
            ids += _strings(item.get("target_evidence_ids")) + _strings(item.get("competitor_evidence_ids"))
        defensibility = _dict(raw.get("defensibility"), "defensibility")
        if _text(defensibility.get("statement")):
            paragraphs.append("방어력: " + defensibility["statement"])
        ids += _strings(defensibility.get("evidence_ids"))
        risks += [_risk_text(item) for item in raw.get("risks", [])]
    source_score = card.get("source_score", analysis.get("score"))
    return {
        "status": "completed" if source_score is not None else "insufficient",
        "summary": _join(paragraphs), "scores": {dimension: source_score},
        "evidence_ids": list(dict.fromkeys(ids)),
        "score_evidence_ids": {dimension: _strings(card.get("evidence_ids"))},
        "risks": list(dict.fromkeys(risks)), "missing_fields": list(dict.fromkeys(missing)),
        "conflicts": _strings(analysis.get("conflicts")),
    }


def _team_result(evaluation: dict, analysis: dict, card: dict) -> dict:
    info = _dict(analysis.get("team_info"), "team_info")
    paragraphs = [_text(card.get("reason"))]
    founders = info.get("founders", [])
    if founders:
        names = [item if isinstance(item, str) else _text(_dict(item, "founder").get("name")) for item in founders]
        paragraphs.append("창업자: " + ", ".join(name for name in names if name))
    for key, label in (("expertise", "전문성"), ("roles", "역할 구성")):
        value = info.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        if _text(value):
            paragraphs.append(f"{label}: {value}")
    if _text(evaluation.get("commitment_note")):
        paragraphs.append(evaluation["commitment_note"])
    scores = {"team": card.get("score")}
    score_ids = {"team": _strings(card.get("evidence_ids"))}
    for key, value in _dict(card.get("criteria"), "team.criteria").items():
        item = _dict(value, f"team.criteria.{key}")
        scores[key] = item.get("score")
        score_ids[key] = _strings(item.get("evidence_ids"))
        if _text(item.get("reason")):
            paragraphs.append(f"{key}: {item['reason']}")
    return {
        "status": "completed" if card.get("score") is not None else "insufficient",
        "summary": _join(paragraphs), "scores": scores,
        "evidence_ids": list(dict.fromkeys(_strings(card.get("evidence_ids")) + _strings(info.get("evidence_ids")))),
        "score_evidence_ids": score_ids,
        "missing_fields": [value for value in _strings(evaluation.get("missing_items")) if value.startswith("창업자·팀")],
    }


def adapt_project_state(state: dict) -> dict:
    """정규화 입력, 직접 handoff, 전체 InvestmentState를 부작용 없이 수용한다.

    이미 ``results_by_company``가 있으면 빈 dict까지 존중한다. 프로젝트 입력은
    현재 판정과 기업 ID를 교차 확인한 뒤 변환하며, 미등록 근거를 만들지 않는다.
    """
    source = _plain(state)
    if not isinstance(source, dict):
        raise ValueError("보고서 입력은 State 또는 handoff dict여야 합니다.")
    project_keys = {"evaluation", "report_payload", "hold_payload", "current_evaluation",
                    "current_candidate", *_SOURCE_KEYS}
    if "results_by_company" in source or not project_keys.intersection(source):
        return source
    payload = _select_payload(source)
    company_id, name, record = _validate_identity(source, payload)
    evaluation = _dict(payload.get("evaluation"), "evaluation")
    decision = evaluation.get("decision")
    if decision not in {"invest", "hold"}:
        raise ValueError("투자 판단 decision은 invest 또는 hold여야 합니다.")
    if "score" not in evaluation or not isinstance(evaluation.get("scorecard"), dict):
        raise ValueError("evaluation.score와 evaluation.scorecard가 필요합니다.")
    cards = evaluation["scorecard"]
    analyses = _dict(evaluation.get("analysis"), "evaluation.analysis")
    sources = _dict(payload.get("source_outputs"), "source_outputs")
    evidence = _supplement_bibliography(
        _registry_evidence(payload.get("evidence_registry")), sources, company_id, name)
    policy = _dict(payload.get("evaluation_policy") or evaluation.get("evaluation_policy"), "evaluation_policy")
    reasons = [_text(evaluation.get("reason")) or _text(evaluation.get("decision_category"))]
    reasons += _strings(evaluation.get("judgment_notes"))
    if _text(evaluation.get("confidence")):
        reasons.append("전달된 판단 신뢰도: " + evaluation["confidence"])
    policy_parts = []
    if "recommend_min_score" in policy:
        policy_parts.append(f"총점 {policy['recommend_min_score']}/100 이상")
    for key, value in _dict(policy.get("minimum_scores"), "minimum_scores").items():
        label = _DIMENSIONS.get(key, (key, key, key))[2]
        policy_parts.append(f"{label} 최소 {value}점")
    if policy_parts:
        reasons.append("전달된 추천 기준: " + ", ".join(policy_parts))
    weighted, score_ids = {}, {}
    company = {"company_id": company_id, "company_name": name, "company_record": record,
               "is_mock": evaluation.get("is_mock", False)}
    for card_key, (dimension, analysis_key, _label) in _DIMENSIONS.items():
        card = _dict(cards.get(card_key), f"scorecard.{card_key}")
        weighted[dimension] = card.get("score")
        score_ids[dimension] = _strings(card.get("evidence_ids"))
        company[f"{dimension}_result"] = (
            _team_result(evaluation, analyses, card) if dimension == "team" else
            _analysis_result(dimension, _dict(analyses.get(analysis_key), analysis_key), card, sources)
        )
    refs = _strings(evaluation.get("evidence_ids"))
    score_ids["final_score"] = refs
    company["investment_result"] = {
        "status": "completed" if evaluation["score"] is not None else "insufficient",
        "decision": {"invest": "추천", "hold": "보류"}[decision],
        "final_score": evaluation["score"], "weighted_scores": weighted,
        "reasons": [item for item in reasons if item], "evidence_ids": refs,
        "score_evidence_ids": score_ids,
        "risks": [_risk_text(item) for item in evaluation.get("critical_risks", [])],
        "missing_fields": _strings(evaluation.get("missing_items")),
        "conflicts": _strings(evaluation.get("conflicts")),
    }
    eligibility = evaluation.get("eligibility_status", "unverified")
    if eligibility not in {"eligible", "excluded", "unverified"}:
        raise ValueError("알 수 없는 eligibility_status입니다.")
    info = _dict(payload["company"].get("eligibility"), "company.eligibility")
    company["eligibility"] = {
        "status": {"eligible": "eligible", "excluded": "ineligible", "unverified": "unknown"}[eligibility],
        "reason": "투자 판단 에이전트가 전달한 적격성 결과: " + eligibility,
        "evidence_ids": _strings(info.get("evidence_ids")),
    }
    config = _dict(source.get("config"), "config").copy()
    config.update(as_of=evaluation.get("as_of_date") or "미확인",
                  is_mock=bool(config.get("is_mock") or evaluation.get("is_mock")))
    return {"config": config, "candidate_company_ids": [company_id],
            "results_by_company": {company_id: company}, "evidence": evidence,
            "source_payload": deepcopy(payload)}
