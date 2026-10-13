"""SDD 1.0 DTOs and async service boundaries for the neutral Supervisor.

Only boundary validation lives here. Content verification, sufficient evidence,
storage byte checks, and routing remain the responsibility of their services.
Canonical JSON is UTF-8, sorted keys, no whitespace, non-ASCII preserved, and
non-finite numbers rejected. Semantic result hashes omit self hashes, artifact
references, and operational timestamps recursively. Claim/evidence hashes use
their complete JSON DTO; DraftReport hashes exactly the four SDD fields.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Mapping, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl, StrictBool, field_validator, model_validator


Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=256, pattern=r"\S")]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=20000, pattern=r"\S")]
ShortText = Annotated[str, Field(strict=True, min_length=1, max_length=2000, pattern=r"\S")]
Sha256 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]


def _iso_date(value: Any) -> Any:
    if type(value) is date or isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    raise ValueError("date must use YYYY-MM-DD")


ISODate = Annotated[date, BeforeValidator(_iso_date)]


class Domain(str, Enum):
    AGTECH = "AgTech"
    ENERGY = "Energy"
    HEALTHCARE_AI = "Healthcare AI"
    PHYSICAL_AI_ROBOTICS = "Physical AI / Robotics"
    SEMICONDUCTOR = "Semiconductor"


class Perspective(str, Enum):
    TECHNOLOGY_MATURITY = "technology_maturity"
    MARKET = "market"
    STAKEHOLDERS = "stakeholders"
    DOMAIN_APPLICATION = "domain_application"


PERSPECTIVES = tuple(Perspective)


class QuestionId(str, Enum):
    TECH_PRINCIPLE = "tech.principle"
    TECH_VALIDATION = "tech.validation"
    MARKET_DEMAND = "market.demand"
    MARKET_ECONOMICS = "market.economics"
    STAKEHOLDERS_ACTORS = "stakeholders.actors"
    STAKEHOLDERS_BURDENS = "stakeholders.burdens"
    DOMAIN_CASES = "domain.cases"
    DOMAIN_CONDITIONS = "domain.conditions"


REQUIRED_QUESTIONS: Mapping[Perspective, tuple[QuestionId, QuestionId]] = {
    Perspective.TECHNOLOGY_MATURITY: (QuestionId.TECH_PRINCIPLE, QuestionId.TECH_VALIDATION),
    Perspective.MARKET: (QuestionId.MARKET_DEMAND, QuestionId.MARKET_ECONOMICS),
    Perspective.STAKEHOLDERS: (QuestionId.STAKEHOLDERS_ACTORS, QuestionId.STAKEHOLDERS_BURDENS),
    Perspective.DOMAIN_APPLICATION: (QuestionId.DOMAIN_CASES, QuestionId.DOMAIN_CONDITIONS),
}


class GapKind(str, Enum):
    MISSING_QUESTION = "missing_question"
    MISSING_EVIDENCE = "missing_evidence"
    CITATION_MISMATCH = "citation_mismatch"
    SOURCE_BIAS = "source_bias"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    ELIGIBILITY_UNKNOWN = "eligibility_unknown"
    REPORT_EXPRESSION = "report_expression"
    REPORT_COVERAGE = "report_coverage"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"


class NextAction(str, Enum):
    ELIGIBILITY = "eligibility"
    TECHNOLOGY_MATURITY = "technology_maturity"
    MARKET = "market"
    STAKEHOLDERS = "stakeholders"
    DOMAIN_APPLICATION = "domain_application"
    WRITER = "writer"
    EVALUATOR = "evaluator"
    PUBLISH = "publish"
    ABORT = "abort"


NodeAction = NextAction


class WorkerNode(str, Enum):
    ELIGIBILITY = "eligibility"
    TECHNOLOGY_MATURITY = "technology_maturity"
    MARKET = "market"
    STAKEHOLDERS = "stakeholders"
    DOMAIN_APPLICATION = "domain_application"
    WRITER = "writer"
    EVALUATOR = "evaluator"


class TerminalReason(str, Enum):
    CONFIG_OR_AUTH_ERROR = "CONFIG_OR_AUTH_ERROR"
    INDEX_MISMATCH = "INDEX_MISMATCH"
    CORPUS_DOMAIN_UNAVAILABLE = "CORPUS_DOMAIN_UNAVAILABLE"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    INELIGIBLE_TARGET = "INELIGIBLE_TARGET"
    ELIGIBILITY_UNKNOWN = "ELIGIBILITY_UNKNOWN"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    QUALITY_EXHAUSTED = "QUALITY_EXHAUSTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    ARTIFACT_INTEGRITY_ERROR = "ARTIFACT_INTEGRITY_ERROR"
    PDF_LAYOUT_FAILED = "PDF_LAYOUT_FAILED"
    RESUME_VERSION_MISMATCH = "RESUME_VERSION_MISMATCH"
    STATE_TOO_LARGE = "STATE_TOO_LARGE"
    SOURCE_INACCESSIBLE = "SOURCE_INACCESSIBLE"


ErrorCode = TerminalReason


class RepairKind(str, Enum):
    REWRITE = "rewrite"
    RESEARCH = "research"
    NONE = "none"


class Criterion(str, Enum):
    GROUNDEDNESS = "groundedness"
    NEUTRALITY = "neutrality"
    BIAS_CONTROL = "bias_control"
    PERSPECTIVE_COVERAGE = "perspective_coverage"


class SectionId(str, Enum):
    SUMMARY = "SUMMARY"
    COMPANY_SCOPE = "company_scope"
    TECHNOLOGY_MATURITY = "technology_maturity"
    MARKET = "market"
    STAKEHOLDERS = "stakeholders"
    DOMAIN_APPLICATION = "domain_application"
    LIMITATIONS = "limitations"
    REFERENCE = "REFERENCE"


SECTION_ORDER = tuple(SectionId)
Condition = StrictBool | Literal["unknown"]
EligibilityStatus = Literal["pending", "eligible", "ineligible", "unknown"]
RunStatus = Literal["running", "completed", "incomplete", "failed"]
ClaimKind = Literal["fact", "inference", "unknown"]
SupportStatus = Literal["unverified", "supported", "partial", "unsupported"]
ResultOutcome = Literal["success", "empty", "transient_error", "permanent_error"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(_json_value(k)): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _semantic_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _semantic_payload(v) for k, v in value.items() if k not in {"result_hash", "content_hash", "ts", "retrieved_at", "created_at", "dispatched_at"} and not k.endswith(("_ref", "_refs"))}
    if isinstance(value, list):
        return [_semantic_payload(v) for v in value]
    return value


def semantic_hash(value: Any) -> str:
    return canonical_hash(_semantic_payload(_json_value(value)))


def _check_hash(model: BaseModel, field: str, payload: Any) -> None:
    expected = canonical_hash(payload)
    supplied = getattr(model, field)
    if supplied is not None and supplied != expected:
        raise ValueError(f"{field} does not match canonical payload")
    object.__setattr__(model, field, expected)


def _unique(values: list[Any] | tuple[Any, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


class ArtifactRef(ContractModel):
    relative_path: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    sha256: Sha256
    media_type: Annotated[str, Field(strict=True, min_length=3, max_length=128, pattern=r"^[\w.+-]+/[\w.+-]+$")]
    schema_version: Identifier = "1.0"

    @field_validator("relative_path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value or ":" in value or any(ord(ch) < 32 for ch in value) or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("artifact path must be a safe root-relative POSIX path")
        return value


class Source(ContractModel):
    source_id: Identifier
    canonical_url: HttpUrl | None = None
    original_file_id: Identifier | None = None
    title: ShortText
    publisher: ShortText | None
    publisher_group: Identifier
    source_type: Literal["company", "paper", "institution", "news", "dataset"]
    published_at: ISODate | None = None
    retrieved_at: datetime
    corporate_controlled: StrictBool
    sha256: Sha256
    content_ref: ArtifactRef

    _retrieved_aware = field_validator("retrieved_at")(_aware)

    @model_validator(mode="after")
    def origin_and_hash(self) -> Source:
        if self.canonical_url is None and self.original_file_id is None:
            raise ValueError("source requires canonical URL or original file ID")
        if self.sha256 != self.content_ref.sha256:
            raise ValueError("source hash differs from content reference")
        return self


class Evidence(ContractModel):
    evidence_id: Identifier
    source_id: Identifier
    company_id: Identifier
    chunk_id: Identifier
    page_number: PositiveInt | None = None
    quote: Text
    quote_hash: Sha256 | None = None
    event_date: ISODate | None = None

    @model_validator(mode="after")
    def quote_integrity(self) -> Evidence:
        expected = sha256_text(self.quote)
        if self.quote_hash is not None and self.quote_hash != expected:
            raise ValueError("quote_hash does not match quote")
        object.__setattr__(self, "quote_hash", expected)
        return self


class Claim(ContractModel):
    claim_id: Identifier
    perspective: Perspective
    text: Text
    kind: ClaimKind
    evidence_ids: list[Identifier]
    limitations: list[ShortText]

    @model_validator(mode="after")
    def grounded_kinds(self) -> Claim:
        _unique(self.evidence_ids, "evidence IDs")
        if self.kind in {"fact", "inference"} and not self.evidence_ids:
            raise ValueError("fact and inference require evidence IDs")
        return self


class ClaimSupport(ContractModel):
    claim_id: Identifier
    claim_hash: Sha256
    evidence_hashes: dict[Identifier, Sha256]
    support_status: SupportStatus
    reason: ShortText
    verifier_version: Identifier

    @model_validator(mode="after")
    def supported_requires_evidence(self) -> ClaimSupport:
        if self.support_status == "supported" and not self.evidence_hashes:
            raise ValueError("supported claims require verified evidence hashes")
        return self


class Gap(ContractModel):
    gap_id: Identifier
    perspective: Perspective | Literal["eligibility", "report"]
    kind: GapKind
    question: ShortText
    severity: Severity
    evidence_ids: list[Identifier] = Field(default_factory=list)
    query_hints: list[ShortText] = Field(default_factory=list)


class RankedHit(ContractModel):
    source_id: Identifier
    chunk_id: Identifier
    rank: PositiveInt
    score: Annotated[float, Field(strict=True, allow_inf_nan=False)]


class RetrievalReceipt(ContractModel):
    query_id: Identifier
    query_text: ShortText
    queries_hash: Sha256
    provider: Identifier
    corpus_version: Identifier
    index_version: Identifier
    filters: dict[str, str]
    ranked_hits: list[RankedHit]
    outcome: Literal["success", "empty", "source_inaccessible", "error"]
    ts: datetime

    _ts_aware = field_validator("ts")(_aware)

    @model_validator(mode="after")
    def hits_match_outcome(self) -> RetrievalReceipt:
        _unique([h.rank for h in self.ranked_hits], "ranks")
        _unique([(h.source_id, h.chunk_id) for h in self.ranked_hits], "ranked hits")
        if self.outcome == "empty" and self.ranked_hits:
            raise ValueError("empty search cannot contain hits")
        if self.outcome == "success" and not self.ranked_hits:
            raise ValueError("successful search requires hits")
        return self


class PerspectiveResult(ContractModel):
    task_id: Identifier
    perspective: Perspective
    attempt: PositiveInt
    claims: list[Claim]
    evidence_ids: list[Identifier]
    answered_questions: dict[QuestionId, list[Identifier]]
    gaps: list[Gap]
    limitations: list[ShortText]
    retrieval_receipts: list[RetrievalReceipt]
    result_hash: Sha256 | None = None

    @model_validator(mode="after")
    def reference_integrity(self) -> PerspectiveResult:
        _unique([c.claim_id for c in self.claims], "claim IDs")
        _unique(self.evidence_ids, "evidence IDs")
        _unique([g.gap_id for g in self.gaps], "gap IDs")
        claims = {c.claim_id: c for c in self.claims}
        for claim in self.claims:
            if claim.perspective != self.perspective or not set(claim.evidence_ids) <= set(self.evidence_ids):
                raise ValueError("claim perspective or evidence link mismatch")
        for question, claim_ids in self.answered_questions.items():
            if question not in REQUIRED_QUESTIONS[self.perspective] or not claim_ids or not set(claim_ids) <= claims.keys():
                raise ValueError("answered question has wrong perspective or missing claims")
            _unique(claim_ids, "answer claim IDs")
        _check_hash(self, "result_hash", _semantic_payload(self.model_dump(mode="json")))
        return self


class EligibilityResult(ContractModel):
    task_id: Identifier
    unlisted: Condition
    funding_stage: Condition
    exit_status: Condition
    verdict: Literal["eligible", "ineligible", "unknown"]
    claims: list[Claim]
    gaps: list[Gap]
    result_hash: Sha256 | None = None

    @model_validator(mode="after")
    def eligibility_conjunction(self) -> EligibilityResult:
        conditions = (self.unlisted, self.funding_stage, self.exit_status)
        expected = "ineligible" if False in conditions else "unknown" if "unknown" in conditions else "eligible"
        if self.verdict != expected:
            raise ValueError("eligibility verdict contradicts conditions")
        _unique([c.claim_id for c in self.claims], "claim IDs")
        _check_hash(self, "result_hash", _semantic_payload(self.model_dump(mode="json")))
        return self


class ResearchRequest(ContractModel):
    task_id: Identifier
    company_id: Identifier
    perspective: Perspective
    as_of_date: ISODate
    prior_result_ref: ArtifactRef | None = None
    gap_ids: list[Identifier] = Field(default_factory=list)
    instructions: list[ShortText] = Field(default_factory=list)
    query_hints: list[ShortText] = Field(default_factory=list)
    excluded_source_ids: list[Identifier] = Field(default_factory=list)
    attempt: PositiveInt
    corpus_version: Identifier


class EligibilityRequest(ContractModel):
    task_id: Identifier
    company_id: Identifier
    company_name: ShortText
    official_url: HttpUrl
    as_of_date: ISODate
    attempt: PositiveInt
    gap_ids: list[Identifier] = Field(default_factory=list)
    instructions: list[ShortText] = Field(default_factory=list)


class SufficiencyVerdict(ContractModel):
    perspective: Perspective
    structural_pass: StrictBool
    semantic_pass: StrictBool
    gaps: list[Gap]
    evidence_ids: list[Identifier]
    claim_support_refs: list[ArtifactRef]
    reason: ShortText
    result_hash: Sha256 | None = None
    rubric_version: Identifier

    @model_validator(mode="after")
    def no_critical_pass(self) -> SufficiencyVerdict:
        if self.structural_pass and self.semantic_pass and (any(g.severity == Severity.CRITICAL for g in self.gaps) or not self.claim_support_refs):
            raise ValueError("sufficiency pass requires claim support refs and no critical gaps")
        _check_hash(self, "result_hash", _semantic_payload(self.model_dump(mode="json")))
        return self


class ReportParagraph(ContractModel):
    paragraph_id: Identifier
    text: Text
    kind: Literal["fact", "inference", "unknown", "connective"]
    claim_ids: list[Identifier]
    evidence_ids: list[Identifier]

    @model_validator(mode="after")
    def grounded_paragraph(self) -> ReportParagraph:
        _unique(self.claim_ids, "paragraph claim IDs")
        _unique(self.evidence_ids, "paragraph evidence IDs")
        if self.kind in {"fact", "inference"} and (not self.claim_ids or not self.evidence_ids):
            raise ValueError("fact and inference paragraphs require claims and evidence")
        if self.kind == "connective" and (self.claim_ids or self.evidence_ids):
            raise ValueError("connective paragraphs cannot introduce factual claim links")
        return self


class ReportSection(ContractModel):
    section_id: SectionId
    title: ShortText
    paragraphs: list[ReportParagraph]


class Citation(ContractModel):
    source_id: Identifier
    evidence_id: Identifier
    paragraph_id: Identifier


class DraftReport(ContractModel):
    revision: PositiveInt
    evidence_epoch: NonNegativeInt
    sections: list[ReportSection]
    claim_ids: list[Identifier]
    citations: list[Citation]
    content_hash: Sha256 | None = None
    document_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def report_integrity(self) -> DraftReport:
        if tuple(s.section_id for s in self.sections) != SECTION_ORDER:
            raise ValueError("report sections must follow the fixed SDD order")
        paragraphs = [p for s in self.sections for p in s.paragraphs]
        _unique([p.paragraph_id for p in paragraphs], "paragraph IDs")
        _unique(self.claim_ids, "report claim IDs")
        used_claims = {cid for p in paragraphs for cid in p.claim_ids}
        if used_claims != set(self.claim_ids):
            raise ValueError("report claim IDs must match paragraph claims")
        by_id = {p.paragraph_id: p for p in paragraphs}
        _unique([(c.source_id, c.evidence_id, c.paragraph_id) for c in self.citations], "citations")
        for citation in self.citations:
            paragraph = by_id.get(citation.paragraph_id)
            if paragraph is None or citation.evidence_id not in paragraph.evidence_ids:
                raise ValueError("citation has dangling paragraph or evidence link")
        for paragraph in paragraphs:
            cited = {c.evidence_id for c in self.citations if c.paragraph_id == paragraph.paragraph_id}
            if paragraph.kind in {"fact", "inference"} and cited != set(paragraph.evidence_ids):
                raise ValueError("factual paragraph evidence must be cited")
        payload = self.model_dump(mode="json", include={"sections", "claim_ids", "citations", "evidence_epoch"})
        _check_hash(self, "content_hash", payload)
        return self


class EvalFinding(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    criterion: Criterion
    severity: Severity
    section_id: SectionId
    claim_id: Identifier | None = None
    evidence_ids: tuple[Identifier, ...] = ()
    reason: ShortText
    repair_kind: RepairKind
    target_perspective: Perspective | None = None

    @model_validator(mode="after")
    def research_target_required(self) -> EvalFinding:
        if self.repair_kind == RepairKind.RESEARCH and self.target_perspective is None:
            raise ValueError("research finding requires target perspective")
        return self


class EvalVerdict(ContractModel):
    report_revision: PositiveInt
    report_hash: Sha256
    evidence_epoch: NonNegativeInt
    structural_checks: dict[Identifier, StrictBool]
    criterion_scores: dict[Criterion, Annotated[int, Field(strict=True, ge=0, le=4)]]
    findings: list[EvalFinding]
    overall_pass: StrictBool
    repair_kind: RepairKind
    rubric_version: Identifier
    judge_model: Identifier
    prompt_hash: Sha256

    @model_validator(mode="after")
    def quality_conjunction(self) -> EvalVerdict:
        if set(self.criterion_scores) != set(Criterion) or not self.structural_checks:
            raise ValueError("quality verdict requires all four criteria and structural checks")
        passed = all(self.structural_checks.values()) and all(v >= 3 for v in self.criterion_scores.values()) and not any(f.severity == Severity.CRITICAL for f in self.findings)
        if self.overall_pass != passed:
            raise ValueError("overall_pass contradicts quality checks")
        if self.overall_pass and self.repair_kind != RepairKind.NONE:
            raise ValueError("passed quality verdict cannot request repair")
        if not self.overall_pass:
            expected = RepairKind.RESEARCH if any(f.repair_kind == RepairKind.RESEARCH for f in self.findings) else RepairKind.REWRITE
            if self.repair_kind != expected:
                raise ValueError("failed quality verdict must request the appropriate repair")
        return self


class Decision(ContractModel):
    decision_id: Identifier
    run_id: Identifier
    trace_id: Identifier
    step: NonNegativeInt
    next_action: NextAction
    reason: ShortText
    gap_ids: list[Identifier]
    input_state_hash: Sha256
    policy_version: Identifier
    ts: datetime

    _ts_aware = field_validator("ts")(_aware)


class GateCertificate(ContractModel):
    evidence_epoch: NonNegativeInt
    eligibility_hash: Sha256
    perspective_hashes: dict[Perspective, Sha256]
    rubric_version: Identifier
    passed: StrictBool
    reason_ref: ArtifactRef

    @model_validator(mode="after")
    def complete_perspectives(self) -> GateCertificate:
        if set(self.perspective_hashes) != set(PERSPECTIVES):
            raise ValueError("gate certificate requires all four perspective hashes")
        return self


class RepairPlan(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cycle_id: Identifier
    failed_report_revision: PositiveInt
    findings: tuple[EvalFinding, ...]
    gap_ids: tuple[Identifier, ...]
    research_targets: tuple[Perspective, ...]
    rewrite_instructions: tuple[ShortText, ...]

    @model_validator(mode="after")
    def all_targets_preserved(self) -> RepairPlan:
        _unique(self.gap_ids, "repair gap IDs")
        _unique(self.research_targets, "research targets")
        requested = {f.target_perspective for f in self.findings if f.repair_kind == RepairKind.RESEARCH}
        if set(self.research_targets) != requested:
            raise ValueError("repair plan must preserve every research target")
        return self


class TaskEnvelope(ContractModel):
    task_id: Identifier
    node: WorkerNode
    perspective: Perspective | None = None
    logical_attempt: PositiveInt
    report_revision: PositiveInt | None = None
    evidence_epoch: NonNegativeInt
    payload_ref: ArtifactRef
    input_hash: Sha256
    dispatched_at: datetime

    _dispatched_aware = field_validator("dispatched_at")(_aware)

    @model_validator(mode="after")
    def matching_worker(self) -> TaskEnvelope:
        if self.node.value in {p.value for p in PERSPECTIVES}:
            if self.perspective is None or self.perspective.value != self.node.value:
                raise ValueError("task perspective must match its worker")
        elif self.perspective is not None:
            raise ValueError("non-perspective task cannot receive perspective")
        if self.node in {WorkerNode.WRITER, WorkerNode.EVALUATOR} and self.report_revision is None:
            raise ValueError("writer/evaluator task requires report revision")
        return self


class PerspectiveSlot(ContractModel):
    status: Literal["pending", "insufficient", "sufficient", "failed"] = "pending"
    latest_result_ref: ArtifactRef | None = None
    result_hash: Sha256 | None = None
    sufficiency_ref: ArtifactRef | None = None
    gap_ids: list[Identifier] = Field(default_factory=list)
    source_count: NonNegativeInt = 0
    publisher_group_count: NonNegativeInt = 0
    answered_question_ids: list[QuestionId] = Field(default_factory=list)
    attempt: NonNegativeInt = 0

    @model_validator(mode="after")
    def slot_integrity(self) -> PerspectiveSlot:
        _unique(self.gap_ids, "slot gap IDs")
        _unique(self.answered_question_ids, "answered question IDs")
        if self.publisher_group_count > self.source_count:
            raise ValueError("publisher group count exceeds source count")
        if self.status == "sufficient" and (self.latest_result_ref is None or self.result_hash is None or self.sufficiency_ref is None):
            raise ValueError("sufficient slot requires result and sufficiency references")
        return self


class ErrorSummary(ContractModel):
    code: ErrorCode
    node: Identifier
    retriable: StrictBool
    message: ShortText


class ResultEnvelope(ContractModel):
    task_id: Identifier
    outcome: ResultOutcome
    result_ref: ArtifactRef | None = None
    error: ErrorSummary | None = None

    @model_validator(mode="after")
    def outcome_integrity(self) -> ResultEnvelope:
        if self.outcome in {"success", "empty"}:
            if self.result_ref is None or self.error is not None:
                raise ValueError("successful/empty result requires artifact and no error")
        elif self.error is None or self.result_ref is not None:
            raise ValueError("error result requires error summary and no result artifact")
        elif self.error.retriable != (self.outcome == "transient_error"):
            raise ValueError("error retry flag contradicts outcome")
        return self


class CorpusDocument(ContractModel):
    document_id: Identifier
    file_path: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    original_url: HttpUrl | None = None
    publisher: ShortText | None = None
    published_at: ISODate | Literal["unknown"] | None = None
    page_count: PositiveInt
    sha256: Sha256


class RunConfig(ContractModel):
    supervisor_model: Identifier | None = None
    research_model: Identifier | None = None
    report_model: Identifier | None = None
    judge_model: Identifier | None = None
    max_research_attempts_per_view: PositiveInt = 3
    max_eligibility_attempts: PositiveInt = 3
    max_quality_repairs: NonNegativeInt = 2
    max_graph_steps: PositiveInt = 60
    recursion_limit: PositiveInt = 100
    run_deadline_seconds: PositiveInt = 900
    tool_timeout_seconds: PositiveInt = 30
    llm_timeout_seconds: PositiveInt = 60
    max_transient_retries: NonNegativeInt = 2
    max_sources_per_run: PositiveInt = 40
    max_queries_per_view: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    top_k: PositiveInt = 5
    chunk_tokens: PositiveInt = 400
    chunk_overlap_tokens: NonNegativeInt = 60
    schema_version: Identifier = "1.0"
    corpus_version: Identifier = "1.0"
    index_version: Identifier = "1.0"
    config_version: Identifier = "1.0"
    prompt_version: Identifier = "1.0"
    policy_version: Identifier = "1.0"
    rubric_version: Identifier = "1.0"
    model_version: Identifier | None = None
    corpus_hash: Sha256 | None = None
    config_hash: Sha256 | None = None
    prompt_hash: Sha256 | None = None
    embedding_model: Identifier | None = None
    embedding_revision: Identifier | None = None
    embedding_dimension: PositiveInt | None = None

    @model_validator(mode="after")
    def overlap_bounded(self) -> RunConfig:
        if self.chunk_overlap_tokens >= self.chunk_tokens:
            raise ValueError("chunk overlap must be smaller than chunk size")
        return self


class RunInput(ContractModel):
    company_name: ShortText
    official_url: HttpUrl
    domain: Domain
    as_of_date: ISODate
    corpus_manifest: list[CorpusDocument]
    run_config: RunConfig | None = None

    @field_validator("as_of_date")
    @classmethod
    def not_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("as_of_date cannot be later than execution date")
        return value

    @model_validator(mode="after")
    def corpus_budget(self) -> RunInput:
        _unique([d.document_id for d in self.corpus_manifest], "document IDs")
        pages_by_hash: dict[str, int] = {}
        for document in self.corpus_manifest:
            previous = pages_by_hash.setdefault(document.sha256, document.page_count)
            if previous != document.page_count:
                raise ValueError("same corpus hash has inconsistent page count")
        if sum(pages_by_hash.values()) > 200:
            raise ValueError("PDF corpus exceeds 200 pages")
        return self


class EvidenceBundle(ContractModel):
    sources: list[Source]
    evidence: list[Evidence]


def deduplicate_claims(claims: list[Claim]) -> list[Claim]:
    """Merge identical approved identities; conflicting facts fail closed."""
    by_id = {}
    for claim in claims:
        if claim.claim_id in by_id and by_id[claim.claim_id] != claim:
            raise ValueError("Conflicting approved claim identities")
        by_id[claim.claim_id] = claim
    return list(by_id.values())


def validate_evidence_bundle(*, company_id: str, sources: list[Source], evidence: list[Evidence], claims: list[Claim]) -> None:
    """Check ID integrity; matching quotes and claim support need content services."""
    _unique([s.source_id for s in sources], "source IDs")
    _unique([e.evidence_id for e in evidence], "evidence IDs")
    _unique([c.claim_id for c in claims], "claim IDs")
    source_by_id = {s.source_id: s for s in sources}
    evidence_ids = {e.evidence_id for e in evidence}
    for item in evidence:
        if item.source_id not in source_by_id or item.company_id != company_id:
            raise ValueError("evidence source or company mismatch")
        source = source_by_id[item.source_id]
        is_pdf = source.content_ref.media_type == "application/pdf" or (source.original_file_id is not None and source.original_file_id.lower().endswith(".pdf"))
        if is_pdf and item.page_number is None:
            raise ValueError("PDF evidence requires an actual page number")
    for item in claims:
        if not set(item.evidence_ids) <= evidence_ids:
            raise ValueError("claim references unknown evidence")


def validate_claim_supports(*, claims: list[Claim], evidence: list[Evidence], supports: list[ClaimSupport]) -> None:
    """Reject stale verifier bindings without making a semantic support decision."""
    _unique([c.claim_id for c in claims], "claim IDs")
    _unique([e.evidence_id for e in evidence], "evidence IDs")
    _unique([s.claim_id for s in supports], "claim support IDs")
    claim_by_id = {c.claim_id: c for c in claims}
    evidence_by_id = {e.evidence_id: e for e in evidence}
    for support in supports:
        claim = claim_by_id.get(support.claim_id)
        if claim is None or support.claim_hash != canonical_hash(claim):
            raise ValueError("claim support references a missing or changed claim")
        if not set(support.evidence_hashes) <= set(claim.evidence_ids):
            raise ValueError("claim support references evidence outside the claim")
        for evidence_id, digest in support.evidence_hashes.items():
            current = evidence_by_id.get(evidence_id)
            if current is None or canonical_hash(current) != digest:
                raise ValueError("claim support references missing or changed evidence")
        if support.support_status == "supported" and claim.kind == "unknown":
            raise ValueError("unknown claim cannot count as supported")


class SufficiencyRequest(ContractModel):
    eligibility: EligibilityResult
    perspective_results: dict[Perspective, PerspectiveResult]
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claim_supports: list[ClaimSupport] = Field(default_factory=list)
    as_of_date: ISODate
    evidence_epoch: NonNegativeInt

    @model_validator(mode="after")
    def perspective_keys_match(self) -> SufficiencyRequest:
        if any(p != r.perspective for p, r in self.perspective_results.items()):
            raise ValueError("perspective key differs from result perspective")
        return self


class WriterRequest(ContractModel):
    task_id: Identifier
    company_id: Identifier
    as_of_date: ISODate
    report_revision: PositiveInt
    evidence_epoch: NonNegativeInt
    eligibility: EligibilityResult
    perspective_results: dict[Perspective, PerspectiveResult]
    gate: GateCertificate
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claim_supports: list[ClaimSupport] = Field(default_factory=list)
    rewrite_instructions: list[ShortText] = Field(default_factory=list)

    @model_validator(mode="after")
    def approved_inputs(self) -> WriterRequest:
        _validate_gate(self.gate, self.evidence_epoch, self.eligibility, self.perspective_results)
        return self


class EvaluatorRequest(ContractModel):
    task_id: Identifier
    report: DraftReport
    claims: list[Claim]
    evidence: list[Evidence]
    sources: list[Source]
    as_of_date: ISODate

    @model_validator(mode="after")
    def report_references_match(self) -> EvaluatorRequest:
        _unique([c.claim_id for c in self.claims], "claim IDs")
        _unique([e.evidence_id for e in self.evidence], "evidence IDs")
        _unique([s.source_id for s in self.sources], "source IDs")
        claims = {c.claim_id: c for c in self.claims}
        evidence = {e.evidence_id: e for e in self.evidence}
        source_ids = {s.source_id for s in self.sources}
        if not set(self.report.claim_ids) <= claims.keys():
            raise ValueError("report uses unapproved claim IDs")
        for claim in self.claims:
            if not set(claim.evidence_ids) <= evidence.keys():
                raise ValueError("claim references unknown evidence")
        for item in self.evidence:
            if item.source_id not in source_ids:
                raise ValueError("evidence references unknown source")
        for citation in self.report.citations:
            item = evidence.get(citation.evidence_id)
            if item is None or item.source_id != citation.source_id:
                raise ValueError("citation source does not own the cited evidence")
        for section in self.report.sections:
            for paragraph in section.paragraphs:
                approved_evidence = {eid for cid in paragraph.claim_ids for eid in claims[cid].evidence_ids}
                if paragraph.kind in {"fact", "inference"} and not set(paragraph.evidence_ids) <= approved_evidence:
                    raise ValueError("paragraph evidence is not linked to its approved claims")
        return self


def _validate_gate(gate: GateCertificate, epoch: int, eligibility: EligibilityResult, results: dict[Perspective, PerspectiveResult]) -> None:
    if not gate.passed or eligibility.verdict != "eligible" or gate.evidence_epoch != epoch or gate.eligibility_hash != eligibility.result_hash:
        raise ValueError("gate is not current or eligible")
    if set(results) != set(PERSPECTIVES) or gate.perspective_hashes != {p: r.result_hash for p, r in results.items()}:
        raise ValueError("gate does not match the four current perspective hashes")
    if any(p != r.perspective for p, r in results.items()):
        raise ValueError("perspective key differs from result perspective")


class PublishRequest(ContractModel):
    task_id: Identifier
    run_id: Identifier
    report: DraftReport
    evaluation: EvalVerdict
    gate: GateCertificate
    eligibility: EligibilityResult
    perspective_results: dict[Perspective, PerspectiveResult]

    @model_validator(mode="after")
    def approved_report(self) -> PublishRequest:
        _validate_gate(self.gate, self.report.evidence_epoch, self.eligibility, self.perspective_results)
        if not self.evaluation.overall_pass or (self.evaluation.report_revision, self.evaluation.report_hash, self.evaluation.evidence_epoch) != (self.report.revision, self.report.content_hash, self.report.evidence_epoch):
            raise ValueError("publication requires latest passing evaluation")
        return self


class PublishResult(ContractModel):
    report_revision: PositiveInt
    report_hash: Sha256
    evidence_epoch: NonNegativeInt
    staging_pdf_ref: ArtifactRef | None = None
    final_manifest_ref: ArtifactRef | None = None
    pdf_validation_pass: StrictBool
    page_count: Annotated[int, Field(strict=True, ge=1, le=10)]
    summary_page_fraction: Annotated[float, Field(strict=True, ge=0, le=0.5, allow_inf_nan=False)]

    @model_validator(mode="after")
    def validated_artifact(self) -> PublishResult:
        if self.staging_pdf_ref is None and self.final_manifest_ref is None:
            raise ValueError("publish result requires a staging PDF or committed manifest")
        if self.final_manifest_ref is not None and not self.pdf_validation_pass:
            raise ValueError("committed manifest requires passing PDF validation")
        return self


class Manifest(ContractModel):
    run_id: Identifier
    status: Literal["completed"]
    report_revision: PositiveInt
    report_hash: Sha256
    evidence_epoch: NonNegativeInt
    eligibility_hash: Sha256
    perspective_hashes: dict[Perspective, Sha256]
    gate_ref: ArtifactRef
    draft_ref: ArtifactRef
    evaluation_ref: ArtifactRef
    report_pdf_ref: ArtifactRef
    pdf_validation_pass: StrictBool
    trace_id: Identifier
    versions: dict[Identifier, Identifier]
    artifacts: dict[Identifier, ArtifactRef] = Field(default_factory=dict)
    committed_at: datetime

    _committed_aware = field_validator("committed_at")(_aware)

    @model_validator(mode="after")
    def complete_manifest(self) -> Manifest:
        if not self.pdf_validation_pass or set(self.perspective_hashes) != set(PERSPECTIVES):
            raise ValueError("completed manifest requires PDF validation and four perspectives")
        if self.report_pdf_ref.media_type != "application/pdf":
            raise ValueError("completed report must be a PDF artifact")
        return self


@runtime_checkable
class ArtifactStore(Protocol):
    """Verified JSON reads and media-independent artifact integrity checks."""
    async def load(self, ref: ArtifactRef) -> dict[str, Any]:
        """Verify a JSON artifact's integrity, then decode its object payload."""
        ...
    async def verify(self, ref: ArtifactRef) -> None:
        """Validate ANY media without JSON decoding (PDF, text, JSON, etc.).

        Reject missing files, unsafe paths, paths/symlinks escaping the artifact
        root, and actual file-byte SHA-256 mismatches. Successful return means
        those integrity checks passed, irrespective of the artifact's media.
        """
        ...
    async def save(self, payload: dict[str, Any], *, key: str, media_type: str = "application/json") -> ArtifactRef: ...
    async def find_receipt(self, task_id: str, input_hash: str) -> ResultEnvelope | None: ...
    async def save_receipt(self, task: TaskEnvelope, result: ResultEnvelope) -> None: ...
    async def commit_manifest(self, manifest: Manifest) -> ArtifactRef: ...
    async def find_manifest(self, run_id: str, report_revision: int) -> ArtifactRef | None:
        """Find an already committed completed manifest for this exact report key.

        Return None before atomic commit, even if a staging PDF exists. Verify
        stored manifest integrity and run_id/report_revision before returning;
        callers additionally verify current report/evaluation/gate bindings.
        """
        ...


