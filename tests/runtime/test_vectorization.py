from __future__ import annotations

import json
from uuid import uuid4
from typing import Any

import pytest

from doc_store_server.runtime.embedding_config import RuntimeEmbeddingConfig
from doc_store_server.runtime.vectorization import (
    ChunkVectorInput,
    ChunkVectorRecord,
    RuntimeVectorizationService,
    VectorizationError,
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
                {"embedding": [float(index), float(index + 1)]}
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
    )


class _BatchSelectingVectorizationService(RuntimeVectorizationService):
    def __init__(self, document_ids: list[Any]) -> None:
        super().__init__("postgresql://unused", _EmbeddingClient(), _config(batch_size=2))
        self.document_ids = document_ids
        self.select_calls: list[dict[str, Any]] = []
        self.persisted_batches: list[list[Any]] = []

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
            ChunkVectorInput(chunk_id=uuid4(), document_id=document_id, body=str(document_id))
            for document_id in document_ids
        )

    async def _embed_chunks(self, chunks: Any) -> tuple[ChunkVectorRecord, ...]:
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
        ChunkVectorInput(chunk_id=uuid4(), document_id=uuid4(), body=f"chunk {index}")
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
async def test_vectorizer_rejects_wrong_embedding_dimension() -> None:
    client = _EmbeddingClient(dimension=3)
    service = RuntimeVectorizationService("postgresql://unused", client, _config(dimension=2))

    with pytest.raises(VectorizationError):
        await service._embed_chunks(
            (ChunkVectorInput(chunk_id=uuid4(), document_id=uuid4(), body="chunk"),)
        )


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
