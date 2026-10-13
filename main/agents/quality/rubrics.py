"""Quality policy and service adapters; shared DTOs belong to supervisor.contracts.

Business DTOs and hashing use the shared contract module. Only external services
are injected; Judge responses have explicit strict Pydantic models.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
import inspect
from pathlib import Path
import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError
from main.agents.supervisor import contracts as c
from main.agents.supervisor.contracts import canonical_hash, semantic_hash, sha256_text

RUBRIC_VERSION = "neutral-quality-1.0"
VERIFIER_VERSION = "claim-support-1.0"
QUESTIONS = {
    "technology_maturity": ("tech.principle", "tech.validation"),
    "market": ("market.demand", "market.economics"),
    "stakeholders": ("stakeholders.actors", "stakeholders.burdens"),
    "domain_application": ("domain.cases", "domain.conditions"),
}
CRITERIA = ("groundedness", "neutrality", "bias_control", "perspective_coverage")
# IDs match the Dongwook contract at 2fb927db25d02e1bda5bdade02a9e25977a3b289.
SECTION_ORDER = (
    "SUMMARY", "company_scope", "technology_maturity", "market", "stakeholders",
    "domain_application", "limitations", "REFERENCE",
)
CLAIM_FIELDS = ("claim_id", "perspective", "text", "kind", "evidence_ids", "limitations")
EVIDENCE_FIELDS = (
    "evidence_id", "source_id", "company_id", "chunk_id", "page_number",
    "quote", "quote_hash", "event_date",
)
RESULT_FIELDS = (
    "task_id", "perspective", "attempt", "claims", "evidence_ids",
    "answered_questions", "gaps", "limitations", "retrieval_receipts",
)
REPORT_FIELDS = ("sections", "claim_ids", "citations", "evidence_epoch")
GAP_KINDS = (
    "missing_question", "missing_evidence", "citation_mismatch", "source_bias",
    "conflicting_evidence", "eligibility_unknown", "report_expression", "report_coverage",
)
_PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts" / "quality"


class QualityInputError(ValueError):
    """Invalid service input; wrappers must treat this as a failed operation."""


def record(value: Any) -> dict[str, Any]:
    """Take an immutable-to-the-caller JSON snapshot of a mapping or shared DTO."""
    if callable(getattr(value, "model_dump", None)):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise QualityInputError("Expected a mapping or common DTO")
    return deepcopy(dict(value))


def records(values: Sequence[Any]) -> list[dict[str, Any]]:
    return [record(value) for value in values]


def index(values: Sequence[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result = {}
    for value in values:
        identifier = value.get(key)
        if not isinstance(identifier, str) or not identifier.strip() or identifier in result:
            raise QualityInputError(f"Missing or duplicate {key}: {identifier!r}")
        result[identifier] = value
    return result


def digest(value: Mapping[str, Any], fields: Sequence[str], hash_fn: Callable) -> str:
    if any(field not in value for field in fields):
        raise QualityInputError("Missing hash input fields")
    return hash_fn({field: value[field] for field in fields})


def quote_hash(text: str) -> str:
    return sha256_text(text)


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise QualityInputError("Invalid date")
    try:
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(
            value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise QualityInputError("Invalid ISO date") from exc


def make_gap(perspective: str, kind: str, question: str, *, evidence_ids=(),
             severity="critical", query_hints=()) -> dict[str, Any]:
    identity = canonical_hash([perspective, kind, question, sorted(evidence_ids)])[:20]
    return {"gap_id": f"quality-{identity}", "perspective": perspective,
            "kind": kind, "question": question, "severity": severity,
            "evidence_ids": list(evidence_ids), "query_hints": list(query_hints)}


def prompt(name: str) -> str:
    return (_PROMPT_ROOT / f"{name}.md").read_text(encoding="utf-8")


async def call_async(callback: Callable, *args, timeout_seconds: float, **kwargs):
    """Only async adapters are accepted, so timeout also bounds adapter execution."""
    if not (inspect.iscoroutinefunction(callback)
            or inspect.iscoroutinefunction(getattr(callback, "__call__", None))):
        raise QualityInputError("Adapter must be an async callable")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise QualityInputError("timeout_seconds must be positive")
    return await asyncio.wait_for(callback(*args, **kwargs), timeout=timeout_seconds)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("Judge text cannot be blank")
    return value


def _unique_ids(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("Judge evidence IDs must be unique")
    return values


Text = Annotated[StrictStr, Field(min_length=1, max_length=2000), AfterValidator(_not_blank)]
EvidenceId = Annotated[StrictStr, Field(min_length=1, max_length=256), AfterValidator(_not_blank)]
EvidenceIds = Annotated[list[EvidenceId], AfterValidator(_unique_ids), Field(json_schema_extra={"uniqueItems": True})]
PerspectiveName = Literal["technology_maturity", "market", "stakeholders", "domain_application"]


class JudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimJudgement(JudgeResponse):
    support_status: Literal["supported", "partial", "unsupported"]
    reason: Text
    supported_evidence_ids: EvidenceIds


class GapJudgement(JudgeResponse):
    kind: Literal[*GAP_KINDS]
    question: Text
    severity: Literal["critical", "warning"]
    evidence_ids: EvidenceIds
    query_hints: list[Text]


class SufficiencyJudgement(JudgeResponse):
    semantic_pass: StrictBool
    reason: Text
    gaps: list[GapJudgement]


class FindingJudgement(JudgeResponse):
    criterion: Literal[*CRITERIA]
    severity: Literal["critical", "warning"]
    section_id: Text
    claim_id: Text | None
    evidence_ids: EvidenceIds
    reason: Text
    repair_kind: Literal["rewrite", "research", "none"]
    target_perspective: PerspectiveName | None


Score = Annotated[StrictInt, Field(ge=0, le=4)]


class CriterionScores(JudgeResponse):
    groundedness: Score
    neutrality: Score
    bias_control: Score
    perspective_coverage: Score


class ReportJudgement(JudgeResponse):
    criterion_scores: CriterionScores
    findings: list[FindingJudgement]


CLAIM_JUDGE_SCHEMA = ClaimJudgement.model_json_schema()
SUFFICIENCY_JUDGE_SCHEMA = SufficiencyJudgement.model_json_schema()
REPORT_JUDGE_SCHEMA = ReportJudgement.model_json_schema()
_RESPONSE_MODELS = {
    canonical_hash(model.model_json_schema()): model
    for model in (ClaimJudgement, SufficiencyJudgement, ReportJudgement)
}


def _response_model(schema):
    try:
        return _RESPONSE_MODELS[canonical_hash(schema)]
    except KeyError:
        raise QualityInputError("Unknown quality Judge response schema") from None


def validate_response(value: Any, schema: Mapping[str, Any]) -> dict:
    try:
        return _response_model(schema).model_validate(record(value)).model_dump(mode="json")
    except ValidationError as exc:
        raise QualityInputError("Judge response violates the required quality contract") from exc


class LLMClientJudge:
    """Use one of the three fixed response models with the common LLM client."""
    def __init__(self, *, client: Any, model: str):
        if not isinstance(model, str) or not model.strip():
            raise QualityInputError("A configured Judge model ID is required")
        self.client, self.model = client, model

    async def __call__(self, *, prompt: str, payload: dict, schema: dict):
        response = await self.client.generate(model=self.model, system_prompt=prompt,
                                              payload=payload, response_model=_response_model(schema))
        return validate_response(response, schema)


def current_support(support: Any, claim: Mapping, evidence_by_id: Mapping,
                    hash_fn: Callable, verifier_version=VERIFIER_VERSION) -> bool:
    value = record(support)
    ids = claim.get("evidence_ids", [])
    try:
        return (claim.get("kind") in {"fact", "inference"} and bool(ids)
                and len(ids) == len(set(ids))
                and value.get("claim_id") == claim["claim_id"]
                and value.get("claim_hash") == digest(claim, CLAIM_FIELDS, hash_fn)
                and value.get("support_status") == "supported"
                and value.get("verifier_version") == verifier_version
                and value.get("evidence_hashes") == {
                    eid: digest(evidence_by_id[eid], EVIDENCE_FIELDS, hash_fn) for eid in ids})
    except (KeyError, QualityInputError):
        return False


def provenance_groups(sources: Sequence[Mapping]) -> list[list[Mapping]]:
    """Group transitive publisher/content/origin identities."""
    parents = list(range(len(sources)))

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, first in enumerate(sources):
        for j in range(i):
            other = sources[j]
            if any(first.get(field) and first.get(field) == other.get(field)
                   for field in ("publisher_group", "sha256", "canonical_url", "original_file_id")):
                parents[root(i)] = root(j)
    groups = {}
    for i, source in enumerate(sources):
        groups.setdefault(root(i), []).append(source)
    return list(groups.values())


def independent_groups(sources: Sequence[Mapping]) -> int:
    return len(provenance_groups(sources))


def has_external_original(selected: Sequence[Mapping], all_sources=None) -> bool:
    """Reprinting company-controlled material does not create external proof."""
    selected_ids = {source["source_id"] for source in selected}
    return any(all(source["corporate_controlled"] is False for source in group)
               and any(source["source_id"] in selected_ids for source in group)
               for group in provenance_groups(all_sources if all_sources is not None else selected))


def merge_gaps(gaps: Sequence[Mapping]) -> list[dict]:
    """Preserve critical severity and all search hints on an identical gap."""
    merged = {}
    for gap in gaps:
        current = merged.setdefault(gap["gap_id"], deepcopy(dict(gap)))
        if gap["severity"] == "critical":
            current["severity"] = "critical"
        current["query_hints"] = list(dict.fromkeys([*current["query_hints"], *gap["query_hints"]]))
    return list(merged.values())


# This catches explicit directives/comparisons; the Judge evaluates subtler context.
NEUTRALITY_PATTERN = re.compile(
    r"투자\s*(?:추천|권유|해야|권장|적합|가치|매력|결정)|추천\s*(?:한다|합니다|드립|기업|대상)|"
    r"(?:가장|최고의|최상의)\s*(?:기업|기술|투자|성능)|(?:기술|기업|경쟁사).{0,15}(?:우월|우위)|"
    r"(?:종합|투자|기술|기업|경쟁)\s*점수|(?:종합|기업|기술|투자)\s*순위|랭킹|"
    r"\b(?:recommend(?:ed|ation|ing)?\s+(?:invest(?:ing|ment)?|buy(?:ing)?)|should\s+invest|investment\s+recommendation|"
    r"best\s+(?:company|technology|investment)|superior\s+to|rank(?:ed|ing)?\s*#?\d|ROI)\b",
    re.IGNORECASE,
)
