"""Pinned open-source dense embedding adapter for the shared async contract.

No API embedding, remote code, truncation, or fallback is performed. Large model
downloads are lazy and use the caller's explicit cache directory when provided.
"""
from __future__ import annotations

import asyncio
import math
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from main.agents.supervisor.contracts import EmbeddingProvider


class EmbeddingError(RuntimeError):
    """A model could not produce valid vectors; never substitute another model."""


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    revision: str
    dimension: int
    max_input_tokens: int
    query_prefix: str = ""
    passage_prefix: str = ""


BGE_M3 = ModelSpec("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181", 1024, 8192)
MULTILINGUAL_E5_BASE = ModelSpec(
    "intfloat/multilingual-e5-base", "d128750597153bb5987e10b1c3493a34e5a4502a",
    768, 512, "query: ", "passage: ",
)
MODEL_SPECS = {spec.model_id: spec for spec in (BGE_M3, MULTILINGUAL_E5_BASE)}


def validate_vectors(vectors: Any, count: int, dimension: int, *, normalize: bool) -> list[list[float]]:
    """Reject wrong shape, zero/nonfinite vectors, and unrecorded normalization."""
    if hasattr(vectors, "tolist"):
        vectors = vectors.tolist()
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingError("Embedding count does not match input")
    result = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimension or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in vector
        ):
            raise EmbeddingError("Embedding has invalid dimension or numeric values")
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm <= 0:
            raise EmbeddingError("Embedding has invalid norm")
        if normalize and not math.isclose(norm, 1.0, abs_tol=1e-4):
            raise EmbeddingError("Embedding normalization differs from configuration")
        result.append([float(x) for x in vector])
    return result


class SentenceTransformerEmbeddings:
    """SentenceTransformer dense vectors implementing EmbeddingProvider.

    E5 prefixes apply to every language and count toward its 512 token limit.
    The caller must construct a new index after changing any model setting.
    """
    def __init__(
        self, model_id: str = BGE_M3.model_id, *, revision: str | None = None,
        normalize: bool = True, cache_folder: str | None = None, device: str = "cpu",
        batch_size: int = 8, local_files_only: bool = False,
        model_factory: Callable[..., Any] | None = None,
    ):
        if model_id not in MODEL_SPECS:
            raise ValueError("Unsupported model: select BAAI/bge-m3 or multilingual-e5-base explicitly")
        self.spec = MODEL_SPECS[model_id]
        self.model_id = model_id
        self.revision = revision or self.spec.revision
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision):
            raise ValueError("Embedding revision must be an immutable 40-character commit SHA")
        if type(normalize) is not bool or type(batch_size) is not int or batch_size < 1:
            raise ValueError("Invalid normalization or batch size")
        self.dimension = self.spec.dimension
        self.normalize = normalize
        self.cache_folder, self.device = cache_folder, device
        self.batch_size, self.local_files_only = batch_size, local_files_only
        self._model_factory, self._model = model_factory, None
        self._lock = threading.RLock()

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id, "revision": self.revision, "dimension": self.dimension,
            "normalize": self.normalize, "max_input_tokens": self.spec.max_input_tokens,
            "query_prefix": self.spec.query_prefix, "passage_prefix": self.spec.passage_prefix,
            "representation": "dense", "device": self.device,
        }

    def load(self) -> Any:
        """Explicit synchronous loader for corpus tokenization and benchmarks."""
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                factory = self._model_factory
                if factory is None:
                    from sentence_transformers import SentenceTransformer
                    factory = SentenceTransformer
                model = factory(
                    self.model_id, revision=self.revision, device=self.device,
                    cache_folder=self.cache_folder, local_files_only=self.local_files_only,
                    trust_remote_code=False,
                )
                if model.get_sentence_embedding_dimension() != self.dimension:
                    raise EmbeddingError("Loaded model dimension differs from the pinned specification")
                # Set the recorded model limit only after checking inputs ourselves.
                model.max_seq_length = self.spec.max_input_tokens
                self._model = model
                return model
            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError(f"Pinned model initialization failed ({type(exc).__name__})") from exc

    def count_tokens(self, text: str, *, query: bool = False, include_special_tokens: bool = True) -> int:
        model = self.load()
        prefix = self.spec.query_prefix if query else self.spec.passage_prefix
        return len(model.tokenizer.encode(prefix + text, add_special_tokens=include_special_tokens, truncation=False))

    def _encode(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not isinstance(texts, list) or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("Embedding input must be nonempty strings")
        if not texts:
            return []
        with self._lock:
            model = self.load()
            prefix = self.spec.query_prefix if query else self.spec.passage_prefix
            inputs = [prefix + t for t in texts]
            if any(len(model.tokenizer.encode(t, add_special_tokens=True, truncation=False)) > self.spec.max_input_tokens for t in inputs):
                raise ValueError("Model input exceeds pinned limit; re-chunk explicitly without truncation")
            try:
                output = model.encode(
                    inputs, batch_size=self.batch_size, normalize_embeddings=self.normalize,
                    convert_to_numpy=True, show_progress_bar=False,
                )
                return validate_vectors(output, len(texts), self.dimension, normalize=self.normalize)
            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError(f"Dense embedding failed ({type(exc).__name__})") from exc

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._encode, texts, query=False)

    async def embed_query(self, text: str) -> list[float]:
        return (await asyncio.to_thread(self._encode, [text], query=True))[0]


def as_embedding_provider(adapter: SentenceTransformerEmbeddings) -> EmbeddingProvider:
    """Type-level integration uses the sole common Protocol, not a duplicate DTO."""
    return adapter
