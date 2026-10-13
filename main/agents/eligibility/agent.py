"""Conservative, source-backed eligibility worker for the SDD async boundary.

The shared Claim contract has no eligibility perspective. Eligibility facts use
``stakeholders`` and an explicit ``eligibility.<criterion>`` prefix; they are
never answers to stakeholders research questions. A real run supplies a store
and a resolver for sources/{source_id}.json and evidence/{evidence_id}.json.
"""
from __future__ import annotations

import asyncio
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

from main.agents.supervisor import contracts as c


CRITERIA = ("unlisted", "funding_stage", "exit_status")
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "eligibility" / "eligibility.md"
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_STAGE = re.compile(r"\bseries\s+([a-z])\b|시리즈\s*([a-z])", re.I)
_CURRENT = re.compile(r"\bas\s+of\b|\bcurrently\b|\bcurrent\b|\blatest\b|기준|현재|최근", re.I)


class EligibilityModelError(RuntimeError):
    """The structured model response violates this worker's evidence boundary."""


def _criterion_signals(quote: str, *, current: bool, company_name: str) -> dict[str, set[bool]]:
    """Recognize explicit status statements; silence is never negative proof.

    This deliberately accepts a narrower set than arbitrary model prose. The
    model cannot turn an old funding announcement or missing exit news into an
    affirmative current-status fact. Completed acquisition is irreversible.
    """
    text = quote.casefold()
    values = {key: set() for key in CRITERIA}
    name = re.escape(re.sub(r"\s+", " ", company_name.strip()).casefold())
    text = re.sub(r"\s+", " ", text)
    if text.rstrip().endswith("?") or re.search(r"\b(?:unclear|unverified|unknown|unconfirmed|may|might|possibly|rumored|suspected|could|expected|projected|planned|if|unless|conditional|proposed)\b|subject to|cannot confirm|확인되지|불확실|추정|조건부|승인되면|예정", text):
        return values
    match = re.search(r"(?<!\w)" + name + r"(?:\s+|(?=는|은|이|가|의)|(?='s\s))", text)
    if match is None:
        return values
    # Status must follow the named target, rather than its rival or partner.
    text = text[match.end():]
    if current:
        if re.match(r"(?:is|remains)\s+(?:currently\s+)?(?:an?\s+)?(?:unlisted|privately[- ]held|not (?:publicly )?(?:listed|traded))\b|(?:는|은|이|가)?\s*(?:현재\s*)?비상장(?:\s|기업|회사|이다|상태)", text):
            values["unlisted"].add(True)
        if re.match(r"(?:is|remains)\s+(?:currently\s+)?(?:an?\s+)?(?:publicly (?:listed|traded)|listed on|not unlisted)\b|(?:는|은|이|가)?\s*(?:현재\s*)?상장\s*(?:기업|회사)|(?:는|은|이|가)?\s*상장(?:을|이)?\s*완료", text):
            values["unlisted"].add(False)
        # A funding stage must describe the current/latest stage, not a round
        # that the company once raised or intends to raise in the future.
        stage = re.match(r"(?:'s\s+)?(?:current|latest)\s+(?:funding\s+)?(?:stage|round)\s*(?:is|was|:|=)?\s*(pre[- ]seed|seed|series\s+[a-z])\b|(?:는|은|이|가|의)?\s*(?:현재|최근|최신)\s*(?:투자\s*)?(?:단계|라운드)(?:는|가|:)?\s*(프리\s*시드|시드|시리즈\s*[a-z])", text)
        if stage:
            token = (stage.group(1) or stage.group(2)).strip()
            letter = _STAGE.search(token)
            if not re.search(r"\bor\b|또는|아니라|아닌", text[stage.end():]):
                values["funding_stage"].add((letter.group(1) or letter.group(2)) in {"a", "b", "c"} if letter else not bool(re.search(r"pre|프리", token)))
        if re.match(r"(?:has|have|had) not (?:yet )?(?:completed|undergone) (?:an? )?exit\b(?!\s+(?:announcement|news|report|plan))|(?:has|had) no completed exit\b(?!\s+(?:announcement|news|report|plan))|(?:는|은|이|가)?\s*(?:exit|엑시트)(?:이|가|을|는)?\s*(?:아직\s*)?(?:완료되지|미완료)", text):
            values["exit_status"].add(True)
    # Negation, proposed transactions, and closing conditions must not be read
    # as a completed exit. Dates and company identity are checked separately.
    completed = re.match(r"(?:has|had) been acquired\b|(?:was|is) acquired\b|(?:completed|closed) (?:its |the |an? )?(?:merger|exit|m&a)\b|(?:'s\s+)?(?:merger|exit|m&a) (?:has |was |is )?(?:closed|completed)\b|(?:'s\s+)?acquisition by .{1,100}\b(?:closed|completed)\b|(?:는|은|이|가)?\s*.{0,40}인수되었|(?:는|은|이|가)?\s*(?:인수합병|엑시트|exit|m&a)(?:이|가|을|는)?\s*완료(?:되었|됐다|됨|하였다|했다|\s|$)", text)
    blocked = re.search(r"\b(?:not|never|pending|proposed|expected|will|intends|plans|would|subject to)\b|미완료|예정|추진|완료되지", text)
    if completed and not blocked:
        values["exit_status"].add(False)
    return values


