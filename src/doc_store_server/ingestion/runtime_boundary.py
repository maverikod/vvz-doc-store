"""Runtime ingestion boundary used by the installed adapter server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from doc_store_server.ingestion.svo_chunking import (
    ChunkerError,
    RuntimeChunk,
    SvoRuntimeChunker,
)
from doc_store_server.ingestion.source_normalizer import normalize_source
from doc_store_server.runtime.ingestion_logs import (
    log_error_event,
    log_processed_file_event,
    log_processed_text_event,
    preview_chars,
    text_preview,
)

CHUNKING_STRATEGIES = frozenset({"paragraph", "sentence", "semantic"})

_INSTALLED_STATUS: InMemoryRuntimeStatus | None = None
_INSTALLED_BOUNDARY: RuntimeIngestionBoundary | None = None
_INSTALLED_BOUNDARY_URL: str | None = None
_INSTALLED_CHUNKER: SvoRuntimeChunker | None = None
_INSTALLED_CHUNKER_KEY: tuple[tuple[str, Any], ...] | None = None


@dataclass(frozen=True, slots=True)
class _PersistencePlan:
    document_id: UUID
    source_version_id: str
    normalized_source_version_id: str
    operation_id: str
    command: str
    text_value: str
    filename: str | None
    content_sha256: str
    chunking_strategy: str
    source_version: int
    title: str
    source_name: str | None
    length: int
    body_sha256: str
    file_id: UUID
    doc_meta: Mapping[str, Any]


@dataclass(slots=True)
class InMemoryRuntimeStatus:
    """Best-effort status snapshots for the current server process."""

    _items: dict[str, dict[str, Any]] = field(default_factory=dict)
    _current: dict[str, Any] | None = None
    _last: dict[str, Any] | None = None
    _database_url: str | None = None

    def configure_database(self, database_url: str | None) -> None:
        """Attach a read-only persisted fallback for queued worker snapshots."""

        self._database_url = database_url

    def record(self, operation_id: str, payload: Mapping[str, Any]) -> None:
        value = dict(payload)
        self._items[operation_id] = value
        status = value.get("status")
        snapshot = {
            "operation_id": operation_id,
            "status": status,
            "document_id": value.get("document_id"),
            "action": value.get("worker", {}).get("action") if isinstance(value.get("worker"), Mapping) else None,
            "updated_at": _utc_now(),
        }
        if status == "running":
            self._current = snapshot
        else:
            self._last = snapshot
            if self._current and self._current.get("operation_id") == operation_id:
                self._current = None

    def get_status(self, operation_id: str, document_id: str | None = None) -> dict[str, Any]:
        value = dict(self._items.get(operation_id) or {})
        if not value:
            value = self._lookup_persisted_status(operation_id, document_id) or {
                "status": "failed",
                "failure": {
                    "code": "STATUS_NOT_FOUND",
                    "message": "operation status is not available in this server process",
                },
            }
        if document_id is not None:
            value["document_id"] = document_id
        return value

    def _lookup_persisted_status(
        self, operation_id: str, document_id: str | None
    ) -> dict[str, Any] | None:
        if not self._database_url:
            return None
        try:
            engine = create_engine(self._database_url)
            sql = (
                "SELECT id::text AS document_id, source_version, processing_status, "
                "block_meta, created_at, updated_at "
                "FROM documents "
                "WHERE block_meta->>'operation_id' = :operation_id "
            )
            params = {"operation_id": operation_id}
            if document_id is not None:
                sql += "AND id::text = :document_id "
                params["document_id"] = document_id
            sql += "ORDER BY updated_at DESC NULLS LAST LIMIT 1"
            with engine.connect() as connection:
                row = connection.execute(
                    text(sql),
                    params,
                ).mappings().first()
        except SQLAlchemyError:
            return None
        if row is None:
            return None

        block_meta = row.get("block_meta") if isinstance(row.get("block_meta"), Mapping) else {}
        timestamps = {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in {
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }.items()
            if value is not None
        }
        status = str(row.get("processing_status") or "draft")
        source_version = row.get("source_version")
        persisted_document_id = str(row["document_id"])
        version_reference: dict[str, Any] = {
            "document_id": persisted_document_id,
            "source_version": source_version,
        }
        source_version_id = block_meta.get("source_version_id")
        if source_version_id:
            version_reference["source_version_id"] = source_version_id
        return {
            "status": status,
            "progress": 100 if status == "completed" else None,
            "timestamps": timestamps,
            "document_id": persisted_document_id,
            "document_reference": {
                "id": persisted_document_id,
                "source_version": source_version,
            },
            "version_reference": version_reference,
            "failure": None,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return process-local worker activity for health reporting."""

        return {
            "state": "running" if self._current else "idle",
            "current_activity": self._current,
            "last_activity": self._last,
            "note": "process-local best-effort snapshot of this server or queue worker",
        }


