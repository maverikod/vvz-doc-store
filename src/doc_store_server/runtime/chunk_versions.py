"""Versioned lifecycle operations for semantic chunk text."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime
import difflib
import hashlib
import json
import os
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config
from doc_store_server.runtime.previews import chunk_preview


VERSION_TABLE = "semantic_chunk_versions"
CURRENT_TABLE = "semantic_chunk_current"
LAST_VERSION_DELETE_CODE = "LAST_VERSION_DELETE_REQUIRES_CHUNK_DELETE"
LAST_VERSION_DELETE_MESSAGE = "delete the chunk instead of deleting its last text version"
CURRENT_VERSION_RETIRE_CODE = "CURRENT_VERSION_RETIRE_REQUIRES_REPLACEMENT"
CURRENT_VERSION_MISMATCH_CODE = "CURRENT_VERSION_MISMATCH"

_VERSION_ROW_COLUMNS = (
    "id, logical_chunk_id, chunk_uuid, previous_version_id, restored_from_version_id, "
    "version_no, text, text_sha256, char_count, source_version_id, source_start, source_end, "
    "order_index, status, is_current, valid_from, valid_to, comment, actor, operation, "
    "operation_id, deleted_at, created_at, updated_at, block_meta"
)
_VERSION_ROW_COLUMN_NAMES = tuple(column.strip() for column in _VERSION_ROW_COLUMNS.split(","))
_VERSION_ROW_COLUMNS_V = ", ".join(f"v.{column} AS {column}" for column in _VERSION_ROW_COLUMN_NAMES)

_VERSION_DEPENDENT_ASSIGNMENTS = (
    "semantic_chunk_type_assignments",
    "semantic_chunk_role_assignments",
    "semantic_chunk_status_assignments",
    "semantic_chunk_block_type_assignments",
    "semantic_chunk_language_assignments",
    "semantic_chunk_category_assignments",
)


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

    def list_versions(self, *, chunk_id: str, include_deleted: bool = False) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            rows = connection.execute(
                text(
                    f"SELECT {_VERSION_ROW_COLUMNS} FROM {VERSION_TABLE} "
                    "WHERE chunk_uuid = :chunk_uuid "
                    "AND (:include_deleted OR deleted_at IS NULL) "
                    "ORDER BY version_no ASC"
                ),
                {"chunk_uuid": chunk_uuid, "include_deleted": include_deleted},
            ).mappings().all()
        items = [_version_payload(row, include_text=False) for row in rows]
        return {"chunk_id": str(chunk_uuid), "items": items, "total": len(items)}

    history = list_versions

    def get_version(
        self,
        *,
        chunk_id: str,
        version_no: int | None = None,
        current: bool = False,
        include_text: bool = True,
    ) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            row = self._current_version(connection, chunk_uuid) if current or version_no is None else self._version(connection, chunk_uuid, _version_number(version_no))
            if row is None:
                raise ChunkTextVersionError(
                    "VERSION_NOT_FOUND",
                    "semantic chunk text version was not found",
                    {"chunk_id": str(chunk_uuid), "version_no": version_no, "current": current},
                )
        return {"chunk_id": str(chunk_uuid), "version": _version_payload(row, include_text=include_text)}

    def append_version(
        self,
        *,
        chunk_id: str,
        text_value: str,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: int | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        return self._append_and_activate(
            chunk_id=chunk_id,
            text_value=text_value,
            comment=comment,
            actor=actor,
            operation="append",
            expected_current_version=expected_current_version,
            operation_id=operation_id,
        )

    def update_text(
        self,
        *,
        chunk_id: str,
        text_value: str,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: int | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        return self._append_and_activate(
            chunk_id=chunk_id,
            text_value=text_value,
            comment=comment,
            actor=actor,
            operation="update",
            expected_current_version=expected_current_version,
            operation_id=operation_id,
        )

    def restore_version(
        self,
        *,
        chunk_id: str,
        version_no: int,
        comment: str | None = None,
        actor: str | None = None,
        expected_current_version: int | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        version = _version_number(version_no)
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            source = self._version(connection, chunk_uuid, version)
            if source is None:
                raise ChunkTextVersionError(
                    "VERSION_NOT_FOUND",
                    "semantic chunk text version was not found",
                    {"chunk_id": str(chunk_uuid), "version_no": version},
                )
            self._assert_expected_current(connection, chunk_uuid, expected_current_version)
            next_version = self._next_version_no(connection, chunk_uuid)
            row = self._insert_version(
                connection,
                chunk_uuid,
                int(next_version),
                str(source["text"]),
                comment=comment,
                actor=actor,
                operation="restore",
                operation_id=operation_id,
                restored_from_version_id=source["id"],
            )
            row = self._activate(connection, chunk_uuid, row, comment=comment, actor=actor, operation="restore")
        return {"chunk_id": str(chunk_uuid), "outcome": "restored", "version": _version_payload(row)}

    def set_current(
        self,
        *,
        chunk_id: str,
        version_no: int,
        comment: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        version = _version_number(version_no)
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            row = self._version(connection, chunk_uuid, version)
            if row is None or row.get("deleted_at") is not None:
                raise ChunkTextVersionError(
                    "VERSION_NOT_FOUND",
                    "semantic chunk text version was not found",
                    {"chunk_id": str(chunk_uuid), "version_no": version},
                )
            row = self._activate(connection, chunk_uuid, row, comment=comment, actor=actor, operation="set_current")
        return {"chunk_id": str(chunk_uuid), "outcome": "set_current", "version": _version_payload(row)}

    def retire_version(
        self,
        *,
        chunk_id: str,
        version_no: int,
        replacement_version_no: int | None = None,
        comment: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        version = _version_number(version_no)
        replacement = _optional_version_number(replacement_version_no, "replacement_version_no")
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            row = self._version(connection, chunk_uuid, version)
            if row is None or row.get("deleted_at") is not None:
                raise ChunkTextVersionError(
                    "VERSION_NOT_FOUND",
                    "semantic chunk text version was not found",
                    {"chunk_id": str(chunk_uuid), "version_no": version},
                )
            active_count = int(
                connection.execute(
                    text(
                        f"SELECT count(*) FROM {VERSION_TABLE} "
                        "WHERE chunk_uuid = :chunk_uuid AND deleted_at IS NULL AND status <> 'deleted'"
                    ),
                    {"chunk_uuid": chunk_uuid},
                ).scalar_one()
            )
            if active_count == 1:
                raise ChunkTextVersionError(
                    LAST_VERSION_DELETE_CODE,
                    LAST_VERSION_DELETE_MESSAGE,
                    {"chunk_id": str(chunk_uuid), "version_no": version},
                )
            replacement_row = None
            if bool(row["is_current"]):
                if replacement is None:
                    raise ChunkTextVersionError(
                        CURRENT_VERSION_RETIRE_CODE,
                        "retiring the current chunk version requires replacement_version_no",
                        {"chunk_id": str(chunk_uuid), "version_no": version},
                    )
                replacement_row = self._version(connection, chunk_uuid, replacement)
                if replacement_row is None or replacement_row.get("deleted_at") is not None or replacement == version:
                    raise ChunkTextVersionError(
                        "VERSION_NOT_FOUND",
                        "replacement semantic chunk text version was not found",
                        {"chunk_id": str(chunk_uuid), "replacement_version_no": replacement},
                    )
                replacement_row = self._activate(connection, chunk_uuid, replacement_row, comment=comment, actor=actor, operation="retire")
            connection.execute(
                text(
                    f"UPDATE {VERSION_TABLE} SET status = 'retired', is_current = FALSE, "
                    "valid_to = COALESCE(valid_to, now()), comment = COALESCE(:comment, comment), "
                    "actor = COALESCE(:actor, actor), operation = 'retire', updated_at = now() "
                    "WHERE id = :version_id"
                ),
                {"version_id": row["id"], "comment": comment, "actor": actor},
            )
        return {
            "chunk_id": str(chunk_uuid),
            "outcome": "retired",
            "retired_version_no": version,
            "current_version_no": replacement if replacement_row is not None else self._current_version_no(chunk_uuid),
        }

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
                        f"SELECT {_VERSION_ROW_COLUMNS} FROM {VERSION_TABLE} WHERE chunk_uuid = :chunk_uuid "
                        "AND deleted_at IS NULL ORDER BY version_no DESC LIMIT 1"
                    ),
                    {"chunk_uuid": chunk_uuid},
                ).mappings().one()
                self._activate(connection, chunk_uuid, replacement, operation="delete")
        return {
            "chunk_id": str(chunk_uuid),
            "outcome": "deleted",
            "deleted_version_no": version,
            "current_version_no": self._current_version_no(chunk_uuid),
        }

    def diff_versions(
        self,
        *,
        chunk_id: str,
        from_version_no: int,
        to_version_no: int,
        context_lines: int = 3,
    ) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        from_version = _version_number(from_version_no)
        to_version = _version_number(to_version_no)
        if isinstance(context_lines, bool) or not isinstance(context_lines, int) or context_lines < 0 or context_lines > 20:
            raise ValueError("context_lines must be an integer between 0 and 20")
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            left = self._version(connection, chunk_uuid, from_version)
            right = self._version(connection, chunk_uuid, to_version)
            if left is None or right is None:
                raise ChunkTextVersionError(
                    "VERSION_NOT_FOUND",
                    "semantic chunk text version was not found",
                    {
                        "chunk_id": str(chunk_uuid),
                        "from_version_no": from_version,
                        "to_version_no": to_version,
                    },
                )
        diff = list(
            difflib.unified_diff(
                str(left["text"]).splitlines(),
                str(right["text"]).splitlines(),
                fromfile=f"v{from_version}",
                tofile=f"v{to_version}",
                lineterm="",
                n=context_lines,
            )
        )
        return {
            "chunk_id": str(chunk_uuid),
            "from_version": _version_payload(left, include_text=False),
            "to_version": _version_payload(right, include_text=False),
            "diff": diff,
            "changed": bool(diff),
        }

    def _append_and_activate(
        self,
        *,
        chunk_id: str,
        text_value: str,
        comment: str | None,
        actor: str | None,
        operation: str,
        expected_current_version: int | None,
        operation_id: str | None,
    ) -> dict[str, Any]:
        chunk_uuid = UUID(chunk_id)
        if not isinstance(text_value, str):
            raise ValueError("text must be a string")
        with self._transaction() as connection:
            self._require_chunk(connection, chunk_uuid)
            self._assert_expected_current(connection, chunk_uuid, expected_current_version)
            current = self._current_version(connection, chunk_uuid)
            if current is not None and current.get("text") == text_value:
                return {
                    "chunk_id": str(chunk_uuid),
                    "outcome": "unchanged",
                    "version": _version_payload(current),
                }
            next_version = self._next_version_no(connection, chunk_uuid)
            row = self._insert_version(
                connection,
                chunk_uuid,
                int(next_version),
                text_value,
                comment=comment,
                actor=actor,
                operation=operation,
                operation_id=operation_id,
                previous_version_id=current["id"] if current is not None else None,
            )
            row = self._activate(connection, chunk_uuid, row, comment=comment, actor=actor, operation=operation)
        return {"chunk_id": str(chunk_uuid), "outcome": "appended", "version": _version_payload(row)}

    def _insert_version(
        self,
        connection: Connection,
        chunk_uuid: UUID,
        version_no: int,
        value: str,
        *,
        comment: str | None = None,
        actor: str | None = None,
        operation: str | None = None,
        operation_id: str | None = None,
        previous_version_id: UUID | None = None,
        restored_from_version_id: UUID | None = None,
    ) -> Mapping[str, Any]:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        source = self._source_metadata(connection, chunk_uuid)
        return connection.execute(
            text(
                f"INSERT INTO {VERSION_TABLE} "
                "(chunk_uuid, logical_chunk_id, previous_version_id, restored_from_version_id, "
                "version_no, text, text_sha256, char_count, source_version_id, source_start, "
                "source_end, order_index, status, is_current, valid_from, comment, actor, "
                "operation, operation_id, block_meta) "
                "VALUES (:chunk_uuid, :logical_chunk_id, :previous_version_id, :restored_from_version_id, "
                ":version_no, :text, :text_sha256, :char_count, :source_version_id, :source_start, "
                ":source_end, :order_index, 'active', FALSE, now(), :comment, :actor, "
                ":operation, :operation_id, CAST(:block_meta AS jsonb)) "
                f"RETURNING {_VERSION_ROW_COLUMNS}"
            ),
            {
                "chunk_uuid": chunk_uuid,
                "logical_chunk_id": source["logical_chunk_id"],
                "previous_version_id": previous_version_id,
                "restored_from_version_id": restored_from_version_id,
                "version_no": version_no,
                "text": value,
                "text_sha256": digest,
                "char_count": len(value),
                "source_version_id": source["source_version_id"],
                "source_start": source["source_start"],
                "source_end": source["source_end"],
                "order_index": source["order_index"],
                "comment": comment,
                "actor": actor,
                "operation": operation,
                "operation_id": _operation_uuid(operation_id),
                "block_meta": json.dumps(source["block_meta"], ensure_ascii=False),
            },
        ).mappings().one()

    def _activate(
        self,
        connection: Connection,
        chunk_uuid: UUID,
        row: Mapping[str, Any],
        *,
        comment: str | None = None,
        actor: str | None = None,
        operation: str | None = None,
    ) -> Mapping[str, Any]:
        version_id = row["id"]
        logical_chunk_id = row["logical_chunk_id"]
        value = str(row["text"])
        digest = str(row["text_sha256"])
        connection.execute(
            text(
                f"UPDATE {VERSION_TABLE} SET is_current = FALSE, status = 'retired', "
                "valid_to = COALESCE(valid_to, now()), updated_at = now() "
                "WHERE logical_chunk_id = :logical_chunk_id AND is_current IS TRUE AND id <> :version_id"
            ),
            {"logical_chunk_id": logical_chunk_id, "version_id": version_id},
        )
        connection.execute(
            text(
                f"UPDATE {VERSION_TABLE} SET is_current = TRUE, status = 'active', deleted_at = NULL, "
                "valid_to = NULL, comment = COALESCE(:comment, comment), actor = COALESCE(:actor, actor), "
                "operation = COALESCE(:operation, operation), updated_at = now() WHERE id = :version_id"
            ),
            {"version_id": version_id, "comment": comment, "actor": actor, "operation": operation},
        )
        connection.execute(
            text(
                f"INSERT INTO {CURRENT_TABLE} (chunk_uuid, version_id, comment, actor, operation) "
                "VALUES (:chunk_uuid, :version_id, :comment, :actor, :operation) "
                "ON CONFLICT (chunk_uuid) DO UPDATE SET version_id = EXCLUDED.version_id, "
                "comment = EXCLUDED.comment, actor = EXCLUDED.actor, operation = EXCLUDED.operation, updated_at = now()"
            ),
            {
                "chunk_uuid": chunk_uuid,
                "version_id": version_id,
                "comment": comment,
                "actor": actor,
                "operation": operation,
            },
        )
        connection.execute(
            text(
                "UPDATE semantic_chunk_texts SET text = :text, text_sha256 = :text_sha256, "
                "char_count = :char_count, block_meta = CAST(:block_meta AS jsonb), updated_at = now() "
                "WHERE chunk_uuid = :chunk_uuid"
            ),
            {
                "chunk_uuid": chunk_uuid,
                "text": value,
                "text_sha256": digest,
                "char_count": len(value),
                "block_meta": json.dumps(row.get("block_meta") or {}, ensure_ascii=False),
            },
        )
        connection.execute(
            text("UPDATE semantic_chunks SET text = '', char_count = :char_count WHERE id = :chunk_uuid"),
            {"chunk_uuid": chunk_uuid, "char_count": len(value)},
        )
        for table_name in _VERSION_DEPENDENT_ASSIGNMENTS:
            connection.execute(
                text(f"UPDATE {table_name} SET chunk_version_id = :version_id, updated_at = now() WHERE chunk_uuid = :chunk_uuid"),
                {"chunk_uuid": chunk_uuid, "version_id": version_id},
            )
        self._invalidate_derived_rows(connection, chunk_uuid)
        return self._version_by_id(connection, version_id)

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
            text(
                f"SELECT {_VERSION_ROW_COLUMNS} FROM {VERSION_TABLE} "
                "WHERE chunk_uuid = :chunk_uuid AND version_no = :version_no FOR UPDATE"
            ),
            {"chunk_uuid": chunk_uuid, "version_no": version_no},
        ).mappings().one_or_none()

    def _version_by_id(self, connection: Connection, version_id: UUID) -> Mapping[str, Any]:
        return connection.execute(
            text(f"SELECT {_VERSION_ROW_COLUMNS} FROM {VERSION_TABLE} WHERE id = :version_id"),
            {"version_id": version_id},
        ).mappings().one()

    def _current_version(self, connection: Connection, chunk_uuid: UUID) -> Mapping[str, Any] | None:
        return connection.execute(
            text(
                f"SELECT {_VERSION_ROW_COLUMNS_V} FROM {CURRENT_TABLE} AS c "
                f"JOIN {VERSION_TABLE} AS v ON v.id = c.version_id "
                "WHERE c.chunk_uuid = :chunk_uuid FOR UPDATE OF v"
            ),
            {"chunk_uuid": chunk_uuid},
        ).mappings().one_or_none()

    def _next_version_no(self, connection: Connection, chunk_uuid: UUID) -> int:
        return int(
            connection.execute(
                text(
                    f"SELECT COALESCE(MAX(version_no), 0) + 1 FROM {VERSION_TABLE} "
                    "WHERE chunk_uuid = :chunk_uuid"
                ),
                {"chunk_uuid": chunk_uuid},
            ).scalar_one()
        )

    def _assert_expected_current(
        self,
        connection: Connection,
        chunk_uuid: UUID,
        expected_current_version: int | None,
    ) -> None:
        expected = _optional_version_number(expected_current_version, "expected_current_version")
        if expected is None:
            return
        current = self._current_version(connection, chunk_uuid)
        actual = int(current["version_no"]) if current is not None else None
        if actual != expected:
            raise ChunkTextVersionError(
                CURRENT_VERSION_MISMATCH_CODE,
                "current semantic chunk text version does not match expected_current_version",
                {"chunk_id": str(chunk_uuid), "expected_current_version": expected, "actual_current_version": actual},
            )

    @staticmethod
    def _require_chunk(connection: Connection, chunk_uuid: UUID) -> None:
        if connection.execute(text("SELECT id FROM semantic_chunks WHERE id = :chunk_uuid FOR UPDATE"), {"chunk_uuid": chunk_uuid}).scalar_one_or_none() is None:
            raise LookupError(str(chunk_uuid))

    @staticmethod
    def _source_metadata(connection: Connection, chunk_uuid: UUID) -> dict[str, Any]:
        row = connection.execute(
            text(
                "SELECT id, source_start, source_end, order_index, block_meta "
                "FROM semantic_chunks WHERE id = :chunk_uuid"
            ),
            {"chunk_uuid": chunk_uuid},
        ).mappings().one()
        block_meta = dict(row.get("block_meta") or {})
        source_version_id = block_meta.get("source_version_id") or block_meta.get("sourceVersionId")
        return {
            "logical_chunk_id": row["id"],
            "source_version_id": str(source_version_id) if source_version_id else None,
            "source_start": row.get("source_start"),
            "source_end": row.get("source_end"),
            "order_index": row.get("order_index"),
            "block_meta": block_meta,
        }

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


def _optional_version_number(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _operation_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    return UUID(str(value))


def _version_payload(row: Mapping[str, Any], *, include_text: bool = False) -> dict[str, Any]:
    result = _json_row(row)
    text_value = result.get("text")
    if isinstance(text_value, str):
        result["preview"] = chunk_preview(text_value)
        if not include_text:
            result.pop("text", None)
    result["current"] = bool(result.pop("is_current", result.get("current", False)))
    result["checksum"] = result.get("text_sha256")
    return result


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
    "CURRENT_VERSION_MISMATCH_CODE",
    "CURRENT_VERSION_RETIRE_CODE",
    "ChunkTextVersionError",
    "ChunkTextVersionService",
    "LAST_VERSION_DELETE_CODE",
    "LAST_VERSION_DELETE_MESSAGE",
    "installed_chunk_text_version_service",
]
