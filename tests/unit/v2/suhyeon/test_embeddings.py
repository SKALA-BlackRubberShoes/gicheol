import asyncio
import math

import pytest

from main.agents.supervisor.contracts import EmbeddingProvider
from main.rag.neutral.embeddings import (
    BGE_M3, MULTILINGUAL_E5_BASE, EmbeddingError, SentenceTransformerEmbeddings, validate_vectors,
)


class Tokenizer:
    def encode(self, text, **kwargs):
        return list(text) + ([0, 1] if kwargs.get("add_special_tokens") else [])


class Model:
    def __init__(self, dimension):
        self.dimension = dimension
        self.tokenizer = Tokenizer()
        self.inputs = []

    def get_sentence_embedding_dimension(self):
        return self.dimension

    def encode(self, inputs, **kwargs):
        self.inputs = inputs
        self.options = kwargs
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in inputs]


@pytest.mark.parametrize("spec", [BGE_M3, MULTILINGUAL_E5_BASE])
def test_pinned_dense_protocol_and_normalization(spec):
    model, calls = Model(spec.dimension), []
    def factory(model_id, **kwargs):
        calls.append((model_id, kwargs))
        return model
    backend = SentenceTransformerEmbeddings(spec.model_id, model_factory=factory)
    assert isinstance(backend, EmbeddingProvider)
    assert len(asyncio.run(backend.embed_query("한국어 질문"))) == spec.dimension
    assert model.inputs == [spec.query_prefix + "한국어 질문"]
    asyncio.run(backend.embed_documents(["English document"]))
    assert model.inputs == [spec.passage_prefix + "English document"]
    assert calls == [(spec.model_id, {"revision": spec.revision, "device": "cpu", "cache_folder": None,
                                     "local_files_only": False, "trust_remote_code": False})]
    assert model.options["normalize_embeddings"] is True
    assert backend.metadata["representation"] == "dense"


def test_e5_prefix_and_special_tokens_count_toward_model_limit():
    model = Model(768)
    backend = SentenceTransformerEmbeddings(MULTILINGUAL_E5_BASE.model_id, model_factory=lambda *a, **k: model)
    asyncio.run(backend.embed_documents(["a" * 501]))  # 501 + passage prefix 9 + special 2
    with pytest.raises(ValueError, match="exceeds pinned limit"):
        asyncio.run(backend.embed_documents(["a" * 502]))
    assert model.inputs == ["passage: " + "a" * 501]


@pytest.mark.parametrize("revision", ["main", "v1.0", "deadbeef"])
def test_mutable_or_short_revision_rejected(revision):
    with pytest.raises(ValueError, match="immutable"):
        SentenceTransformerEmbeddings(revision=revision)


def test_unavailable_model_has_no_fallback():
    calls = []
    def factory(*args, **kwargs):
        calls.append(args[0])
        raise OSError("offline")
    adapter = SentenceTransformerEmbeddings(model_factory=factory)
    with pytest.raises(EmbeddingError, match="initialization"):
        asyncio.run(adapter.embed_query("query"))
    assert calls == [BGE_M3.model_id]


@pytest.mark.parametrize("vectors", [[[1.0]], [[0.0, 0.0]], [[math.nan, 1.0]], [[True, 1.0]], [[2.0, 0.0]], []])
def test_invalid_vectors_fail_closed(vectors):
    with pytest.raises(EmbeddingError):
        validate_vectors(vectors, 1, 2, normalize=True)


def test_loaded_dimension_mismatch_rejected():
    adapter = SentenceTransformerEmbeddings(model_factory=lambda *a, **k: Model(768))
    with pytest.raises(EmbeddingError, match="dimension"):
        asyncio.run(adapter.embed_query("query"))
