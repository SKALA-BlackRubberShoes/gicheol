"""Async adapter: legacy web search discovers URLs; acquired bodies supply evidence."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Literal
from urllib.parse import urlsplit

import httpx

from main.agents.supervisor.contracts import (
    Domain, FetchedContent, SearchHit, SearchResult, Source, canonical_hash,
)


class SourceInaccessibleError(RuntimeError):
    """A discovered URL did not yield an acquired document body."""


@dataclass(frozen=True)
class WebSourceMetadata:
    """Source classification is supplied by the runtime, never inferred by an LLM."""
    source_type: Literal['company', 'paper', 'institution', 'news', 'dataset']
    publisher: str | None = None
    publisher_group: str | None = None
    published_at: date | None = None
    corporate_controlled: bool = False


class _BodyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.focus = 0
        self.parts = []
        self.focus_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript', 'template'}:
            self.hidden += 1
        if tag in {'main', 'article'}:
            self.focus += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript', 'template'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'main', 'article'}:
            self.focus = max(0, self.focus - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            value = re.sub(r'\s+', ' ', data).strip()
            self.parts.append(value)
            if self.focus:
                self.focus_parts.append(value)

    def text(self):
        return '\n'.join(self.focus_parts or self.parts)


class LegacySearchProvider:
    """Wrap the repository's WebSearchBackend and fetch each URL independently.

    company aliases, taxonomy domain and metadata classifier are explicit input.
    Snippets and generated summaries are only discovery material. HTML and PDF
    bodies are saved through the existing JSON ArtifactStore contract, retaining
    PDF page numbers. Publication dates that are unknown stay null.
    """
    def __init__(self, *, backend, store, company_id: str, company_aliases,
                 domain: Domain, classify_source: Callable[[SearchHit], WebSourceMetadata],
                 client: httpx.AsyncClient | None = None, timeout_seconds=30,
                 max_bytes=10 * 1024 * 1024, initial_pdf_pages=0):
        if not company_id or not company_aliases or timeout_seconds <= 0 or max_bytes <= 0:
            raise ValueError('Invalid search provider settings')
        self.backend, self.store = backend, store
        self.company_id = company_id
        self.aliases = tuple(a.casefold() for a in company_aliases if a.strip())
        if not self.aliases:
            raise ValueError('Company aliases cannot be empty')
        self.domain = Domain(domain)
        self.classify_source = classify_source
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True)
        self._owns_client = client is None
        self.max_bytes = max_bytes
        if type(initial_pdf_pages) is not int or not 0 <= initial_pdf_pages <= 200:
            raise ValueError('Initial PDF page count must be 0..200')
        self._pdf_pages = initial_pdf_pages
        self._pdf_hashes = set()
        self._hits: dict[str, SearchHit] = {}

    def _scope(self, company_id, domain, top_k):
        if company_id != self.company_id or Domain(domain) != self.domain:
            raise ValueError('SEARCH_COMPANY_OR_DOMAIN_MISMATCH')
        if type(top_k) is not int or not 1 <= top_k <= 5:
            raise ValueError('top_k must be 1..5')

    async def search(self, query, *, company_id, domain, as_of_date, top_k=5):
        self._scope(company_id, domain, top_k)
        if not query.strip() or len(query) > 2000:
            raise ValueError('Invalid search query')
        scoped_query = f'{self.aliases[0]} {query} before or on {as_of_date.isoformat()}'
        # The legacy sync backend has its own network timeout; the runtime also
        # bounds this await. Async-native backends may be injected unchanged.
        try:
            if asyncio.iscoroutinefunction(self.backend.search):
                rows = await self.backend.search([scoped_query], max_results_per_query=top_k)
            else:
                rows = await asyncio.to_thread(self.backend.search, [scoped_query],
                                               max_results_per_query=top_k)
        except Exception:
            return SearchResult(hits=[], outcome='error')
        hits, seen = [], set()
        for row in rows:
            url = str(row.url)
            parsed = urlsplit(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username:
                continue
            # Fragments identify body positions, not independent sources.
            url = parsed._replace(fragment='').geturl()
            if url in seen:
                continue
            seen.add(url)
            hit = SearchHit(source_id='web-' + canonical_hash(url)[:24], url=url,
                            title=row.title, snippet=row.content[:20000] or None)
            self._hits[hit.source_id] = hit
            hits.append(hit)
            if len(hits) == top_k:
                break
        return SearchResult(hits=hits, outcome='success' if hits else 'empty')

    async def fetch(self, hit: SearchHit) -> FetchedContent:
        if self._hits.get(hit.source_id) != hit:
            raise ValueError('Fetch requires a hit discovered by this provider')
        try:
            async with self.client.stream('GET', str(hit.url)) as response:
                response.raise_for_status()
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > self.max_bytes:
                        raise SourceInaccessibleError('SOURCE_BODY_TOO_LARGE')
                content_type = response.headers.get('content-type', '').lower()
                resolved_url = str(response.url)
                encoding = response.encoding or 'utf-8'
        except httpx.HTTPError as exc:
            raise SourceInaccessibleError('SOURCE_INACCESSIBLE') from exc
        pages = None
        if 'pdf' in content_type or bytes(data).startswith(b'%PDF-'):
            from pypdf import PdfReader
            try:
                reader = PdfReader(io.BytesIO(data), strict=True)
                if reader.is_encrypted or len(reader.pages) > 200:
                    raise SourceInaccessibleError('PDF_ENCRYPTED_OR_OVER_BUDGET')
                digest = hashlib.sha256(data).hexdigest()
                if digest not in self._pdf_hashes:
                    if self._pdf_pages + len(reader.pages) > 200:
                        raise SourceInaccessibleError('PDF_POOL_OVER_BUDGET')
                    self._pdf_pages += len(reader.pages)
                    self._pdf_hashes.add(digest)
                pages = [{'page_number': i + 1, 'text': p.extract_text() or ''}
                         for i, p in enumerate(reader.pages)]
                text = '\n'.join(p['text'] for p in pages)
            except SourceInaccessibleError:
                raise
            except Exception as exc:
                raise SourceInaccessibleError('PDF_PARSE_FAILED') from exc
        elif 'html' in content_type:
            parser = _BodyParser()
            parser.feed(bytes(data).decode(encoding, errors='replace'))
            text = parser.text()
        elif 'text/plain' in content_type:
            text = bytes(data).decode(encoding, errors='replace').strip()
        else:
            raise SourceInaccessibleError('UNSUPPORTED_BODY_TYPE')
        if not text.strip():
            raise SourceInaccessibleError('EMPTY_SOURCE_BODY')
        if not any(alias in text.casefold() for alias in self.aliases):
            raise SourceInaccessibleError('BODY_COMPANY_MISMATCH')
        metadata = self.classify_source(hit.model_copy(update={'url': resolved_url}))
        payload = {'text': text, 'pages': pages, 'body_verified': True,
                   'company_ids': [self.company_id], 'domain': self.domain.value,
                   'url': resolved_url, 'acquisition': 'http_body',
                   'original_body_sha256': hashlib.sha256(data).hexdigest(),
                   'original_body_base64': base64.b64encode(data).decode('ascii'),
                   'content_type': content_type}
        ref = await self.store.save(payload,
                                    key=f'content/{hit.source_id}/{canonical_hash(payload)}.json')
        source = Source(source_id=hit.source_id, canonical_url=resolved_url,
                        original_file_id=('http-' + hashlib.sha256(data).hexdigest() + '.pdf')
                        if pages is not None else None,
                        title=hit.title, publisher=metadata.publisher,
                        publisher_group=metadata.publisher_group or urlsplit(resolved_url).hostname,
                        source_type=metadata.source_type, published_at=metadata.published_at,
                        retrieved_at=datetime.now(timezone.utc),
                        corporate_controlled=metadata.corporate_controlled,
                        sha256=ref.sha256, content_ref=ref)
        # FetchedContent text is a bounded actual excerpt. Full body/pages remain
        # available in content_ref; no generated summary is substituted.
        return FetchedContent(source=source, text=text[:20000])

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
