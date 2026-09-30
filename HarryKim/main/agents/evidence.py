"""Adapt an exact BaseRAG company record to the agents' citation format."""

from __future__ import annotations

from main.baseRAG import BaseRAG, CompanyRecord


def get_selected_company(rag: BaseRAG, company_id: str) -> CompanyRecord:
    if not isinstance(company_id, str) or not company_id.strip():
        raise ValueError("state.company_id must be a nonempty string")
    record = rag.get_company(company_id.strip())
    if record is None:
        raise ValueError(f"Unknown company_id: {company_id}")
    return record


def as_evidence(record: CompanyRecord) -> dict[str, object]:
    """One CSV row is one source; its provenance remains unverified."""
    if not record.content.strip():
        raise ValueError(f"Company {record.company_id} has no searchable CSV content")
    return {
        "id": f"CSV-{record.company_id}",
        "company": record.company_name,
        "title": "기업 정보 CSV의 한 행",
        "locator": f"{record.source.csv_path}#record={record.source.record_number}",
        "text": record.content,
        "stage": "unknown",
        "is_mock": None,
    }
