"""대상 기업과 전달받은 경쟁사의 비교 흐름입니다."""

from __future__ import annotations

import json
from typing import Any, Callable
from main.rag.company import BaseRAG
from main.agents.common.evidence import (
    Evidence,
    normalize_evidence,
    search_company_evidence,
    data_label,
)
from main.agents.common.llm import resolve_chat_model
from .schemas import CompetitorComparison
from .prompts import SYSTEM_PROMPT
from .scoring import validate_citations, _build_scorecard


def _company_from_state(state: dict[str, Any]) -> str:
    primary = state.get("company")
    alias = state.get("company_name")
    for value in (primary, alias):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("state company/company_name must be a nonempty string")
    if primary and alias and primary.strip().casefold() != alias.strip().casefold():
        raise ValueError("state company and company_name disagree")
    company = primary or alias
    if not company:
        raise ValueError("state needs company or company_name")
    return company.strip()


def _normalize_competitors(company: str, names: Any) -> list[str]:
    if not isinstance(names, list):
        raise ValueError("competitors must be a list of company names")
    normalized: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("competitor names must be nonempty strings")
        value = name.strip()
        key = value.casefold()
        if key == company.casefold():
            raise ValueError("A company cannot be its own competitor")
        if key in seen:
            raise ValueError(f"Duplicate competitor: {value}")
        normalized.append(value)
        seen.add(key)
    if not normalized:
        raise ValueError("At least one competitor is required")
    return normalized


def _collect_evidence(companies, search_company, evidence_adapter) -> list[Evidence]:
    records = []
    ids = set()
    for company in companies:
        raw = search_company(company)
        supplied = evidence_adapter(raw, company) if evidence_adapter else raw
        for record in normalize_evidence(supplied, company, allow_empty=True):
            key = record.id.strip().casefold()
            if key in ids:
                raise ValueError(f"Evidence ID occurs more than once: {record.id}")
            ids.add(key)
            records.append(record)
    return records


def run_agent(
    state: dict[str, Any],
    rag: BaseRAG,
    competitors: list[str] | None = None,
    model: str | Any = "openai:gpt-4.1",
    *,
    search_company: Callable[[str], Any] | None = None,
    evidence_adapter: Callable[[Any, str], Any] | None = None,
) -> dict[str, Any]:
    """조회·모델 호출 후 검증된 비교 결과를 반환합니다. 경쟁사는 호출자가 지정합니다."""
    if not isinstance(state, dict):
        raise TypeError("state must be a dict")
    company = _company_from_state(state)
    names = _normalize_competitors(
        company,
        competitors if competitors is not None else state.get("competitors"),
    )
    if search_company is None:
        search_company = lambda name: search_company_evidence(rag, name)
    if not callable(search_company):
        raise ValueError("search_company must be callable")
    if evidence_adapter is not None and not callable(evidence_adapter):
        raise TypeError("evidence_adapter must be callable")
    records = _collect_evidence([company, *names], search_company, evidence_adapter)
    if not any(
        record.company.strip().casefold() == company.casefold() for record in records
    ):
        raise ValueError(f"No BaseRAG evidence found for target company: {company}")

    llm = resolve_chat_model(model).with_structured_output(CompetitorComparison)
    payload = {
        "target_company": company,
        "competitors": names,
        "evidence": [
            record.model_dump(mode="json", exclude_none=True) for record in records
        ],
    }
    response = llm.invoke(
        [
            ("system", SYSTEM_PROMPT),
            ("human", json.dumps(payload, ensure_ascii=False)),
        ]
    )
    report = CompetitorComparison.model_validate(response)
    sources = validate_citations(report, records, company, names)
    scorecard = _build_scorecard(report)
    result = report.model_dump(mode="json")
    result["criterion_scores"] = scorecard["criteria"]
    result["sources"] = sources
    result["data_label"] = data_label(sources)
    state.update({"competitor_comparison": result, "competitor_score": scorecard})
    return state
