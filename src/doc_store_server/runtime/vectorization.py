"""External embed-client based vectorization worker for stored chunks."""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.embedding_config import RuntimeEmbeddingConfig, runtime_embedding_config
from doc_store_server.runtime.ingestion_logs import DEFAULT_LOG_DIR
from doc_store_server.runtime.previews import chunk_preview


@dataclass(frozen=True, slots=True)
class ChunkVectorInput:
    chunk_id: UUID
    document_id: UUID
    body: str


@dataclass(frozen=True, slots=True)
class ChunkVectorRecord:
    chunk_id: UUID
    vector: tuple[float, ...]


class VectorizationError(RuntimeError):
    """Raised when external embedding or vector persistence fails."""


class RuntimeVectorizationService:
    """Select chunks needing vectors and rebuild active pgvector rows."""

    def __init__(
        self,
        database_url: str | None,
        embedding_client: Any,
        embedding_config: RuntimeEmbeddingConfig,
    ) -> None:
        self._database_url = database_url
        self._embedding_client = embedding_client
        self._embedding_config = embedding_config
        self._embedding_unavailable_logged = False

    async def rebuild(
        self,
        *,
        document_id: str | None = None,
        all_documents: bool = False,
        document_limit: int | None = None,
        document_batch_size: int = 5,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not self._database_url:
            raise VectorizationError("database URL is not configured")
        if document_batch_size <= 0:
            raise ValueError("document_batch_size must be positive")
        processed_docs: list[UUID] = []
        processed_chunks: list[ChunkVectorInput] = []
        all_documents_after: UUID | None = None

        while True:
            if document_id is not None:
                if processed_docs:
                    break
                docs = self._select_documents(
                    document_id=document_id,
                    all_documents=False,
                    document_limit=1,
                )
            elif all_documents:
                remaining = None if document_limit is None else document_limit - len(processed_docs)
                if remaining is not None and remaining <= 0:
                    break
                docs = self._select_documents(
                    document_id=None,
                    all_documents=True,
                    document_limit=min(document_batch_size, remaining)
                    if remaining is not None
                    else document_batch_size,
                    after_document_id=all_documents_after,
                )
                if not docs:
                    break
                all_documents_after = docs[-1]
            else:
                remaining = None if document_limit is None else document_limit - len(processed_docs)
                if remaining is not None and remaining <= 0:
                    break
                docs = self._select_documents(
                    document_id=None,
                    all_documents=False,
                    document_limit=min(document_batch_size, remaining)
                    if remaining is not None
                    else document_batch_size,
                )
                if not docs:
                    break
            chunks = self._select_chunks(docs)
            if dry_run:
                processed_docs.extend(docs)
                processed_chunks.extend(chunks)
                continue
            try:
                vectors = await self._embed_chunks(chunks)
            except VectorizationError as exc:
                self._log_embedding_unavailable(exc)
                return self._result(
                    "embedding_unavailable",
                    processed_docs,
                    processed_chunks,
                    dry_run=False,
                    issue=str(exc),
                )
            self._persist_vectors(docs, vectors)
            self._log_embedding_recovered_if_needed()
            self._log_processed_chunks(chunks)
            processed_docs.extend(docs)
            processed_chunks.extend(chunks)
        return self._result("dry_run" if dry_run else "ok", processed_docs, processed_chunks, dry_run=dry_run)

    def _select_documents(
        self,
        *,
        document_id: str | None,
        all_documents: bool,
        document_limit: int | None,
        after_document_id: UUID | None = None,
    ) -> tuple[UUID, ...]:
        params: dict[str, Any] = {}
        where = ["d.deleted_at IS NULL"]
        if document_id is not None:
            where.append("d.id = :document_id")
            params["document_id"] = UUID(document_id)
        elif after_document_id is not None:
            where.append("d.id > :after_document_id")
            params["after_document_id"] = after_document_id
        elif not all_documents:
            where.append("(d.needs_revectorize IS TRUE OR f.needs_revectorize IS TRUE)")
        limit_sql = ""
        if document_limit is not None:
            if document_limit <= 0:
                raise ValueError("document_limit must be positive")
            limit_sql = " LIMIT :document_limit"
            params["document_limit"] = document_limit
        order_sql = (
            "d.id ASC"
            if all_documents
            else "d.updated_at ASC NULLS FIRST, d.created_at ASC, d.id ASC"
        )
        sql = f"""
            SELECT d.id
            FROM documents AS d
            LEFT JOIN files AS f ON f.id = d.owner_id
            WHERE {' AND '.join(where)}
            ORDER BY {order_sql}
            {limit_sql}
        """
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                return tuple(connection.execute(text(sql), params).scalars().all())
        finally:
            engine.dispose()

    def _select_chunks(self, document_ids: Sequence[UUID]) -> tuple[ChunkVectorInput, ...]:
        if not document_ids:
            return ()
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT id, document_id, text FROM semantic_chunks "
                        "WHERE deleted_at IS NULL "
                        "AND document_id = ANY(CAST(:document_ids AS uuid[])) "
                        "ORDER BY document_id, order_index ASC, id ASC"
                    ),
                    {"document_ids": [str(item) for item in document_ids]},
                ).mappings().all()
        finally:
            engine.dispose()
        return tuple(
            ChunkVectorInput(
                chunk_id=row["id"],
                document_id=row["document_id"],
                body=str(row["text"]),
            )
            for row in rows
        )

    async def _embed_chunks(
        self,
        chunks: Sequence[ChunkVectorInput],
    ) -> tuple[ChunkVectorRecord, ...]:
        config = self._embedding_config
        if config.batch_size <= 0:
            raise VectorizationError("embedding batch_size must be positive")
        records: list[ChunkVectorRecord] = []
        for start in range(0, len(chunks), config.batch_size):
            batch = chunks[start : start + config.batch_size]
            try:
                response = self._embedding_client.embed(
                    [item.body for item in batch],
                    model=config.model,
                    dimension=config.dimension,
                    wait=True,
                    wait_timeout=config.wait_timeout,
                    poll_interval=config.poll_interval,
                    device=config.device,
                )
                response = await response if inspect.isawaitable(response) else response
            except Exception as exc:
                raise VectorizationError(f"embedding client failed: {exc}") from exc
            vectors = _extract_vectors(response, expected_count=len(batch), config=config)
            records.extend(
                ChunkVectorRecord(chunk_id=item.chunk_id, vector=vector)
                for item, vector in zip(batch, vectors, strict=True)
            )
        return tuple(records)

    def _persist_vectors(
        self,
        document_ids: Sequence[UUID],
        vectors: Sequence[ChunkVectorRecord],
    ) -> None:
        config = self._embedding_config
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                chunk_ids = [str(record.chunk_id) for record in vectors]
                if chunk_ids:
                    connection.execute(
                        text(
                            "UPDATE semantic_chunk_embeddings SET active = FALSE "
                            "WHERE chunk_uuid = ANY(CAST(:chunk_ids AS uuid[]))"
                        ),
                        {"chunk_ids": chunk_ids},
                    )
                for record in vectors:
                    connection.execute(
                        text(
                            "INSERT INTO semantic_chunk_embeddings "
                            "(chunk_uuid, vector, model, dimension, provider, model_version, active) "
                            "VALUES (:chunk_uuid, CAST(:vector AS vector), :model, :dimension, "
                            ":provider, :model_version, TRUE)"
                        ),
                        {
                            "chunk_uuid": record.chunk_id,
                            "vector": _vector_literal(record.vector),
                            "model": config.model,
                            "dimension": config.dimension,
                            "provider": config.provider,
                            "model_version": config.model_version,
                        },
                    )
                connection.execute(
                    text(
                        "UPDATE documents SET needs_revectorize = FALSE, updated_at = now() "
                        "WHERE id = ANY(CAST(:document_ids AS uuid[]))"
                    ),
                    {"document_ids": [str(item) for item in document_ids]},
                )
                connection.execute(
                    text(
                        "UPDATE files SET needs_revectorize = FALSE, updated_at = now() "
                        "WHERE id IN (SELECT owner_id FROM documents "
                        "WHERE id = ANY(CAST(:document_ids AS uuid[])))"
                    ),
                    {"document_ids": [str(item) for item in document_ids]},
                )
        finally:
            engine.dispose()

    def _log_processed_chunks(self, chunks: Sequence[ChunkVectorInput]) -> None:
        for chunk in chunks:
            _append_vectorizer_log(
                "vectorizer_processed.jsonl",
                {
                    "event": "chunk_vectorized",
                    "chunk_id": str(chunk.chunk_id),
                    "document_id": str(chunk.document_id),
                    "preview": chunk_preview(chunk.body),
                },
            )

    def _log_embedding_unavailable(self, exc: Exception) -> None:
        if self._embedding_unavailable_logged:
            return
        self._embedding_unavailable_logged = True
        _append_vectorizer_log(
            "vectorizer_errors.jsonl",
            {
                "event": "embedding_unavailable",
                "error": str(exc),
                "provider": self._embedding_config.provider,
                "model": self._embedding_config.model,
            },
        )

    def _log_embedding_recovered_if_needed(self) -> None:
        if not self._embedding_unavailable_logged:
            return
        self._embedding_unavailable_logged = False
        _append_vectorizer_log(
            "vectorizer_processed.jsonl",
            {
                "event": "embedding_recovered",
                "provider": self._embedding_config.provider,
                "model": self._embedding_config.model,
            },
        )

    def _result(
        self,
        status: str,
        document_ids: Sequence[UUID],
        chunks: Sequence[ChunkVectorInput],
        *,
        dry_run: bool,
        issue: str | None = None,
    ) -> dict[str, Any]:
        config = self._embedding_config
        result: dict[str, Any] = {
            "status": status,
            "dry_run": dry_run,
            "documents": [str(item) for item in document_ids],
            "document_count": len(document_ids),
            "chunk_count": len(chunks),
            "embedding": {
                "provider": config.provider,
                "model": config.model,
                "model_version": config.model_version,
                "dimension": config.dimension,
                "batch_size": config.batch_size,
            },
        }
        if issue:
            result["issue"] = issue
        return result


