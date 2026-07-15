"""Runtime ingestion boundary used by the installed adapter server."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from doc_store_server.ingestion.source_normalizer import normalize_source

_INSTALLED_STATUS: InMemoryRuntimeStatus | None = None
_INSTALLED_BOUNDARY: RuntimeIngestionBoundary | None = None
_INSTALLED_BOUNDARY_URL: str | None = None


@dataclass(slots=True)
class InMemoryRuntimeStatus:
    """Best-effort status snapshots for the current server process."""

    _items: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, operation_id: str, payload: Mapping[str, Any]) -> None:
        self._items[operation_id] = dict(payload)

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
    ) -> dict[str, Any]:
        started = _utc_now()
        self._status.record(
            operation_id,
            {
                "status": "running",
                "progress": 10,
                "document_id": document_id,
                "timestamps": {"started_at": started},
            },
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

        request = result.request
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
    ) -> dict[str, Any]:
        del document_uuid
        document_id = requested_document_id
        source_version = _source_version_number(source_version_id)
        title = _title(filename, text_value)
        source_name = _source_name(filename)
        chapter_id = uuid4()
        paragraph_id = uuid4()
        chunk_id = uuid4()
        length = len(text_value)
        doc_meta = {
            "source_version_id": source_version_id,
            "normalized_source_version_id": normalized_source_version_id,
            "operation_id": operation_id,
            "ingestion_command": command,
        }
        chunk_meta = {
            **doc_meta,
            "chapter_id": str(chapter_id),
            "source_name": source_name,
        }
        engine = create_engine(self._database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                existing = connection.execute(
                    text(
                        "SELECT id FROM documents "
                        "WHERE id = :document_id "
                        "AND block_meta->>'source_version_id' = :source_version_id"
                    ),
                    {"document_id": document_id, "source_version_id": source_version_id},
                ).scalar_one_or_none()
                if existing is not None:
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
                        "source_version": source_version,
                        "chunk_ids": chunk_ids,
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
                connection.execute(
                    text(
                        "INSERT INTO chapters "
                        "(id, document_id, order_index, heading, level, source_start, source_end, block_meta) "
                        "VALUES (:id, :document_id, 0, :heading, 1, 0, :source_end, CAST(:block_meta AS jsonb))"
                    ),
                    {
                        "id": chapter_id,
                        "document_id": document_id,
                        "heading": title,
                        "source_end": length,
                        "block_meta": json.dumps(doc_meta),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO paragraphs "
                        "(id, document_id, chapter_id, order_index, text, source_start, source_end, "
                        "search_weight, block_meta) "
                        "VALUES (:id, :document_id, :chapter_id, 0, :body, 0, :source_end, 1, "
                        "CAST(:block_meta AS jsonb))"
                    ),
                    {
                        "id": paragraph_id,
                        "document_id": document_id,
                        "chapter_id": chapter_id,
                        "body": text_value,
                        "source_end": length,
                        "block_meta": json.dumps(doc_meta),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO semantic_chunks "
                        "(id, document_id, paragraph_id, chapter_id, order_index, text, source_start, "
                        "source_end, char_count, chunk_type, search_weight, block_meta) "
                        "VALUES (:id, :document_id, :paragraph_id, :chapter_id, 0, :body, 0, "
                        ":source_end, :char_count, 'DocBlock', 1, CAST(:block_meta AS jsonb))"
                    ),
                    {
                        "id": chunk_id,
                        "document_id": document_id,
                        "paragraph_id": paragraph_id,
                        "chapter_id": chapter_id,
                        "body": text_value,
                        "source_end": length,
                        "char_count": length,
                        "block_meta": json.dumps(chunk_meta),
                    },
                )
            return {
                "idempotent": False,
                "document_id": str(document_id),
                "source_version": source_version,
                "chapter_ids": (str(chapter_id),),
                "paragraph_ids": (str(paragraph_id),),
                "chunk_ids": (str(chunk_id),),
            }
        finally:
            engine.dispose()

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
        }
        self._status.record(operation_id, snapshot)
        return {"status": "rolled_back", "failure": dict(failure)}


def _source_version_number(source_version_id: str) -> int:
    digest = hashlib.sha256(source_version_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 2_147_483_646 + 1


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
