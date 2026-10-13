"""Explicit-model structured LLM adapter with one shared technical budget."""
from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from main.agents.supervisor.contracts import ErrorSummary
from .errors import ModelOutputFailure, failure
from .retries import RetryBudget, call_scope, current_scope, retry_call, utc_now

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class StructuredLLMClient:
    """Use an injected async transport, OpenAI, or LangChain OpenAI.

    A transport accepts the four ``LLMClient.generate`` keyword arguments and
    returns a DTO, mapping, or JSON string. SDK-internal retries are disabled.
    No fallback model is selected, and ambient prompt tracing is suppressed.
    """
    def __init__(self, transport: Callable[..., Awaitable[Any]] | None = None, *,
                 deadline_at: datetime | None = None, timeout_seconds: float = 60,
                 max_retries: int = 2, backend: str = "openai", client: Any = None,
                 now: Callable[[], datetime] = utc_now, sleep=asyncio.sleep):
        RetryBudget(max_retries=max_retries)
        if backend not in {"openai", "langchain"}:
            raise ValueError("backend must be openai or langchain")
        self.transport, self.deadline_at = transport, deadline_at
        self.timeout_seconds, self.max_retries, self.backend = timeout_seconds, max_retries, backend
        self.client, self.now, self.sleep = client, now, sleep

    def call_scope(self, *, deadline_at: datetime, node: str = "llm", budget=None):
        return call_scope(deadline_at=deadline_at, node=node,
                          budget=budget if budget is not None else RetryBudget(self.max_retries))

    async def generate(self, *, model: str, system_prompt: str,
                       payload: dict[str, Any], response_model: type[ResponseT]) -> ResponseT:
        scope = current_scope()
        node = scope.node if scope is not None else "llm"
        if not isinstance(model, str) or not model.strip():
            raise failure("CONFIG_OR_AUTH_ERROR", node, "An explicit model identifier is required")
        deadline = scope.deadline_at if scope is not None else self.deadline_at
        if deadline is None:
            raise failure("CONFIG_OR_AUTH_ERROR", node, "The original run deadline must be bound before generation")
        budget = scope.budget if scope is not None else RetryBudget(self.max_retries)
        prompt = system_prompt
        while True:
            async def invoke():
                try:
                    # LangChain's ambient tracing can otherwise upload complete
                    # prompts even when the runtime's own recorder is disabled.
                    try:
                        from langsmith import tracing_context
                        context = tracing_context(enabled=False)
                    except ImportError:
                        context = nullcontext()
                    with context:
                        transport = self.transport or self._provider_generate
                        raw = await transport(model=model, system_prompt=prompt,
                                              payload=payload, response_model=response_model)
                    if isinstance(raw, BaseModel):
                        return response_model.model_validate(raw.model_dump(mode="json"))
                    if isinstance(raw, (str, bytes, bytearray)):
                        return response_model.model_validate_json(raw)
                    return response_model.model_validate(raw)
                except (ValidationError, json.JSONDecodeError) as exc:
                    raise ModelOutputFailure(ErrorSummary(
                        code="MODEL_OUTPUT_INVALID", node=node, retriable=False,
                        message="Model output does not satisfy the required response DTO")) from None
            try:
                return await retry_call(invoke, node=node, deadline_at=deadline,
                                        timeout_seconds=self.timeout_seconds, budget=budget,
                                        now=self.now, sleep=self.sleep)
            except ModelOutputFailure:
                if not budget.consume(correction=True):
                    raise
                prompt = system_prompt + (
                    "\nYour previous response failed schema validation. Return only a JSON object "
                    "that satisfies the supplied response schema, including all required fields. "
                    "Preserve factual uncertainty and do not invent missing evidence."
                )

    async def _provider_generate(self, *, model, system_prompt, payload, response_model):
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, allow_nan=False)},
        ]
        if self.backend == "langchain":
            from langchain_openai import ChatOpenAI
            model_client = self.client or ChatOpenAI(model=model, max_retries=0, timeout=self.timeout_seconds)
            # An injected LangChain instance must explicitly bind the requested
            # model; a configured client must never silently select another one.
            configured_model = getattr(model_client, "model_name", None)
            if configured_model != model:
                raise failure("CONFIG_OR_AUTH_ERROR", "llm", "Injected client model differs from the requested model")
            if self.client is not None and getattr(model_client, "max_retries", None) != 0:
                raise failure("CONFIG_OR_AUTH_ERROR", "llm", "Injected LangChain client must disable SDK retries")
            structured = model_client.with_structured_output(response_model, method="json_schema", include_raw=True)
            value = await structured.ainvoke(messages, config={"callbacks": []})
            if isinstance(value, dict) and "parsed" in value:
                if value.get("parsing_error") is not None or value["parsed"] is None:
                    raise ModelOutputFailure(ErrorSummary(code="MODEL_OUTPUT_INVALID", node="llm", retriable=False,
                        message="Model output does not satisfy the required response DTO"))
                return value["parsed"]
            return value
        from openai import AsyncOpenAI, OpenAIError
        if self.client is None:
            try:
                self.client = AsyncOpenAI(max_retries=0, timeout=self.timeout_seconds)
            except (OpenAIError, ValueError):
                raise failure("CONFIG_OR_AUTH_ERROR", "llm", "OpenAI client credentials or settings are invalid") from None
        # with_options also disables hidden retries on an injected SDK client.
        client = self.client.with_options(max_retries=0, timeout=self.timeout_seconds)
        response = await client.chat.completions.create(
            model=model, messages=messages,
            response_format={"type": "json_schema", "json_schema": {
                "name": response_model.__name__, "schema": response_model.model_json_schema(), "strict": False,
            }},
        )
        return response.choices[0].message.content
