"""시작 노드의 단일 후보 선정과 구조화 출력 실패를 검증합니다."""

import json
from collections import deque
from unittest.mock import patch

import httpx
import pytest
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from qdrant_client import QdrantClient

from main.agents.start import StartAgent
from main.rag.company import BaseRAG


class ScriptedLLM:
    def __init__(self, *responses):
        self.responses = deque(responses)

    def with_structured_output(self, schema):
        return RunnableLambda(
            lambda _prompt: schema.model_validate(self.responses.popleft())
        )


class TestEmbeddings:
    model_name = "text-embedding-3-small"
    dimensions = 3

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


@pytest.fixture
def rag():
    instance = BaseRAG()
    yield instance
    instance.close()


@pytest.fixture
def indexed_rag():
    instance = BaseRAG(dimensions=3, embeddings=TestEmbeddings())
    instance._client = QdrantClient(":memory:")
    instance.build_index()
    yield instance
    instance.close()


def request(**updates):
    return {
        "company_id": None,
        "filters": [],
        "semantic_query": None,
        "mode": "recommend",
        "sort_field": None,
        "unsupported_reason": None,
        **updates,
    }


def test_explicit_id_selects_without_second_model_call(rag):
    agent = StartAgent(rag, ScriptedLLM(request(company_id="3")))
    assert agent.invoke({"prompt": "기업 아이디 3번으로 해줘"}) == {
        "company_id": "3", "message": None,
    }


def test_unique_exact_filter_selects_without_second_model_call(rag):
    agent = StartAgent(rag, ScriptedLLM(request(filters=[
        {"field": "company_name", "op": "eq", "value": "카본식스"}
    ])))
    assert agent.invoke({"prompt": "카본식스를 선택해줘"}) == {
        "company_id": "17", "message": None,
    }


def test_unique_id_with_semantic_query_still_requires_model_check(indexed_rag):
    agent = StartAgent(indexed_rag, ScriptedLLM(
        request(company_id="17", semantic_query="양자 컴퓨터"),
        {"eligible_company_ids": [], "company_id": None},
    ))
    assert agent.invoke({"prompt": "17번 회사가 양자 컴퓨터를 만든다면 선택해줘"}) == {
        "company_id": None,
        "message": "조건에 해당하는 회사가 없습니다. 조건을 다시 입력해주세요.",
    }


def test_tied_ranking_still_uses_model_judgment(rag):
    company1, company2 = rag.get_company("1"), rag.get_company("2")
    company2.values["funding_latest_won"] = company1.values["funding_latest_won"]
    with patch.object(rag, "list_companies", return_value=[company1, company2]):
        agent = StartAgent(rag, ScriptedLLM(
            request(mode="max", sort_field="funding_latest_won"),
            {"eligible_company_ids": ["1", "2"], "company_id": "2"},
        ))
        assert agent.invoke({"prompt": "최근 투자액 1위 회사"}) == {
            "company_id": "2", "message": None,
        }


@pytest.mark.parametrize("failed_call", [1, 2])
def test_empty_chatopenai_structured_response_returns_retry(rag, failed_call):
    contents = deque(
        [""] if failed_call == 1 else [json.dumps(request()), ""]
    )

    def respond(_request):
        return httpx.Response(200, json={
            "id": "chatcmpl-empty-structured-test", "object": "chat.completion",
            "created": 0, "model": "gpt-4.1-mini",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": contents.popleft()}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        llm = ChatOpenAI(
            model="gpt-4.1-mini", api_key="test-only",
            base_url="https://example.invalid/v1", http_client=client,
            use_responses_api=False, max_retries=0,
        )
        result = StartAgent(rag, llm).invoke({"prompt": "회사 하나 골라줘"})

    assert result["company_id"] is None
    assert "회사 선택 응답을 해석하지 못했습니다" in result["message"]


def test_unrelated_model_value_error_propagates(rag):
    class FailingLLM:
        def with_structured_output(self, schema):
            def fail(_prompt):
                raise ValueError("provider unavailable")

            return RunnableLambda(fail)

    with pytest.raises(ValueError, match="provider unavailable"):
        StartAgent(rag, FailingLLM()).invoke({"prompt": "회사 하나 골라줘"})


def test_rag_value_error_propagates(rag):
    with patch.object(rag, "list_companies", side_effect=ValueError("CSV lookup failed")):
        with pytest.raises(ValueError, match="CSV lookup failed"):
            StartAgent(rag, ScriptedLLM()).invoke({"prompt": "회사 하나 골라줘"})
