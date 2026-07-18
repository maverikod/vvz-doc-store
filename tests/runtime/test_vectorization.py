from __future__ import annotations

import json
import inspect
from uuid import uuid4
from typing import Any

import pytest

from doc_store_server.runtime.embedding_config import RuntimeEmbeddingConfig, runtime_embedding_config
from doc_store_server.runtime.vectorization import (
    ChunkVectorInput,
    ChunkVectorRecord,
    InMemoryVectorizationStatus,
    RuntimeVectorizationService,
    VectorizationError,
    _extract_bm25_token_groups,
    _extract_vectors,
    _last_vectorizer_activity_event,
    _mean_vector,
)


class _EmbeddingClient:
    def __init__(self, *, dimension: int = 2) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], **kwargs: object) -> dict[str, object]:
        self.calls.append(texts)
        assert kwargs["model"] == "model-a"
        assert kwargs["dimension"] == self.dimension
        return {
            "results": [
                {
                    "embedding": [float(index), float(index + 1)],
                    "bm25_tokens": [f"token-{index}", "  spaced token  "],
                }
                for index, _text in enumerate(texts)
            ],
            "model": "model-a",
            "dimension": self.dimension,
        }


def _config(*, batch_size: int = 2, dimension: int = 2) -> RuntimeEmbeddingConfig:
    return RuntimeEmbeddingConfig(
        protocol="https",
        host="embedding-service",
        port=8001,
        cert=None,
        key=None,
        ca=None,
        check_hostname=False,
        token=None,
        token_header=None,
        timeout=300.0,
        wait_timeout=300,
        poll_interval=1.0,
        provider="embedding-service-vvz",
        model="model-a",
        model_version="v1",
        dimension=dimension,
        device=None,
        batch_size=batch_size,
        direct_text_max_chars=0,
    )


