"""Runtime ingestion boundary used by the installed adapter server."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from doc_store_server.ingestion.source_normalizer import normalize_source
from doc_store_server.query.runtime_boundary import (
    RUNTIME_EMBEDDING_DIMENSION,
    RUNTIME_EMBEDDING_MODEL,
    RUNTIME_EMBEDDING_PROVIDER,
    RUNTIME_EMBEDDING_VERSION,
    runtime_embedding,
)
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


@dataclass(slots=True)
class InMemoryRuntimeStatus:
    """Best-effort status snapshots for the current server process."""

    _items: dict[str, dict[str, Any]] = field(default_factory=dict)
    _current: dict[str, Any] | None = None
    _last: dict[str, Any] | None = None

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
            value = {
                "status": "failed",
                "failure": {
                    "code": "STATUS_NOT_FOUND",
                    "message": "operation status is not available in this server process",
                },
            }
        if document_id is not None:
            value["document_id"] = document_id
        return value

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

    def __init__(self, database_url: str | None, status: InMemoryRuntimeStatus) -> None:
        self._database_url = database_url
        self._status = status

    def __call__(
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
            return self._rechunk_existing(
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
            references = self._persist(
                document_uuid=request.document_id,
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

    def _persist(
        self,
        *,
        document_uuid: UUID,
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
        del document_uuid
        document_id = requested_document_id
        source_version = _source_version_number(source_version_id)
        title = _title(filename, text_value)
        source_name = _source_name(filename)
        length = len(text_value)
        chunks = _chunk_units(text_value, chunking_strategy) or [(text_value, 0, length)]
        doc_meta = {
            "source_version_id": source_version_id,
            "normalized_source_version_id": normalized_source_version_id,
            "operation_id": operation_id,
            "ingestion_command": command,
            "chunking_strategy": chunking_strategy,
        }
        inferred = _source_properties(text_value, default_project="doc-store")
        doc_meta.update(inferred)
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                existing = connection.execute(
                    text(
                        "SELECT id, block_meta->>'chunking_strategy' AS chunking_strategy FROM documents "
                        "WHERE id = :document_id "
                        "AND block_meta->>'source_version_id' = :source_version_id"
                    ),
                    {"document_id": document_id, "source_version_id": source_version_id},
                ).mappings().one_or_none()
                if existing is not None and command != "document_chunk":
                    chunk_ids = tuple(
                        str(row)
                        for row in connection.execute(
                            text(
                                "SELECT id FROM semantic_chunks "
                                "WHERE document_id = :document_id ORDER BY order_index"
                            ),
                            {"document_id": document_id},
                        ).scalars()
                    )
                    return {
                        "idempotent": True,
                        "document_id": str(document_id),
                        "source_version_id": source_version_id,
                        "source_version": source_version,
                        "chunk_ids": chunk_ids,
                        "chunking_strategy": existing.get("chunking_strategy") or chunking_strategy,
                    }

                connection.execute(
                    text("DELETE FROM chapters WHERE document_id = :document_id"),
                    {"document_id": document_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO documents "
                        "(id, source_upload_id, source_version, source_path, source_name, source_hash, "
                        "title, processing_status, processing_attempt, processing_trace_id, "
                        "processing_started_at, processing_completed_at, block_meta) "
                        "VALUES (:id, :source_upload_id, :source_version, :source_path, :source_name, "
                        ":source_hash, :title, 'completed', 1, :trace_id, now(), now(), "
                        "CAST(:block_meta AS jsonb)) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "source_upload_id = EXCLUDED.source_upload_id, "
                        "source_version = EXCLUDED.source_version, "
                        "source_path = EXCLUDED.source_path, "
                        "source_name = EXCLUDED.source_name, "
                        "source_hash = EXCLUDED.source_hash, "
                        "title = EXCLUDED.title, "
                        "processing_status = 'completed', "
                        "processing_attempt = documents.processing_attempt + 1, "
                        "processing_trace_id = EXCLUDED.processing_trace_id, "
                        "processing_started_at = EXCLUDED.processing_started_at, "
                        "processing_completed_at = EXCLUDED.processing_completed_at, "
                        "updated_at = now(), "
                        "deleted_at = NULL, "
                        "block_meta = EXCLUDED.block_meta"
                    ),
                    {
                        "id": document_id,
                        "source_upload_id": document_id,
                        "source_version": source_version,
                        "source_path": filename,
                        "source_name": source_name,
                        "source_hash": content_sha256,
                        "title": title,
                        "trace_id": UUID(operation_id),
                        "block_meta": json.dumps(doc_meta),
                    },
                )
                chapter_id = uuid4()
                chapter_ids = [str(chapter_id)]
                paragraph_ids: list[str] = []
                chunk_ids: list[str] = []
                connection.execute(
                    text(
                        "INSERT INTO chapters "
                        "(id, document_id, order_index, heading, level, source_start, source_end, "
                        "block_meta) "
                        "VALUES (:id, :document_id, 0, :heading, 1, 0, :source_end, "
                        "CAST(:block_meta AS jsonb))"
                    ),
                    {
                        "id": chapter_id,
                        "document_id": document_id,
                        "heading": title,
                        "source_end": length,
                        "block_meta": json.dumps(doc_meta),
                    },
                )
                for order_index, (paragraph_text, source_start, source_end) in enumerate(chunks):
                    paragraph_id = uuid4()
                    chunk_id = uuid4()
                    paragraph_ids.append(str(paragraph_id))
                    chunk_ids.append(str(chunk_id))
                    paragraph_meta = {
                        **doc_meta,
                        **_source_properties(paragraph_text),
                        "paragraph_number": order_index + 1,
                    }
                    chunk_meta = {
                        **paragraph_meta,
                        "chapter_id": str(chapter_id),
                        "source_name": source_name,
                        "type": "DocBlock",
                    }
                    connection.execute(
                        text(
                            "INSERT INTO paragraphs "
                            "(id, document_id, chapter_id, order_index, text, source_start, "
                            "source_end, search_weight, block_meta) "
                            "VALUES (:id, :document_id, :chapter_id, :order_index, :body, "
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
                    connection.execute(
                        text(
                            "INSERT INTO semantic_chunks "
                            "(id, document_id, paragraph_id, chapter_id, order_index, text, "
                            "source_start, source_end, char_count, chunk_type, search_weight, "
                            "block_meta) "
                            "VALUES (:id, :document_id, :paragraph_id, :chapter_id, "
                            ":order_index, :body, :source_start, :source_end, :char_count, "
                            "'DocBlock', 1, CAST(:block_meta AS jsonb))"
                        ),
                        {
                            "id": chunk_id,
                            "document_id": document_id,
                            "paragraph_id": paragraph_id,
                            "chapter_id": chapter_id,
                            "order_index": order_index,
                            "body": paragraph_text,
                            "source_start": source_start,
                            "source_end": source_end,
                            "char_count": len(paragraph_text),
                            "block_meta": json.dumps(chunk_meta),
                        },
                    )
                    connection.execute(
                        text(
                            "INSERT INTO semantic_chunk_embeddings "
                            "(chunk_uuid, vector, model, dimension, provider, model_version, active) "
                            "VALUES (:chunk_uuid, CAST(:vector AS vector), :model, :dimension, :provider, "
                            ":model_version, TRUE)"
                        ),
                        {
                            "chunk_uuid": chunk_id,
                            "vector": _vector_literal(runtime_embedding(paragraph_text)),
                            "model": RUNTIME_EMBEDDING_MODEL,
                            "dimension": RUNTIME_EMBEDDING_DIMENSION,
                            "provider": RUNTIME_EMBEDDING_PROVIDER,
                            "model_version": RUNTIME_EMBEDDING_VERSION,
                        },
                    )
            return {
                "idempotent": False,
                "document_id": str(document_id),
                "source_version_id": source_version_id,
                "source_version": source_version,
                "chapter_ids": tuple(chapter_ids),
                "paragraph_ids": tuple(paragraph_ids),
                "chunk_ids": tuple(chunk_ids),
                "chunking_strategy": chunking_strategy,
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

    def _rechunk_existing(
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
            references = self._persist(
                document_uuid=UUID(document_id),
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


def _paragraphs(text_value: str) -> list[tuple[str, int, int]]:
    paragraphs: list[tuple[str, int, int]] = []
    for match in re.finditer(r"\S[\s\S]*?(?=(?:\r?\n[ \t]*){2,}|\Z)", text_value):
        raw_paragraph = match.group(0)
        paragraph = raw_paragraph.strip()
        if paragraph:
            source_start = match.start() + len(raw_paragraph) - len(raw_paragraph.lstrip())
            source_end = match.end() - (len(raw_paragraph) - len(raw_paragraph.rstrip()))
            paragraphs.append((paragraph, source_start, source_end))
    return paragraphs


def _sentences(text_value: str) -> list[tuple[str, int, int]]:
    sentences: list[tuple[str, int, int]] = []
    for match in re.finditer(r"\S[\s\S]*?(?:(?<=[.!?])(?=\s+|$)|\Z)", text_value):
        raw_sentence = match.group(0)
        sentence = raw_sentence.strip()
        if sentence:
            source_start = match.start() + len(raw_sentence) - len(raw_sentence.lstrip())
            source_end = match.end() - (len(raw_sentence) - len(raw_sentence.rstrip()))
            sentences.append((sentence, source_start, source_end))
    return sentences


def _semantic_units(text_value: str) -> list[tuple[str, int, int]]:
    units = _paragraphs(text_value) or _sentences(text_value)
    if not units:
        return []
    grouped: list[tuple[str, int, int]] = []
    current_text: list[str] = []
    current_start: int | None = None
    current_end = 0
    for unit_text, source_start, source_end in units:
        proposed_length = sum(len(item) for item in current_text) + len(unit_text)
        if current_text and proposed_length > 700:
            grouped.append(("\n\n".join(current_text), current_start or 0, current_end))
            current_text = []
            current_start = None
        if current_start is None:
            current_start = source_start
        current_text.append(unit_text)
        current_end = source_end
    if current_text:
        grouped.append(("\n\n".join(current_text), current_start or 0, current_end))
    return grouped


def _validated_chunking_strategy(value: str) -> str:
    if value not in CHUNKING_STRATEGIES:
        raise ValueError("chunking_strategy must be one of paragraph, sentence, semantic")
    return value


def _chunk_units(text_value: str, strategy: str) -> list[tuple[str, int, int]]:
    strategy = _validated_chunking_strategy(strategy)
    if strategy == "paragraph":
        return _paragraphs(text_value)
    if strategy == "sentence":
        return _sentences(text_value)
    return _semantic_units(text_value)


def _source_properties(text_value: str, *, default_project: str | None = None) -> dict[str, Any]:
    lowered = text_value.lower()
    project_match = re.search(r"\bproject\s+([a-z0-9_-]+)\b", lowered)
    tags = sorted(set(re.findall(r"\btag\s+([a-z0-9_-]+)\b", lowered)))
    project = project_match.group(1) if project_match else default_project
    result: dict[str, Any] = {"tags": tags}
    if project is not None:
        result["project"] = project
    return result


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
        _INSTALLED_BOUNDARY = RuntimeIngestionBoundary(url, installed_runtime_status())
    return _INSTALLED_BOUNDARY


__all__ = [
    "InMemoryRuntimeStatus",
    "RuntimeIngestionBoundary",
    "installed_ingestion_boundary",
    "installed_runtime_status",
    "runtime_database_url_from_env",
]
