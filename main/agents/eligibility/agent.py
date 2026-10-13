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
_COORDINATING = re.compile(r"(?:,\s*)?\s+(?:and|but|while|whereas|however)\s+|\s*(?:하지만|그러나|이지만)\s*", re.I)
_STAGE_TOKEN = r"pre[- ]seed|seed|series\s+[a-z]|프리\s*시드|시드|시리즈\s*[a-z]"


class EligibilityModelError(RuntimeError):
    """The structured model response violates this worker's evidence boundary."""


def _assertion_head(quote: str) -> str:
    """Keep the status assertion separate from a coordinated product/date fact."""
    return _COORDINATING.split(quote, maxsplit=1)[0]


def _current_snapshot(assertion: str, *, as_of_date: date, published_at: date) -> bool:
    dates = set(_dates(assertion))
    # A sentence containing two different dates is insufficient to bind the
    # target status to either. Publication cannot override a historic snapshot.
    return (not dates and published_at == as_of_date) or (
        dates == {as_of_date} and bool(_CURRENT.search(assertion)))


def _funding_condition(text: str, *, current: bool, as_of_date: date, published_at: date,
                       company_name: str) -> bool | None:
    stage = re.match(r"(?:'s\s+)?(?:current|latest)\s+(?:funding\s+)?(?:stage|round)\s*(?:is|was|:|=)?\s*(" + _STAGE_TOKEN + r")\b|(?:는|은|이|가|의)?\s*(?:현재|최근|최신)\s*(?:투자\s*)?(?:단계|라운드)(?:는|가|:)?\s*(" + _STAGE_TOKEN + r")", text)
    if stage is None:
        return None
    token = (stage.group(1) or stage.group(2)).strip()
    tail = text[stage.end():]
    # An explicit current update supersedes the first status, including an old
    # round. Do not mistake a known current D for the preceding permitted C.
    name = re.escape(re.sub(r"\s+", " ", company_name.strip()).casefold())
    update = re.search(r"\b(?:but|and|however)\s+(?:(?:" + name + r"|it)\s+)?(?:is\s+(?:now|currently)\s+(?:at\s+)?(?:the\s+)?|(?:its\s+)?(?:current|latest)\s+(?:funding\s+)?(?:stage|round)\s+is\s+(?:now\s+)?)(" + _STAGE_TOKEN + r")\b|(?:하지만|그러나|이지만)\s*(?:현재(?:는)?|지금(?:은)?)\s*(?:투자\s*)?(?:단계(?:는)?\s*)?(" + _STAGE_TOKEN + r")", tail)
    if update:
        token = update.group(1) or update.group(2)
        selected = tail[update.start():]
        if not _current_snapshot(selected, as_of_date=as_of_date, published_at=published_at):
            return None
        tail = tail[update.end():]
        current = True
    if not current:
        return None
    # C/D, C or D, C以上, and C..D do not prove one admissible current stage.
    # A clearly marked historical comparison can accompany a definite current
    # stage without making that past stage the present condition.
    historic = (bool(re.search(r"\b(?:previously|formerly|used to|in the past)\b|과거|이전", tail)) and
                not re.search(r"\b(?:current|currently|latest|now)\b|현재|지금|최근", tail) and
                len(_STAGE.findall(tail)) <= 1)
    if re.search(r"\bor\b|또는|아니라|아닌|이상|이하|부터|까지|[/～~]|\b(?:or higher|and above|at least)\b", tail):
        return None
    if not historic and (_STAGE.search(tail) or re.search(r"^\s*[-–—]\s*[a-z]\b", tail)):
        return None
    letter = _STAGE.search(token)
    return (letter.group(1) or letter.group(2)) in {"a", "b", "c"} if letter else not bool(re.search(r"pre|프리", token))


def _criterion_signals(quote: str, *, current: bool, company_name: str,
                       as_of_date: date, published_at: date) -> dict[str, set[bool]]:
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
        if re.match(r"(?:has|have|had) not (?:yet )?(?:completed|undergone) (?:an? )?exit\b(?!\s+(?:announcement|news|report|plan))|(?:has|had) no completed exit\b(?!\s+(?:announcement|news|report|plan))|(?:는|은|이|가)?\s*(?:exit|엑시트)(?:이|가|을|는)?\s*(?:아직\s*)?(?:완료되지|미완료)", text):
            values["exit_status"].add(True)
    stage_condition = _funding_condition(text, current=current, as_of_date=as_of_date,
                                         published_at=published_at, company_name=company_name)
    if stage_condition is not None:
        values["funding_stage"].add(stage_condition)
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


