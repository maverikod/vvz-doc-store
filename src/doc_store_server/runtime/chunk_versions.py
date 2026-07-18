"""Versioned lifecycle operations for semantic chunk text."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import os
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config


VERSION_TABLE = "semantic_chunk_versions"
CURRENT_TABLE = "semantic_chunk_current"
LAST_VERSION_DELETE_CODE = "LAST_VERSION_DELETE_REQUIRES_CHUNK_DELETE"
LAST_VERSION_DELETE_MESSAGE = "delete the chunk instead of deleting its last text version"


class ChunkTextVersionError(RuntimeError):
    """Raised when a semantic chunk text version operation cannot be completed."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class ChunkTextVersionService:
    """Append and manage semantic chunk text versions and their current projection."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def list_versions(self, *, chunk_id: str) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            rows = connection.execute(
                text(
                    "SELECT id, chunk_uuid, version_no, text, text_sha256, char_count, "
                    "created_at, updated_at, block_meta "
                    f"FROM {VERSION_TABLE} WHERE chunk_uuid = :chunk_uuid "
                    "ORDER BY version_no ASC"
                ),
                {"chunk_uuid": chunk_uuid},
            ).mappings().all()
            current = connection.execute(
                text(
                    f"SELECT v.id FROM {CURRENT_TABLE} AS c "
                    f"JOIN {VERSION_TABLE} AS v ON v.id = c.version_id "
                    "WHERE c.chunk_uuid = :chunk_uuid"
                ),
                {"chunk_uuid": chunk_uuid},
            ).scalar_one_or_none()
        items = [_json_row(row) for row in rows]
        for item in items:
            item["is_current"] = item["id"] == str(current)
        return {"chunk_id": str(chunk_uuid), "items": items, "total": len(items)}

    def append_version(self, *, chunk_id: str, text_value: str) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        if not isinstance(text_value, str):
            raise ValueError("text must be a string")
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            next_version = connection.execute(
                text(
                    f"SELECT COALESCE(MAX(version_no), 0) + 1 FROM {VERSION_TABLE} "
                    "WHERE chunk_uuid = :chunk_uuid"
                ),
                {"chunk_uuid": chunk_uuid},
            ).scalar_one()
            row = self._insert_version(connection, chunk_uuid, int(next_version), text_value)
            self._activate(connection, chunk_uuid, row["id"], text_value, row["text_sha256"])
        return {"chunk_id": str(chunk_uuid), "outcome": "appended", "version": _json_row(row)}

    update_text = append_version

    def set_current(self, *, chunk_id: str, version_no: int) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        version = _version_number(version_no)
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            row = self._version(connection, chunk_uuid, version)
            if row is None:
                raise ChunkTextVersionError(
                    "VERSION_NOT_FOUND",
                    "semantic chunk text version was not found",
                    {"chunk_id": str(chunk_uuid), "version_no": version},
                )
            self._activate(connection, chunk_uuid, row["id"], str(row["text"]), str(row["text_sha256"]))
        return {"chunk_id": str(chunk_uuid), "outcome": "set_current", "version": _json_row(row)}

    def delete_version(self, *, chunk_id: str, version_no: int) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        version = _version_number(version_no)
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            row = self._version(connection, chunk_uuid, version)
            if row is None:
                raise ChunkTextVersionError(
                    "VERSION_NOT_FOUND",
                    "semantic chunk text version was not found",
                    {"chunk_id": str(chunk_uuid), "version_no": version},
                )
            count = int(
                connection.execute(
                    text(f"SELECT count(*) FROM {VERSION_TABLE} WHERE chunk_uuid = :chunk_uuid"),
                    {"chunk_uuid": chunk_uuid},
                ).scalar_one()
            )
            if count == 1:
                raise ChunkTextVersionError(
                    LAST_VERSION_DELETE_CODE,
                    LAST_VERSION_DELETE_MESSAGE,
                    {"chunk_id": str(chunk_uuid), "version_no": version},
                )
            connection.execute(
                text(f"DELETE FROM {VERSION_TABLE} WHERE chunk_uuid = :chunk_uuid AND version_no = :version_no"),
                {"chunk_uuid": chunk_uuid, "version_no": version},
            )
            current_id = connection.execute(
                text(f"SELECT version_id FROM {CURRENT_TABLE} WHERE chunk_uuid = :chunk_uuid"),
                {"chunk_uuid": chunk_uuid},
            ).scalar_one_or_none()
            if current_id == row["id"]:
                replacement = connection.execute(
                    text(
                        f"SELECT * FROM {VERSION_TABLE} WHERE chunk_uuid = :chunk_uuid "
                        "ORDER BY version_no DESC LIMIT 1"
                    ),
                    {"chunk_uuid": chunk_uuid},
                ).mappings().one()
                self._activate(
                    connection,
                    chunk_uuid,
                    replacement["id"],
                    str(replacement["text"]),
                    str(replacement["text_sha256"]),
                )
        return {
            "chunk_id": str(chunk_uuid),
            "outcome": "deleted",
            "deleted_version_no": version,
            "current_version_no": self._current_version_no(chunk_uuid),
        }

    def _insert_version(self, connection: Connection, chunk_uuid: UUID, version_no: int, value: str) -> Mapping[str, Any]:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return connection.execute(
            text(
                f"INSERT INTO {VERSION_TABLE} "
                "(chunk_uuid, version_no, text, text_sha256, char_count, block_meta) "
                "VALUES (:chunk_uuid, :version_no, :text, :text_sha256, :char_count, CAST(:block_meta AS jsonb)) "
                "RETURNING id, chunk_uuid, version_no, text, text_sha256, char_count, created_at, updated_at, block_meta"
            ),
            {
                "chunk_uuid": chunk_uuid,
                "version_no": version_no,
                "text": value,
                "text_sha256": digest,
                "char_count": len(value),
                "block_meta": "{}",
            },
        ).mappings().one()

    def _activate(self, connection: Connection, chunk_uuid: UUID, version_id: UUID, value: str, digest: str) -> None:
        connection.execute(
            text(
                f"INSERT INTO {CURRENT_TABLE} (chunk_uuid, version_id) "
                "VALUES (:chunk_uuid, :version_id) "
                "ON CONFLICT (chunk_uuid) DO UPDATE SET version_id = EXCLUDED.version_id, updated_at = now()"
            ),
            {"chunk_uuid": chunk_uuid, "version_id": version_id},
        )
        connection.execute(
            text(
                "UPDATE semantic_chunk_texts SET text = :text, text_sha256 = :text_sha256, "
                "char_count = :char_count, updated_at = now() WHERE chunk_uuid = :chunk_uuid"
            ),
            {"chunk_uuid": chunk_uuid, "text": value, "text_sha256": digest, "char_count": len(value)},
        )
        connection.execute(
            text(
                "UPDATE semantic_chunks SET text = '', char_count = :char_count WHERE id = :chunk_uuid"
            ),
            {"chunk_uuid": chunk_uuid, "char_count": len(value)},
        )
        self._invalidate_derived_rows(connection, chunk_uuid)

    @staticmethod
    def _invalidate_derived_rows(connection: Connection, chunk_uuid: UUID) -> None:
        for statement in (
            "DELETE FROM semantic_chunk_feedback WHERE chunk_uuid = :chunk_uuid",
            "DELETE FROM semantic_chunk_metrics WHERE chunk_uuid = :chunk_uuid",
            "DELETE FROM semantic_chunk_tokens WHERE chunk_uuid = :chunk_uuid",
            "DELETE FROM semantic_chunk_tags WHERE chunk_uuid = :chunk_uuid",
            "UPDATE semantic_chunk_embeddings SET active = FALSE WHERE entity_type = 'semantic_chunk' AND entity_id = :chunk_uuid",
        ):
            connection.execute(text(statement), {"chunk_uuid": chunk_uuid})

    def _version(self, connection: Connection, chunk_uuid: UUID, version_no: int) -> Mapping[str, Any] | None:
        return connection.execute(
            text(f"SELECT * FROM {VERSION_TABLE} WHERE chunk_uuid = :chunk_uuid AND version_no = :version_no FOR UPDATE"),
            {"chunk_uuid": chunk_uuid, "version_no": version_no},
        ).mappings().one_or_none()

    @staticmethod
    def _require_chunk(connection: Connection, chunk_uuid: UUID) -> None:
        if connection.execute(text("SELECT id FROM semantic_chunks WHERE id = :chunk_uuid FOR UPDATE"), {"chunk_uuid": chunk_uuid}).scalar_one_or_none() is None:
            raise LookupError(str(chunk_uuid))

    def _current_version_no(self, chunk_uuid: UUID) -> int | None:
        with self._transaction() as connection:
            row = connection.execute(
                text(
                    f"SELECT v.version_no FROM {CURRENT_TABLE} AS c "
                    f"JOIN {VERSION_TABLE} AS v ON v.id = c.version_id "
                    "WHERE c.chunk_uuid = :chunk_uuid"
                ),
                {"chunk_uuid": chunk_uuid},
            ).scalar_one_or_none()
        return int(row) if row is not None else None

    def _engine(self) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        return create_engine(self._database_url, pool_pre_ping=True)

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        engine = self._engine()
        try:
            with engine.begin() as connection:
                yield connection
        finally:
            engine.dispose()


def installed_chunk_text_version_service(config: Mapping[str, Any] | None = None) -> ChunkTextVersionService | None:
    database_url = database_url_from_config(config or {}) or os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    return ChunkTextVersionService(database_url) if database_url else None


def _version_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("version_no must be a positive integer")
    return value


def _json_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
        elif isinstance(value, date):
            result[key] = value.isoformat()
    return result


__all__ = [
    "ChunkTextVersionError",
    "ChunkTextVersionService",
    "LAST_VERSION_DELETE_CODE",
    "LAST_VERSION_DELETE_MESSAGE",
    "installed_chunk_text_version_service",
]
