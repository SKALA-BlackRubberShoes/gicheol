"""CSV 로딩·정규화와 정확 조건 필터링입니다."""

from __future__ import annotations

import csv
import math
import operator
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import CompanyRecord, CompanyFilter, RAGDataError, Scalar, SourceRef

_FIELDS = {
    "company_id": ("company_id", "str"),
    "company_name": ("기업명", "str"),
    "location": ("소재지역", "str"),
    "sector": ("분야", "str"),
    "subsector": ("소분야", "str"),
    "technology": ("기술", "str"),
    "product_type": ("제품 형태", "str"),
    "funding_stage": ("투자 유치 단계 (최근)", "str"),
    "funding_latest_won": ("투자 유치 금액 (최근)", "money"),
    "funding_total_won": ("투자 유치 금액 (누적)", "money"),
    "employees": ("임직원 수", "int"),
    "employees_change": ("1개월전 대비 임직원 수", "int"),
    "patent_count": ("특허 수", "int"),
    "funding_latest_date": ("투자 유치일 (최근)", "date"),
    "company_age_years": ("업력", "age"),
}


_CONTENT_COLUMNS = (
    "기업명",
    "대표제품",
    "서비스",
    "분야",
    "소분야",
    "기술",
    "제품 형태",
)


_REQUIRED_COLUMNS = (
    {c for c, _ in _FIELDS.values()}
    | set(_CONTENT_COLUMNS)
    | {"홈페이지", "투자자 분류", "인증/자격"}
)


_COMPARISONS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


def _iso_date(value: str) -> str:
    """YYYY-MM-DD 형식과 실제로 존재하는 날짜인지 확인합니다."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def _normalize(value: str, kind: str) -> Scalar:
    """CSV 문자열을 조회용 값으로 변환합니다. 빈 값·NULL은 None으로 유지합니다."""
    value = value.strip()
    if not value or value == "NULL":
        return None
    if kind == "str":
        return value
    if kind == "date":
        return _iso_date(value)
    if kind == "int":
        if not re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)", value):
            raise ValueError("expected an integer")
        return int(value.replace(",", ""))
    if kind == "money":
        match = re.fullmatch(rf"({_NUMBER})\s*(억원|만원|원)", value)
        if not match:
            raise ValueError("expected a KRW amount with 원, 만원, or 억원")
        # 원·만원·억원을 원 단위 정수로 통일합니다.
        amount = (
            Decimal(match[1].replace(",", ""))
            * {"원": 1, "만원": 10_000, "억원": 100_000_000}[match[2]]
        )
        if amount != amount.to_integral_value():
            raise ValueError("amount must be a whole number of won")
        return int(amount)
    if not re.fullmatch(r"\d+(?:\.\d+)?년차", value):
        raise ValueError("expected years followed by 년차")
    result = float(value[:-2])
    if not math.isfinite(result):
        raise ValueError("years must be finite")
    return result


def _load_csv(path: Path) -> dict[str, CompanyRecord]:
    """회사 ID를 키로 CSV를 읽습니다. 행 순서로 ID를 새로 만들지 않습니다."""
    records = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            headers = reader.fieldnames or []
            missing = _REQUIRED_COLUMNS - set(headers)
            if missing or len(headers) != len(set(headers)):
                raise RAGDataError(
                    f"{path}: record 1: missing/duplicate columns: {sorted(missing)}"
                )
            for number, raw in enumerate(reader, start=2):
                if None in raw or any(v is None for v in raw.values()):
                    raise RAGDataError(
                        f"{path}: record {number}: incorrect column count"
                    )
                values = {}
                for key, (column, kind) in _FIELDS.items():
                    try:
                        values[key] = _normalize(raw[column], kind)
                    except (ValueError, ArithmeticError) as exc:
                        raise RAGDataError(
                            f"{path}: record {number}, column {column}: {exc}"
                        ) from exc
                # 공백을 제거한 뒤에도 ID가 비어 있거나 중복이면 적재를 중단합니다.
                company_id = values["company_id"]
                if company_id is None or company_id in records:
                    raise RAGDataError(
                        f"{path}: record {number}, column company_id: blank/NULL or duplicate ID {company_id!r}"
                    )
                if values["company_name"] is None:
                    raise RAGDataError(
                        f"{path}: record {number}, column 기업명: required"
                    )
                content = "\n".join(
                    f"{c}: {raw[c].strip()}"
                    for c in _CONTENT_COLUMNS
                    if raw[c].strip() not in ("", "NULL")
                )
                records[company_id] = CompanyRecord(
                    company_id=company_id,
                    company_name=values["company_name"],
                    raw=raw,
                    values=values,
                    content=content,
                    source=SourceRef(csv_path=str(path), record_number=number),
                )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RAGDataError(f"{path}: cannot read CSV: {exc}") from exc
    return records


def _validate_filters(filters: list[CompanyFilter] | None) -> list[CompanyFilter]:
    """필터 항목·연산자·값의 자료형이 비교 가능한 조합인지 확인합니다."""
    if filters is None:
        return []
    if not isinstance(filters, list) or any(
        not isinstance(f, CompanyFilter) for f in filters
    ):
        raise ValueError("filters must be a list of CompanyFilter")
    for f in filters:
        if f.field not in _FIELDS or f.op not in {*_COMPARISONS, "is_null", "not_null"}:
            raise ValueError(f"Unsupported filter: {f.field} {f.op}")
        kind = _FIELDS[f.field][1]
        if f.op in ("is_null", "not_null"):
            if f.value is not None:
                raise ValueError("Null filters take no value")
            continue
        if kind in ("str", "date"):
            valid = isinstance(f.value, str)
        else:
            valid = type(f.value) in (int, float) and math.isfinite(f.value)
        if not valid:
            raise ValueError(f"Invalid value type for {f.field}")
        if kind == "date":
            _iso_date(f.value)
        if kind == "str" and f.op not in ("eq", "ne"):
            raise ValueError("Range comparisons require numeric or date fields")
    return filters


def _matches(record: CompanyRecord, filters: list[CompanyFilter]) -> bool:
    """회사가 모든 조건을 만족하는지 확인합니다. 결측은 일반 비교에서 제외합니다."""
    for f in filters:
        value = record.values[f.field]
        if f.op == "is_null":
            ok = value is None
        elif f.op == "not_null":
            ok = value is not None
        else:
            ok = value is not None and _COMPARISONS[f.op](value, f.value)
        if not ok:
            return False
    return True


def _validate_id(company_id: str) -> str:
    """조회할 ID의 앞뒤 공백을 제거하고 빈 문자열을 거부합니다."""
    if not isinstance(company_id, str) or not company_id.strip():
        raise ValueError("company_id must be a nonempty string")
    return company_id.strip()