def _quote_spans(text: str) -> list[tuple[int, int, str]]:
    # A clause must name the target itself; another company's status elsewhere
    # in the same paragraph must not be attributed to this company.
    spans, start = [], 0
    boundaries = [(match.start(), match.end()) for match in re.finditer(r"(?<=[.!?;])\s+|\r?\n", text)]
    for stop, next_start in boundaries + [(len(text), len(text))]:
        part = text[start:stop]
        left, right = len(part) - len(part.lstrip()), len(part.rstrip())
        if left < right:
            spans.append((start + left, start + right, text[start + left:start + right]))
        start = next_start
    return spans


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
                 model: str, store: c.ArtifactStore | None = None, resolver: Any | None = None,
                 timeout_seconds: float = 30):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be an explicit non-empty model identifier")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.tools, self.llm, self.domain = tools, llm, c.Domain(domain)
        self.model, self.store, self.timeout_seconds = model, store, timeout_seconds
        self.resolver = resolver
        if resolver is not None and (not callable(getattr(resolver, "record", None)) or
                                    not isinstance(getattr(resolver, "source_refs", None), dict) or
                                    not isinstance(getattr(resolver, "evidence_refs", None), dict)):
            raise ValueError("resolver must record evidence and expose source_refs/evidence_refs registries")
        self.source_refs = resolver.source_refs if resolver is not None else {}
        self.evidence_refs = resolver.evidence_refs if resolver is not None else {}
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
            if source.source_id != hit.source_id:
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
            body_text = item.text
            if source.content_ref.media_type == "application/json":
                acquired = await self.store.load(source.content_ref)
                body_companies = acquired.get("company_ids")
                if (acquired.get("body_verified") is not True or not isinstance(body_companies, list) or
                        not all(isinstance(value, str) for value in body_companies) or request.company_id not in body_companies or
                        acquired.get("domain") != self.domain.value):
                    raise ValueError("Fetched JSON artifact lacks verified company/domain body provenance")
                body_text = acquired.get("text", "")
                if not isinstance(body_text, str) or not body_text.startswith(item.text):
                    raise ValueError("Fetched excerpt does not match the original acquired body")
                if acquired.get("pages"):
                    context["notes"].append("PDF evidence requires a page-preserving retrieval adapter; unpaged fetch body is not eligibility proof")
                    continue
                if source.canonical_url != hit.url and acquired.get("url") != str(source.canonical_url):
                    raise ValueError("Redirected canonical source URL is not attested by its acquired artifact")
            elif source.canonical_url != hit.url:
                context["notes"].append("Fetched source canonical URL did not match the search hit")
                continue
            safe_id = "eligibility-source-" + c.semantic_hash(source)[:32]
            source = c.Source.model_validate({**source.model_dump(mode="json"), "source_id": safe_id})
            evidence_for_source = []
            proofs_for_source = []
            for start, end, quote in _quote_spans(body_text):
                if not _company_in_quote(request.company_name, quote):
                    continue
                dates = _dates(quote)
                if any(value > request.as_of_date for value in dates):
                    continue
                # An explicit historical status date overrides a newer source
                # publication date (for example a current retrospective).
                current = _current_snapshot(_assertion_head(quote), as_of_date=request.as_of_date,
                                            published_at=source.published_at)
                signals = _criterion_signals(quote, current=current, company_name=request.company_name,
                    as_of_date=request.as_of_date, published_at=source.published_at)
                if not any(signals.values()):
                    continue
                if len(quote) > 19500:
                    context["notes"].append("Original status assertion exceeds the bounded quote/claim size")
                    continue
                mutable_proof = bool(signals["unlisted"] or signals["funding_stage"] or True in signals["exit_status"])
                status_dates = _dates(_assertion_head(quote))
                event_date = request.as_of_date if mutable_proof else max(status_dates) if status_dates else source.published_at
                evidence_id = "eligibility-evidence-" + c.canonical_hash({"source_id": safe_id,
                    "company_id": request.company_id, "quote": quote, "event_date": event_date,
                    "start": start, "end": end})[:32]
                evidence = c.Evidence(evidence_id=evidence_id, source_id=safe_id,
                    company_id=request.company_id, chunk_id=f"{safe_id}:{start}-{end}", quote=quote, event_date=event_date)
                evidence_for_source.append(evidence)
                for criterion, values in signals.items():
                    proofs_for_source.extend((criterion, value, evidence_id) for value in sorted(values))
            if not evidence_for_source:
                continue
            # Evidence is exposed only after its source and quotes are persisted.
            if self.resolver is not None:
                await self.resolver.record([source], evidence_for_source)
                if safe_id not in self.source_refs or any(item.evidence_id not in self.evidence_refs for item in evidence_for_source):
                    raise ValueError("Resolver did not register the acquired eligibility evidence")
            else:
                self.source_refs[safe_id] = await self.store.save(source.model_dump(mode="json"), key=f"sources/{safe_id}.json")
                for evidence in evidence_for_source:
                    self.evidence_refs[evidence.evidence_id] = await self.store.save(evidence.model_dump(mode="json"), key=f"evidence/{evidence.evidence_id}.json")
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
