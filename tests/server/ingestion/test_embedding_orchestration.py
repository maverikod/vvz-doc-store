"""Contract tests for the public embedding orchestration boundary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import pytest
from chunk_metadata_adapter import SemanticChunk
from embed_client import EmbeddingClient

import doc_store_server.ingestion.embedding_orchestration as orchestration_module
from doc_store_server.db.link_embedding_metadata_schema import select_active_embedding
from doc_store_server.ingestion.embedding_orchestration import (
    EmbeddingResponseError,
    orchestrate_embeddings,
)


PROVIDER = "test-provider"
MODEL = "test-model"
MODEL_VERSION = "2026.07"
CREATED_AT = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _chunk(text: str, ordinal: int, start: int, end: int) -> SemanticChunk:
    return SemanticChunk.from_dict_with_autofill_and_validation(
        {
            "uuid": str(uuid4()),
            "source_id": str(uuid4()),
            "block_id": str(uuid4()),
            "type": "DocBlock",
            "body": text,
            "text": text,
            "ordinal": ordinal,
            "start": start,
            "end": end,
            "block_meta": {"chapter_id": str(uuid4()), "accepted": True},
        }
    )


def _client(response: dict[str, object]) -> EmbeddingClient:
    client = create_autospec(EmbeddingClient, instance=True)
    client.embed = AsyncMock(return_value=response)
    return client


def _run(coroutine: object) -> object:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def test_orchestration_uses_real_embedding_client_shapes_and_preserves_public_chunk_metadata() -> None:
    source = (_chunk("first", 3, 10, 15), _chunk("second", 4, 20, 26))
    client = _client(
        {
            "results": [
                {"embedding": [0.1, 0.2], "provider": PROVIDER, "model_version": MODEL_VERSION},
                {"embedding": [0.3, 0.4], "provider": PROVIDER, "model_version": MODEL_VERSION},
            ],
            "provider": PROVIDER,
            "model": MODEL,
            "model_version": MODEL_VERSION,
            "dimension": 2,
        }
    )

    result = _run(
        orchestrate_embeddings(
            source,
            client,
            provider=PROVIDER,
            model=MODEL,
            model_version=MODEL_VERSION,
            dimension=2,
            created_at=CREATED_AT,
        )
    )

    client.embed.assert_awaited_once_with(
        ["first", "second"], model=MODEL, dimension=2, wait=True
    )
    assert [chunk.text for chunk in result.chunks] == ["first", "second"]
    assert [chunk.embedding for chunk in result.chunks] == [[0.1, 0.2], [0.3, 0.4]]
    assert [chunk.embedding_model for chunk in result.chunks] == [MODEL, MODEL]
    assert [(chunk.ordinal, chunk.start, chunk.end, chunk.block_meta) for chunk in result.chunks] == [
        (chunk.ordinal, chunk.start, chunk.end, chunk.block_meta) for chunk in source
    ]
    assert [record.chunk_id for record in result.embeddings] == [str(chunk.uuid) for chunk in source]
    assert [record.vector for record in result.embeddings] == [(0.1, 0.2), (0.3, 0.4)]
    assert all(
        (record.provider, record.model, record.model_version, record.dimension, record.created_at)
        == (PROVIDER, MODEL, MODEL_VERSION, 2, CREATED_AT)
        and record.compatible
        and record.active
        for record in result.embeddings
    )


def test_new_active_compatible_vector_adds_history_and_selection_keeps_only_new_vector_active() -> None:
    source = (_chunk("stable text", 0, 0, 11),)
    old = _run(
        orchestrate_embeddings(
            source,
            _client(
                {
                    "results": [[1.0, 2.0]],
                    "provider": PROVIDER,
                    "model": MODEL,
                    "model_version": "1",
                    "dimension": 2,
                }
            ),
            provider=PROVIDER,
            model=MODEL,
            model_version="1",
            created_at=CREATED_AT,
        )
    ).embeddings[0]
    new = _run(
        orchestrate_embeddings(
            source,
            _client(
                {
                    "results": [[3.0, 4.0]],
                    "provider": PROVIDER,
                    "model": MODEL,
                    "model_version": "2",
                    "dimension": 2,
                }
            ),
            provider=PROVIDER,
            model=MODEL,
            model_version="2",
            created_at=CREATED_AT.replace(second=1),
        )
    ).embeddings[0]

    history = [
        {"id": "old", "vector": list(old.vector), "model": old.model, "dimension": old.dimension,
         "provider": old.provider, "model_version": old.model_version, "created_at": old.created_at,
         "active": old.active},
        {"id": "new", "vector": list(new.vector), "model": new.model, "dimension": new.dimension,
         "provider": new.provider, "model_version": new.model_version, "created_at": new.created_at,
         "active": new.active},
    ]

    selected = select_active_embedding(history, MODEL, 2)
    assert selected is history[1]
    assert len(history) == 2
    assert history[0]["vector"] == [1.0, 2.0]
    assert history[0]["model_version"] == "1"
    assert history[1]["vector"] == [3.0, 4.0]
    assert history[1]["model_version"] == "2"


@pytest.mark.parametrize(
    "response",
    [
        {"results": [[1.0]], "provider": PROVIDER, "model": MODEL, "model_version": MODEL_VERSION, "dimension": 2},
        {"results": [[1.0, 2.0]], "provider": PROVIDER, "model": "other", "model_version": MODEL_VERSION, "dimension": 2},
        {"results": [[1.0, 2.0]], "provider": PROVIDER, "model": MODEL, "model_version": MODEL_VERSION, "dimension": 2},
        {"results": [[float("nan"), 2.0]], "provider": PROVIDER, "model": MODEL, "model_version": MODEL_VERSION, "dimension": 2},
    ],
)
def test_malformed_or_mismatched_response_is_rejected(response: dict[str, object]) -> None:
    client = _client(response)
    with pytest.raises(EmbeddingResponseError):
        _run(
            orchestrate_embeddings(
                (_chunk("text", 0, 0, 4), _chunk("other", 1, 4, 9)),
                client,
                provider=PROVIDER,
                model=MODEL,
                model_version=MODEL_VERSION,
                dimension=2,
            )
        )


def test_orchestration_isolated_from_provider_workers_queue_persistence_and_publication() -> None:
    client = _client(
        {
            "results": [[1.0, 2.0]],
            "provider": PROVIDER,
            "model": MODEL,
            "model_version": MODEL_VERSION,
            "dimension": 2,
        }
    )
    result = _run(
        orchestration_module.orchestrate_embeddings(
            (_chunk("text", 0, 0, 4),),
            client,
            provider=PROVIDER,
            model=MODEL,
            model_version=MODEL_VERSION,
        )
    )

    assert len(result.embeddings) == 1
    assert set(vars(orchestration_module)) <= {
        "Any", "EmbeddingClientProtocol", "EmbeddingMetadata", "EmbeddingOrchestrationResult",
        "EmbeddingResponseError", "Mapping", "Protocol", "Sequence", "SemanticChunk", "TypeAlias",
        "Vector", "_attach_vector", "_extract_vectors", "_required_text", "_response_vector",
        "datetime", "dataclass", "isfinite", "orchestrate_embeddings", "serialize_semantic_chunk",
        "timezone", "validate_semantic_chunk", "vectorize_chunks", "__all__", "__builtins__",
        "__annotations__", "__cached__", "__doc__", "__file__", "__loader__", "__name__", "__package__", "__spec__",
        "annotations",
    }
    assert not any(
        name in vars(orchestration_module)
        for name in ("EmbeddingProvider", "Worker", "VectorizationQueue", "SemanticChunkRepository", "AsyncSession", "transaction", "publish")
    )
