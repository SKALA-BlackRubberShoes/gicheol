"""Quality policy and service adapters; shared DTOs belong to supervisor.contracts.

Factories and hashing are required injections. ``canonical_hash`` is a fixture
implementation of the proposed JSON convention, not a second DTO definition.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
from typing import Any, Literal

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


def canonical_hash(value: Any) -> str:
    """Fixture hash: UTF-8, sorted object keys, compact JSON, no NaN."""
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def semantic_hash(value: Any) -> str:
    """Fixture counterpart to the shared recursive semantic hash convention."""
    def semantic(item):
        if isinstance(item, dict):
            return {key: semantic(val) for key, val in item.items()
                    if key not in {"result_hash", "content_hash", "ts", "retrieved_at",
                                   "created_at", "dispatched_at"}
                    and not key.endswith(("_ref", "_refs"))}
        if isinstance(item, list):
            return [semantic(val) for val in item]
        return item
    return canonical_hash(semantic(value))


def digest(value: Mapping[str, Any], fields: Sequence[str], hash_fn: Callable) -> str:
    if any(field not in value for field in fields):
        raise QualityInputError("Missing hash input fields")
    return hash_fn({field: value[field] for field in fields})


def quote_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


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


def validate_response(value: Any, schema: Mapping[str, Any]) -> Any:
    """Validate the small JSON-schema subset used by injected structured Judges."""
    if callable(getattr(value, "model_dump", None)):
        value = value.model_dump(mode="json")
    kind = schema.get("type")
    if isinstance(kind, list) and value is None and "null" in kind:
        return None
    if isinstance(kind, list):
        kind = next(item for item in kind if item != "null")
    valid = {
        "object": isinstance(value, Mapping), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": type(value) is bool,
        "integer": type(value) is int,
    }
    if kind and not valid.get(kind, False):
        raise QualityInputError(f"Judge response expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise QualityInputError("Judge response has an invalid enum")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", ())) - value.keys():
            raise QualityInputError("Judge response is missing fields")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise QualityInputError("Judge response contains unknown fields")
        for key, item in value.items():
            if key in properties:
                validate_response(item, properties[key])
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise QualityInputError("Judge response has too few items")
        for item in value:
            validate_response(item, schema["items"])
        if schema.get("uniqueItems") and len({canonical_hash(item) for item in value}) != len(value):
            raise QualityInputError("Judge response repeats items")
    if kind == "string" and len(value.strip()) < schema.get("minLength", 0):
        raise QualityInputError("Judge response has empty text")
    if kind == "string" and len(value) > schema.get("maxLength", len(value)):
        raise QualityInputError("Judge response text exceeds the common DTO limit")
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise QualityInputError("Judge score outside rubric range")
    return deepcopy(value)


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


class LLMClientJudge:
    """Bridge a configured common LLMClient to the quality Judge callbacks.

    Only Judge-response models are created here. Common domain DTOs remain in
    supervisor.contracts. Pydantic is loaded lazily for the configured live adapter.
    """

    def __init__(self, *, client: Any, model: str):
        if not isinstance(model, str) or not model.strip():
            raise QualityInputError("A configured Judge model ID is required")
        self.client = client
        self.model = model
        self._response_models = {}

    async def __call__(self, *, prompt: str, payload: dict, schema: dict):
        key = canonical_hash(schema)
        if key not in self._response_models:
            self._response_models[key] = _judge_response_model(schema)
        response = await self.client.generate(model=self.model, system_prompt=prompt,
                                              payload=payload, response_model=self._response_models[key])
        return validate_response(response, schema)


def _judge_response_model(schema: Mapping, name="QualityJudgement"):
    from pydantic import ConfigDict, Field, StrictBool, StrictInt, StrictStr, create_model

    def annotation(item, field_name):
        kind = item["type"]
        if "enum" in item:
            return Literal[tuple(item["enum"])]
        if isinstance(kind, list):
            inner = annotation({**item, "type": next(k for k in kind if k != "null")}, field_name)
            return inner | None
        if kind == "object":
            return _judge_response_model(item, f"{name}_{field_name}")
        if kind == "array":
            return list[annotation(item["items"], field_name)]
        return {"string": StrictStr, "integer": StrictInt, "boolean": StrictBool}[kind]

    fields = {}
    for key, item in schema["properties"].items():
        limits = {pydantic_name: item[json_name] for json_name, pydantic_name in (
            ("minLength", "min_length"), ("maxLength", "max_length"),
            ("minItems", "min_length"), ("minimum", "ge"), ("maximum", "le")) if json_name in item}
        fields[key] = (annotation(item, key), Field(..., **limits))
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)


TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
IDS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 256}, "uniqueItems": True}
CLAIM_JUDGE_SCHEMA = object_schema({
    "support_status": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
    "reason": TEXT, "supported_evidence_ids": IDS,
})
GAP_SCHEMA = object_schema({
    "kind": {"type": "string", "enum": list(GAP_KINDS)}, "question": TEXT,
    "severity": {"type": "string", "enum": ["critical", "warning"]},
    "evidence_ids": IDS, "query_hints": {"type": "array", "items": TEXT},
})
SUFFICIENCY_JUDGE_SCHEMA = object_schema({
    "semantic_pass": {"type": "boolean"}, "reason": TEXT,
    "gaps": {"type": "array", "items": GAP_SCHEMA},
})
FINDING_SCHEMA = object_schema({
    "criterion": {"type": "string", "enum": list(CRITERIA)},
    "severity": {"type": "string", "enum": ["critical", "warning"]},
    "section_id": TEXT, "claim_id": {"type": ["string", "null"], "minLength": 1},
    "evidence_ids": IDS, "reason": TEXT,
    "repair_kind": {"type": "string", "enum": ["rewrite", "research", "none"]},
    "target_perspective": {"type": ["string", "null"], "enum": [None, *QUESTIONS]},
})
REPORT_JUDGE_SCHEMA = object_schema({
    "criterion_scores": object_schema({name: {"type": "integer", "minimum": 0, "maximum": 4}
                                      for name in CRITERIA}),
    "findings": {"type": "array", "items": FINDING_SCHEMA},
})


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
