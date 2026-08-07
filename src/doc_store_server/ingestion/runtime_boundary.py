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

SOURCE_STATE_PAYLOAD_FAMILY = "source_state"
"""``TemporalAssertion.payload_family`` whose payload table is ``source_states``."""

NORMALIZATION_PROFILE_ID = "doc-store.ingestion.runtime_boundary"
"""Immutable identity of the deterministic normalization profile applied here."""

NORMALIZATION_PROFILE_VERSION = "1"
"""Version of that profile; a changed algorithm takes a new version, never a rewrite."""

NORMALIZED_SOURCE_ENCODING = "utf-8"
"""Encoding the normalizer decodes into and the reconstruction retains."""

COMPARISON_SCOPE = "document_normalized_source"
"""The one recorded scope this boundary compares: a document's normalized source."""

DEFAULT_RUN_ACTOR = "doc-store:ingestion-runtime-boundary"
RUN_ACTOR_ENV = "DOC_STORE_RUN_ACTOR"

RUN_KIND_BY_COMMAND: Mapping[str, str] = {
    "document_create": "document_create",
    "document_update": "document_update",
    # A rechunk re-derives an existing document's hierarchy from its stored
    # source and re-verifies the round trip: an existing-document recheck.
    "document_chunk": "integrity_recheck",
}
DEFAULT_RUN_KIND = "file_ingest"

