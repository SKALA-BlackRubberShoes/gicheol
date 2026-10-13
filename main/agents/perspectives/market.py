"""Neutral market research and the shared, State-free research implementation.

The common implementation is here so the three perspective modules can share
boundary checks without introducing another worker or changing common modules.
The injected retrieval adapter owns PDF quote extraction. New web evidence is
usable only with an ArtifactStore; snippets never become evidence. A shared
resolver records and verifies acquired evidence before any result is exposed.
"""
from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, TypeVar
from urllib.parse import urlsplit

from pydantic import ValidationError

from main.agents.supervisor.contracts import (
    ArtifactRef, ArtifactStore, Domain, ErrorSummary, Evidence, FetchedContent, Gap, GapKind, LLMClient,
    Perspective, PerspectiveResult, RankedHit, REQUIRED_QUESTIONS,
    ResearchRequest, ResearchTools, RetrievalReceipt, RetrievalResult,
    SearchResult, Severity, Source, canonical_hash, semantic_hash, sha256_text,
)
from main.rag.neutral.search import SourceInaccessibleError


class PerspectiveModelOutputError(RuntimeError):
    """Invalid model output; graph wrappers classify this as MODEL_OUTPUT_INVALID."""


_T = TypeVar("_T")
_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
_NON_NEUTRAL = re.compile(
    r"(?:\b(?:buy|sell|invest)\s+(?:in\s+)?(?:this|the)\s+(?:company|stock)\b"
    r"|\b(?:investment|investing)\s+(?:recommendation|rating)\b"
    r"|\b(?:overall|investment|evaluation)\s+score\b"
    r"|\b(?:ranked|ranks)\s+(?:first|best|#?\d+)\b"
    r"|(?:투자|매수|매도)\s*(?:추천|권고|등급|점수)"
    r"|(?:종합|평가|투자)\s*점수\s*[:：]?\s*\d+)", re.IGNORECASE,
)


def _key(category: str, identifier: str) -> str:
    safe = identifier if len(identifier) <= 200 and re.fullmatch(r"[A-Za-z0-9_.-]+", identifier) else canonical_hash(identifier)
    return f"{category}/{safe}.json"


def _is_pdf(source: Source) -> bool:
    return source.content_ref.media_type == "application/pdf" or bool(
        source.original_file_id and source.original_file_id.lower().endswith(".pdf")
    ) or bool(source.canonical_url and urlsplit(str(source.canonical_url)).path.lower().endswith(".pdf"))


def _company_match(name: str, text: str) -> re.Match[str] | None:
    """Match a name without accepting a different company with the same prefix."""
    name = name.strip()
    pattern = re.escape(name).replace(r"\ ", r"\s+")
    boundary = r"(?=$|[^\w]|(?:는|은|이|가|의|에|를|을|와|과)(?:\s|$|[.,!?]))" if re.search(r"[가-힣]$", name) else r"(?!\w)"
    return re.search(r"(?<!\w)" + pattern + boundary, text, re.IGNORECASE)


def _http_status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