class RuntimeIngestionBoundary:
    """Normalize one source and persist a minimal semantic chunk hierarchy."""

    def __init__(
        self,
        database_url: str | None,
        status: InMemoryRuntimeStatus,
        chunker: SvoRuntimeChunker | None = None,
    ) -> None:
        self._database_url = database_url
        self._status = status
        self._chunker = chunker
        self._status.configure_database(database_url)

    async def __call__(
        self,
        *,
        document_id: str,
        source_version_id: str,
        operation_id: str,
        command: str,
        raw_text: str | None = None,
        transferred_file: Mapping[str, Any] | None = None,
        chunking_strategy: str | None = None,
    ) -> dict[str, Any]:
        started = _utc_now()
        self._status.record(
            operation_id,
            {
                "status": "running",
                "progress": 10,
                "document_id": document_id,
                "worker": {"action": "normalizing_source", "command": command},
                "timestamps": {"started_at": started},
            },
        )
        if command == "document_chunk":
            return await self._rechunk_existing(
                document_id=document_id,
                source_version_id=source_version_id,
                operation_id=operation_id,
                command=command,
                started=started,
                chunking_strategy=chunking_strategy,
            )
        result = normalize_source(
            raw_text=raw_text,
            transferred_file=transferred_file,
            document_id=document_id,
        )
        if result.diagnostic is not None or result.request is None:
            failure = {
                "code": result.diagnostic.code if result.diagnostic else "NORMALIZATION_FAILED",
                "message": result.diagnostic.message if result.diagnostic else "normalization failed",
                "context": dict(result.diagnostic.context) if result.diagnostic else {},
            }
            return self._record_failed(operation_id, document_id, started, failure)
        request = result.request
        resolved_strategy = self._resolve_chunking_strategy(
            document_id=document_id,
            command=command,
            requested_strategy=chunking_strategy,
        )
        if isinstance(resolved_strategy, Mapping):
            return self._record_failed(operation_id, document_id, started, resolved_strategy)
        self._log_processed_source(
            operation_id=operation_id,
            document_id=document_id,
            source_version_id=source_version_id,
            command=command,
            text_value=request.text,
            filename=request.source_metadata.filename,
            content_sha256=request.source_metadata.content_sha256,
            transferred_file=transferred_file,
        )
        if not self._database_url:
            return self._record_failed(
                operation_id,
                document_id,
                started,
                {
                    "code": "DATABASE_NOT_CONFIGURED",
                    "message": "DOC_STORE_DATABASE_URL is not configured",
                },
            )

        self._status.record(
            operation_id,
            {
                "status": "running",
                "progress": 40,
                "document_id": document_id,
                "worker": {"action": "persisting_hierarchy_and_embeddings", "command": command},
                "timestamps": {"started_at": started},
            },
        )
        try:
            references = await self._persist_source(
                requested_document_id=UUID(document_id),
                source_version_id=source_version_id,
                normalized_source_version_id=request.source_version_id,
                operation_id=operation_id,
                command=command,
                text_value=request.text,
                filename=request.source_metadata.filename,
                content_sha256=request.source_metadata.content_sha256,
                chunking_strategy=resolved_strategy,
            )
        except ChunkerError as exc:
            return self._record_failed(
                operation_id,
                document_id,
                started,
                {
                    "code": exc.code,
                    "message": str(exc),
                    "context": exc.details,
                },
            )
        except (SQLAlchemyError, OSError, ValueError) as exc:
            return self._record_failed(
                operation_id,
                document_id,
                started,
                {
                    "code": "PERSISTENCE_FAILED",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            )

        status = "idempotent_replay" if references.get("idempotent") else "committed"
        public_status = "idempotent" if references.get("idempotent") else "completed"
        snapshot = {
            "status": public_status,
            "progress": 100,
            "document_id": document_id,
            "timestamps": {"started_at": started, "completed_at": _utc_now()},
            "document_reference": {"id": document_id, "version": references["source_version"]},
            "version_reference": references,
            "failure": None,
            "worker": {"action": "completed", "command": command},
        }
        self._status.record(operation_id, snapshot)
        return {"status": status, **references}

    async def _persist_source(
        self,
        *,
        requested_document_id: UUID,
        source_version_id: str,
        normalized_source_version_id: str,
        operation_id: str,
        command: str,
        text_value: str,
        filename: str | None,
        content_sha256: str,
        chunking_strategy: str,
    ) -> dict[str, Any]:
        prepared = await asyncio.to_thread(
            self._prepare_persistence,
            requested_document_id=requested_document_id,
            source_version_id=source_version_id,
            normalized_source_version_id=normalized_source_version_id,
            operation_id=operation_id,
            command=command,
            text_value=text_value,
            filename=filename,
            content_sha256=content_sha256,
            chunking_strategy=chunking_strategy,
        )
        if isinstance(prepared, Mapping):
            return dict(prepared)
        chunker = self._chunker or installed_svo_runtime_chunker()
        paragraph_chunks = await chunker.chunk(
            text=prepared.text_value,
            strategy="paragraph",
            source_id=str(prepared.document_id),
        )
        sentence_chunks = await _sentence_chunks_from_paragraph_batch(
            chunker=chunker,
            paragraph_chunks=paragraph_chunks,
            source_id=str(prepared.document_id),
        )
        return await asyncio.to_thread(
            self._persist_prepared_chunks,
            prepared,
            paragraph_chunks,
            sentence_chunks,
        )

    def _prepare_persistence(
        self,
        *,
        requested_document_id: UUID,
        source_version_id: str,
        normalized_source_version_id: str,
        operation_id: str,
        command: str,
        text_value: str,
        filename: str | None,
        content_sha256: str,
        chunking_strategy: str,
    ) -> dict[str, Any] | _PersistencePlan:
        document_id = requested_document_id
        source_version = _source_version_number(source_version_id)
        title = _title(filename, text_value)
        source_name = _source_name(filename)
        length = len(text_value)
        body_sha256 = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
        file_id = _stable_uuid4(f"doc-store:file:{document_id}:{body_sha256}:{filename or ''}")
        doc_meta = {
            "file_id": str(file_id),
            "source_version_id": source_version_id,
            "normalized_source_version_id": normalized_source_version_id,
            "operation_id": operation_id,
            "ingestion_command": command,
            "chunking_strategy": chunking_strategy,
            "checksum_algorithm": "sha256",
            "content_sha256": content_sha256,
            "source_sha256": body_sha256,
            "file_sha256": body_sha256,
            "body_sha256": body_sha256,
        }
        inferred = _source_properties(text_value, default_project="doc-store")
        doc_meta.update(inferred)
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                existing_state = _load_document_file_state(connection, document_id)
                if (
                    command != "document_chunk"
                    and filename is not None
                    and existing_state is not None
                    and (
                        existing_state["file_content_sha256"] == content_sha256
                        or existing_state["file_body_sha256"] == body_sha256
                    )
                    and existing_state["file_needs_rechunk"] is False
                    and existing_state["file_needs_revectorize"] is False
                    and existing_state["document_needs_revectorize"] is False
                ):
                    return _existing_references(
                        connection,
                        document_id=document_id,
                        source_version_id=source_version_id,
                        source_version=source_version,
                        chunking_strategy=chunking_strategy,
                        idempotent_reason="file_checksum_match",
                    )
                existing = connection.execute(
                    text(
                        "SELECT id, block_meta->>'chunking_strategy' AS chunking_strategy FROM documents "
                        "WHERE id = :document_id "
                        "AND block_meta->>'source_version_id' = :source_version_id"
                    ),
                    {"document_id": document_id, "source_version_id": source_version_id},
                ).mappings().one_or_none()
                if existing is not None and command != "document_chunk":
                    if (
                        existing_state is not None
                        and existing_state["file_needs_rechunk"] is False
                        and existing_state["file_needs_revectorize"] is False
                        and existing_state["document_needs_revectorize"] is False
                    ):
                        return _existing_references(
                            connection,
                            document_id=document_id,
                            source_version_id=source_version_id,
                            source_version=source_version,
                            chunking_strategy=existing.get("chunking_strategy") or chunking_strategy,
                            idempotent_reason="source_version_match",
                        )
                    if (
                        existing_state is not None
                        and existing_state["file_needs_rechunk"] is False
                        and (
                            existing_state["file_needs_revectorize"] is True
                            or existing_state["document_needs_revectorize"] is True
                        )
                    ):
                        return _existing_references(
                            connection,
                            document_id=document_id,
                            source_version_id=source_version_id,
                            source_version=source_version,
                            chunking_strategy=existing.get("chunking_strategy") or chunking_strategy,
                            idempotent=False,
                            idempotent_reason="revectorization_pending",
                        )

            return _PersistencePlan(
                document_id=document_id,
                source_version_id=source_version_id,
                normalized_source_version_id=normalized_source_version_id,
                operation_id=operation_id,
                command=command,
                text_value=text_value,
                filename=filename,
                content_sha256=content_sha256,
                chunking_strategy=chunking_strategy,
                source_version=source_version,
                title=title,
                source_name=source_name,
                length=length,
                body_sha256=body_sha256,
                file_id=file_id,
                doc_meta=doc_meta,
            )
        finally:
            engine.dispose()

    def _persist_prepared_chunks(
        self,
        prepared: _PersistencePlan,
        paragraph_chunks: Sequence[RuntimeChunk],
        sentence_chunks: Sequence[RuntimeChunk],
    ) -> dict[str, Any]:
        if not paragraph_chunks:
            raise ValueError("chunker returned no paragraph chunks")
        if not sentence_chunks:
            raise ValueError("chunker returned no sentence chunks")
        document_id = prepared.document_id
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                _mark_existing_hierarchy_deleted(connection, document_id)
                connection.execute(
                    text(
                        "UPDATE files SET is_deleted = TRUE, deleted_at = now(), updated_at = now() "
                        "WHERE owner_id = :document_id OR id = (SELECT owner_id FROM documents WHERE id = :document_id)"
                    ),
                    {"document_id": document_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO files "
                        "(id, owner_id, path, name, media_type, byte_length, char_count, "
                        "checksum_algorithm, content_sha256, body_sha256, needs_revectorize, "
                        "needs_rechunk, is_deleted, deleted_at, block_meta) "
                        "VALUES (:id, NULL, :path, :name, 'text/plain', NULL, :char_count, "
                        "'sha256', :content_sha256, :body_sha256, TRUE, FALSE, FALSE, NULL, "
                        "CAST(:block_meta AS jsonb)) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "path = EXCLUDED.path, "
                        "name = EXCLUDED.name, "
                        "media_type = EXCLUDED.media_type, "
                        "char_count = EXCLUDED.char_count, "
                        "content_sha256 = EXCLUDED.content_sha256, "
                        "body_sha256 = EXCLUDED.body_sha256, "
                        "needs_revectorize = TRUE, "
                        "needs_rechunk = FALSE, "
                        "is_deleted = FALSE, "
                        "deleted_at = NULL, "
                        "updated_at = now(), "
                        "block_meta = EXCLUDED.block_meta"
                    ),
                    {
                        "id": prepared.file_id,
                        "path": prepared.filename or prepared.source_name or str(prepared.file_id),
                        "name": prepared.source_name or prepared.filename or str(prepared.file_id),
                        "char_count": prepared.length,
                        "content_sha256": prepared.content_sha256,
                        "body_sha256": prepared.body_sha256,
                        "block_meta": json.dumps(dict(prepared.doc_meta)),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO documents "
                        "(id, owner_id, source_upload_id, source_version, source_path, source_name, source_hash, "
                        "checksum_algorithm, content_sha256, body_sha256, title, processing_status, "
                        "processing_attempt, needs_revectorize, processing_trace_id, "
                        "processing_started_at, processing_completed_at, block_meta) "
                        "VALUES (:id, :owner_id, :source_upload_id, :source_version, :source_path, :source_name, "
                        ":source_hash, 'sha256', :content_sha256, :body_sha256, :title, 'draft', "
                        "1, TRUE, :trace_id, now(), NULL, "
                        "CAST(:block_meta AS jsonb)) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "owner_id = EXCLUDED.owner_id, "
                        "source_upload_id = EXCLUDED.source_upload_id, "
                        "source_version = EXCLUDED.source_version, "
                        "source_path = EXCLUDED.source_path, "
                        "source_name = EXCLUDED.source_name, "
                        "source_hash = EXCLUDED.source_hash, "
                        "checksum_algorithm = EXCLUDED.checksum_algorithm, "
                        "content_sha256 = EXCLUDED.content_sha256, "
                        "body_sha256 = EXCLUDED.body_sha256, "
                        "title = EXCLUDED.title, "
                        "processing_status = 'draft', "
                        "processing_attempt = documents.processing_attempt + 1, "
                        "needs_revectorize = TRUE, "
                        "processing_trace_id = EXCLUDED.processing_trace_id, "
                        "processing_started_at = EXCLUDED.processing_started_at, "
                        "processing_completed_at = EXCLUDED.processing_completed_at, "
                        "updated_at = now(), "
                        "deleted_at = NULL, "
                        "block_meta = EXCLUDED.block_meta"
                    ),
                    {
                        "id": document_id,
                        "owner_id": prepared.file_id,
                        "source_upload_id": document_id,
                        "source_version": prepared.source_version,
                        "source_path": prepared.filename,
                        "source_name": prepared.source_name,
                        "source_hash": prepared.body_sha256,
                        "content_sha256": prepared.content_sha256,
                        "body_sha256": prepared.body_sha256,
                        "title": prepared.title,
                        "trace_id": UUID(prepared.operation_id),
                        "block_meta": json.dumps(dict(prepared.doc_meta)),
                    },
                )
                chapter_id = uuid4()
                chapter_ids = [str(chapter_id)]
                paragraph_ids: list[str] = []
                chunk_ids: list[str] = []
                connection.execute(
                    text(
                        "INSERT INTO chapters "
                        "(id, owner_id, document_id, order_index, heading, level, source_start, source_end, "
                        "block_meta) "
                        "VALUES (:id, :document_id, :document_id, 0, :heading, 1, 0, :source_end, "
                        "CAST(:block_meta AS jsonb))"
                    ),
                    {
                        "id": chapter_id,
                        "document_id": document_id,
                        "heading": prepared.title,
                        "source_end": prepared.length,
                        "block_meta": json.dumps(dict(prepared.doc_meta)),
                    },
                )
                for order_index, paragraph_chunk in enumerate(paragraph_chunks):
                    paragraph_text = paragraph_chunk.text
                    source_start = paragraph_chunk.start
                    source_end = paragraph_chunk.end
                    paragraph_id = uuid4()
                    chunk_features = _chunk_features(
                        paragraph_text,
                        source_name=prepared.source_name,
                        chunking_strategy="paragraph",
                    )
                    chunk_features.update(_chunk_metadata_features(paragraph_chunk.metadata))
                    paragraph_ids.append(str(paragraph_id))
                    paragraph_meta = {
                        **dict(prepared.doc_meta),
                        **_source_properties(paragraph_text),
                        **chunk_features,
                        "paragraph_number": order_index + 1,
                        "chunker": "svo",
                    }
                    connection.execute(
                        text(
                            "INSERT INTO paragraphs "
                            "(id, owner_id, document_id, chapter_id, order_index, text, source_start, "
                            "source_end, search_weight, block_meta) "
                            "VALUES (:id, :chapter_id, :document_id, :chapter_id, :order_index, :body, "
                            ":source_start, :source_end, 1, CAST(:block_meta AS jsonb))"
                        ),
                        {
                            "id": paragraph_id,
                            "document_id": document_id,
                            "chapter_id": chapter_id,
                            "order_index": order_index,
                            "body": paragraph_text,
                            "source_start": source_start,
                            "source_end": source_end,
                            "block_meta": json.dumps(paragraph_meta),
                        },
                    )
                    for sentence_chunk in _sentence_chunks_for_paragraph(
                        paragraph_chunk,
                        sentence_chunks,
                    ):
                        sentence_text = sentence_chunk.text
                        chunk_id = sentence_chunk.uuid
                        sentence_features = _chunk_features(
                            sentence_text,
                            source_name=prepared.source_name,
                            chunking_strategy="sentence",
                        )
                        sentence_features.update(_chunk_metadata_features(sentence_chunk.metadata))
                        sentence_features["block_type"] = "sentence"
                        classifier_values = _semantic_classifier_values(sentence_features)
                        dictionary_ids = _semantic_dictionary_ids(connection, classifier_values)
                        chunk_ids.append(str(chunk_id))
                        chunk_meta = {
                            **paragraph_meta,
                            **sentence_features,
                            "unit_type": "sentence",
                            "chapter_id": str(chapter_id),
                            "paragraph_id": str(paragraph_id),
                            "source_name": prepared.source_name,
                            "svo_chunk": dict(sentence_chunk.metadata),
                            **classifier_values,
                        }
                        connection.execute(
                            text(
                                "INSERT INTO semantic_chunks "
                                "(id, owner_id, document_id, paragraph_id, chapter_id, order_index, text, "
                                "source_start, source_end, char_count, chunk_type, chunk_type_id, "
                                "role_id, status_id, block_type_id, language_id, category_id, "
                                "search_weight, block_meta) "
                                "VALUES (:id, :paragraph_id, :document_id, :paragraph_id, :chapter_id, "
                                ":order_index, '', :source_start, :source_end, :char_count, "
                                ":chunk_type, :chunk_type_id, :role_id, :status_id, "
                                ":block_type_id, :language_id, :category_id, 1, "
                                "CAST(:block_meta AS jsonb))"
                            ),
                            {
                                "id": chunk_id,
                                "document_id": document_id,
                                "paragraph_id": paragraph_id,
                                "chapter_id": chapter_id,
                                "order_index": sentence_chunk.ordinal,
                                "source_start": sentence_chunk.start,
                                "source_end": sentence_chunk.end,
                                "char_count": len(sentence_text),
                                "chunk_type": classifier_values["type"],
                                "chunk_type_id": dictionary_ids["chunk_type_id"],
                                "role_id": dictionary_ids["role_id"],
                                "status_id": dictionary_ids["status_id"],
                                "block_type_id": dictionary_ids["block_type_id"],
                                "language_id": dictionary_ids["language_id"],
                                "category_id": dictionary_ids["category_id"],
                                "block_meta": json.dumps(chunk_meta),
                            },
                        )
                        connection.execute(
                            text(
                                "INSERT INTO semantic_chunk_texts "
                                "(chunk_uuid, text, text_sha256, char_count, block_meta) "
                                "VALUES (:chunk_uuid, :body, :text_sha256, :char_count, "
                                "CAST(:block_meta AS jsonb))"
                            ),
                            {
                                "chunk_uuid": chunk_id,
                                "body": sentence_text,
                                "text_sha256": hashlib.sha256(sentence_text.encode("utf-8")).hexdigest(),
                                "char_count": len(sentence_text),
                                "block_meta": json.dumps(chunk_meta),
                            },
                        )
                        _upsert_semantic_chunk_classifier_assignments(
                            connection,
                            chunk_id,
                            dictionary_ids,
                        )
                        _insert_semantic_chunk_default_metrics(connection, chunk_id)
                        for token_kind in ("tokens", "bm25_tokens"):
                            for token_ordinal, token_value in enumerate(sentence_features[token_kind]):
                                connection.execute(
                                    text(
                                        "INSERT INTO semantic_chunk_tokens "
                                        "(chunk_uuid, token_kind, ordinal, token_value) "
                                        "VALUES (:chunk_uuid, :token_kind, :ordinal, :token_value)"
                                    ),
                                    {
                                        "chunk_uuid": chunk_id,
                                        "token_kind": token_kind,
                                        "ordinal": token_ordinal,
                                        "token_value": token_value,
                                    },
                                )
                        for tag_ordinal, tag_value in enumerate(sentence_features["tags"]):
                            connection.execute(
                                text(
                                    "INSERT INTO semantic_chunk_tags "
                                    "(chunk_uuid, ordinal, tag_value) "
                                    "VALUES (:chunk_uuid, :ordinal, :tag_value)"
                                ),
                                {"chunk_uuid": chunk_id, "ordinal": tag_ordinal, "tag_value": tag_value},
                            )
            return {
                "idempotent": False,
                "document_id": str(document_id),
                "source_version_id": prepared.source_version_id,
                "source_version": prepared.source_version,
                "chapter_ids": tuple(chapter_ids),
                "paragraph_ids": tuple(paragraph_ids),
                "chunk_ids": tuple(chunk_ids),
                "chunking_strategy": prepared.chunking_strategy,
                "chunker": "svo",
            }
        finally:
            engine.dispose()

    def _resolve_chunking_strategy(
        self,
        *,
        document_id: str,
        command: str,
        requested_strategy: str | None,
    ) -> str | dict[str, Any]:
        if requested_strategy is not None:
            return _validated_chunking_strategy(requested_strategy)
        if command == "document_create":
            return {
                "code": "CHUNKING_STRATEGY_REQUIRED",
                "message": "chunking_strategy is required when creating a document",
            }
        stored = self._stored_chunking_strategy(document_id)
        if stored is None:
            return {
                "code": "CHUNKING_STRATEGY_REQUIRED",
                "message": "document has no stored chunking_strategy",
            }
        return stored

    def _stored_chunking_strategy(self, document_id: str) -> str | None:
        if not self._database_url:
            return None
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                value = connection.execute(
                    text(
                        "SELECT block_meta->>'chunking_strategy' "
                        "FROM documents WHERE id = :document_id AND deleted_at IS NULL"
                    ),
                    {"document_id": UUID(document_id)},
                ).scalar_one_or_none()
        finally:
            engine.dispose()
        if isinstance(value, str) and value in CHUNKING_STRATEGIES:
            return value
        return None

    async def _rechunk_existing(
        self,
        *,
        document_id: str,
        source_version_id: str,
        operation_id: str,
        command: str,
        started: str,
        chunking_strategy: str | None,
    ) -> dict[str, Any]:
        if not self._database_url:
            return self._record_failed(
                operation_id,
                document_id,
                started,
                {
                    "code": "DATABASE_NOT_CONFIGURED",
                    "message": "DOC_STORE_DATABASE_URL is not configured",
                },
            )
        resolved_strategy = self._resolve_chunking_strategy(
            document_id=document_id,
            command=command,
            requested_strategy=chunking_strategy,
        )
        if isinstance(resolved_strategy, Mapping):
            return self._record_failed(operation_id, document_id, started, resolved_strategy)
        source = self._load_existing_document_source(document_id)
        if isinstance(source, Mapping):
            return self._record_failed(operation_id, document_id, started, source)
        text_value, stored_source_version_id, filename, content_sha256 = source
        effective_source_version_id = source_version_id if source_version_id != "stored" else stored_source_version_id
        try:
            references = await self._persist_source(
                requested_document_id=UUID(document_id),
                source_version_id=effective_source_version_id,
                normalized_source_version_id=effective_source_version_id,
                operation_id=operation_id,
                command=command,
                text_value=text_value,
                filename=filename,
                content_sha256=content_sha256,
                chunking_strategy=resolved_strategy,
            )
        except ChunkerError as exc:
            return self._record_failed(
                operation_id,
                document_id,
                started,
                {
                    "code": exc.code,
                    "message": str(exc),
                    "context": exc.details,
                },
            )
        except (SQLAlchemyError, OSError, ValueError) as exc:
            return self._record_failed(
                operation_id,
                document_id,
                started,
                {
                    "code": "PERSISTENCE_FAILED",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            )
        self._status.record(
            operation_id,
            {
                "status": "completed",
                "progress": 100,
                "document_id": document_id,
                "timestamps": {"started_at": started, "completed_at": _utc_now()},
                "document_reference": {"id": document_id, "version": references["source_version"]},
                "version_reference": references,
                "failure": None,
                "worker": {"action": "completed", "command": command},
            },
        )
        return {"status": "committed", **references}

    def _load_existing_document_source(
        self,
        document_id: str,
    ) -> tuple[str, str, str | None, str] | dict[str, Any]:
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                document = connection.execute(
                    text(
                        "SELECT source_name, source_hash, block_meta "
                        "FROM documents WHERE id = :document_id AND deleted_at IS NULL"
                    ),
                    {"document_id": UUID(document_id)},
                ).mappings().one_or_none()
                if document is None:
                    return {"code": "DOCUMENT_NOT_FOUND", "message": "document does not exist"}
                rows = connection.execute(
                    text(
                        "SELECT text FROM paragraphs "
                        "WHERE document_id = :document_id AND deleted_at IS NULL "
                        "ORDER BY order_index ASC, id ASC"
                    ),
                    {"document_id": UUID(document_id)},
                ).scalars().all()
        finally:
            engine.dispose()
        text_value = "\n\n".join(str(row) for row in rows if str(row))
        if not text_value:
            return {"code": "DOCUMENT_TEXT_NOT_FOUND", "message": "document has no stored text"}
        meta = document.get("block_meta") if isinstance(document, Mapping) else {}
        if not isinstance(meta, Mapping):
            meta = {}
        source_version_id = str(meta.get("source_version_id") or "stored")
        content_sha256 = str(document.get("source_hash") or hashlib.sha256(text_value.encode("utf-8")).hexdigest())
        return text_value, source_version_id, document.get("source_name"), content_sha256

    def _record_failed(
        self,
        operation_id: str,
        document_id: str,
        started: str,
        failure: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot = {
            "status": "failed",
            "progress": None,
            "document_id": document_id,
            "timestamps": {"started_at": started, "completed_at": _utc_now()},
            "document_reference": None,
            "version_reference": None,
            "failure": dict(failure),
            "worker": {"action": "failed"},
        }
        self._status.record(operation_id, snapshot)
        log_error_event(
            {
                "operation_id": operation_id,
                "document_id": document_id,
                "failure": dict(failure),
            }
        )
        return {"status": "rolled_back", "failure": dict(failure)}

    @staticmethod
    def _log_processed_source(
        *,
        operation_id: str,
        document_id: str,
        source_version_id: str,
        command: str,
        text_value: str,
        filename: str | None,
        content_sha256: str,
        transferred_file: Mapping[str, Any] | None,
    ) -> None:
        common = {
            "operation_id": operation_id,
            "document_id": document_id,
            "source_version_id": source_version_id,
            "command": command,
            "content_sha256": content_sha256,
        }
        log_processed_text_event(
            {
                **common,
                "char_count": len(text_value),
                "preview_chars": preview_chars(),
                "preview": text_preview(text_value),
            }
        )
        if filename or transferred_file is not None:
            log_processed_file_event(
                {
                    **common,
                    "filename": filename,
                    "transferred_file": _file_log_descriptor(transferred_file),
                }
            )


def _source_version_number(source_version_id: str) -> int:
    digest = hashlib.sha256(source_version_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2_147_483_646 + 1


def _file_log_descriptor(transferred_file: Mapping[str, Any] | None) -> dict[str, Any]:
    if transferred_file is None:
        return {}
    descriptor: dict[str, Any] = {}
    for key in ("transfer_id", "filename", "name", "media_type", "content_type", "size_bytes", "checksum_value"):
        if key in transferred_file:
            descriptor[key] = transferred_file[key]
    if "content" in transferred_file or "data" in transferred_file:
        descriptor["content_redacted"] = True
    return descriptor


def _load_document_file_state(connection: Any, document_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            "SELECT d.needs_revectorize AS document_needs_revectorize, "
            "d.body_sha256 AS document_body_sha256, "
            "f.id AS file_id, f.content_sha256 AS file_content_sha256, "
            "f.body_sha256 AS file_body_sha256, "
            "f.needs_revectorize AS file_needs_revectorize, "
            "f.needs_rechunk AS file_needs_rechunk "
            "FROM documents d "
            "LEFT JOIN files f ON f.id = d.owner_id "
            "WHERE d.id = :document_id AND d.deleted_at IS NULL"
        ),
        {"document_id": document_id},
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        "document_needs_revectorize": bool(row["document_needs_revectorize"]),
        "document_body_sha256": row["document_body_sha256"],
        "file_id": row["file_id"],
        "file_content_sha256": row["file_content_sha256"],
        "file_body_sha256": row["file_body_sha256"],
        "file_needs_revectorize": bool(row["file_needs_revectorize"]),
        "file_needs_rechunk": bool(row["file_needs_rechunk"]),
    }


def _sentence_chunks_for_paragraph(
    paragraph_chunk: RuntimeChunk,
    sentence_chunks: Sequence[RuntimeChunk],
) -> tuple[RuntimeChunk, ...]:
    matches = [
        chunk
        for chunk in sentence_chunks
        if chunk.start >= paragraph_chunk.start and chunk.start < paragraph_chunk.end
    ]
    if not matches:
        matches = [
            chunk
            for chunk in sentence_chunks
            if chunk.end > paragraph_chunk.start and chunk.start < paragraph_chunk.end
        ]
    if not matches:
        raise ValueError(
            "sentence chunker returned no sentence chunks inside paragraph range "
            f"{paragraph_chunk.start}:{paragraph_chunk.end}"
        )
    return tuple(sorted(matches, key=lambda chunk: (chunk.start, chunk.end, chunk.ordinal)))


async def _sentence_chunks_from_paragraph_batch(
    *,
    chunker: Any,
    paragraph_chunks: Sequence[RuntimeChunk],
    source_id: str,
) -> tuple[RuntimeChunk, ...]:
    if not paragraph_chunks:
        return ()
    batch_chunk = getattr(chunker, "chunk_batch", None)
    if not callable(batch_chunk):
        raise ChunkerError(
            "CHUNKER_BATCH_UNAVAILABLE",
            "external chunker wrapper does not support paragraph sentence batches",
        )
    batches = await batch_chunk(
        texts=[chunk.text for chunk in paragraph_chunks],
        strategy="sentence",
        source_ids=[source_id for _ in paragraph_chunks],
    )
    if len(batches) != len(paragraph_chunks):
        raise ChunkerError(
            "CHUNKER_INVALID_RESPONSE",
            "sentence chunk batch count does not match paragraph count",
            {"paragraph_count": len(paragraph_chunks), "batch_count": len(batches)},
        )
    result: list[RuntimeChunk] = []
    ordinal = 0
    for paragraph, sentence_batch in zip(paragraph_chunks, batches, strict=True):
        if not sentence_batch:
            raise ChunkerError(
                "CHUNKER_EMPTY_RESULT",
                "sentence chunker returned no chunks for paragraph",
                {"paragraph_start": paragraph.start, "paragraph_end": paragraph.end},
            )
        for sentence in sentence_batch:
            start = paragraph.start + sentence.start
            end = paragraph.start + sentence.end
            if start < paragraph.start or end > paragraph.end or start >= end:
                raise ChunkerError(
                    "CHUNKER_CONTRACT_ERROR",
                    "sentence chunk range is outside its paragraph range",
                    {
                        "paragraph_start": paragraph.start,
                        "paragraph_end": paragraph.end,
                        "sentence_start": sentence.start,
                        "sentence_end": sentence.end,
                    },
                )
            result.append(
                RuntimeChunk(
                    uuid=sentence.uuid,
                    text=sentence.text,
                    start=start,
                    end=end,
                    ordinal=ordinal,
                    metadata=sentence.metadata,
                )
            )
            ordinal += 1
    return tuple(result)


def _existing_references(
    connection: Any,
    *,
    document_id: UUID,
    source_version_id: str,
    source_version: int,
    chunking_strategy: str,
    idempotent: bool = True,
    idempotent_reason: str,
) -> dict[str, Any]:
    chunk_ids = tuple(
        str(row)
        for row in connection.execute(
            text(
                "SELECT id FROM semantic_chunks "
                "WHERE document_id = :document_id AND deleted_at IS NULL "
                "ORDER BY order_index"
            ),
            {"document_id": document_id},
        ).scalars()
    )
    return {
        "idempotent": idempotent,
        "idempotent_reason": idempotent_reason,
        "document_id": str(document_id),
        "source_version_id": source_version_id,
        "source_version": source_version,
        "chunk_ids": chunk_ids,
        "chunking_strategy": chunking_strategy,
    }


def _mark_existing_hierarchy_deleted(connection: Any, document_id: UUID) -> None:
    chunk_ids = list(
        connection.execute(
            text(
                "SELECT id FROM semantic_chunks "
                "WHERE document_id = :document_id AND deleted_at IS NULL"
            ),
            {"document_id": document_id},
        ).scalars()
    )
    if chunk_ids:
        connection.execute(
            text(
                "UPDATE semantic_chunk_embeddings SET active = FALSE "
                "WHERE chunk_uuid = ANY(CAST(:chunk_ids AS uuid[]))"
            ),
            {"chunk_ids": [str(item) for item in chunk_ids]},
        )
    for table in ("semantic_chunks", "paragraphs", "chapters"):
        connection.execute(
            text(
                f"UPDATE {table} SET is_deleted = TRUE, deleted_at = now() "
                "WHERE document_id = :document_id AND deleted_at IS NULL"
            ),
            {"document_id": document_id},
        )
    connection.execute(
        text(
            "UPDATE chapters SET order_index = order_index + ("
            "SELECT COALESCE(MAX(order_index), 0) + 1 FROM chapters WHERE document_id = :document_id"
            ") WHERE document_id = :document_id AND is_deleted IS TRUE"
        ),
        {"document_id": document_id},
    )


def _clear_reprocessing_flags(connection: Any, document_id: UUID) -> None:
    connection.execute(
        text(
            "UPDATE documents SET needs_revectorize = FALSE, updated_at = now() "
            "WHERE id = :document_id"
        ),
        {"document_id": document_id},
    )
    connection.execute(
        text(
            "UPDATE files SET needs_revectorize = FALSE, needs_rechunk = FALSE, updated_at = now() "
            "WHERE id = (SELECT owner_id FROM documents WHERE id = :document_id)"
        ),
        {"document_id": document_id},
    )


def _source_name(filename: str | None) -> str | None:
    if not filename:
        return None
    return PurePosixPath(filename).name[:512]


def _title(filename: str | None, text_value: str) -> str:
    for line in text_value.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:1024]
    return (_source_name(filename) or "Untitled document")[:1024]


def _validated_chunking_strategy(value: str) -> str:
    if value not in CHUNKING_STRATEGIES:
        raise ValueError("chunking_strategy must be one of paragraph, sentence, semantic")
    return value


def _chunk_features(text_value: str, *, source_name: str | None, chunking_strategy: str) -> dict[str, Any]:
    tokens = _tokens(text_value)
    bm25_tokens = _bm25_tokens(tokens)
    tags = sorted(set(_source_properties(text_value).get("tags") or []))
    if source_name:
        tags.append(PurePosixPath(source_name).suffix.lower().lstrip(".") or "text")
    tags.append(f"chunking:{chunking_strategy}")
    category = _category(text_value, source_name=source_name)
    tags.append(f"category:{category}")
    unique_tags = sorted({tag for tag in tags if tag})
    return {
        "category": category,
        "tags": unique_tags,
        "tags_flat": ", ".join(unique_tags),
        "tokens": tokens,
        "bm25_tokens": bm25_tokens,
    }


def _chunk_metadata_features(metadata: Mapping[str, Any]) -> dict[str, Any]:
    features: dict[str, Any] = {}
    for key in ("type", "role", "status", "block_type", "language", "category"):
        value = metadata.get(key)
        if value is None:
            continue
        if hasattr(value, "value"):
            value = value.value
        value_text = str(value).strip()
        if value_text:
            features[key] = value_text
    for key in ("chunking_version", "block_id", "block_index"):
        value = metadata.get(key)
        if value is not None:
            features[key] = str(value)
    return features


def _tokens(text_value: str) -> list[str]:
    return re.findall(r"[\wА-Яа-яЁёІіЇїЄєҐґ]+", text_value.lower())[:256]


def _bm25_tokens(tokens: Sequence[str]) -> list[str]:
    stop = {"и", "в", "во", "на", "с", "со", "к", "ко", "а", "но", "the", "and", "or", "of", "to"}
    return [token for token in tokens if len(token) > 2 and token not in stop][:256]


def _category(text_value: str, *, source_name: str | None) -> str:
    lowered = f"{source_name or ''}\n{text_value[:500]}".lower()
    if "%%7d-" in lowered or "теори" in lowered:
        return "theory"
    if text_value.lstrip().startswith("#"):
        return "heading"
    return "uncategorized"


def _source_properties(text_value: str, *, default_project: str | None = None) -> dict[str, Any]:
    lowered = text_value.lower()
    project_match = re.search(r"\bproject\s+([a-z0-9_-]+)\b", lowered)
    tags = sorted(set(re.findall(r"\btag\s+([a-z0-9_-]+)\b", lowered)))
    project = project_match.group(1) if project_match else default_project
    result: dict[str, Any] = {"tags": tags}
    if project is not None:
        result["project"] = project
    return result


DICTIONARY_TABLES = {
    "chunk_types",
    "chunk_roles",
    "chunk_statuses",
    "block_types",
    "languages",
    "categories",
}

SEMANTIC_CLASSIFIER_DEFAULTS = {
    "type": "DocBlock",
    "role": "system",
    "status": "needs_review",
    "block_type": "paragraph",
    "language": "UNKNOWN",
    "category": "uncategorized",
}

SEMANTIC_CLASSIFIER_DICTIONARIES = {
    "type": ("chunk_type_id", "chunk_types"),
    "role": ("role_id", "chunk_roles"),
    "status": ("status_id", "chunk_statuses"),
    "block_type": ("block_type_id", "block_types"),
    "language": ("language_id", "languages"),
    "category": ("category_id", "categories"),
}


def _semantic_dictionary_defaults(connection: Any) -> dict[str, UUID]:
    return _semantic_dictionary_ids(connection, SEMANTIC_CLASSIFIER_DEFAULTS)


def _semantic_classifier_values(chunk_features: dict[str, Any]) -> dict[str, str]:
    values = dict(SEMANTIC_CLASSIFIER_DEFAULTS)
    for classifier_field in SEMANTIC_CLASSIFIER_DEFAULTS:
        candidate = chunk_features.get(classifier_field)
        if isinstance(candidate, str) and candidate.strip():
            values[classifier_field] = candidate.strip()
    return values


def _semantic_dictionary_ids(connection: Any, values: dict[str, str]) -> dict[str, UUID]:
    return {
        column: _dictionary_id(connection, table, values.get(field) or SEMANTIC_CLASSIFIER_DEFAULTS[field])
        for field, (column, table) in SEMANTIC_CLASSIFIER_DICTIONARIES.items()
    }


def _dictionary_id(connection: Any, table: str, descr: str) -> UUID:
    if table not in DICTIONARY_TABLES:
        raise ValueError(f"unsupported dictionary table: {table}")
    if not isinstance(descr, str) or not descr.strip():
        raise ValueError("dictionary descr must be a non-empty string")
    value = descr.strip()
    if len(value) > 100:
        raise ValueError("dictionary descr must be at most 100 characters")
    row = connection.execute(
        text(f"SELECT id FROM {table} WHERE descr = :descr"),
        {"descr": value},
    ).scalar_one_or_none()
    if row is not None:
        return row
    return connection.execute(
        text(
            f"INSERT INTO {table} (id, descr, is_deleted, deleted_at) "
            "VALUES (:id, :descr, FALSE, NULL) "
            "ON CONFLICT (descr) DO UPDATE SET "
            "is_deleted = FALSE, deleted_at = NULL, updated_at = now() "
            "RETURNING id"
        ),
        {"id": uuid4(), "descr": value},
    ).scalar_one()


SEMANTIC_CHUNK_ASSIGNMENT_TABLES = (
    ("semantic_chunk_type_assignments", "chunk_type_id"),
    ("semantic_chunk_role_assignments", "role_id"),
    ("semantic_chunk_status_assignments", "status_id"),
    ("semantic_chunk_block_type_assignments", "block_type_id"),
    ("semantic_chunk_language_assignments", "language_id"),
    ("semantic_chunk_category_assignments", "category_id"),
)


def _upsert_semantic_chunk_classifier_assignments(
    connection: Any,
    chunk_id: UUID,
    dictionary_ids: dict[str, UUID],
) -> None:
    for table, column in SEMANTIC_CHUNK_ASSIGNMENT_TABLES:
        value = dictionary_ids[column]
        connection.execute(
            text(
                f"INSERT INTO {table} (chunk_uuid, {column}) "
                f"VALUES (:chunk_uuid, :dictionary_id) "
                "ON CONFLICT (chunk_uuid) DO UPDATE SET "
                f"{column} = EXCLUDED.{column}, "
                "updated_at = now()"
            ),
            {"chunk_uuid": chunk_id, "dictionary_id": value},
        )


def _insert_semantic_chunk_default_metrics(connection: Any, chunk_id: UUID) -> None:
    """Create chunk-owned metrics rows with only semantically safe defaults."""

    connection.execute(
        text(
            "INSERT INTO semantic_chunk_metrics "
            "(chunk_uuid, quality_score, coverage, cohesion, boundary_prev, "
            "boundary_next, matches, used_in_generation, used_as_input, used_as_context) "
            "VALUES (:chunk_uuid, NULL, NULL, NULL, NULL, NULL, 0, FALSE, FALSE, FALSE)"
        ),
        {"chunk_uuid": chunk_id},
    )
    connection.execute(
        text(
            "INSERT INTO semantic_chunk_feedback "
            "(chunk_uuid, accepted, rejected, modifications) "
            "VALUES (:chunk_uuid, 0, 0, 0)"
        ),
        {"chunk_uuid": chunk_id},
    )


def _stable_uuid4(value: str) -> UUID:
    raw = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _vector_literal(values: tuple[float, ...]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_database_url_from_env() -> str | None:
    """Resolve database URL in main and queued worker processes."""

    direct = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if direct:
        return direct
    config_path = os.getenv("DOC_STORE_CONFIG")
    if not config_path:
        return None
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    database = payload.get("database") if isinstance(payload, Mapping) else None
    if not isinstance(database, Mapping):
        return None
    url = database.get("url")
    return str(url) if url else None


def installed_svo_runtime_chunker(config: Mapping[str, Any] | None = None) -> SvoRuntimeChunker:
    """Return a process-local SVO chunker wrapper for installed runtime workers."""

    global _INSTALLED_CHUNKER, _INSTALLED_CHUNKER_KEY
    options = _svo_options(config)
    key = tuple(sorted(options.items()))
    if _INSTALLED_CHUNKER is None or key != _INSTALLED_CHUNKER_KEY:
        from svo_client import SvoChunkerClient

        client = SvoChunkerClient(
            protocol=str(options["protocol"]),
            host=str(options["host"]),
            port=int(options["port"]),
            cert=_optional_option(options.get("cert")),
            key=_optional_option(options.get("key")),
            ca=_optional_option(options.get("ca")),
            check_hostname=bool(options["check_hostname"]),
            timeout=float(options["timeout"]),
            poll_interval=float(options["poll_interval"]),
        )
        chunk_sets = {
            "paragraph": _optional_option(options.get("paragraph_chunk_set")),
            "sentence": _optional_option(options.get("sentence_chunk_set")),
            "semantic": _optional_option(options.get("semantic_chunk_set")),
        }
        _INSTALLED_CHUNKER = SvoRuntimeChunker(
            client,
            chunk_sets=chunk_sets,
            language=_optional_option(options.get("language")),
            project=_optional_option(options.get("project")),
        )
        _INSTALLED_CHUNKER_KEY = key
    return _INSTALLED_CHUNKER


def _svo_options(config: Mapping[str, Any] | None) -> dict[str, Any]:
    section = config.get("svo") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = config.get("chunker") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = {}
    ssl = section.get("ssl") if isinstance(section.get("ssl"), Mapping) else {}
    cert = _config_text(ssl, section, "cert")
    key = _config_text(ssl, section, "key")
    ca = _config_text(ssl, section, "ca")
    return {
        "protocol": os.getenv("DOC_STORE_SVO_PROTOCOL", str(section.get("protocol", "https"))),
        "host": os.getenv("DOC_STORE_SVO_HOST", str(section.get("host", "svo-chunker"))),
        "port": int(os.getenv("DOC_STORE_SVO_PORT", str(section.get("port", 8009)))),
        "cert": os.getenv("DOC_STORE_SVO_CERT", cert or ""),
        "key": os.getenv("DOC_STORE_SVO_KEY", key or ""),
        "ca": os.getenv("DOC_STORE_SVO_CA", ca or ""),
        "check_hostname": _env_bool_value(
            os.getenv("DOC_STORE_SVO_CHECK_HOSTNAME"),
            bool(ssl.get("check_hostname", section.get("check_hostname", False))),
        ),
        "timeout": float(os.getenv("DOC_STORE_SVO_TIMEOUT", str(section.get("timeout", 300.0)))),
        "poll_interval": float(
            os.getenv("DOC_STORE_SVO_POLL_INTERVAL", str(section.get("poll_interval", 1.0)))
        ),
        "language": os.getenv("DOC_STORE_SVO_LANGUAGE", str(section.get("language", ""))),
        "project": os.getenv("DOC_STORE_SVO_PROJECT", str(section.get("project", ""))),
        "paragraph_chunk_set": os.getenv(
            "DOC_STORE_SVO_PARAGRAPH_CHUNK_SET",
            str(section.get("paragraph_chunk_set", "")),
        ),
        "sentence_chunk_set": os.getenv(
            "DOC_STORE_SVO_SENTENCE_CHUNK_SET",
            str(section.get("sentence_chunk_set", "")),
        ),
        "semantic_chunk_set": os.getenv(
            "DOC_STORE_SVO_SEMANTIC_CHUNK_SET",
            str(section.get("semantic_chunk_set", "")),
        ),
    }


def _config_text(primary: Mapping[str, Any], fallback: Mapping[str, Any], key: str) -> str | None:
    value = primary.get(key)
    if value is None:
        value = fallback.get(key)
    if value is None:
        return None
    return str(value)


def _optional_option(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _env_bool_value(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def installed_runtime_status() -> InMemoryRuntimeStatus:
    """Return the process-local runtime status store."""

    global _INSTALLED_STATUS
    if _INSTALLED_STATUS is None:
        _INSTALLED_STATUS = InMemoryRuntimeStatus()
    return _INSTALLED_STATUS


def installed_ingestion_boundary() -> RuntimeIngestionBoundary:
    """Return a process-local ingestion boundary for installed server workers."""

    global _INSTALLED_BOUNDARY, _INSTALLED_BOUNDARY_URL
    url = runtime_database_url_from_env()
    if _INSTALLED_BOUNDARY is None or url != _INSTALLED_BOUNDARY_URL:
        _INSTALLED_BOUNDARY_URL = url
        _INSTALLED_BOUNDARY = RuntimeIngestionBoundary(
            url,
            installed_runtime_status(),
            installed_svo_runtime_chunker(),
        )
    return _INSTALLED_BOUNDARY


__all__ = [
    "InMemoryRuntimeStatus",
    "RuntimeIngestionBoundary",
    "installed_ingestion_boundary",
    "installed_runtime_status",
    "installed_svo_runtime_chunker",
    "runtime_database_url_from_env",
]
