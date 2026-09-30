"""기업 근거의 공통 형식·CSV 변환·인용 식별자 처리입니다."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from main.rag.company import BaseRAG, CompanyRecord, CompanyFilter


class Evidence(BaseModel):
    """Normalized result supplied by BaseRAG or an evidence_adapter."""

    model_config = ConfigDict(strict=True, extra="ignore")

    id: str = Field(description="Stable ID, unique across all searched companies")
    company: str
    title: str
    locator: str = Field(description="URL, source section, or page number")
    text: str
    date: str | None = None
    stage: Literal["demo", "pilot", "commercial", "unknown"] = "unknown"
    is_mock: bool | None = None

    @model_validator(mode="after")
    def nonempty_fields(self) -> "Evidence":
        for field in ("id", "company", "title", "locator", "text"):
            if not getattr(self, field).strip():
                raise ValueError(f"Evidence.{field} cannot be empty")
        return self


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


def search_company_evidence(rag: BaseRAG, company: str) -> list[dict[str, object]]:
    """회사 이름을 CSV의 정확 조건으로 조회합니다. 의미 검색이나 API 호출은 없습니다."""
    return [
        as_evidence(record)
        for record in rag.list_companies(
            filters=[CompanyFilter("company_name", "eq", company)]
        )
    ]


def normalize_evidence(
    raw: Any,
    company: str,
    *,
    allow_empty: bool = False,
    max_records: int | None = None,
    max_text_chars: int | None = None,
    max_total_chars: int | None = None,
) -> list[Evidence]:
    """공통 필드를 확인합니다. 자료량 제한은 사용하는 에이전트가 지정합니다."""
    if not isinstance(raw, list):
        raise ValueError(
            "Evidence must be a list of dicts; provide evidence_adapter for other formats"
        )
    if not raw and not allow_empty:
        raise ValueError(f"No evidence found for company: {company}")
    if max_records is not None and len(raw) > max_records:
        raise ValueError(f"Evidence exceeds {max_records} records")
    records = []
    seen_ids = set()
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each evidence item must be a dict")
        record = Evidence.model_validate(item)
        if record.company.strip().casefold() != company.casefold():
            raise ValueError(f"Evidence {record.id!r} belongs to a different company")
        key = record.id.strip().casefold()
        if key in seen_ids:
            raise ValueError(f"Duplicate evidence ID: {record.id}")
        seen_ids.add(key)
        if max_text_chars is not None and len(record.text) > max_text_chars:
            raise ValueError(
                f"Evidence {record.id!r} exceeds {max_text_chars} text characters"
            )
        total += len(record.text)
        if max_total_chars is not None and total > max_total_chars:
            raise ValueError(
                f"Combined evidence exceeds {max_total_chars} text characters"
            )
        records.append(record)
    return records


def validate_evidence_ids(
    ids: list[str],
    by_id: dict[str, Evidence],
    label: str,
    required: bool,
    *,
    owner: str | None = None,
    unique: bool = False,
) -> list[Evidence]:
    """알려진 출처와 회사 소유권을 확인합니다. 인용 내용의 사실성까지 검증하지는 않습니다."""
    if required and not ids:
        raise ValueError(f"{label} needs at least one evidence ID")
    if unique and len(ids) != len(set(ids)):
        raise ValueError(f"{label} repeats an evidence ID")
    records = []
    for evidence_id in ids:
        record = by_id.get(evidence_id)
        if (
            record is None
            or owner is not None
            and record.company.strip().casefold() != owner.casefold()
        ):
            raise ValueError(
                f"{label} cites unknown or wrong-company ID: {evidence_id}"
            )
        records.append(record)
    return records


def source_metadata(records, *, include_company: bool = False) -> list[dict[str, Any]]:
    sources = []
    for record in sorted(records, key=lambda item: item.id):
        source = {
            "id": record.id,
            "title": record.title,
            "locator": record.locator,
            "date": record.date,
            "is_mock": record.is_mock,
        }
        if include_company:
            source["company"] = record.company
        sources.append(source)
    return sources


def data_label(sources: list[dict[str, Any]]) -> str:
    """Label the origin of the company documents cited in the result."""
    flags = {source["is_mock"] for source in sources}
    if not flags:
        return "NO CITED COMPANY EVIDENCE"
    if flags == {True}:
        return "MOCK DATA / 가상 자료"
    if flags == {False}:
        return "REAL DATA"
    if flags == {True, False}:
        return "MIXED REAL AND MOCK DATA / 실제·가상 자료 혼합"
    if flags == {None}:
        return "UNVERIFIED PROVENANCE / 자료 유형 미확인"
    return "MIXED OR UNVERIFIED PROVENANCE / 혼합 또는 유형 미확인"
