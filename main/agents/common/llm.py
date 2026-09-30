"""채팅 모델은 식별자로 생성하거나 호출자가 만든 객체를 그대로 사용합니다."""

from typing import Any


def resolve_chat_model(model: str | Any):
    if isinstance(model, str):
        try:
            from langchain.chat_models import init_chat_model
        except ImportError as exc:
            raise RuntimeError(
                "Install LangChain and the configured model provider"
            ) from exc
        model = init_chat_model(model)
    if not callable(getattr(model, "with_structured_output", None)):
        raise ValueError(
            "model must be a model identifier or support with_structured_output"
        )
    return model