@runtime_checkable
class EvidenceResolver(Protocol):
    async def resolve(self, evidence_ids: list[str]) -> EvidenceBundle: ...


class SearchHit(ContractModel):
    source_id: Identifier
    url: HttpUrl
    title: ShortText
    snippet: Text | None = None


class SearchResult(ContractModel):
    hits: list[SearchHit]
    outcome: Literal["success", "empty", "source_inaccessible", "error"]


class FetchedContent(ContractModel):
    source: Source
    text: Text


class RetrievalResult(ContractModel):
    sources: list[Source]
    evidence: list[Evidence]
    receipt: RetrievalReceipt


@runtime_checkable
class SearchProvider(Protocol):
    async def search(self, query: str, *, company_id: str, domain: Domain, as_of_date: date, top_k: int) -> SearchResult: ...
    async def fetch(self, hit: SearchHit) -> FetchedContent: ...


@runtime_checkable
class ResearchTools(Protocol):
    async def search(self, query: str, *, company_id: str, domain: Domain, as_of_date: date, top_k: int = 5) -> SearchResult: ...
    async def fetch(self, hit: SearchHit) -> FetchedContent: ...
    async def retrieve(self, query: str, *, company_id: str, domain: Domain, as_of_date: date, top_k: int = 5) -> RetrievalResult: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    model_id: str
    revision: str
    dimension: int
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


ResponseT = TypeVar("ResponseT", bound=BaseModel)


@runtime_checkable
class LLMClient(Protocol):
    async def generate(self, *, model: str, system_prompt: str, payload: dict[str, Any], response_model: type[ResponseT]) -> ResponseT: ...
