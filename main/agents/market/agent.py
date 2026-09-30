"""회사별 시장 근거 수집과 평가 흐름을 조립합니다."""

from __future__ import annotations

from main.rag.company import BaseRAG
from main.rag.market import MarketRAG, MarketSearchHit
from .schemas import (
    EvidenceSource,
    MarketEvaluationBackend,
    MarketEvaluationDraft,
    MarketEvaluationError,
    MarketEvaluationResult,
    WebSearchResult,
)
from .scoring import CRITERION_ORDER, calculate_market_score, scores_from_results
from .web_search import WebSearchBackend, WebSearchError


class MarketEvaluationAgent:
    """BaseRAG, MarketRAG, 웹 검색과 LLM을 한 번의 기업 평가로 조립합니다."""

    def __init__(
        self,
        *,
        base_rag: BaseRAG,
        market_rag: MarketRAG,
        web_search: WebSearchBackend,
        evaluation_backend: MarketEvaluationBackend,
        pdf_results_per_query: int = 3,
        max_pdf_evidence: int = 20,
        max_web_evidence: int = 20,
    ):
        if any(
            type(value) is not int or value < 1
            for value in (pdf_results_per_query, max_pdf_evidence, max_web_evidence)
        ):
            raise ValueError("Evidence limits must be positive integers")
        self.base_rag = base_rag
        self.market_rag = market_rag
        self.web_search = web_search
        self.evaluation_backend = evaluation_backend
        self.pdf_results_per_query = pdf_results_per_query
        self.max_pdf_evidence = max_pdf_evidence
        self.max_web_evidence = max_web_evidence

    def _collect_pdf_evidence(self, queries: list[str]) -> list[EvidenceSource]:
        """여러 검색어의 PDF 결과를 청크 ID 기준으로 중복 제거합니다."""

        unique: dict[str, MarketSearchHit] = {}
        for query in queries:
            for hit in self.market_rag.retrieve(
                query,
                n_results=self.pdf_results_per_query,
            ):
                unique.setdefault(hit.chunk.chunk_id, hit)

        evidence: list[EvidenceSource] = []
        for index, hit in enumerate(
            list(unique.values())[: self.max_pdf_evidence], start=1
        ):
            chunk = hit.chunk
            evidence.append(
                EvidenceSource(
                    source_id=f"P{index:03d}",
                    source_type="pdf",
                    title=chunk.title,
                    publisher=chunk.publisher,
                    url=chunk.source_url,
                    page=chunk.page,
                    published_at=str(chunk.published_year),
                    excerpt=chunk.text[:1_500],
                )
            )
        return evidence

    def _collect_web_evidence(self, queries: list[str]) -> list[EvidenceSource]:
        """웹 결과를 URL 기준으로 중복 제거하고 공통 근거 형식으로 바꿉니다."""

        results: list[WebSearchResult] = self.web_search.search(queries)
        unique: dict[str, WebSearchResult] = {}
        for result in results:
            unique.setdefault(result.url, result)

        evidence: list[EvidenceSource] = []
        for index, result in enumerate(
            list(unique.values())[: self.max_web_evidence], start=1
        ):
            evidence.append(
                EvidenceSource(
                    source_id=f"W{index:03d}",
                    source_type="web",
                    title=result.title,
                    publisher=result.publisher,
                    url=result.url,
                    page=None,
                    published_at=result.published_at,
                    excerpt=result.content[:1_500],
                    content_kind=result.content_kind,
                )
            )
        return evidence

    @staticmethod
    def _validate_source_ids(
        draft: MarketEvaluationDraft, evidence: list[EvidenceSource]
    ) -> None:
        """LLM이 제공되지 않은 출처 ID를 임의로 인용하지 못하게 확인합니다."""

        allowed = {item.source_id for item in evidence}
        unknown = sorted(
            {
                source_id
                for criterion in draft.criteria
                for source_id in criterion.source_ids
                if source_id not in allowed
            }
        )
        if unknown:
            raise MarketEvaluationError(
                f"Evaluation cited unknown source IDs: {unknown}"
            )

    def evaluate(self, company_id: str) -> MarketEvaluationResult:
        """기업 한 곳을 조사하고 구조화된 시장성 점수를 반환합니다."""

        company = self.base_rag.get_company(company_id)
        if company is None:
            raise MarketEvaluationError(f"Unknown company_id: {company_id}")

        plan = self.evaluation_backend.create_research_plan(company)
        pdf_evidence = self._collect_pdf_evidence(plan.pdf_queries)
        collection_risks: list[str] = []
        try:
            web_evidence = self._collect_web_evidence(plan.web_queries)
        except WebSearchError:
            # 웹 검색은 최신성 보강 수단입니다. 일시적인 timeout 때문에 기업
            # 전체 평가를 중단하지 않고, 이미 확보한 기업 정보와 PDF 근거로
            # 보수적인 평가를 계속합니다. 실제 예외 문자열은 API 내부 정보가
            # 섞일 수 있어 최종 보고서에는 노출하지 않습니다.
            web_evidence = []
            collection_risks.append(
                "웹 검색 근거 수집에 실패하여 기업정보와 PDF 근거만으로 평가했습니다."
            )
        else:
            if getattr(self.web_search, "last_warnings", []):
                collection_risks.append(
                    "일부 웹 검색이 실패하여 수집에 성공한 근거만으로 평가했습니다."
                )
        evidence = pdf_evidence + web_evidence

        draft = self.evaluation_backend.evaluate(
            company=company,
            plan=plan,
            evidence=evidence,
        )
        self._validate_source_ids(draft, evidence)

        scores = scores_from_results(draft.criteria)
        score_100, score_25 = calculate_market_score(scores)

        # 결과 순서를 항상 평가표 순서로 맞춰 저장하면 JSON과 보고서를 읽기 쉽습니다.
        by_name = {item.criterion: item for item in draft.criteria}
        ordered_criteria = [by_name[name] for name in CRITERION_ORDER]

        # LLM이 찾은 시장 위험과 수집 단계의 데이터 한계를 함께 전달합니다.
        # dict를 이용하면 순서를 유지하면서 같은 문장을 중복 제거할 수 있습니다.
        market_risks = list(
            dict.fromkeys([*draft.market_risks, *collection_risks])
        )

        return MarketEvaluationResult(
            company_id=company.company_id,
            company_name=company.company_name,
            market_definition=plan.market_definition,
            criteria=ordered_criteria,
            evidence=evidence,
            market_score_100=score_100,
            investment_score_25=score_25,
            market_risks=market_risks,
        )
