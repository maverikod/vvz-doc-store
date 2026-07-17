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
from sqlalchemy.exc import SQLAlchemyError

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.embedding_config import RuntimeEmbeddingConfig, runtime_embedding_config
from doc_store_server.runtime.ingestion_logs import DEFAULT_LOG_DIR
from doc_store_server.runtime.previews import chunk_preview


@dataclass(frozen=True, slots=True)
class ChunkVectorInput:
    chunk_id: UUID
    document_id: UUID
    body: str
    entity_type: str = "semantic_chunk"
    entity_id: UUID | None = None

    @property
    def vector_entity_id(self) -> UUID:
        return self.entity_id or self.chunk_id


@dataclass(frozen=True, slots=True)
class ChunkVectorRecord:
    chunk_id: UUID
    vector: tuple[float, ...]
    bm25_tokens: tuple[str, ...] | None = None
    entity_type: str = "semantic_chunk"
    entity_id: UUID | None = None
    document_id: UUID | None = None

    @property
    def vector_entity_id(self) -> UUID:
        return self.entity_id or self.chunk_id


class InMemoryVectorizationStatus:
    """Process-local vectorizer activity snapshot for health reporting."""

    def __init__(self) -> None:
        self._current: dict[str, Any] | None = None
        self._last: dict[str, Any] | None = None

    def record_current(
        self,
        *,
        action: str,
        documents: Sequence[Mapping[str, Any]],
        chunk_count: int,
    ) -> None:
        payload = {
            "action": action,
            "documents": [dict(item) for item in documents],
            "chunk_count": chunk_count,
            "timestamp": _utc_now(),
        }
        if len(documents) == 1:
            document = documents[0]
            payload["current_document_id"] = document.get("document_id")
            payload["current_file"] = document.get("file") or document.get("source_name")
            payload["current_title"] = document.get("title")
        else:
            payload["current_document_ids"] = [item.get("document_id") for item in documents]
            payload["current_files"] = [
                item.get("file") or item.get("source_name")
                for item in documents
            ]
        self._current = payload
        self._write_snapshot()

    def record_completed(
        self,
        *,
        action: str,
        documents: Sequence[Mapping[str, Any]],
        chunk_count: int,
    ) -> None:
        payload = {
            "action": action,
            "documents": [dict(item) for item in documents],
            "chunk_count": chunk_count,
            "timestamp": _utc_now(),
        }
        self._last = payload
        self._current = None
        self._write_snapshot()

    def snapshot(
        self,
        *,
        database_url: str | None = None,
        embedding_config: RuntimeEmbeddingConfig | None = None,
    ) -> dict[str, Any]:
        log_current = _current_activity_from_logs(
            database_url=database_url,
            embedding_config=embedding_config,
        )
        persisted = self._read_snapshot()
        if log_current is not None:
            current = log_current
            last = persisted.get("last_activity") if persisted else self._last
            return {
                "state": "running",
                "current_activity": current,
                "last_activity": last,
                "note": "log-backed best-effort vectorizer snapshot",
            }
        if self._current is None and persisted is not None:
            return persisted
        return {
            "state": "running" if self._current else "idle",
            "current_activity": self._current,
            "last_activity": self._last,
            "note": "process-local and persisted best-effort vectorizer snapshot",
        }

    def _write_snapshot(self) -> None:
        try:
            path = _vectorizer_status_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "state": "running" if self._current else "idle",
                "current_activity": self._current,
                "last_activity": self._last,
                "note": "process-local and persisted best-effort vectorizer snapshot",
            }
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            tmp_path.replace(path)
        except OSError:
            return

    def _read_snapshot(self) -> dict[str, Any] | None:
        try:
            path = _vectorizer_status_path()
            if not path.exists():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


class VectorizationError(RuntimeError):
    """Raised when external embedding or vector persistence fails."""