RECONSTRUCTION_SEPARATORS: Mapping[str, str] = {
    # The separators ``source_file_reconstruct`` re-inserts between the stored
    # chunk payloads. They are part of the restoration manifest because without
    # them the persisted chunks do not determine one text.
    "within_paragraph": " ",
    "between_paragraphs": "\n\n",
}

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
    # Facts about the *original* bytes, retained so the immutable PrimarySource
    # can be stated from what was actually received rather than guessed. Both are
    # unknown on the rechunk path, where the File already names its original.
    media_type: str | None = None
    byte_length: int | None = None


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
    """Normalize one source and persist a minimal semantic chunk hierarchy.

    Every publication that actually executes also leaves its provenance behind:
    the immutable `PrimarySource` the File is bound to, one `ProcessingRun` for
    the single comparison scope it covered, and the `SourceState` that run
    produced, all in the publishing transaction. An idempotent replay executes
    nothing — it publishes no hierarchy and re-derives no source — so it appends
    no second run and no second state; the evidence of the execution that did
    the work stays the answer.
    """

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
                media_type=request.source_metadata.media_type,
                byte_length=request.source_metadata.byte_length,
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
        media_type: str | None = None,
        byte_length: int | None = None,
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
            media_type=media_type,
            byte_length=byte_length,
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
        media_type: str | None = None,
        byte_length: int | None = None,
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
                media_type=media_type,
                byte_length=byte_length,
            )
        finally:
            engine.dispose()

    def _persist_prepared_chunks(
        self,
        prepared: _PersistencePlan,
        paragraph_chunks: Sequence[RuntimeChunk],
        sentence_chunks: Sequence[RuntimeChunk],
    ) -> dict[str, Any]:
        """Publish one hierarchy and the provenance that makes it accountable.

        One transaction writes three things that may never disagree: the
        immutable `PrimarySource` the File is bound to, the Document, Chapter,
        Paragraph and SemanticChunk hierarchy derived from it, and the
        `ProcessingRun` plus `SourceState` evidence that says which original was
        consumed, under which deterministic configuration, and whether the
        normalized source survived the round trip. Because it is one
        transaction, a hierarchy without its run, or a run naming a hierarchy
        that was rolled back, is unrepresentable.

        The round-trip verdict is measured, not assumed: the normalized source
        is compared against the same scope read back through the reconstruction
        rule ``source_file_reconstruct`` applies, so ``status = 'succeeded'``
        (which the database only accepts on equal hashes) is earned. A
        non-match is recorded truthfully as a non-successful run carrying both
        hashes and the first differing byte offset; turning that verdict into a
        controlled integrity error for the caller belongs to the diagnostics
        step, not to this one, so publication semantics are unchanged here.
        """

        if not paragraph_chunks:
            raise ValueError("chunker returned no paragraph chunks")
        if not sentence_chunks:
            raise ValueError("chunker returned no sentence chunks")
        document_id = prepared.document_id
        run_id = uuid4()
        run_started_at = datetime.now(timezone.utc)
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                primary_source_id = _resolve_primary_source(connection, prepared)
                _mark_existing_hierarchy_deleted(connection, document_id)
                connection.execute(
                    text(
                        "UPDATE files SET is_deleted = TRUE, deleted_at = now(), updated_at = now() "
                        "WHERE owner_id = :document_id OR id = (SELECT owner_id FROM documents WHERE id = :document_id)"
                    ),
                    {"document_id": document_id},
                )
                # `primary_source_id` is required and unique: a File states which
                # immutable original it represents, or it does not exist. The
                # conflict branch deliberately leaves the column alone, because a
                # File never swaps the original it was accepted with.
                connection.execute(
                    text(
                        "INSERT INTO files "
                        "(id, owner_id, primary_source_id, path, name, media_type, byte_length, "
                        "char_count, checksum_algorithm, content_sha256, body_sha256, "
                        "needs_revectorize, needs_rechunk, is_deleted, deleted_at, block_meta) "
                        "VALUES (:id, NULL, :primary_source_id, :path, :name, 'text/plain', NULL, "
                        ":char_count, 'sha256', :content_sha256, :body_sha256, TRUE, FALSE, FALSE, "
                        "NULL, CAST(:block_meta AS jsonb)) "
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
                        "primary_source_id": primary_source_id,
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
                        _insert_semantic_chunk_initial_version(
                            connection,
                            chunk_id,
                            text_value=sentence_text,
                            text_sha256=hashlib.sha256(sentence_text.encode("utf-8")).hexdigest(),
                            char_count=len(sentence_text),
                            block_meta=json.dumps(chunk_meta),
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
                reconstructed = _reconstructed_scope_text(connection, document_id)
                reconstruction_sha256 = hashlib.sha256(
                    reconstructed.encode("utf-8")
                ).hexdigest()
                integrity_outcome, first_difference_offset = _integrity_verdict(
                    prepared.text_value, reconstructed
                )
                run_status = "succeeded" if integrity_outcome == "match" else "failed"
                _insert_processing_run(
                    connection,
                    prepared=prepared,
                    run_id=run_id,
                    primary_source_id=primary_source_id,
                    chapter_id=chapter_id,
                    status=run_status,
                    source_sha256=prepared.body_sha256,
                    reconstruction_sha256=reconstruction_sha256,
                    integrity_outcome=integrity_outcome,
                    first_difference_offset=first_difference_offset,
                    started_at=run_started_at,
                    completed_at=datetime.now(timezone.utc),
                )
                assertion_id = _append_source_state(
                    connection,
                    prepared=prepared,
                    run_id=run_id,
                    primary_source_id=primary_source_id,
                    reconstruction_sha256=reconstruction_sha256,
                    chapter_ids=chapter_ids,
                    paragraph_count=len(paragraph_ids),
                    chunk_count=len(chunk_ids),
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
                "primary_source_id": str(primary_source_id),
                "processing_run_id": str(run_id),
                "processing_run_status": run_status,
                "integrity_outcome": integrity_outcome,
                "source_state_assertion_id": str(assertion_id),
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


def _run_kind(command: str) -> str:
    """Name the allowlisted execution kind one ingestion command performs."""

    return RUN_KIND_BY_COMMAND.get(command, DEFAULT_RUN_KIND)


def _run_actor() -> str:
    """Return on whose behalf this run executed; deployments may name themselves."""

    return os.getenv(RUN_ACTOR_ENV, "").strip() or DEFAULT_RUN_ACTOR


def _normalization_parameters(prepared: _PersistencePlan) -> dict[str, Any]:
    """State the complete parameter set the run's normalization profile applied.

    Completeness is the point: together with the profile ID and version these
    values determine the normalized source, so a later execution can tell
    whether it ran under the same configuration or a different one.
    """

    return {
        "chunking_strategy": prepared.chunking_strategy,
        "chunker": "svo",
        "paragraph_and_sentence_units": True,
        "text_encoding": NORMALIZED_SOURCE_ENCODING,
        "checksum_algorithm": "sha256",
        "comparison_scope": COMPARISON_SCOPE,
        "reconstruction_separators": dict(RECONSTRUCTION_SEPARATORS),
        "normalized_source_version_id": prepared.normalized_source_version_id,
    }


def _configuration_hash(command: str, parameters: Mapping[str, Any]) -> str:
    """Hash the deterministic configuration: same inputs, same digest, always."""

    payload = {
        "normalization_profile_id": NORMALIZATION_PROFILE_ID,
        "normalization_profile_version": NORMALIZATION_PROFILE_VERSION,
        "run_kind": _run_kind(command),
        "comparison_scope": COMPARISON_SCOPE,
        "parameters": parameters,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_locator(prepared: _PersistencePlan) -> str:
    """Name where the original came from; never empty, so the original is findable."""

    return (
        prepared.filename
        or prepared.source_name
        or f"doc-store:document:{prepared.document_id}:{prepared.source_version_id}"
    )[:2048]


def _resolve_primary_source(connection: Any, prepared: _PersistencePlan) -> UUID:
    """Return the immutable original this File is bound to, creating it once.

    ``files.primary_source_id`` is required and unique, so the File row cannot
    be written before its original exists. A File that already names one keeps
    it: an original is immutable, and re-ingesting the same File is not a reason
    to mint a second original for the same bytes. Only when no binding exists
    yet is one created, from the original's own recorded facts — locator, media
    type, byte length and the SHA-256 of the *original* bytes, never of the
    normalized rendering, which belongs to the run instead.
    """

    existing = connection.execute(
        text("SELECT primary_source_id FROM files WHERE id = :file_id"),
        {"file_id": prepared.file_id},
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))

    source_id = _stable_uuid4(
        f"doc-store:primary-source:{prepared.file_id}:{prepared.content_sha256}"
    )
    provenance = {
        "captured_by": "doc_store_server.ingestion.runtime_boundary",
        "ingestion_command": prepared.command,
        "operation_id": prepared.operation_id,
        "document_id": str(prepared.document_id),
        "file_id": str(prepared.file_id),
        "source_version_id": prepared.source_version_id,
        "filename": prepared.filename,
        "source_name": prepared.source_name,
    }
    connection.execute(
        text(
            "INSERT INTO primary_sources "
            "(id, source_locator, media_type, recorded_encoding, byte_length, "
            "checksum_algorithm, original_sha256, provenance, created_at) "
            "VALUES (:id, :source_locator, :media_type, :recorded_encoding, :byte_length, "
            "'sha256', :original_sha256, CAST(:provenance AS jsonb), now()) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": source_id,
            "source_locator": _source_locator(prepared),
            "media_type": prepared.media_type,
            "recorded_encoding": NORMALIZED_SOURCE_ENCODING,
            "byte_length": prepared.byte_length,
            "original_sha256": prepared.content_sha256,
            "provenance": json.dumps(provenance),
        },
    )
    return source_id


def _reconstructed_scope_text(connection: Any, document_id: UUID) -> str:
    """Read the compared scope back exactly as ``source_file_reconstruct`` would.

    The round trip is only evidence if it is measured against the reconstruction
    callers actually get, so this mirrors that service: current, non-deleted
    chunk payloads in chapter, paragraph and chunk order, joined by a space
    inside a paragraph and a blank line between paragraphs.
    """

    rows = connection.execute(
        text(
            "SELECT sc.paragraph_id::text AS paragraph_id, sct.text AS text "
            "FROM semantic_chunks AS sc "
            "JOIN semantic_chunk_texts AS sct ON sct.chunk_uuid = sc.id "
            "JOIN paragraphs AS p ON p.id = sc.paragraph_id "
            "JOIN chapters AS c ON c.id = sc.chapter_id "
            "JOIN documents AS d ON d.id = sc.document_id "
            "WHERE sc.document_id = :document_id "
            "AND d.deleted_at IS NULL AND c.deleted_at IS NULL "
            "AND p.deleted_at IS NULL AND sc.deleted_at IS NULL "
            "AND NOT d.is_deleted AND NOT c.is_deleted "
            "AND NOT p.is_deleted AND NOT sc.is_deleted "
            "ORDER BY c.order_index ASC, p.order_index ASC, sc.order_index ASC, sc.id ASC"
        ),
        {"document_id": document_id},
    ).mappings().all()
    pieces: list[str] = []
    previous_paragraph: str | None = None
    for row in rows:
        paragraph_id = str(row["paragraph_id"])
        if previous_paragraph is None:
            separator = ""
        elif paragraph_id == previous_paragraph:
            separator = RECONSTRUCTION_SEPARATORS["within_paragraph"]
        else:
            separator = RECONSTRUCTION_SEPARATORS["between_paragraphs"]
        pieces.append(f"{separator}{row['text']}")
        previous_paragraph = paragraph_id
    return "".join(pieces)


def _integrity_verdict(source_text: str, reconstructed_text: str) -> tuple[str, int | None]:
    """Compare the normalized source with its reconstruction and say where they part.

    Returns the allowlisted outcome and the byte offset that supports it: no
    offset for a match, the first differing byte for a mismatch, and the length
    boundary when one value is a strict prefix of the other.
    """

    if source_text == reconstructed_text:
        return "match", None
    source_bytes = source_text.encode("utf-8")
    reconstructed_bytes = reconstructed_text.encode("utf-8")
    shared = min(len(source_bytes), len(reconstructed_bytes))
    offset = 0
    while offset < shared and source_bytes[offset] == reconstructed_bytes[offset]:
        offset += 1
    if offset == shared:
        return "length_boundary", shared
    return "mismatch", offset


def _insert_processing_run(
    connection: Any,
    *,
    prepared: _PersistencePlan,
    run_id: UUID,
    primary_source_id: UUID,
    chapter_id: UUID,
    status: str,
    source_sha256: str,
    reconstruction_sha256: str,
    integrity_outcome: str,
    first_difference_offset: int | None,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    """Record one immutable execution over exactly one comparison scope.

    The row is written once, at the end of the execution it describes, because
    the database refuses to let a run's audit identity drift and refuses any
    change once it is terminal. ``chunk_id`` stays null: the scope is a whole
    document and its chunks are reachable through ``document_id``; naming the
    chunk a mismatch falls in is SourceSpan diagnostics, which this boundary
    does not own. For the same reason a non-match records the hashes and the
    offset but leaves the diagnostic authorization and redaction policy unset
    rather than inventing a release decision.
    """

    parameters = _normalization_parameters(prepared)
    connection.execute(
        text(
            "INSERT INTO processing_runs "
            "(run_id, primary_source_id, run_kind, comparison_scope, actor, status, "
            "configuration_hash, normalization_profile_id, normalization_profile_version, "
            "normalization_parameters, source_encoding, reconstructed_encoding, "
            "checksum_algorithm, source_sha256, reconstruction_sha256, integrity_outcome, "
            "first_difference_offset, document_id, chapter_id, chunk_id, operation_id, "
            "processing_trace_id, started_at, completed_at, created_at) "
            "VALUES (:run_id, :primary_source_id, :run_kind, :comparison_scope, :actor, :status, "
            ":configuration_hash, :profile_id, :profile_version, "
            "CAST(:parameters AS jsonb), :source_encoding, :reconstructed_encoding, "
            "'sha256', :source_sha256, :reconstruction_sha256, :integrity_outcome, "
            ":first_difference_offset, :document_id, :chapter_id, NULL, "
            "CAST(:operation_id AS uuid), CAST(:operation_id AS uuid), "
            "CAST(:started_at AS timestamptz), CAST(:completed_at AS timestamptz), now())"
        ),
        {
            "run_id": run_id,
            "primary_source_id": primary_source_id,
            "run_kind": _run_kind(prepared.command),
            "comparison_scope": COMPARISON_SCOPE,
            "actor": _run_actor(),
            "status": status,
            "configuration_hash": _configuration_hash(prepared.command, parameters),
            "profile_id": NORMALIZATION_PROFILE_ID,
            "profile_version": NORMALIZATION_PROFILE_VERSION,
            "parameters": json.dumps(parameters, sort_keys=True),
            "source_encoding": NORMALIZED_SOURCE_ENCODING,
            "reconstructed_encoding": NORMALIZED_SOURCE_ENCODING,
            "source_sha256": source_sha256,
            "reconstruction_sha256": reconstruction_sha256,
            "integrity_outcome": integrity_outcome,
            "first_difference_offset": first_difference_offset,
            "document_id": prepared.document_id,
            "chapter_id": chapter_id,
            "operation_id": prepared.operation_id,
            "started_at": started_at,
            "completed_at": completed_at,
        },
    )


def _append_source_state(
    connection: Any,
    *,
    prepared: _PersistencePlan,
    run_id: UUID,
    primary_source_id: UUID,
    reconstruction_sha256: str,
    chapter_ids: Sequence[str],
    paragraph_count: int,
    chunk_count: int,
) -> UUID:
    """Append the document's source state as one assertion, never as a rewrite.

    The state is a payload of the shared ``TemporalAssertion`` identity, so the
    assertion UUID *is* the source-version identity a caller cites when asking
    for an as-of state. A republish appends a successor that supersedes the
    accepted state instead of editing it, which is what keeps every earlier
    known time still resolving to what was true then. The registry row is locked
    first so concurrent publications of one document keep a monotonic revision
    order.
    """

    document_id = prepared.document_id
    registered = connection.execute(
        text("SELECT entity_id FROM entity_uuid_registry WHERE entity_id = :object_id FOR UPDATE"),
        {"object_id": document_id},
    ).scalar_one_or_none()
    if registered is None:
        raise ValueError(f"document {document_id} is not a registered object")

    prior = connection.execute(
        text(
            "SELECT a.assertion_id FROM temporal_assertions AS a "
            "WHERE a.object_id = :object_id AND a.payload_family = :payload_family "
            "AND a.assertion_kind <> 'deletion' "
            "AND NOT EXISTS (SELECT 1 FROM temporal_assertions AS s "
            "WHERE s.supersedes_assertion_id = a.assertion_id) "
            "ORDER BY a.revision_no DESC LIMIT 1"
        ),
        {"object_id": document_id, "payload_family": SOURCE_STATE_PAYLOAD_FAMILY},
    ).scalar_one_or_none()

    assertion_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO temporal_assertions "
            "(assertion_id, object_id, payload_family, assertion_kind, revision_no, "
            "supersedes_assertion_id, effective_from, effective_until, recorded_at, "
            "accepted_by, accepted_via, acceptance_operation_id, acceptance_comment) "
            "SELECT CAST(:assertion_id AS uuid), :object_id, :payload_family, :assertion_kind, "
            "COALESCE(MAX(revision_no), 0) + 1, CAST(:supersedes AS uuid), "
            "now(), NULL, now(), :accepted_by, :accepted_via, "
            "CAST(:operation_id AS uuid), NULL "
            "FROM temporal_assertions WHERE object_id = :object_id"
        ),
        {
            "assertion_id": assertion_id,
            "object_id": document_id,
            "payload_family": SOURCE_STATE_PAYLOAD_FAMILY,
            "assertion_kind": "payload" if prior is None else "supersession",
            "supersedes": prior,
            "accepted_by": _run_actor(),
            "accepted_via": prepared.command,
            "operation_id": prepared.operation_id,
        },
    )

    locator = f"doc-store:source_file_reconstruct?document_id={document_id}"
    manifest = {
        "restoration": {
            "command": "source_file_reconstruct",
            "selector": {"document_id": str(document_id)},
            "separators": dict(RECONSTRUCTION_SEPARATORS),
        },
        "normalization": {
            "profile_id": NORMALIZATION_PROFILE_ID,
            "profile_version": NORMALIZATION_PROFILE_VERSION,
            "parameters": _normalization_parameters(prepared),
        },
        "hierarchy": {
            "chapter_ids": list(chapter_ids),
            "paragraph_count": paragraph_count,
            "chunk_count": chunk_count,
        },
        "source": {
            "source_version_id": prepared.source_version_id,
            "normalized_source_version_id": prepared.normalized_source_version_id,
            "char_count": prepared.length,
        },
    }
    connection.execute(
        text(
            "INSERT INTO source_states "
            "(assertion_id, primary_source_id, produced_by_run_id, manifest, "
            "reconstruction_locator, checksum_algorithm, restoration_sha256, "
            "reconstructed_sha256, recorded_encoding, comparison_scope) "
            "VALUES (:assertion_id, :primary_source_id, :produced_by_run_id, "
            "CAST(:manifest AS jsonb), :reconstruction_locator, 'sha256', "
            ":restoration_sha256, :reconstructed_sha256, :recorded_encoding, :comparison_scope)"
        ),
        {
            "assertion_id": assertion_id,
            "primary_source_id": primary_source_id,
            "produced_by_run_id": run_id,
            "manifest": json.dumps(manifest, sort_keys=True),
            "reconstruction_locator": locator[:2048],
            "restoration_sha256": prepared.body_sha256,
            "reconstructed_sha256": reconstruction_sha256,
            "recorded_encoding": NORMALIZED_SOURCE_ENCODING,
            "comparison_scope": COMPARISON_SCOPE,
        },
    )
    return assertion_id


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


def _insert_semantic_chunk_initial_version(
    connection: Any,
    chunk_id: UUID,
    *,
    text_value: str,
    text_sha256: str,
    char_count: int,
    block_meta: str,
) -> None:
    """Seed immutable version 1 and the current pointer for a new chunk."""

    version_id = connection.execute(
        text(
            "WITH inserted AS ("
            "INSERT INTO semantic_chunk_versions "
            "(chunk_uuid, logical_chunk_id, version_no, text, text_sha256, char_count, "
            "source_start, source_end, order_index, status, is_current, valid_from, "
            "operation, block_meta) "
            "SELECT sc.id, sc.id, 1, :body, :text_sha256, :char_count, "
            "sc.source_start, sc.source_end, sc.order_index, 'active', TRUE, now(), "
            "'ingest', CAST(:block_meta AS jsonb) "
            "FROM semantic_chunks AS sc WHERE sc.id = :chunk_uuid "
            "ON CONFLICT (chunk_uuid, version_no) DO NOTHING "
            "RETURNING id"
            "), existing AS ("
            "SELECT id FROM semantic_chunk_versions "
            "WHERE chunk_uuid = :chunk_uuid AND version_no = 1"
            ") "
            "SELECT id FROM inserted UNION ALL SELECT id FROM existing LIMIT 1"
        ),
        {
            "chunk_uuid": chunk_id,
            "body": text_value,
            "text_sha256": text_sha256,
            "char_count": char_count,
            "block_meta": block_meta,
        },
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO semantic_chunk_current (chunk_uuid, version_id) "
            "VALUES (:chunk_uuid, :version_id) "
            "ON CONFLICT (chunk_uuid) DO UPDATE SET "
            "version_id = EXCLUDED.version_id, updated_at = now()"
        ),
        {"chunk_uuid": chunk_id, "version_id": version_id},
    )
    for table_name in (
        "semantic_chunk_metrics",
        "semantic_chunk_feedback",
        "semantic_chunk_tokens",
        "semantic_chunk_tags",
        "semantic_chunk_type_assignments",
        "semantic_chunk_role_assignments",
        "semantic_chunk_status_assignments",
        "semantic_chunk_block_type_assignments",
        "semantic_chunk_language_assignments",
        "semantic_chunk_category_assignments",
    ):
        connection.execute(
            text(
                f"UPDATE {table_name} SET chunk_version_id = :version_id "
                "WHERE chunk_uuid = :chunk_uuid AND chunk_version_id IS NULL"
            ),
            {"chunk_uuid": chunk_id, "version_id": version_id},
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
