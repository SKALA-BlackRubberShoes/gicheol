"""대상 기업과 전달받은 경쟁사의 비교 흐름입니다."""

from __future__ import annotations

import json
from typing import Any, Callable
from main.rag.company import BaseRAG
from main.agents.common.evidence import (
    Evidence,
    normalize_evidence,
    data_label,
)
from main.agents.common.llm import resolve_chat_model
from main.agents.common.pdf_evidence import collect_company_evidence
from main.agents.common.rag_judgment import validate_rag_judgment, merge_rag_judgment, complete_sources
from main.agents.common.csv_judgment import (
    COMPETITION_CRITERIA,
    find_unique_company_record,
    merge_csv_judgment,
    validate_csv_judgment,
)
from .schemas import CompetitorComparison
from .prompts import SYSTEM_PROMPT
from .scoring import (
    CONDITION_DIMENSIONS,
    ConditionChecksError,
    validate_citations,
    _build_scorecard,
)


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
    pdf_rag: Any = None,
) -> dict[str, Any]:
    """조회·모델 호출 후 검증된 비교 결과를 반환합니다. 경쟁사는 호출자가 지정합니다."""
    if not isinstance(state, dict):
        raise TypeError("state must be a dict")
    company = _company_from_state(state)
    names = _normalize_competitors(
        company,
        competitors if competitors is not None else state.get("competitors"),
    )
    retrieval = []
    if search_company is None:
        def search_company(name):
            evidence, trace = collect_company_evidence(rag, name, "competition", pdf_rag=pdf_rag)
            retrieval.append(trace)
            return evidence
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
    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", json.dumps(payload, ensure_ascii=False)),
    ]
    for attempt in range(2):
        response = llm.invoke(messages)
        report = CompetitorComparison.model_validate(response)
        try:
            sources = validate_citations(report, records, company, names)
        except ConditionChecksError as exc:
            if attempt:
                raise ConditionChecksError(exc.issues, retry_exhausted=True) from exc
            # Keep the original evidence and revalidate the complete new response.
            messages = [
                *messages,
                ("assistant", report.model_dump_json()),
                ("human", (
                    "이전 응답의 condition_checks에 누락 또는 중복이 있습니다. "
                    "아래 JSON은 검증 진단 데이터입니다. 최초 입력의 기업과 근거를 그대로 "
                    "사용해 각 경쟁사의 여섯 항목을 각각 정확히 한 번 작성하세요. "
                    "자료가 부족한 항목도 생략하지 말고 status=unverified와 이유를 적으세요. "
                    "하나라도 different 또는 unverified이면 like_for_like=false, "
                    "verdict=insufficient로 두고, 직접 비교 점수에도 기존 근거 조건을 "
                    "적용하세요. 다른 기업·출처를 만들지 말고 전체 구조화 응답을 다시 반환하세요.\n"
                    + json.dumps({
                        "required_dimensions": CONDITION_DIMENSIONS,
                        "issues": exc.issues,
                    }, ensure_ascii=False)
                )),
            ]
        else:
            break
    scorecard = _build_scorecard(report)
    strict_score = scorecard
    csv_matches = [find_unique_company_record(rag, name) for name in [company, *names]]
    csv_records = [record for record in csv_matches if record is not None]
    evidence_ids = {item.id for item in records}
    if len(csv_records) == len(names) + 1 and all(
        f"CSV-{item.company_id}" in evidence_ids for item in csv_records
    ):
        scorecard = merge_csv_judgment(
            scorecard,
            validate_csv_judgment(
                report.csv_assessment_scores, csv_records, records,
                COMPETITION_CRITERIA,
            ),
        )
    rag_scores = validate_rag_judgment(
        report.rag_assessment_scores, records, company, COMPETITION_CRITERIA, strict_score,
    )
    scorecard = merge_rag_judgment(strict_score, scorecard, rag_scores)
    sources = complete_sources(sources, records, scorecard)
    result = report.model_dump(mode="json")
    result["rag_assessment_scores"] = rag_scores
    result["rag_retrieval"] = retrieval
    result["criterion_scores"] = scorecard["criteria"]
    result["sources"] = sources
    result["data_label"] = data_label(sources)
    state.update({"competitor_comparison": result, "competitor_score": scorecard})
    return state
