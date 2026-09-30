"""시장 조사 계획·평가를 위한 OpenAI 모델 호출입니다."""

from __future__ import annotations

import json
import os
from main.rag.company import CompanyRecord
from .schemas import (
    EvidenceSource,
    MarketEvaluationDraft,
    MarketResearchPlan,
    MarketEvaluationError,
)


def _response_schema(model_type, name: str) -> dict:
    """Pydantic 모델을 Responses API의 JSON Schema 출력 설정으로 바꿉니다."""

    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "schema": model_type.model_json_schema(),
            # strict=False여도 JSON Schema를 따르도록 유도되고, Pydantic이 한 번 더
            # 검증합니다. Optional 필드가 있는 스키마와도 호환성이 좋습니다.
            "strict": False,
        }
    }


class OpenAIMarketEvaluationBackend:
    """OpenAI Responses API로 검색 계획과 구조화된 평가를 생성합니다."""

    def __init__(self, *, model_name: str | None = None):
        self.model_name = model_name or os.getenv("OPENAI_MARKET_MODEL", "gpt-5-mini")
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not os.environ.get("OPENAI_API_KEY"):
            raise MarketEvaluationError("Set OPENAI_API_KEY before market evaluation")
        try:
            from openai import OpenAI

            self._client = OpenAI(timeout=90.0, max_retries=2)
        except Exception as exc:
            raise MarketEvaluationError(
                f"Cannot initialize OpenAI client ({type(exc).__name__})"
            ) from exc
        return self._client

    def _structured_call(
        self, *, instructions: str, prompt: str, model_type, name: str
    ):
        """모델 출력을 JSON으로 제한하고 Pydantic으로 최종 검증합니다."""

        try:
            response = self._get_client().responses.create(
                model=self.model_name,
                instructions=instructions,
                input=prompt,
                text=_response_schema(model_type, name),
                store=False,
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            raise MarketEvaluationError(
                f"OpenAI structured response failed ({type(exc).__name__}, status={status})"
            ) from exc

        output_text = (getattr(response, "output_text", "") or "").strip()
        if not output_text:
            raise MarketEvaluationError("OpenAI returned an empty structured response")
        try:
            return model_type.model_validate(json.loads(output_text))
        except Exception as exc:
            raise MarketEvaluationError(
                f"OpenAI output does not match {name}: {exc}"
            ) from exc

    def create_research_plan(self, company: CompanyRecord) -> MarketResearchPlan:
        """기업 기본정보를 시장 정의와 PDF·웹 검색어로 변환합니다."""

        company_data = {
            "company_id": company.company_id,
            "company_name": company.company_name,
            "location": company.values.get("location"),
            "sector": company.values.get("sector"),
            "subsector": company.values.get("subsector"),
            "technology": company.values.get("technology"),
            "product_type": company.values.get("product_type"),
            "homepage": company.raw.get("홈페이지"),
            "representative_product": company.raw.get("대표제품"),
            "service": company.raw.get("서비스"),
        }
        instructions = """
당신은 Physical AI/Robotics 스타트업의 시장 조사 계획을 만드는 분석가다.
제공된 기업정보를 사실로만 사용하고 모르는 고객이나 제품을 만들어내지 않는다.
시장 정의는 반드시 '이 기업은 [지역]의 [고객]에게 [업무·문제]를 해결하는
[제품·서비스]를 판매한다.' 형식의 한 문장으로 작성한다.
PDF 검색어는 산업 통계·시장 규모·성장률·도입 장벽을 찾도록 만들고,
웹 검색어는 해당 기업의 고객·계약·도입·가격·ROI·확장 사례를 찾도록 만든다.
""".strip()
        prompt = (
            "다음 기업을 위한 시장 정의와 PDF 검색어 3~8개, 웹 검색어 3~8개를 "
            "작성하라.\n\n" + json.dumps(company_data, ensure_ascii=False, indent=2)
        )
        return self._structured_call(
            instructions=instructions,
            prompt=prompt,
            model_type=MarketResearchPlan,
            name="market_research_plan",
        )

    def evaluate(
        self,
        *,
        company: CompanyRecord,
        plan: MarketResearchPlan,
        evidence: list[EvidenceSource],
    ) -> MarketEvaluationDraft:
        """수집한 근거를 핵심 평가표의 다섯 항목으로 평가합니다."""

        company_data = {
            "company_id": company.company_id,
            "company_name": company.company_name,
            "location": company.values.get("location"),
            "sector": company.values.get("sector"),
            "subsector": company.values.get("subsector"),
            "technology": company.values.get("technology"),
            "product_type": company.values.get("product_type"),
            "representative_product": company.raw.get("대표제품"),
            "service": company.raw.get("서비스"),
        }
        evidence_data = [item.model_dump() for item in evidence]

        instructions = """
당신은 Physical AI/Robotics 스타트업의 시장성 평가 담당자다.
기업정보와 제공된 근거만 사용하고, 근거에 없는 사실은 만들지 않는다.

아래 다섯 항목을 각각 정확히 한 번, 1~5점으로 평가한다.

1. customer_willingness (25%): 고객 문제와 지불의향
   - 1점: 문제가 약하거나 회사 주장만 있고 지불 증거가 없음
   - 3점: 문제와 예산은 확인되지만 유료 검증이 제한적
   - 5점: 문제가 크고 반복되며 유료 도입·계약 근거가 복수 존재
2. market_size (25%): 시장 크기와 획득 가능성
   - 1점: 시장이 작거나 TAM만 있고 SAM·SOM 근거가 없음
   - 3점: 의미 있는 틈새시장이며 SAM·SOM 계산이 대체로 타당
   - 5점: 큰 시장에서 현실적인 SAM·SOM과 획득 경로가 명확
3. growth_timing (20%): 성장성과 진입 타이밍
   - 1점: 정체·축소 시장이거나 상용화 시점이 너무 이르거나 늦음
   - 3점: 성장 동인은 있으나 속도·시점이 불확실
   - 5점: 높은 성장률과 장기 수요 동력이 확인되고 구매가 시작됨
4. adoption_feasibility (20%): 고객 도입 가능성
   - 1점: ROI가 불명확하거나 통합·규제 장벽이 매우 큼
   - 3점: 경제성은 있으나 설치·조달·인증 부담이 존재
   - 5점: 회수기간이 짧고 도입·운영·인증 경로가 명확
5. scalability (10%): 사업 확장성
   - 1점: 고객별 맞춤개발과 일회성 판매에 의존
   - 3점: 일부 표준화와 반복매출이 가능
   - 5점: 표준제품·반복매출 구조로 업종·지역 확장이 가능

2점과 4점은 양옆 기준의 중간 수준이다. 각 판단에는 제공된 source_id만
인용한다. 검색 유사도는 시장성 점수가 아니므로 점수 근거로 사용하지 않는다.
항목의 판단을 뒷받침할 인용 근거가 없으면 score=null로 반환한다.
근거의 content_kind=generated_summary는 검색 모델이 작성한 요약이며 원문의 직접 인용이 아니다.
가중합은 계산하지 말고 항목별 1~5점 또는 null만 반환한다.
""".strip()
        prompt = f"""
[기업정보]
{json.dumps(company_data, ensure_ascii=False, indent=2)}

[확정된 시장 정의]
{plan.market_definition}

[사용 가능한 근거]
{json.dumps(evidence_data, ensure_ascii=False, indent=2)}

다섯 평가항목의 점수, 이유, 사용한 source_id와 핵심 시장 리스크를 반환하라.
""".strip()
        return self._structured_call(
            instructions=instructions,
            prompt=prompt,
            model_type=MarketEvaluationDraft,
            name="market_evaluation_draft",
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
