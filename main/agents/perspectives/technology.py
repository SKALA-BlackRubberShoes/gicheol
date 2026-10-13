"""Neutral technology worker. It returns research, never a supported verdict."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from main.agents.supervisor.contracts import (
    Claim, Domain, Evidence, Gap, GapKind, Perspective, PerspectiveResult,
    QuestionId, ResearchRequest, RetrievalReceipt, Severity, canonical_hash,
    ErrorSummary, ResultEnvelope, TerminalReason,
)
from main.rag.neutral.retrieval import (
    ArtifactIntegrityError, CorpusDomainUnavailableError, IndexMismatchError,
)
from main.rag.neutral.search import SourceInaccessibleError
from main.rag.neutral.tools import StoredEvidenceResolver


class _TechnologyDraft(BaseModel):
    """Private extraction response; all shared data uses the canonical DTOs."""
    model_config = ConfigDict(extra='forbid')
    claims: list[Claim]
    answered_questions: dict[QuestionId, list[str]]
    gaps: list[Gap] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class TechnologyWorker:
    def __init__(self, *, tools, store, llm, model: str, company_id: str,
                 company_name: str, domain: Domain, corpus_version: str,
                 resolver=None, include_web=False, max_queries=3):
        if not model.strip() or not company_id.strip() or not company_name.strip():
            raise ValueError('Worker model and company must be configured')
        if type(max_queries) is not int or not 1 <= max_queries <= 3:
            raise ValueError('max_queries must be 1..3')
        self.tools, self.store, self.llm, self.model = tools, store, llm, model
        self.company_id, self.company_name = company_id, company_name
        self.domain, self.corpus_version = Domain(domain), corpus_version
        self.include_web, self.max_queries = include_web, max_queries
        self.resolver = resolver or StoredEvidenceResolver(
            store=store, company_id=company_id, domain=self.domain)
        self.system_prompt = (Path(__file__).parents[2] / 'prompts/technology/research.md').read_text()

    def _gap(self, request, suffix, question, *, evidence_ids=()):
        return Gap(gap_id='tech-' + canonical_hash([request.task_id, suffix])[:24],
                   perspective=Perspective.TECHNOLOGY_MATURITY,
                   kind=GapKind.MISSING_EVIDENCE, question=question,
                   severity=Severity.CRITICAL, evidence_ids=list(evidence_ids),
                   query_hints=[question])

    async def __call__(self, request: ResearchRequest) -> PerspectiveResult | ResultEnvelope:
        request = ResearchRequest.model_validate(request)
        try:
            return await self._research(request)
        except (IndexMismatchError, CorpusDomainUnavailableError, ArtifactIntegrityError) as exc:
            code = (TerminalReason.INDEX_MISMATCH if isinstance(exc, IndexMismatchError)
                    else TerminalReason.CORPUS_DOMAIN_UNAVAILABLE
                    if isinstance(exc, CorpusDomainUnavailableError)
                    else TerminalReason.ARTIFACT_INTEGRITY_ERROR)
            return ResultEnvelope(task_id=request.task_id, outcome='permanent_error',
                                  error=ErrorSummary(code=code,
                                                     node=Perspective.TECHNOLOGY_MATURITY.value,
                                                     retriable=False,
                                                     message=f'Technology retrieval failed: {code.value}'))

    async def _research(self, request: ResearchRequest) -> PerspectiveResult:
        request = ResearchRequest.model_validate(request)
        if (request.company_id != self.company_id
                or request.perspective != Perspective.TECHNOLOGY_MATURITY):
            raise ValueError('TECHNOLOGY_REQUEST_SCOPE_MISMATCH')
        if request.corpus_version != self.corpus_version:
            raise ValueError('CORPUS_VERSION_MISMATCH')
        defaults = [
            f'{self.company_name} 모델 원리 구조 학습 데이터 principle architecture training data',
            f'{self.company_name} 검증 실험 시험 조건 성공률 한계 validation protocol trials limitations',
        ]
        queries = list(dict.fromkeys(q.strip() for q in [*request.query_hints, *defaults]
                                     if q.strip()))[:self.max_queries]
        queries_hash = canonical_hash(queries)
        if request.prior_result_ref is not None:
            prior = PerspectiveResult.model_validate(await self.store.load(request.prior_result_ref))
            if prior.perspective != request.perspective:
                raise ValueError('PRIOR_RESULT_PERSPECTIVE_MISMATCH')
            if any(r.queries_hash == queries_hash for r in prior.retrieval_receipts):
                # A recorded tool failure permits retry with the identical query;
                # empty evidence alone does not justify repeating the same search.
                if not any(r.outcome in {'error', 'source_inaccessible'} for r in prior.retrieval_receipts):
                    raise ValueError('REPEATED_QUERIES_REQUIRE_NEW_HINTS')
        sources, evidence, receipts = {}, {}, []
        rag_pdf_ids = set()
        excluded = set(request.excluded_source_ids)
        for query in queries:
            result = await self.tools.retrieve(query, company_id=self.company_id,
                                               domain=self.domain, as_of_date=request.as_of_date,
                                               top_k=5)
            if (result.receipt.query_text != query
                    or result.receipt.corpus_version != request.corpus_version
                    or result.receipt.filters.get('company_id') != self.company_id
                    or result.receipt.filters.get('domain') != self.domain.value
                    or result.receipt.filters.get('as_of_date') != request.as_of_date.isoformat()):
                raise ValueError('RETRIEVAL_RECEIPT_SCOPE_MISMATCH')
            receipt = RetrievalReceipt.model_validate({**result.receipt.model_dump(),
                                                        'queries_hash': queries_hash})
            # The document retrieval receipt is retained even when empty/error;
            # a web search can never stand in for the required PDF RAG operation.
            receipts.append(receipt)
            selected_sources = {s.source_id: s for s in result.sources if s.source_id not in excluded}
            for item in result.evidence:
                if item.source_id in excluded:
                    continue
                if item.source_id not in selected_sources:
                    raise ValueError('RETRIEVAL_EVIDENCE_SOURCE_MISSING')
                source = selected_sources[item.source_id]
                if source.published_at is not None and source.published_at > request.as_of_date:
                    raise ValueError('FUTURE_SOURCE_REJECTED')
                if item.event_date is not None and item.event_date > request.as_of_date:
                    raise ValueError('FUTURE_EVIDENCE_REJECTED')
                old = evidence.get(item.evidence_id)
                if old is not None and old != item:
                    raise ValueError('CONFLICTING_EVIDENCE_ID')
                evidence[item.evidence_id] = item
                sources[source.source_id] = source
                if item.page_number is not None and result.receipt.outcome == 'success' and any(
                        h.source_id == item.source_id and h.chunk_id == item.chunk_id
                        for h in result.receipt.ranked_hits):
                    rag_pdf_ids.add(item.evidence_id)
            if self.include_web:
                await self._web_evidence(request, query, queries_hash, excluded,
                                         sources, evidence, receipts)
        await self.resolver.record(list(sources.values()), list(evidence.values()))
        for receipt in receipts:
            await self.store.save(receipt.model_dump(mode='json'),
                                  key=f'retrieval/tasks/{request.task_id}/{receipt.query_id}.json')
        limitations = ['원문과 발췌의 일치만 검사했다. 주장 지지와 근거 충분성은 Supervisor 검증 단계에서 판정한다.']
        for source in sources.values():
            content = await self.store.load(source.content_ref)
            source_limitations = content.get('provenance', {}).get('limitations') or content.get('limitations')
            if source_limitations:
                if isinstance(source_limitations, str):
                    limitations.append(source_limitations[:2000])
                else:
                    limitations.extend(str(value)[:2000] for value in source_limitations)
            if content.get('provenance', {}).get('is_derived') or content.get('is_derived'):
                limitations.append('재서술한 파생 자료가 포함되어 원출처의 주장이나 독립 검증을 직접 증명하지 않는다.')
        if any(source.published_at is None for source in sources.values()):
            limitations.append('발행일이 확인되지 않은 출처가 포함되어 기준일 당시의 최신성을 확정할 수 없다.')
        gaps = []
        if not rag_pdf_ids:
            outcomes = ', '.join(sorted({r.outcome for r in receipts}))
            gaps.append(self._gap(request, 'pdf',
                                  f'문서 RAG 검색으로 얻은 실제 PDF 근거와 일치하는 성공 receipt가 필요하다. 검색 결과: {outcomes}'))
        if not evidence:
            for question in ('tech.principle', 'tech.validation'):
                gaps.append(self._gap(request, question, f'{question}에 답할 실제 원문 근거가 필요하다.'))
            return PerspectiveResult(task_id=request.task_id, perspective=request.perspective,
                                     attempt=request.attempt, claims=[], evidence_ids=[],
                                     answered_questions={}, gaps=gaps, limitations=limitations,
                                     retrieval_receipts=receipts)
        draft = await self.llm.generate(
            model=self.model, system_prompt=self.system_prompt,
            payload={'company_id': self.company_id, 'company_name': self.company_name,
                     'as_of_date': request.as_of_date.isoformat(),
                     'instructions': request.instructions, 'gap_ids': request.gap_ids,
                     'required_questions': ['tech.principle', 'tech.validation'],
                     'sources': [s.model_dump(mode='json') for s in sources.values()],
                     'evidence': [e.model_dump(mode='json') for e in evidence.values()]},
            response_model=_TechnologyDraft)
        draft = _TechnologyDraft.model_validate(draft)
        if any(c.perspective != request.perspective or not set(c.evidence_ids) <= evidence.keys()
               for c in draft.claims):
            raise ValueError('TECHNOLOGY_CLAIM_SCOPE_OR_CITATION_MISMATCH')
        claims = {c.claim_id: c for c in draft.claims}
        for question, ids in draft.answered_questions.items():
            if question not in {QuestionId.TECH_PRINCIPLE, QuestionId.TECH_VALIDATION}:
                raise ValueError('TECHNOLOGY_QUESTION_MISMATCH')
            if any(i not in claims or claims[i].kind == 'unknown' for i in ids):
                raise ValueError('UNKNOWN_CLAIM_CANNOT_ANSWER_QUESTION')
        for question in (QuestionId.TECH_PRINCIPLE, QuestionId.TECH_VALIDATION):
            if not draft.answered_questions.get(question):
                gaps.append(self._gap(request, question.value, f'{question.value}에 답하는 근거 주장이 필요하다.'))
        return PerspectiveResult(task_id=request.task_id, perspective=request.perspective,
                                 attempt=request.attempt, claims=draft.claims,
                                 evidence_ids=list(evidence), answered_questions=draft.answered_questions,
                                 gaps=[*draft.gaps, *gaps],
                                 limitations=list(dict.fromkeys([*draft.limitations, *limitations])),
                                 retrieval_receipts=receipts)

    async def _web_evidence(self, request, query, queries_hash, excluded,
                            sources, evidence, receipts):
        result = await self.tools.search(query, company_id=self.company_id, domain=self.domain,
                                         as_of_date=request.as_of_date, top_k=5)
        outcome = result.outcome
        from main.agents.supervisor.contracts import RankedHit
        ranked = []
        inaccessible_count = 0
        for rank, hit in enumerate(result.hits, 1):
            if hit.source_id in excluded:
                continue
            try:
                fetched = await self.tools.fetch(hit)
            except SourceInaccessibleError:
                inaccessible_count += 1
                continue
            source = fetched.source
            if source.source_id != hit.source_id:
                raise ValueError('FETCHED_SOURCE_ID_MISMATCH')
            if source.published_at is not None and source.published_at > request.as_of_date:
                continue
            body = await self.store.load(source.content_ref)
            if body.get('pages'):
                pages = [p for p in body['pages'] if self.company_name.casefold() in p['text'].casefold()]
                if not pages:
                    continue
                page = pages[0]
                quote, page_number = page['text'][:20000], page['page_number']
                chunk_id = f'{source.source_id}:p{page_number}:0-{len(quote)}'
            else:
                quote, page_number = fetched.text, None
                chunk_id = f'{source.source_id}:0-{len(quote)}'
            eid = 'ev-' + canonical_hash([self.company_id, source.source_id, chunk_id, quote])[:32]
            item = Evidence(evidence_id=eid, source_id=source.source_id,
                            company_id=self.company_id, chunk_id=chunk_id,
                            page_number=page_number, quote=quote)
            evidence[eid], sources[source.source_id] = item, source
            ranked.append(RankedHit(source_id=source.source_id, chunk_id=chunk_id,
                                    rank=rank, score=0.0))
        if ranked:
            outcome = 'success'
        elif outcome == 'success':
            outcome = 'source_inaccessible' if inaccessible_count else 'empty'
        receipts.append(RetrievalReceipt(
            query_id='web-query-' + canonical_hash([request.task_id, query])[:24],
            query_text=query, queries_hash=queries_hash, provider='legacy_web_search_http_body',
            corpus_version=request.corpus_version, index_version='web-body-v1',
            filters={'company_id': self.company_id, 'domain': self.domain.value,
                     'as_of_date': request.as_of_date.isoformat(),
                     'source_inaccessible_count': str(inaccessible_count)}, ranked_hits=ranked,
            outcome=outcome, ts=datetime.now(timezone.utc)))