class _NeutralResearchAgent:
    perspective: Perspective
    prompt_directory: str
    question_terms: tuple[str, str]

    def __init__(self, *, tools: ResearchTools, llm: LLMClient, domain: Domain,
                 model: str, store: ArtifactStore | None = None,
                 timeout_seconds: float = 30.0, company_name: str | None = None,
                 official_url: str | None = None, company_id: str | None = None,
                 resolver: Any | None = None):
        if not isinstance(domain, Domain):
            raise ValueError("domain must be an allowed Domain")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be an explicit model identifier")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.tools, self.llm = tools, llm
        self.resolver = resolver
        self.store = store if store is not None else getattr(resolver, "store", None)
        if resolver is not None and (not callable(getattr(resolver, "record", None))
                or not isinstance(getattr(resolver, "source_refs", None), dict)
                or not isinstance(getattr(resolver, "evidence_refs", None), dict)):
            raise ValueError("resolver must record evidence and expose its persistent reference maps")
        self.source_refs: dict[str, ArtifactRef] = resolver.source_refs if resolver is not None else {}
        self.evidence_refs: dict[str, ArtifactRef] = resolver.evidence_refs if resolver is not None else {}
        self.domain, self.model = domain, model
        self.timeout_seconds = timeout_seconds
        if company_name is not None and (not isinstance(company_name, str) or not company_name.strip() or len(company_name) > 2000):
            raise ValueError("company_name must identify the bound company")
        if official_url is not None:
            parsed = urlsplit(str(official_url))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("official_url must be an absolute HTTP(S) URL")
        if company_id is not None and (not isinstance(company_id, str) or not company_id.strip()):
            raise ValueError("company_id must be a nonempty bound identity")
        self.company_name, self.official_url, self.company_id = company_name, str(official_url) if official_url is not None else None, company_id
        self.system_prompt = (_PROMPTS / self.prompt_directory / "system.md").read_text(encoding="utf-8")

    def _raise_auth(self) -> None:
        # Classify a provider rejection using the existing graph error boundary;
        # no State, other worker or graph execution is accessible to this worker.
        from main.graph.supervisor_nodes import WorkerFailure
        raise WorkerFailure(ErrorSummary(code="CONFIG_OR_AUTH_ERROR", node=self.perspective.value,
            retriable=False, message="Research provider or model authentication was rejected"))

    async def _call(self, operation: Awaitable[_T], *, auth: bool = False) -> _T:
        try:
            return await asyncio.wait_for(operation, timeout=self.timeout_seconds)
        except Exception as exc:
            if auth and (isinstance(exc, PermissionError) or _http_status(exc) in {401, 403}):
                self._raise_auth()
            raise

    def _queries(self, request: ResearchRequest) -> list[str]:
        hints = list(dict.fromkeys(request.query_hints))
        terms = hints[:3] if hints else [*self.question_terms, "independent original sources limitations conflicting evidence"]
        queries = []
        for term in terms:
            company = self.company_name or request.company_id
            website = f" official site {self.official_url}" if self.official_url else ""
            scope = f'"{company[:500]}"{website} {self.domain.value} {self.perspective.value} before {request.as_of_date.isoformat()}'
            followup = ""
            if request.attempt > 1:
                detail = " ".join([*request.gap_ids, *request.instructions])[:450]
                followup = f" additional independent evidence follow-up {request.attempt} {detail}"
            queries.append(f"{scope} {term[:850]}{followup}"[:1800])
        return list(dict.fromkeys(queries))[:3]

    def _gap(self, request: ResearchRequest, kind: GapKind, question: str,
             *, evidence_ids: list[str] | None = None, severity: Severity = Severity.CRITICAL) -> Gap:
        digest = canonical_hash({"task": request.task_id, "kind": kind, "question": question})[:24]
        return Gap(gap_id=f"{self.perspective.value}-{digest}", perspective=self.perspective,
                   kind=kind, question=question[:1900], severity=severity,
                   evidence_ids=evidence_ids or [], query_hints=[question[:1900]])

    async def _persist(self, source: Source, evidence: Evidence) -> None:
        if self.resolver is not None:
            await self._call(self.resolver.record([source], [evidence]))
            if source.source_id not in self.source_refs or evidence.evidence_id not in self.evidence_refs:
                raise ValueError("resolver did not retain the recorded artifact references")
            return
        if self.store is None:
            return
        await self._call(self.store.verify(source.content_ref))
        source_ref = self.source_refs.get(source.source_id)
        if source_ref is not None:
            old = Source.model_validate(await self._call(self.store.load(source_ref)))
            if semantic_hash(old) != semantic_hash(source):
                raise ValueError("persisted source ID has conflicting content or metadata")
        else:
            self.source_refs[source.source_id] = await self._call(self.store.save(
                source.model_dump(mode="json"), key=_key("sources", source.source_id)))
        evidence_ref = self.evidence_refs.get(evidence.evidence_id)
        if evidence_ref is not None:
            old = Evidence.model_validate(await self._call(self.store.load(evidence_ref)))
            if old != evidence:
                raise ValueError("persisted evidence ID has conflicting quote or location")
        else:
            self.evidence_refs[evidence.evidence_id] = await self._call(self.store.save(
                evidence.model_dump(mode="json"), key=_key("evidence", evidence.evidence_id)))

    async def _body(self, request: ResearchRequest, fetched: FetchedContent,
                    hit_url: Any) -> str:
        """Read the full acquired body; the fetch DTO may contain only its prefix."""
        source = fetched.source
        if source.content_ref.media_type != "application/json":
            if source.canonical_url != hit_url:
                raise ValueError("fetched source identity differs from search hit")
            if source.content_ref.media_type == "text/plain" and sha256_text(fetched.text) != source.sha256:
                raise ValueError("fetched text does not match the original body hash")
            return fetched.text
        if self.store is None:
            raise ValueError("acquired JSON body requires an ArtifactStore")
        await self._call(self.store.verify(source.content_ref))
        body = await self._call(self.store.load(source.content_ref))
        if not isinstance(body, dict):
            raise ValueError("acquired body must be a JSON object")
        company_ids = body.get("company_ids")
        if (body.get("body_verified") is not True or not isinstance(company_ids, list)
                or request.company_id not in company_ids
                or body.get("domain") != self.domain.value):
            raise ValueError("acquired body has wrong company, domain or verification status")
        if body.get("pages"):
            raise ValueError("PDF body requires page-preserving retrieval")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or not text.startswith(fetched.text):
            raise ValueError("fetched text does not match the acquired body prefix")
        canonical_url = body.get("url")
        if canonical_url is not None and str(source.canonical_url) != str(canonical_url):
            raise ValueError("acquired body URL differs from source identity")
        if source.canonical_url != hit_url and (body.get("acquisition") != "http_body"
                or canonical_url is None):
            raise ValueError("fetched source redirect lacks acquired URL provenance")
        return text

    def _accept(self, request: ResearchRequest, source: Source, item: Evidence,
                sources: dict[str, Source], evidence: dict[str, Evidence],
                limitations: list[str]) -> bool:
        if item.company_id != request.company_id or item.source_id != source.source_id:
            raise ValueError("retrieved evidence has wrong company or source")
        if _is_pdf(source) and item.page_number is None:
            raise ValueError("PDF evidence requires its actual page number")
        if source.source_id in request.excluded_source_ids:
            limitations.append(f"Excluded duplicate source {source.source_id} was not used.")
            return False
        if (source.published_at and source.published_at > request.as_of_date) or (item.event_date and item.event_date > request.as_of_date):
            limitations.append(f"Source {source.source_id} or its event is after {request.as_of_date}; it was not used.")
            return False
        if source.published_at is None and item.event_date is None:
            limitations.append(f"Source {source.source_id} has no confirmed publication or event date; its applicability at {request.as_of_date} remains unverified.")
        previous = sources.get(source.source_id)
        if previous is not None and semantic_hash(previous) != semantic_hash(source):
            raise ValueError("same source ID has conflicting content or metadata")
        previous_evidence = evidence.get(item.evidence_id)
        if previous_evidence is not None and previous_evidence != item:
            raise ValueError("same evidence ID has conflicting quotes")
        # A duplicate source/chunk cannot inflate evidence even if an adapter
        # assigns a second ID. Different quotes for one chunk fail closed.
        for existing in evidence.values():
            if (existing.source_id, existing.chunk_id) == (item.source_id, item.chunk_id):
                if existing.quote != item.quote:
                    raise ValueError("same source/chunk has conflicting quotes")
                return False
        sources.setdefault(source.source_id, source)
        evidence.setdefault(item.evidence_id, item)
        return True

    async def _retrieve(self, request: ResearchRequest, query: str,
                        sources: dict[str, Source], evidence: dict[str, Evidence],
                        limitations: list[str]) -> RetrievalReceipt:
        raw = await self._call(self.tools.retrieve(query, company_id=request.company_id,
                         domain=self.domain, as_of_date=request.as_of_date, top_k=5), auth=True)
        result = RetrievalResult.model_validate(raw.model_dump(mode="json") if isinstance(raw, RetrievalResult) else raw)
        receipt = result.receipt
        if (receipt.query_text != query or receipt.corpus_version != request.corpus_version
                or receipt.filters.get("company_id") != request.company_id
                or receipt.filters.get("domain") != self.domain.value
                or ("as_of_date" in receipt.filters and receipt.filters["as_of_date"] != request.as_of_date.isoformat())
                or len(receipt.ranked_hits) > 5 or any(h.rank > 5 for h in receipt.ranked_hits)):
            raise ValueError("retrieval receipt has wrong query, corpus, filters or top-k")
        by_source = {s.source_id: s for s in result.sources}
        if len(by_source) != len(result.sources):
            raise ValueError("duplicate retrieval source identities")
        hit_pairs = {(h.source_id, h.chunk_id) for h in receipt.ranked_hits}
        if result.evidence and receipt.outcome != "success":
            raise ValueError("unsuccessful retrieval cannot supply evidence")
        usable_pairs = set()
        for item in result.evidence:
            source = by_source.get(item.source_id)
            if source is None or (item.source_id, item.chunk_id) not in hit_pairs:
                raise ValueError("retrieved evidence lacks original source or receipt hit")
            accepted = self._accept(request, source, item, sources, evidence, limitations)
            if accepted:
                await self._persist(source, item)
            if item.evidence_id in evidence and evidence[item.evidence_id] == item and semantic_hash(sources[item.source_id]) == semantic_hash(source):
                usable_pairs.add((item.source_id, item.chunk_id))
        hits = [h for h in receipt.ranked_hits if (h.source_id, h.chunk_id) in usable_pairs]
        outcome = receipt.outcome if receipt.outcome != "success" or hits else "empty"
        if outcome in {"source_inaccessible", "error"}:
            limitations.append(f"PDF retrieval returned {outcome} for query {query}.")
        data = receipt.model_dump(mode="json")
        data.update(ranked_hits=[h.model_dump(mode="json") for h in hits], outcome=outcome)
        return RetrievalReceipt.model_validate(data)

    async def _web(self, request: ResearchRequest, query: str, index: int,
                   queries_hash: str, sources: dict[str, Source],
                   evidence: dict[str, Evidence], limitations: list[str],
                   fetched_sources: set[str]) -> RetrievalReceipt:
        raw = await self._call(self.tools.search(query, company_id=request.company_id,
                         domain=self.domain, as_of_date=request.as_of_date, top_k=5), auth=True)
        result = SearchResult.model_validate(raw.model_dump(mode="json") if isinstance(raw, SearchResult) else raw)
        if len(result.hits) > 5:
            raise ValueError("web provider exceeded top_k=5")
        if result.hits and result.outcome != "success":
            raise ValueError("unsuccessful web search returned hits")
        inaccessible = result.outcome == "source_inaccessible"
        hits = []
        for rank, hit in enumerate(result.hits, 1):
            if hit.source_id in request.excluded_source_ids or hit.source_id in fetched_sources:
                continue
            fetched_sources.add(hit.source_id)
            try:
                content = await self._call(self.tools.fetch(hit))
            except Exception as exc:
                if isinstance(exc, (SourceInaccessibleError, PermissionError, FileNotFoundError)) or _http_status(exc) == 403:
                    inaccessible = True
                    limitations.append(f"The body of source {hit.source_id} was inaccessible; its snippet was not used.")
                    continue
                if _http_status(exc) == 401:
                    self._raise_auth()
                raise
            fetched = FetchedContent.model_validate(content.model_dump(mode="json") if isinstance(content, FetchedContent) else content)
            source = fetched.source
            if source.source_id != hit.source_id:
                raise ValueError("fetched source identity differs from search hit")
            if _is_pdf(source):
                limitations.append(f"PDF source {source.source_id} needs page-preserving retrieval; web extraction was not used.")
                continue
            if self.store is None:
                limitations.append("New web evidence requires an injected ArtifactStore; fetched bodies were not cited without persistent evidence records.")
                continue
            body = await self._body(request, fetched, hit.url)
            company_match = _company_match(self.company_name, body) if self.company_name else None
            if self.company_name and company_match is None:
                limitations.append(f"Source {source.source_id} does not identify the bound company {self.company_name}; its body was not attributed to that company.")
                continue
            # The quotation is a contiguous excerpt of the real body. The model
            # receives only these pre-existing evidence IDs and cannot add one.
            excerpt_start = max(0, company_match.start() - 300) if company_match else 0
            excerpt = body[excerpt_start:excerpt_start + 2000]
            start = excerpt_start + len(excerpt) - len(excerpt.lstrip())
            end = excerpt_start + len(excerpt.rstrip())
            quote = body[start:end]
            if not quote or quote != excerpt.strip():
                raise ValueError("web evidence quote is not an exact body excerpt")
            if self.company_name and _company_match(self.company_name, quote) is None:
                limitations.append(f"Source {source.source_id} did not yield an exact company-identifying excerpt; it was not cited.")
                continue
            prefix = source.source_id if len(source.source_id) <= 200 else "web-" + canonical_hash(source.source_id)[:32]
            chunk = f"{prefix}:{start}-{end}"
            eid = "web-" + canonical_hash({"source": source.source_id, "company": request.company_id,
                                        "chunk": chunk, "quote": quote})[:24]
            item = Evidence(evidence_id=eid, source_id=source.source_id,
                            company_id=request.company_id, chunk_id=chunk,
                            quote=quote, event_date=source.published_at)
            if self._accept(request, source, item, sources, evidence, limitations):
                await self._persist(source, item)
                hits.append(RankedHit(source_id=source.source_id, chunk_id=chunk, rank=rank, score=0.0))
        outcome = "success" if hits else "source_inaccessible" if inaccessible else "error" if result.outcome == "error" else "empty"
        if result.outcome == "error":
            limitations.append(f"Web search failed for query {query}; failure is not absence of evidence.")
        return RetrievalReceipt(query_id=f"{canonical_hash(request.task_id)[:20]}-web-{index}",
            query_text=query, queries_hash=queries_hash, provider="web-body",
            corpus_version=request.corpus_version, index_version="web-body-v1",
            filters={"company_id": request.company_id, "domain": self.domain.value,
                     "as_of_date": request.as_of_date.isoformat()}, ranked_hits=hits,
            outcome=outcome, ts=datetime.now(timezone.utc))

    def _validate_model(self, raw: Any, request: ResearchRequest,
                        evidence: dict[str, Evidence], sources: dict[str, Source]) -> PerspectiveResult:
        result = PerspectiveResult.model_validate(raw.model_dump(mode="json") if isinstance(raw, PerspectiveResult) else raw)
        if (result.task_id, result.perspective, result.attempt) != (request.task_id, self.perspective, request.attempt):
            raise PerspectiveModelOutputError("model result has stale task, perspective or attempt")
        if set(result.evidence_ids) - evidence.keys():
            raise PerspectiveModelOutputError("model fabricated an evidence ID")
        expressions = [*result.limitations, *(g.question for g in result.gaps), *(text for c in result.claims for text in [c.text, *c.limitations])]
        if any(_NON_NEUTRAL.search(text) for text in expressions):
            raise PerspectiveModelOutputError("research output contains an investment recommendation, score or rank")
        for claim in result.claims:
            if claim.kind == "fact" and any(sources[evidence[eid].source_id].published_at is None and evidence[eid].event_date is None for eid in claim.evidence_ids):
                raise PerspectiveModelOutputError("undated evidence cannot establish a fact at the assessment date")
            if claim.kind in {"inference", "unknown"} and not claim.limitations:
                raise PerspectiveModelOutputError("inferences and unknowns must state conditions or search limitations")
        for gap in result.gaps:
            if set(gap.evidence_ids) - evidence.keys():
                raise PerspectiveModelOutputError("gap fabricated an evidence ID")
        return result

    async def __call__(self, request: ResearchRequest) -> PerspectiveResult:
        request = ResearchRequest.model_validate(request.model_dump(mode="json"))
        if request.perspective != self.perspective:
            raise ValueError("ResearchRequest perspective differs from worker")
        if self.company_id is not None and self.company_id != request.company_id:
            raise ValueError("ResearchRequest company differs from the bound worker")
        prior = None
        if request.prior_result_ref is not None:
            if self.store is None:
                raise ValueError("a prior result reference requires an ArtifactStore")
            prior = PerspectiveResult.model_validate(await self._call(self.store.load(request.prior_result_ref)))
            if prior.perspective != self.perspective or prior.attempt >= request.attempt:
                raise ValueError("prior result has wrong perspective or stale attempt ordering")
        queries = self._queries(request)
        sources: dict[str, Source] = {}
        evidence: dict[str, Evidence] = {}
        limitations: list[str] = []
        receipts = []
        fetched_sources: set[str] = set()
        queries_hash = canonical_hash(queries)
        for index, query in enumerate(queries, 1):
            receipts.append(await self._retrieve(request, query, sources, evidence, limitations))
            receipts.append(await self._web(request, query, index, queries_hash, sources, evidence, limitations, fetched_sources))
        payload = {
            "request": request.model_dump(mode="json"), "domain": self.domain.value,
            "company_name": self.company_name, "official_url": self.official_url,
            "required_questions": [q.value for q in REQUIRED_QUESTIONS[self.perspective]],
            "queries": queries, "queries_hash": queries_hash,
            "sources": [s.model_dump(mode="json") for s in sources.values()],
            "evidence": [e.model_dump(mode="json") for e in evidence.values()],
            "retrieval_receipts": [r.model_dump(mode="json") for r in receipts],
            "retrieval_limitations": list(dict.fromkeys(limitations)),
            "prior_result": prior.model_dump(mode="json") if prior is not None else None,
            "support_status": "unverified; independent sufficiency assessor must verify every claim",
        }
        if not evidence:
            result = PerspectiveResult(task_id=request.task_id, perspective=self.perspective,
                attempt=request.attempt, claims=[], evidence_ids=[], answered_questions={},
                gaps=[], limitations=[], retrieval_receipts=[])
        else:
            for correction in range(2):
                try:
                    raw = await self._call(self.llm.generate(model=self.model, system_prompt=self.system_prompt,
                                                           payload=payload, response_model=PerspectiveResult), auth=True)
                    result = self._validate_model(raw, request, evidence, sources)
                    break
                except (ValidationError, PerspectiveModelOutputError, TypeError, ValueError) as exc:
                    if correction:
                        raise PerspectiveModelOutputError("model output remained invalid after one structured correction") from exc
                    payload = {**payload, "correction": "The previous output violated DTO/binding/citation/neutrality requirements. Return only a valid PerspectiveResult for this exact request and the supplied evidence IDs."}
        gaps = list(result.gaps)
        for question in REQUIRED_QUESTIONS[self.perspective]:
            answers = result.answered_questions.get(question, [])
            usable_claims = [c for c in result.claims if c.claim_id in answers and c.kind != "unknown"]
            if not usable_claims:
                gaps.append(self._gap(request, GapKind.MISSING_QUESTION, f"Answer fixed question {question.value} with original, date-valid evidence."))
        used_ids = {eid for c in result.claims if c.kind != "unknown" for eid in c.evidence_ids}
        used_sources = {evidence[eid].source_id for eid in used_ids}
        if not used_ids:
            outcomes = sorted({r.outcome for r in receipts})
            gaps.append(self._gap(request, GapKind.MISSING_EVIDENCE, f"No usable factual evidence was established; actual search outcomes: {', '.join(outcomes)}. Obtain accessible original bodies."))
        groups = {sources[sid].publisher_group for sid in used_sources}
        if len(groups) < 2 or not any(not sources[sid].corporate_controlled for sid in used_sources):
            gaps.append(self._gap(request, GapKind.SOURCE_BIAS, "Obtain two distinct publisher groups, including a source not controlled by the company.", evidence_ids=sorted(used_ids)))
        limitations.extend(result.limitations)
        limitations.append(f"Research is limited to {len(queries)} distinct queries with top_k=5 per provider, for {request.company_id} as of {request.as_of_date}; every claim remains unverified until independent sufficiency assessment.")
        if not any(g.kind == GapKind.CONFLICTING_EVIDENCE for g in gaps):
            limitations.append("No conflicting evidence was identified in the searched material; this does not establish that conflicting evidence is absent.")
        # Empty/unknown answers do not count toward the fixed-question gate.
        answered = {q: ids for q, ids in result.answered_questions.items()
                    if any(c.claim_id in ids and c.kind != "unknown" for c in result.claims)}
        return PerspectiveResult(task_id=request.task_id, perspective=self.perspective,
            attempt=request.attempt, claims=result.claims, evidence_ids=list(evidence),
            answered_questions=answered, gaps=list({g.gap_id: g for g in gaps}.values()),
            limitations=list(dict.fromkeys(limitations)), retrieval_receipts=receipts)


class MarketAgent(_NeutralResearchAgent):
    perspective = Perspective.MARKET
    prompt_directory = "market"
    question_terms = ("customer demand market definition adoption evidence", "revenue business model competition market entry pricing dated metrics")