class RuntimeVectorizationService:
    """Select chunks needing vectors and rebuild active pgvector rows."""

    def __init__(
        self,
        database_url: str | None,
        embedding_client: Any,
        embedding_config: RuntimeEmbeddingConfig,
        status: InMemoryVectorizationStatus | None = None,
    ) -> None:
        self._database_url = database_url
        self._embedding_client = embedding_client
        self._embedding_config = embedding_config
        self._status = status
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
            chunks = self._select_vector_inputs(docs)
            document_details = self._document_details(docs)
            if dry_run:
                processed_docs.extend(docs)
                processed_chunks.extend(chunks)
                continue
            self._log_documents_started(document_details, chunks)
            if self._status is not None:
                self._status.record_current(
                    action="embedding_documents",
                    documents=document_details,
                    chunk_count=len(chunks),
                )
            try:
                vectors = await self._embed_chunks(chunks)
            except VectorizationError as exc:
                self._log_embedding_unavailable(exc)
                if self._status is not None:
                    self._status.record_completed(
                        action="embedding_unavailable",
                        documents=document_details,
                        chunk_count=len(chunks),
                    )
                return self._result(
                    "embedding_unavailable",
                    processed_docs,
                    processed_chunks,
                    dry_run=False,
                    issue=str(exc),
                )
            self._persist_vectors(docs, vectors)
            self._log_embedding_recovered_if_needed()
            self._log_documents_completed(document_details, chunks)
            self._log_processed_chunks(chunks)
            if self._status is not None:
                self._status.record_completed(
                    action="embedded_documents",
                    documents=document_details,
                    chunk_count=len(chunks),
                )
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

    def _select_vector_inputs(self, document_ids: Sequence[UUID]) -> tuple[ChunkVectorInput, ...]:
        if not document_ids:
            return ()
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                paragraph_rows = connection.execute(
                    text(
                        "SELECT p.id, p.document_id, p.text "
                        "FROM paragraphs AS p "
                        "WHERE p.deleted_at IS NULL "
                        "AND p.document_id = ANY(CAST(:document_ids AS uuid[])) "
                        "AND btrim(p.text) <> '' "
                        "ORDER BY p.document_id, p.order_index ASC, p.id ASC"
                    ),
                    {"document_ids": [str(item) for item in document_ids]},
                ).mappings().all()
                chunk_rows = connection.execute(
                    text(
                        "SELECT sc.id, sc.document_id, sct.text "
                        "FROM semantic_chunks AS sc "
                        "JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id "
                        "WHERE sc.deleted_at IS NULL "
                        "AND sc.document_id = ANY(CAST(:document_ids AS uuid[])) "
                        "ORDER BY sc.document_id, sc.order_index ASC, sc.id ASC"
                    ),
                    {"document_ids": [str(item) for item in document_ids]},
                ).mappings().all()
        finally:
            engine.dispose()
        paragraphs = tuple(
            ChunkVectorInput(
                chunk_id=row["id"],
                entity_id=row["id"],
                entity_type="paragraph",
                document_id=row["document_id"],
                body=str(row["text"]),
            )
            for row in paragraph_rows
        )
        chunks = tuple(
            ChunkVectorInput(
                chunk_id=row["id"],
                entity_id=row["id"],
                entity_type="semantic_chunk",
                document_id=row["document_id"],
                body=str(row["text"]),
            )
            for row in chunk_rows
        )
        return paragraphs + chunks

    def _document_details(self, document_ids: Sequence[UUID]) -> tuple[dict[str, Any], ...]:
        if not document_ids:
            return ()
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        "SELECT d.id::text AS document_id, d.title, d.source_name, "
                        "d.source_path, f.name AS file_name, f.path AS file_path "
                        "FROM documents AS d "
                        "LEFT JOIN files AS f ON f.id = d.owner_id "
                        "WHERE d.id = ANY(CAST(:document_ids AS uuid[])) "
                        "ORDER BY d.id ASC"
                    ),
                    {"document_ids": [str(item) for item in document_ids]},
                ).mappings().all()
        finally:
            engine.dispose()
        return tuple(_document_detail(row) for row in rows)

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
            bm25_tokens = _extract_bm25_token_groups(response, expected_count=len(batch))
            records.extend(
                ChunkVectorRecord(
                    chunk_id=item.chunk_id,
                    entity_id=item.vector_entity_id,
                    entity_type=item.entity_type,
                    document_id=item.document_id,
                    vector=vector,
                    bm25_tokens=tokens if item.entity_type == "semantic_chunk" else None,
                )
                for item, vector, tokens in zip(batch, vectors, bm25_tokens, strict=True)
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
                all_vectors = tuple(vectors) + self._aggregate_document_file_vectors(
                    connection,
                    document_ids,
                    vectors,
                )
                for record in all_vectors:
                    connection.execute(
                        text(
                            "UPDATE semantic_chunk_embeddings SET active = FALSE "
                            "WHERE entity_type = :entity_type "
                            "AND entity_id = :entity_id "
                            "AND model = :model "
                            "AND dimension = :dimension"
                        ),
                        {
                            "entity_type": record.entity_type,
                            "entity_id": record.vector_entity_id,
                            "model": config.model,
                            "dimension": config.dimension,
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO semantic_chunk_embeddings "
                            "(entity_type, entity_id, chunk_uuid, vector, model, dimension, "
                            "provider, model_version, active) "
                            "VALUES (:entity_type, :entity_id, :chunk_uuid, CAST(:vector AS vector), :model, :dimension, "
                            ":provider, :model_version, TRUE) "
                            "ON CONFLICT ON CONSTRAINT uq_semantic_chunk_embeddings_entity_version "
                            "DO UPDATE SET vector = EXCLUDED.vector, active = TRUE, "
                            "created_at = now()"
                        ),
                        {
                            "entity_type": record.entity_type,
                            "entity_id": record.vector_entity_id,
                            "chunk_uuid": record.vector_entity_id
                            if record.entity_type == "semantic_chunk"
                            else None,
                            "vector": _vector_literal(record.vector),
                            "model": config.model,
                            "dimension": config.dimension,
                            "provider": config.provider,
                            "model_version": config.model_version,
                        },
                    )
                    if record.entity_type == "semantic_chunk" and record.bm25_tokens is not None:
                        connection.execute(
                            text(
                                "DELETE FROM semantic_chunk_tokens "
                                "WHERE chunk_uuid = :chunk_uuid AND token_kind = 'bm25_tokens'"
                            ),
                            {"chunk_uuid": record.chunk_id},
                        )
                        if record.bm25_tokens:
                            connection.execute(
                                text(
                                    "INSERT INTO semantic_chunk_tokens "
                                    "(chunk_uuid, token_kind, ordinal, token_value) "
                                    "VALUES (:chunk_uuid, 'bm25_tokens', :ordinal, :token_value)"
                                ),
                                [
                                    {
                                        "chunk_uuid": record.chunk_id,
                                        "ordinal": ordinal,
                                        "token_value": token_value,
                                    }
                                    for ordinal, token_value in enumerate(record.bm25_tokens)
                                ],
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

    def _aggregate_document_file_vectors(
        self,
        connection: Any,
        document_ids: Sequence[UUID],
        vectors: Sequence[ChunkVectorRecord],
    ) -> tuple[ChunkVectorRecord, ...]:
        by_document: dict[UUID, list[tuple[float, ...]]] = {}
        for record in vectors:
            if record.entity_type != "paragraph":
                continue
            document_id = record.document_id
            if document_id is not None:
                by_document.setdefault(document_id, []).append(record.vector)
        if not by_document:
            for record in vectors:
                if record.entity_type != "semantic_chunk":
                    continue
                document_id = record.document_id
                if document_id is not None:
                    by_document.setdefault(document_id, []).append(record.vector)
        document_records = tuple(
            ChunkVectorRecord(
                chunk_id=document_id,
                entity_id=document_id,
                entity_type="document",
                vector=_mean_vector(items),
            )
            for document_id, items in by_document.items()
            if items
        )
        file_rows = connection.execute(
            text(
                "SELECT id, owner_id FROM documents "
                "WHERE id = ANY(CAST(:document_ids AS uuid[])) AND owner_id IS NOT NULL"
            ),
            {"document_ids": [str(item) for item in document_ids]},
        ).mappings().all()
        document_vectors = {record.vector_entity_id: record.vector for record in document_records}
        by_file: dict[UUID, list[tuple[float, ...]]] = {}
        for row in file_rows:
            vector = document_vectors.get(row["id"])
            if vector is not None:
                by_file.setdefault(row["owner_id"], []).append(vector)
        file_records = tuple(
            ChunkVectorRecord(
                chunk_id=file_id,
                entity_id=file_id,
                entity_type="file",
                vector=_mean_vector(items),
            )
            for file_id, items in by_file.items()
            if items
        )
        return document_records + file_records

    def _log_processed_chunks(self, chunks: Sequence[ChunkVectorInput]) -> None:
        for chunk in chunks:
            event = (
                "chunk_vectorized"
                if chunk.entity_type == "semantic_chunk"
                else f"{chunk.entity_type}_vectorized"
            )
            _append_vectorizer_log(
                "vectorizer_processed.jsonl",
                {
                    "event": event,
                    "entity_type": chunk.entity_type,
                    "entity_id": str(chunk.vector_entity_id),
                    "chunk_id": str(chunk.chunk_id) if chunk.entity_type == "semantic_chunk" else None,
                    "document_id": str(chunk.document_id),
                    "preview": chunk_preview(chunk.body),
                },
            )

    def _log_documents_started(
        self,
        documents: Sequence[Mapping[str, Any]],
        chunks: Sequence[ChunkVectorInput],
    ) -> None:
        counts = _chunk_counts_by_document(chunks)
        for document in documents:
            _append_vectorizer_log(
                "vectorizer_activity.jsonl",
                {
                    "event": "document_vectorization_started",
                    "document_id": document.get("document_id"),
                    "file": document.get("file") or document.get("source_name"),
                    "title": document.get("title"),
                    "chunk_count": counts.get(str(document.get("document_id")), 0),
                    "provider": self._embedding_config.provider,
                    "model": self._embedding_config.model,
                    "model_version": self._embedding_config.model_version,
                    "dimension": self._embedding_config.dimension,
                },
            )

    def _log_documents_completed(
        self,
        documents: Sequence[Mapping[str, Any]],
        chunks: Sequence[ChunkVectorInput],
    ) -> None:
        counts = _chunk_counts_by_document(chunks)
        for document in documents:
            _append_vectorizer_log(
                "vectorizer_activity.jsonl",
                {
                    "event": "document_vectorized",
                    "document_id": document.get("document_id"),
                    "file": document.get("file") or document.get("source_name"),
                    "title": document.get("title"),
                    "chunk_count": counts.get(str(document.get("document_id")), 0),
                    "provider": self._embedding_config.provider,
                    "model": self._embedding_config.model,
                    "model_version": self._embedding_config.model_version,
                    "dimension": self._embedding_config.dimension,
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
            "entity_count": len(chunks),
            "entity_counts": _entity_counts(chunks),
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
        installed_vectorization_status(),
    )


_INSTALLED_VECTORIZATION_STATUS: InMemoryVectorizationStatus | None = None


def installed_vectorization_status() -> InMemoryVectorizationStatus:
    global _INSTALLED_VECTORIZATION_STATUS
    if _INSTALLED_VECTORIZATION_STATUS is None:
        _INSTALLED_VECTORIZATION_STATUS = InMemoryVectorizationStatus()
    return _INSTALLED_VECTORIZATION_STATUS


def installed_vectorization_snapshot(
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    embedding_config = runtime_embedding_config(config)
    return installed_vectorization_status().snapshot(
        database_url=database_url,
        embedding_config=embedding_config,
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
        if isinstance(item, Mapping):
            error = item.get("error")
            if error:
                raise VectorizationError(f"embedding response item {index} failed: {error}")
            raw = item.get("embedding", item.get("vector"))
        else:
            raw = item
        vectors.append(_vector(raw, index, config.dimension))
    return tuple(vectors)


def _extract_bm25_token_groups(
    response: Mapping[str, Any],
    *,
    expected_count: int,
) -> tuple[tuple[str, ...] | None, ...]:
    if not isinstance(response, Mapping):
        raise VectorizationError("embedding client returned a non-mapping response")
    results = response.get("results", response.get("embeddings"))
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes, bytearray)):
        raise VectorizationError("embedding response must contain a results sequence")
    if len(results) != expected_count:
        raise VectorizationError("embedding response count does not match input count")
    groups: list[tuple[str, ...] | None] = []
    for index, item in enumerate(results):
        if not isinstance(item, Mapping) or "bm25_tokens" not in item:
            groups.append(None)
            continue
        groups.append(_bm25_tokens(item["bm25_tokens"], index))
    return tuple(groups)


def _bm25_tokens(value: Any, index: int) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise VectorizationError(f"embedding response bm25_tokens {index} is not a sequence")
    result: list[str] = []
    for token_index, token in enumerate(value):
        if not isinstance(token, str):
            raise VectorizationError(
                f"embedding response bm25_tokens {index}.{token_index} is not a string"
            )
        normalized = " ".join(token.split())
        if normalized:
            result.append(normalized)
    return tuple(result[:256])


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


def _chunk_counts_by_document(chunks: Sequence[ChunkVectorInput]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        key = str(chunk.document_id)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _entity_counts(chunks: Sequence[ChunkVectorInput]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in chunks:
        counts[chunk.entity_type] = counts.get(chunk.entity_type, 0) + 1
    return counts


def _mean_vector(vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    if not vectors:
        raise VectorizationError("cannot average an empty vector set")
    dimension = len(vectors[0])
    if dimension <= 0:
        raise VectorizationError("cannot average empty vectors")
    totals = [0.0] * dimension
    for vector in vectors:
        if len(vector) != dimension:
            raise VectorizationError("cannot average vectors with different dimensions")
        for index, value in enumerate(vector):
            totals[index] += float(value)
    count = float(len(vectors))
    return tuple(value / count for value in totals)


def _document_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    file_value = (
        row.get("file_path")
        or row.get("file_name")
        or row.get("source_path")
        or row.get("source_name")
        or row.get("title")
        or row.get("document_id")
    )
    return {
        "document_id": str(row.get("document_id")),
        "title": row.get("title"),
        "source_name": row.get("source_name"),
        "source_path": row.get("source_path"),
        "file_name": row.get("file_name"),
        "file_path": row.get("file_path"),
        "file": file_value,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vectorizer_log_dir() -> Path:
    return Path(
        os.getenv("DOC_STORE_VECTORIZER_LOG_DIR")
        or os.getenv("DOC_STORE_EVENT_LOG_DIR", DEFAULT_LOG_DIR)
    )


def _vectorizer_status_path() -> Path:
    return _vectorizer_log_dir() / "vectorizer_status.json"


def _vectorizer_activity_path() -> Path:
    return _vectorizer_log_dir() / "vectorizer_activity.jsonl"


def _current_activity_from_logs(
    *,
    database_url: str | None,
    embedding_config: RuntimeEmbeddingConfig | None,
) -> dict[str, Any] | None:
    event = _last_vectorizer_activity_event()
    if not event or event.get("event") != "document_vectorization_started":
        return None
    document_id = event.get("document_id")
    if not document_id:
        return None
    if not _document_needs_active_vectors(
        database_url=database_url,
        document_id=str(document_id),
        embedding_config=embedding_config,
    ):
        return None
    return {
        "action": "embedding_documents",
        "current_document_id": str(document_id),
        "current_file": event.get("file"),
        "current_title": event.get("title"),
        "chunk_count": event.get("chunk_count"),
        "timestamp": event.get("timestamp"),
        "source": "vectorizer_activity_log",
    }


def _last_vectorizer_activity_event() -> dict[str, Any] | None:
    path = _vectorizer_activity_path()
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            buffer = bytearray()
            while position > 0:
                read_size = min(8192, position)
                position -= read_size
                stream.seek(position)
                chunk = stream.read(read_size)
                buffer[:0] = chunk
                while b"\n" in buffer:
                    line = bytes(buffer.rsplit(b"\n", 1)[-1]).strip()
                    buffer = bytearray(buffer.rsplit(b"\n", 1)[0])
                    if not line:
                        continue
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    return event if isinstance(event, dict) else None
            line = bytes(buffer).strip()
            if line:
                event = json.loads(line.decode("utf-8"))
                return event if isinstance(event, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _document_needs_active_vectors(
    *,
    database_url: str | None,
    document_id: str,
    embedding_config: RuntimeEmbeddingConfig | None,
) -> bool:
    if not database_url or embedding_config is None:
        return True
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            missing = connection.execute(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM ("
                    "SELECT 'document'::text AS entity_type, d.id AS entity_id "
                    "FROM documents AS d "
                    "WHERE d.deleted_at IS NULL AND d.id = CAST(:document_id AS uuid) "
                    "UNION ALL "
                    "SELECT 'paragraph'::text AS entity_type, p.id AS entity_id "
                    "FROM paragraphs AS p "
                    "WHERE p.deleted_at IS NULL AND p.document_id = CAST(:document_id AS uuid) "
                    "UNION ALL "
                    "SELECT 'semantic_chunk'::text AS entity_type, c.id AS entity_id "
                    "FROM semantic_chunks AS c "
                    "WHERE c.deleted_at IS NULL AND c.document_id = CAST(:document_id AS uuid) "
                    ") AS targets "
                    "LEFT JOIN semantic_chunk_embeddings AS e "
                    "ON e.entity_type = targets.entity_type "
                    "AND e.entity_id = targets.entity_id "
                    "AND e.active IS TRUE "
                    "AND e.provider = :provider "
                    "AND e.model = :model "
                    "AND e.model_version = :model_version "
                    "AND e.dimension = :dimension "
                    "WHERE e.id IS NULL "
                    ")"
                ),
                {
                    "document_id": document_id,
                    "provider": embedding_config.provider,
                    "model": embedding_config.model,
                    "model_version": embedding_config.model_version,
                    "dimension": embedding_config.dimension,
                },
            ).scalar_one()
    except SQLAlchemyError:
        return True
    finally:
        engine.dispose()
    return bool(missing)


def _append_vectorizer_log(filename: str, payload: Mapping[str, Any]) -> None:
    try:
        directory = _vectorizer_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), **dict(payload)}
        with (directory / filename).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


__all__ = [
    "InMemoryVectorizationStatus",
    "RuntimeVectorizationService",
    "VectorizationError",
    "installed_embedding_client",
    "installed_vectorization_service",
    "installed_vectorization_snapshot",
    "installed_vectorization_status",
]
