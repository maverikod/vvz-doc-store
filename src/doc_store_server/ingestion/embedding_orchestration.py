"""Typed orchestration boundary for hierarchy-enriched chunk embeddings.

This module deliberately stops at the public chunk and embed-client contracts.
It does not own providers, queues, persistence, or publication.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Protocol, TypeAlias

from chunk_metadata_adapter import SemanticChunk

from ..domain.semantic_chunk import serialize_semantic_chunk, validate_semantic_chunk


Vector: TypeAlias = tuple[float, ...]


class EmbeddingClientProtocol(Protocol):
    """Small duck-typed slice of :class:`embed_client.EmbeddingClient`."""

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        dimension: int | None = None,
        wait: bool = True,
        **kwargs: Any,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class EmbeddingMetadata:
    """One versioned embedding record; prior records remain untouched."""

    chunk_id: str
    vector: Vector
    provider: str
    model: str
    model_version: str
    dimension: int
    created_at: datetime
    compatible: bool
    active: bool


@dataclass(frozen=True, slots=True)
class EmbeddingOrchestrationResult:
    """Validated chunks plus records ready for a later publication boundary."""

    chunks: tuple[SemanticChunk, ...]
    embeddings: tuple[EmbeddingMetadata, ...]


class EmbeddingResponseError(ValueError):
    """Raised when the embedding service response cannot be trusted."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingResponseError(f"embedding response {field} must be a non-empty string")
    return value


def _response_vector(value: Any, index: int) -> Vector:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EmbeddingResponseError(f"embedding response vector {index} is not a sequence")
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise EmbeddingResponseError(f"embedding response vector {index} contains a non-number")
        number = float(item)
        if not isfinite(number):
            raise EmbeddingResponseError(f"embedding response vector {index} contains a non-finite value")
        vector.append(number)
    if not vector:
        raise EmbeddingResponseError(f"embedding response vector {index} is empty")
    return tuple(vector)


def _extract_vectors(response: Mapping[str, Any]) -> tuple[tuple[Vector, ...], str, str, str, int]:
    results = response.get("results", response.get("embeddings"))
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)):
        raise EmbeddingResponseError("embedding response must contain a results sequence")

    vectors: list[Vector] = []
    item_provider: str | None = None
    item_version: str | None = None
    for index, item in enumerate(results):
        if isinstance(item, Mapping):
            raw_vector = item.get("embedding", item.get("vector"))
            provider = item.get("provider")
            version = item.get("model_version")
            if provider is not None:
                item_provider = _required_text(provider, "provider")
            if version is not None:
                item_version = _required_text(version, "model_version")
        else:
            raw_vector = item
        vectors.append(_response_vector(raw_vector, index))

    provider = response.get("provider", item_provider)
    model = response.get("model")
    version = response.get("model_version", item_version)
    dimension = response.get("dimension")
    if provider is None or model is None or version is None or dimension is None:
        raise EmbeddingResponseError(
            "embedding response must contain provider, model, model_version, and dimension"
        )
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise EmbeddingResponseError("embedding response dimension must be a positive integer")
    return tuple(vectors), _required_text(provider, "provider"), _required_text(model, "model"), _required_text(version, "model_version"), dimension


def _attach_vector(chunk: SemanticChunk, vector: Vector, model: str) -> SemanticChunk:
    payload = serialize_semantic_chunk(chunk)
    payload["embedding"] = list(vector)
    payload["embedding_model"] = model
    return validate_semantic_chunk(SemanticChunk.from_dict_with_autofill_and_validation(payload))


async def orchestrate_embeddings(
    chunks: Sequence[SemanticChunk],
    client: EmbeddingClientProtocol,
    *,
    provider: str,
    model: str,
    model_version: str,
    dimension: int | None = None,
    created_at: datetime | None = None,
) -> EmbeddingOrchestrationResult:
    """Vectorize chunks in order and return new active, compatible records."""

    source = tuple(validate_semantic_chunk(chunk) for chunk in chunks)
    if not source:
        return EmbeddingOrchestrationResult((), ())
    provider = _required_text(provider, "provider")
    model = _required_text(model, "model")
    model_version = _required_text(model_version, "model_version")
    if dimension is not None and (isinstance(dimension, bool) or dimension <= 0):
        raise ValueError("dimension must be a positive integer or None")

    response = await client.embed(
        [chunk.text for chunk in source],
        model=model,
        dimension=dimension,
        wait=True,
    )
    if not isinstance(response, Mapping):
        raise EmbeddingResponseError("embedding client returned a non-mapping response")
    vectors, actual_provider, actual_model, actual_version, actual_dimension = _extract_vectors(response)
    if len(vectors) != len(source):
        raise EmbeddingResponseError("embedding response count does not match input count")
    if (actual_provider, actual_model, actual_version) != (provider, model, model_version):
        raise EmbeddingResponseError("embedding response provider/model/version does not match request")
    if dimension is not None and actual_dimension != dimension:
        raise EmbeddingResponseError("embedding response dimension does not match requested dimension")
    if any(len(vector) != actual_dimension for vector in vectors):
        raise EmbeddingResponseError("embedding response vector length does not match actual dimension")

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    enriched: list[SemanticChunk] = []
    records: list[EmbeddingMetadata] = []
    for chunk, vector in zip(source, vectors, strict=True):
        enriched_chunk = _attach_vector(chunk, vector, actual_model)
        enriched.append(enriched_chunk)
        records.append(
            EmbeddingMetadata(
                chunk_id=str(enriched_chunk.uuid),
                vector=vector,
                provider=actual_provider,
                model=actual_model,
                model_version=actual_version,
                dimension=actual_dimension,
                created_at=timestamp,
                compatible=True,
                active=True,
            )
        )
    return EmbeddingOrchestrationResult(tuple(enriched), tuple(records))


vectorize_chunks = orchestrate_embeddings


__all__ = (
    "EmbeddingClientProtocol",
    "EmbeddingMetadata",
    "EmbeddingOrchestrationResult",
    "EmbeddingResponseError",
    "orchestrate_embeddings",
    "vectorize_chunks",
)