def _company_in_quote(name: str, quote: str) -> bool:
    normalized = re.sub(r"\s+", " ", name.strip()).casefold()
    text = re.sub(r"\s+", " ", quote).casefold()
    # Word boundaries prevent a request for Acme from matching AcmeOther.
    boundary = r"(?=$|[^\w]|(?:는|은|이|가|의|에|를|을|와|과)(?:\s|$))" if re.search(r"[가-힣]$", normalized) else r"(?!\w)"
    return re.search(r"(?<!\w)" + re.escape(normalized) + boundary, text) is not None


def _quotes(text: str) -> list[str]:
    # A clause must name the target itself; another company's status elsewhere
    # in the same paragraph must not be attributed to this company.
    return list(dict.fromkeys(part.strip() for part in re.split(r"(?<=[.!?;])\s+|\r?\n", text) if part.strip()))


def _dates(quote: str) -> list[date]:
    result = []
    for value in _DATE.findall(quote):
        try:
            result.append(date.fromisoformat(value))
        except ValueError:
            continue
    return result


def _auth_failure(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "status_code", None) in {401, 403}


def _raise_auth_failure() -> None:
    # The graph's classified-error type is imported only for this failure path.
    # Normal worker imports and deterministic tests need no graph dependency.
    from main.graph.supervisor_nodes import WorkerFailure
    raise WorkerFailure(c.ErrorSummary(code="CONFIG_OR_AUTH_ERROR", node="eligibility", retriable=False,
                                       message="Eligibility research authentication was rejected"))