def installed_vectorization_service(
    config: Mapping[str, Any] | None = None,
) -> RuntimeVectorizationService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        import os

        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    embedding_config = runtime_embedding_config(config)
    return RuntimeVectorizationService(
        database_url,
        installed_embedding_client(embedding_config),
        embedding_config,
    )


def installed_embedding_client(config: RuntimeEmbeddingConfig) -> Any:
    from embed_client import EmbeddingClient

    return EmbeddingClient(
        protocol=config.protocol,
        host=config.host,
        port=config.port,
        cert=config.cert,
        key=config.key,
        ca=config.ca,
        check_hostname=config.check_hostname,
        token=config.token,
        token_header=config.token_header,
        timeout=config.timeout,
    )


def _extract_vectors(
    response: Mapping[str, Any],
    *,
    expected_count: int,
    config: RuntimeEmbeddingConfig,
) -> tuple[tuple[float, ...], ...]:
    if not isinstance(response, Mapping):
        raise VectorizationError("embedding client returned a non-mapping response")
    response_model = response.get("model")
    if response_model is not None and response_model != config.model:
        raise VectorizationError("embedding response model does not match configuration")
    response_dimension = response.get("dimension")
    if response_dimension is not None and int(response_dimension) != config.dimension:
        raise VectorizationError("embedding response dimension does not match configuration")
    results = response.get("results", response.get("embeddings"))
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)):
        raise VectorizationError("embedding response must contain a results sequence")
    if len(results) != expected_count:
        raise VectorizationError("embedding response count does not match input count")
    vectors: list[tuple[float, ...]] = []
    for index, item in enumerate(results):
        raw = item.get("embedding", item.get("vector")) if isinstance(item, Mapping) else item
        vectors.append(_vector(raw, index, config.dimension))
    return tuple(vectors)


def _vector(value: Any, index: int, dimension: int) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise VectorizationError(f"embedding response vector {index} is not a sequence")
    vector = tuple(float(item) for item in value)
    if len(vector) != dimension:
        raise VectorizationError(f"embedding response vector {index} dimension mismatch")
    if any(not isfinite(item) for item in vector):
        raise VectorizationError(f"embedding response vector {index} contains a non-finite value")
    return vector


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _append_vectorizer_log(filename: str, payload: Mapping[str, Any]) -> None:
    try:
        directory = Path(os.getenv("DOC_STORE_VECTORIZER_LOG_DIR") or os.getenv("DOC_STORE_EVENT_LOG_DIR", DEFAULT_LOG_DIR))
        directory.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), **dict(payload)}
        with (directory / filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


__all__ = [
    "RuntimeVectorizationService",
    "VectorizationError",
    "installed_embedding_client",
    "installed_vectorization_service",
]
