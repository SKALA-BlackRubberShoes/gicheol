"""기업·시장 RAG가 공유하는 OpenAI 임베딩 처리입니다."""

from __future__ import annotations

import math
import os
from typing import Protocol


class EmbeddingProviderError(RuntimeError):
    """임베딩 제공자의 호출 또는 응답에 문제가 있습니다."""


class EmbeddingBackend(Protocol):
    """OpenAI 임베더와 테스트용 임베더가 공통으로 제공하는 인터페이스입니다."""

    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def validate_vectors(
    vectors: list[list[float]], count: int, dimensions: int
) -> list[list[float]]:
    """임베딩 개수·차원·수치가 Qdrant에 저장하고 검색할 수 있는 값인지 확인합니다."""
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingProviderError("Embedding response count does not match input")
    for vector in vectors:
        if (
            not isinstance(vector, list)
            or len(vector) != dimensions
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)
        ):
            raise EmbeddingProviderError(
                "Invalid embedding dimension or nonfinite/nonnumeric value"
            )
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise EmbeddingProviderError("Embedding must have a finite, nonzero norm")
    return vectors


class OpenAIEmbeddings:
    """문서와 질문을 같은 OpenAI 모델·차원으로 임베딩합니다."""

    def __init__(self, model_name: str, dimensions: int):
        self.model_name, self.dimensions = model_name, dimensions
        self._client = self._encoder = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """문서를 최대 16개씩 임베딩합니다. 길이 초과 문서를 자동으로 자르지 않습니다."""
        if not isinstance(texts, list) or any(
            not isinstance(t, str) or not t.strip() for t in texts
        ):
            raise ValueError("Embedding input must be a list of nonempty strings")
        if not texts:
            return []
        if not os.environ.get("OPENAI_API_KEY"):
            raise EmbeddingProviderError(
                "Set OPENAI_API_KEY before embedding documents or queries"
            )
        try:
            if self._encoder is None:
                import tiktoken

                self._encoder = tiktoken.encoding_for_model(self.model_name)
            lengths = [
                len(self._encoder.encode(t, disallowed_special=())) for t in texts
            ]
        except Exception as exc:
            raise EmbeddingProviderError(
                f"Tokenizer initialization failed ({type(exc).__name__})"
            ) from exc
        if any(n > 8192 for n in lengths):
            raise ValueError(
                "Embedding input exceeds 8192 tokens; shorten it explicitly"
            )
        vectors = []
        try:
            if self._client is None:
                from openai import OpenAI

                # 실제 임베딩 시점에만 환경변수의 API 키로 클라이언트를 만듭니다.
                self._client = OpenAI(timeout=30.0, max_retries=2)
            for start in range(0, len(texts), 16):
                batch = texts[start : start + 16]
                response = self._client.embeddings.create(
                    model=self.model_name,
                    dimensions=self.dimensions,
                    input=batch,
                    encoding_format="float",
                )
                items = response.data
                indices = [item.index for item in items]
                if any(type(i) is not int for i in indices) or sorted(indices) != list(
                    range(len(batch))
                ):
                    raise EmbeddingProviderError(
                        "Embedding response has missing/duplicate/invalid indices"
                    )
                # 응답 순서가 달라도 입력 문서와 벡터의 대응을 유지합니다.
                ordered = [
                    item.embedding
                    for item in sorted(items, key=lambda item: item.index)
                ]
                vectors.extend(validate_vectors(ordered, len(batch), self.dimensions))
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            # 외부로 전달하는 오류 메시지에는 요청 본문이나 API 키를 넣지 않습니다.
            status = getattr(exc, "status_code", None)
            raise EmbeddingProviderError(
                f"OpenAI embedding failed ({type(exc).__name__}, status={status})"
            ) from exc
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """질문 하나를 문서와 동일한 설정으로 임베딩합니다."""
        return self.embed_documents([text])[0]

    def close(self) -> None:
        """이 임베더가 생성한 OpenAI 클라이언트를 닫습니다."""
        if self._client is not None:
            self._client.close()
            self._client = None
