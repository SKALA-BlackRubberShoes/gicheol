"""Shared research tool facade and persistent evidence resolution for V2 workers."""
from __future__ import annotations

from main.agents.common.evidence import verify_neutral_quote
from main.agents.supervisor.contracts import (
    Domain, Evidence, EvidenceBundle, Source, canonical_hash,
)


class NeutralResearchTools:
    def __init__(self, *, retriever, search_provider=None):
        self.retriever, self.search_provider = retriever, search_provider

    async def retrieve(self, query, *, company_id, domain, as_of_date, top_k=5):
        return await self.retriever.retrieve(query, company_id=company_id, domain=domain,
                                             as_of_date=as_of_date, top_k=top_k)

    async def search(self, query, *, company_id, domain, as_of_date, top_k=5):
        if self.search_provider is None:
            raise RuntimeError('SEARCH_PROVIDER_NOT_CONFIGURED')
        return await self.search_provider.search(query, company_id=company_id, domain=domain,
                                                 as_of_date=as_of_date, top_k=top_k)

    async def fetch(self, hit):
        if self.search_provider is None:
            raise RuntimeError('SEARCH_PROVIDER_NOT_CONFIGURED')
        return await self.search_provider.fetch(hit)


async def verify_acquired_evidence(store, evidence: Evidence, source: Source, *, company_id: str,
                                   domain: Domain) -> None:
    """Resolve the original artifact and compare quote with the actual location."""
    await store.verify(source.content_ref)
    content = await store.load(source.content_ref)
    if company_id not in content.get('company_ids', ()) or content.get('domain') != domain.value:
        raise ValueError('SOURCE_COMPANY_OR_DOMAIN_MISMATCH')
    if not content.get('body_verified', False):
        raise ValueError('SNIPPET_IS_NOT_ACQUIRED_BODY')
    if evidence.page_number is None:
        if content.get('pages'):
            raise ValueError('PDF_EVIDENCE_REQUIRES_PAGE')
        text = content.get('text', '')
        # Web chunk IDs retain the exact acquired character span.
        try:
            start, end = map(int, evidence.chunk_id.rsplit(':', 1)[1].split('-'))
        except (ValueError, IndexError) as exc:
            raise ValueError('WEB_LOCATION_MISSING') from exc
        if start < 0 or end <= start or end > len(text) or text[start:end] != evidence.quote:
            raise ValueError('WEB_LOCATION_MISMATCH')
    else:
        pages = content.get('pages') or []
        matches = [page['text'] for page in pages if page.get('page_number') == evidence.page_number]
        if len(matches) != 1:
            raise ValueError('PDF_PAGE_MISSING')
        text = matches[0]
    verify_neutral_quote(evidence, source, text=text, company_id=company_id,
                         page_number=evidence.page_number)


class StoredEvidenceResolver:
    """Use recorded ArtifactRefs so resolve continues after runtime reconstruction.

    source_refs/evidence_refs are persisted by the runtime in its run registry.
    Do not derive unchecked filesystem paths from an evidence ID.
    """
    def __init__(self, *, store, company_id: str, domain: Domain,
                 source_refs=None, evidence_refs=None):
        self.store, self.company_id, self.domain = store, company_id, Domain(domain)
        self.source_refs = source_refs if source_refs is not None else {}
        self.evidence_refs = evidence_refs if evidence_refs is not None else {}

    async def record(self, sources, evidence):
        by_id = {source.source_id: source for source in sources}
        for item in evidence:
            if item.source_id not in by_id:
                raise ValueError('EVIDENCE_SOURCE_MISSING')
            await verify_acquired_evidence(self.store, item, by_id[item.source_id],
                                           company_id=self.company_id, domain=self.domain)
        for source in sources:
            previous = self.source_refs.get(source.source_id)
            if previous:
                old = Source.model_validate(await self.store.load(previous))
                # retrieved_at is operational; a changed body or provenance must
                # receive a new source revision rather than replace the registry.
                if canonical_hash(old.model_dump(mode='json', exclude={'retrieved_at'})) != canonical_hash(
                        source.model_dump(mode='json', exclude={'retrieved_at'})):
                    raise ValueError('SOURCE_ID_CONTENT_CHANGED')
            else:
                self.source_refs[source.source_id] = await self.store.save(
                    source.model_dump(mode='json'), key=f'sources/{source.source_id}.json')
        for item in evidence:
            old_ref = self.evidence_refs.get(item.evidence_id)
            if old_ref and Evidence.model_validate(await self.store.load(old_ref)) != item:
                raise ValueError('EVIDENCE_ID_CONTENT_CHANGED')
            if not old_ref:
                self.evidence_refs[item.evidence_id] = await self.store.save(
                    item.model_dump(mode='json'), key=f'evidence/{item.evidence_id}.json')

    async def resolve(self, evidence_ids):
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError('Duplicate evidence IDs')
        evidence, sources = [], {}
        for evidence_id in evidence_ids:
            ref = self.evidence_refs.get(evidence_id)
            if ref is None:
                raise ValueError('UNKNOWN_EVIDENCE_ID')
            item = Evidence.model_validate(await self.store.load(ref))
            if item.evidence_id != evidence_id:
                raise ValueError('EVIDENCE_ID_MISMATCH')
            source_ref = self.source_refs.get(item.source_id)
            if source_ref is None:
                raise ValueError('UNKNOWN_SOURCE_ID')
            source = Source.model_validate(await self.store.load(source_ref))
            await verify_acquired_evidence(self.store, item, source,
                                           company_id=self.company_id, domain=self.domain)
            evidence.append(item)
            sources[source.source_id] = source
        return EvidenceBundle(sources=list(sources.values()), evidence=evidence)