class EligibilityAgent:
    """Verify all three target conditions using original, dated source bodies.

    Mutable conditions require a status snapshot on the assessment date: an
    explicit as-of statement or a source published on that date. Historical
    funding rounds and absence of public exit announcements stay unknown.
    ``timeout_seconds`` bounds the whole call, including search, model, storage.
    """

    def __init__(self, *, tools: c.ResearchTools, llm: c.LLMClient, domain: c.Domain,
                 model: str, store: c.ArtifactStore | None = None, timeout_seconds: float = 30):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be an explicit non-empty model identifier")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.tools, self.llm, self.domain = tools, llm, c.Domain(domain)
        self.model, self.store, self.timeout_seconds = model, store, timeout_seconds
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    async def __call__(self, request: c.EligibilityRequest) -> c.EligibilityResult:
        request = c.EligibilityRequest.model_validate(request.model_dump(mode="json"))
        context: dict[str, Any] = {"sources": {}, "evidence": {}, "proofs": {key: [] for key in CRITERIA}, "notes": [], "model_validated": False}
        try:
            await asyncio.wait_for(self._run(request, context), timeout=self.timeout_seconds)
        except TimeoutError:
            # Persisted proofs collected before a model timeout remain usable.
            context["notes"].append("Eligibility collection exceeded its timeout; unproven conditions remain unknown")
        return self._compose(request, context)

    async def _run(self, request: c.EligibilityRequest, context: dict[str, Any]) -> None:
        followup = " ".join(request.instructions)
        queries = [f'{request.company_name} {criterion} status as of {request.as_of_date.isoformat()} original source {followup}'.strip()
                   for criterion in CRITERIA]
        searches = await asyncio.gather(*(self.tools.search(query, company_id=request.company_id,
            domain=self.domain, as_of_date=request.as_of_date, top_k=5) for query in queries), return_exceptions=True)
        hits = {}
        for raw in searches:
            if isinstance(raw, BaseException):
                if isinstance(raw, asyncio.CancelledError):
                    raise raw
                if _auth_failure(raw):
                    _raise_auth_failure()
                # Already classified runtime failures retain their classification.
                if getattr(raw, "error", None) is not None:
                    raise raw
                context["notes"].append("Original-source search failed; no condition inferred from the failure")
                continue
            result = c.SearchResult.model_validate(raw.model_dump(mode="json") if isinstance(raw, c.SearchResult) else raw)
            if len(result.hits) > 5 or (result.outcome == "success") != bool(result.hits):
                raise ValueError("Search outcome or hit count contradicts the requested top_k")
            if result.outcome != "success":
                context["notes"].append(f"Original-source search outcome: {result.outcome}")
                continue
            for hit in result.hits:
                if str(hit.url) in hits and hits[str(hit.url)] != hit:
                    raise ValueError("Search returned conflicting metadata for the same source URL")
                hits.setdefault(str(hit.url), hit)
        fetched = await asyncio.gather(*(self.tools.fetch(hit) for hit in hits.values()), return_exceptions=True)
        for hit, raw in zip(hits.values(), fetched):
            if isinstance(raw, BaseException):
                if isinstance(raw, asyncio.CancelledError):
                    raise raw
                if _auth_failure(raw):
                    _raise_auth_failure()
                if getattr(raw, "error", None) is not None:
                    raise raw
                context["notes"].append("Original source inaccessible; search snippets are not evidence")
                continue
            item = c.FetchedContent.model_validate(raw.model_dump(mode="json") if isinstance(raw, c.FetchedContent) else raw)
            source = item.source
            if source.source_id != hit.source_id or source.canonical_url != hit.url:
                context["notes"].append("Fetched source identity did not match the search hit")
                continue
            if source.published_at is None or source.published_at > request.as_of_date:
                context["notes"].append("Source publication date absent or later than the assessment date")
                continue
            is_pdf = source.content_ref.media_type == "application/pdf" or (source.original_file_id or "").lower().endswith(".pdf") or str(source.canonical_url).split("?", 1)[0].lower().endswith(".pdf")
            if is_pdf:
                context["notes"].append("PDF evidence requires a page-preserving retrieval adapter; unpaged fetch body is not eligibility proof")
                continue
            if source.content_ref.media_type == "text/plain":
                if c.sha256_text(item.text) != source.sha256:
                    raise ValueError("Fetched source body hash does not match its content reference")
            if self.store is None:
                context["notes"].append("An artifact store is required to persist verifiable eligibility evidence for EvidenceResolver")
                continue
            await self.store.verify(source.content_ref)
            safe_id = "eligibility-source-" + c.semantic_hash(source)[:32]
            source = c.Source.model_validate({**source.model_dump(mode="json"), "source_id": safe_id})
            evidence_for_source = []
            proofs_for_source = []
            for index, quote in enumerate(_quotes(item.text)):
                if not _company_in_quote(request.company_name, quote):
                    continue
                dates = _dates(quote)
                if any(value > request.as_of_date for value in dates):
                    continue
                # An explicit historical status date overrides a newer source
                # publication date (for example a current retrospective).
                current = (not dates and source.published_at == request.as_of_date) or (
                    request.as_of_date in dates and bool(_CURRENT.search(quote)))
                signals = _criterion_signals(quote, current=current, company_name=request.company_name)
                if not any(signals.values()):
                    continue
                event_date = max(dates) if dates else source.published_at
                evidence_id = "eligibility-evidence-" + c.canonical_hash({"source_id": safe_id,
                    "company_id": request.company_id, "quote": quote, "event_date": event_date})[:32]
                evidence = c.Evidence(evidence_id=evidence_id, source_id=safe_id,
                    company_id=request.company_id, chunk_id=f"paragraph-{index + 1}", quote=quote, event_date=event_date)
                evidence_for_source.append(evidence)
                for criterion, values in signals.items():
                    proofs_for_source.extend((criterion, value, evidence_id) for value in sorted(values))
            if not evidence_for_source:
                continue
            # Evidence is exposed only after its source and quotes are persisted.
            if self.store is not None:
                await self.store.save(source.model_dump(mode="json"), key=f"sources/{safe_id}.json")
                for evidence in evidence_for_source:
                    await self.store.save(evidence.model_dump(mode="json"), key=f"evidence/{evidence.evidence_id}.json")
            context["sources"][safe_id] = source
            context["evidence"].update({item.evidence_id: item for item in evidence_for_source})
            for criterion, value, evidence_id in proofs_for_source:
                context["proofs"][criterion].append((value, evidence_id))
        if not context["evidence"]:
            return
        payload = {**request.model_dump(mode="json"), "domain": self.domain.value, "criteria": list(CRITERIA),
                   "sources": [s.model_dump(mode="json") for s in context["sources"].values()],
                   "evidence": [e.model_dump(mode="json") for e in context["evidence"].values()]}
        raw = await self.llm.generate(model=self.model, system_prompt=self.system_prompt,
                                      payload=payload, response_model=c.EligibilityResult)
        result = c.EligibilityResult.model_validate(raw.model_dump(mode="json") if isinstance(raw, c.EligibilityResult) else raw)
        if result.task_id != request.task_id:
            raise EligibilityModelError("Eligibility model returned a different task_id")
        for claim in result.claims:
            if claim.perspective != c.Perspective.STAKEHOLDERS or not set(claim.evidence_ids) <= context["evidence"].keys():
                raise EligibilityModelError("Eligibility model returned an unrelated perspective or evidence ID")
            if claim.kind != "unknown" and not any(f"eligibility.{key}" in claim.text for key in CRITERIA):
                raise EligibilityModelError("Eligibility model did not preserve the eligibility criterion")
            for criterion in CRITERIA:
                if claim.kind != "unknown" and f"eligibility.{criterion}" in claim.text and not any(
                    evidence_id in claim.evidence_ids for _, evidence_id in context["proofs"][criterion]
                ):
                    raise EligibilityModelError("Eligibility model cited evidence for a different criterion")
        for gap in result.gaps:
            if gap.perspective != "eligibility" or not set(gap.evidence_ids) <= context["evidence"].keys():
                raise EligibilityModelError("Eligibility model returned an unrelated gap")
        context["model_validated"] = True
        # Model statuses/prose are intentionally not trusted as independent facts.
        # _compose derives each status and claim from admissible original quotes.

    def _compose(self, request: c.EligibilityRequest, context: dict[str, Any]) -> c.EligibilityResult:
        conditions, claims, gaps = {}, [], []
        questions = {
            "unlisted": "Obtain an affirmative dated statement that the company is unlisted",
            "funding_stage": "Confirm the latest funding stage is Seed through Series C on the assessment date",
            "exit_status": "Obtain affirmative dated proof that no M&A or other exit has completed",
        }
        for criterion in CRITERIA:
            proofs = sorted(set(context["proofs"][criterion]), key=lambda proof: (proof[0], proof[1]))
            values = {value for value, _ in proofs}
            conditions[criterion] = False if False in values else True if True in values and context["model_validated"] else "unknown"
            if conditions[criterion] == "unknown":
                claims.append(c.Claim(claim_id="eligibility-claim-" + c.canonical_hash({"task": request.task_id,
                    "criterion": criterion, "status": "unknown"})[:32], perspective="stakeholders",
                    text=f"eligibility.{criterion}=unknown; as_of_date={request.as_of_date.isoformat()}; no affirmative original-source proof",
                    kind="unknown", evidence_ids=[], limitations=list(dict.fromkeys(context["notes"]))[:8]))
                question = f"{questions[criterion]}: {request.company_name}, as_of_date={request.as_of_date.isoformat()}"
                gaps.append(c.Gap(gap_id="eligibility-gap-" + c.canonical_hash({"task": request.task_id,
                    "criterion": criterion})[:32], perspective="eligibility", kind="eligibility_unknown",
                    question=question, severity="critical", query_hints=[question]))
            for value, evidence_id in proofs:
                item = context["evidence"][evidence_id]
                text = (f"eligibility.{criterion}={str(value).lower()}; as_of_date={request.as_of_date.isoformat()}; "
                        f"proof_date={item.event_date.isoformat()}; source statement: {item.quote}")
                # Prefix overhead must not exceed the common Text boundary.
                if len(text) > 20000:
                    raise ValueError("Eligibility proof exceeds the Claim text boundary")
                claims.append(c.Claim(claim_id="eligibility-claim-" + c.canonical_hash({"task": request.task_id,
                    "criterion": criterion, "value": value, "evidence": evidence_id})[:32], perspective="stakeholders",
                    text=text, kind="fact", evidence_ids=[evidence_id],
                    limitations=["Eligibility fact stored with stakeholders perspective; not a stakeholders research answer"] +
                                list(dict.fromkeys(context["notes"]))[:7]))
            if len(values) > 1:
                gaps.append(c.Gap(gap_id="eligibility-conflict-" + c.canonical_hash({"task": request.task_id,
                    "criterion": criterion})[:32], perspective="eligibility", kind="conflicting_evidence",
                    question=f"Reconcile contradictory original-source proof for eligibility.{criterion} on {request.as_of_date.isoformat()}",
                    severity="critical", evidence_ids=[evidence_id for _, evidence_id in proofs]))
        verdict = "ineligible" if False in conditions.values() else "unknown" if "unknown" in conditions.values() else "eligible"
        return c.EligibilityResult(task_id=request.task_id, **conditions, verdict=verdict, claims=claims, gaps=gaps)