class _BatchSelectingVectorizationService(RuntimeVectorizationService):
    def __init__(
        self,
        document_ids: list[Any],
        *,
        status: InMemoryVectorizationStatus | None = None,
    ) -> None:
        super().__init__("postgresql://unused", _EmbeddingClient(), _config(batch_size=2), status)
        self.document_ids = document_ids
        self.select_calls: list[dict[str, Any]] = []
        self.persisted_batches: list[list[Any]] = []
        self.vectorizer_snapshots: list[dict[str, Any]] = []

    def _select_documents(self, **kwargs: Any) -> tuple[Any, ...]:
        self.select_calls.append(dict(kwargs))
        after = kwargs.get("after_document_id")
        start = 0
        if after is not None:
            start = self.document_ids.index(after) + 1
        limit = kwargs.get("document_limit")
        end = None if limit is None else start + int(limit)
        return tuple(self.document_ids[start:end])

    def _select_chunks(self, document_ids: Any) -> tuple[ChunkVectorInput, ...]:
        return tuple(
            ChunkVectorInput(
                chunk_id=uuid4(),
                entity_id=uuid4(),
                entity_type="paragraph",
                document_id=document_id,
                body=str(document_id),
            )
            for document_id in document_ids
        )

    def _select_vector_inputs(self, document_ids: Any) -> tuple[ChunkVectorInput, ...]:
        return self._select_chunks(document_ids)

    def _document_details(self, document_ids: Any) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "document_id": str(document_id),
                "title": f"Document {document_id}",
                "source_name": f"{document_id}.md",
                "file": f"/docs/{document_id}.md",
            }
            for document_id in document_ids
        )

    async def _embed_chunks(self, chunks: Any) -> tuple[ChunkVectorRecord, ...]:
        if self._status is not None:
            self.vectorizer_snapshots.append(self._status.snapshot())
        return tuple(
            ChunkVectorRecord(chunk_id=chunk.chunk_id, vector=(1.0, 2.0))
            for chunk in chunks
        )

    def _persist_vectors(self, document_ids: Any, vectors: Any) -> None:
        self.persisted_batches.append(list(document_ids))

    def _log_processed_chunks(self, chunks: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_vectorizer_calls_embed_client_in_text_batches() -> None:
    client = _EmbeddingClient()
    service = RuntimeVectorizationService("postgresql://unused", client, _config(batch_size=2))
    chunks = tuple(
        ChunkVectorInput(
            chunk_id=uuid4(),
            entity_id=uuid4(),
            entity_type="paragraph" if index == 0 else "semantic_chunk",
            document_id=uuid4(),
            body=f"chunk {index}",
        )
        for index in range(5)
    )

    records = await service._embed_chunks(chunks)

    assert [call for call in client.calls] == [
        ["chunk 0", "chunk 1"],
        ["chunk 2", "chunk 3"],
        ["chunk 4"],
    ]
    assert [record.chunk_id for record in records] == [chunk.chunk_id for chunk in chunks]
    assert all(len(record.vector) == 2 for record in records)
    assert records[0].bm25_tokens is None
    assert records[1].bm25_tokens == ("token-1", "spaced token")
    assert records[0].entity_type == "paragraph"
    assert records[1].entity_type == "semantic_chunk"


@pytest.mark.asyncio
async def test_vectorizer_reads_all_documents_in_document_batches() -> None:
    document_ids = [uuid4() for _ in range(5)]
    service = _BatchSelectingVectorizationService(document_ids)

    result = await service.rebuild(all_documents=True, document_batch_size=2)

    assert result["status"] == "ok"
    assert result["document_count"] == 5
    assert [call["document_limit"] for call in service.select_calls] == [2, 2, 2, 2]
    assert [call["after_document_id"] for call in service.select_calls] == [
        None,
        document_ids[1],
        document_ids[3],
        document_ids[4],
    ]
    assert service.persisted_batches == [
        document_ids[:2],
        document_ids[2:4],
        document_ids[4:],
    ]


@pytest.mark.asyncio
async def test_vectorizer_status_exposes_current_file_while_embedding(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOC_STORE_VECTORIZER_LOG_DIR", str(tmp_path))
    document_id = uuid4()
    status = InMemoryVectorizationStatus()
    service = _BatchSelectingVectorizationService([document_id], status=status)

    result = await service.rebuild(all_documents=True, document_batch_size=1)

    assert result["status"] == "ok"
    current = service.vectorizer_snapshots[0]["current_activity"]
    assert current["current_document_id"] == str(document_id)
    assert current["current_file"] == f"/docs/{document_id}.md"
    assert status.snapshot()["state"] == "idle"
    assert status.snapshot()["last_activity"]["action"] == "embedded_documents"


@pytest.mark.asyncio
async def test_vectorizer_activity_log_records_document_before_embedding(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOC_STORE_VECTORIZER_LOG_DIR", str(tmp_path))
    document_id = uuid4()
    service = _BatchSelectingVectorizationService([document_id])

    await service.rebuild(all_documents=True, document_batch_size=1)

    events = [
        json.loads(line)
        for line in (tmp_path / "vectorizer_activity.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["event"] == "document_vectorization_started"
    assert events[0]["document_id"] == str(document_id)
    assert events[0]["file"] == f"/docs/{document_id}.md"
    assert events[-1]["event"] == "document_vectorized"
    assert _last_vectorizer_activity_event()["event"] == "document_vectorized"


@pytest.mark.asyncio
async def test_vectorizer_rejects_wrong_embedding_dimension() -> None:
    client = _EmbeddingClient(dimension=3)
    service = RuntimeVectorizationService("postgresql://unused", client, _config(dimension=2))

    with pytest.raises(VectorizationError):
        await service._embed_chunks(
            (ChunkVectorInput(chunk_id=uuid4(), document_id=uuid4(), body="chunk"),)
        )


def test_runtime_embedding_config_defaults_to_safe_text_batch_size(monkeypatch) -> None:
    monkeypatch.delenv("DOC_STORE_EMBEDDING_BATCH_SIZE", raising=False)

    config = runtime_embedding_config({})

    assert config.batch_size == 16


def test_vectorizer_reports_embedding_item_error_before_vector_validation() -> None:
    with pytest.raises(VectorizationError, match="embedding response item 0 failed"):
        _extract_vectors(
            {
                "results": [
                    {
                        "embedding": None,
                        "error": {
                            "code": "encode_error",
                            "message": "model server rejected the batch",
                        },
                    }
                ],
                "model": "model-a",
                "dimension": 2,
            },
            expected_count=1,
            config=_config(),
        )


def test_vectorizer_extracts_optional_bm25_tokens_from_embedding_items() -> None:
    groups = _extract_bm25_token_groups(
        {
            "results": [
                {"embedding": [0.0, 1.0], "bm25_tokens": ["alpha", "  beta  ", ""]},
                {"embedding": [1.0, 2.0]},
            ]
        },
        expected_count=2,
    )

    assert groups == (("alpha", "beta"), None)


def test_vectorizer_rejects_malformed_bm25_tokens() -> None:
    with pytest.raises(VectorizationError, match="bm25_tokens 0 is not a sequence"):
        _extract_bm25_token_groups({"results": [{"bm25_tokens": "alpha"}]}, expected_count=1)


def test_vectorizer_aggregates_document_and_file_vectors_by_average() -> None:
    document_id = uuid4()
    file_id = uuid4()
    service = RuntimeVectorizationService("postgresql://unused", _EmbeddingClient(), _config())

    class _Result:
        def mappings(self) -> Any:
            return self

        def all(self) -> list[dict[str, Any]]:
            return [{"id": document_id, "owner_id": file_id}]

    class _Connection:
        def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
            return _Result()

    records = (
        ChunkVectorRecord(
            chunk_id=uuid4(),
            entity_id=uuid4(),
            entity_type="paragraph",
            document_id=document_id,
            vector=(1.0, 3.0),
        ),
        ChunkVectorRecord(
            chunk_id=uuid4(),
            entity_id=uuid4(),
            entity_type="paragraph",
            document_id=document_id,
            vector=(3.0, 5.0),
        ),
        ChunkVectorRecord(
            chunk_id=uuid4(),
            entity_id=uuid4(),
            entity_type="semantic_chunk",
            document_id=document_id,
            vector=(100.0, 100.0),
        ),
    )

    aggregate = service._aggregate_document_file_vectors(_Connection(), [document_id], records)

    assert [(item.entity_type, item.vector_entity_id, item.vector) for item in aggregate] == [
        ("document", document_id, (2.0, 4.0)),
        ("file", file_id, (2.0, 4.0)),
    ]


def test_vectorizer_uses_direct_document_vectors_for_file_average() -> None:
    document_id = uuid4()
    file_id = uuid4()
    service = RuntimeVectorizationService("postgresql://unused", _EmbeddingClient(), _config())

    class _Result:
        def mappings(self) -> Any:
            return self

        def all(self) -> list[dict[str, Any]]:
            return [{"id": document_id, "owner_id": file_id}]

    class _Connection:
        def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
            return _Result()

    records = (
        ChunkVectorRecord(
            chunk_id=document_id,
            entity_id=document_id,
            entity_type="document",
            vector=(10.0, 20.0),
        ),
        ChunkVectorRecord(
            chunk_id=uuid4(),
            entity_id=uuid4(),
            entity_type="paragraph",
            document_id=document_id,
            vector=(1.0, 3.0),
        ),
    )

    aggregate = service._aggregate_document_file_vectors(_Connection(), [document_id], records)

    assert [(item.entity_type, item.vector_entity_id, item.vector) for item in aggregate] == [
        ("file", file_id, (10.0, 20.0)),
    ]


def test_mean_vector_rejects_mismatched_dimensions() -> None:
    with pytest.raises(VectorizationError, match="different dimensions"):
        _mean_vector(((1.0, 2.0), (1.0,)))


def test_vectorizer_embedding_persistence_is_idempotent_for_same_model_version() -> None:
    source = inspect.getsource(RuntimeVectorizationService._persist_vectors)

    assert "ON CONFLICT ON CONSTRAINT uq_semantic_chunk_embeddings_entity_version" in source
    assert "entity_type, entity_id, chunk_uuid" in source
    assert "DO UPDATE SET vector = EXCLUDED.vector, chunk_version_id = EXCLUDED.chunk_version_id, active = TRUE" in source
    assert "chunk_version_id" in source
    assert "DELETE FROM semantic_chunk_tokens" in source
    assert "INSERT INTO semantic_chunk_tokens" in source
    assert "bm25_tokens" in source


def test_vectorizer_unavailable_log_is_suppressed_until_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DOC_STORE_VECTORIZER_LOG_DIR", str(tmp_path))
    service = RuntimeVectorizationService("postgresql://unused", _EmbeddingClient(), _config())

    service._log_embedding_unavailable(RuntimeError("down"))
    service._log_embedding_unavailable(RuntimeError("down again"))
    service._log_embedding_recovered_if_needed()
    service._log_embedding_unavailable(RuntimeError("down after recovery"))

    errors = [
        json.loads(line)
        for line in (tmp_path / "vectorizer_errors.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    processed = [
        json.loads(line)
        for line in (tmp_path / "vectorizer_processed.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["event"] for item in errors] == [
        "embedding_unavailable",
        "embedding_unavailable",
    ]
    assert processed[0]["event"] == "embedding_recovered"
