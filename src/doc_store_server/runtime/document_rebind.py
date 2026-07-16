"""Runtime document metadata rebinding service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, text


class DocumentRebindError(RuntimeError):
    """Raised when a document cannot be rebound."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DocumentRebindService:
    """Update document and child block metadata without touching text/order/vectors."""

    database_url: str | None

    def rebind_document(
        self,
        *,
        document_id: str,
        project: str | None = None,
        project_id: str | None = None,
        project_description: str | None = None,
        document_properties: Mapping[str, Any] | None = None,
        chunk_properties: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.database_url:
            raise DocumentRebindError(
                "DATABASE_NOT_CONFIGURED",
                "database URL is not configured",
            )

        document_updates = dict(document_properties or {})
        child_updates = dict(chunk_properties or {})
        applied_project_id = project_id
        applied_project_description = project_description

        engine = create_engine(self.database_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                document_row = connection.execute(
                    text(
                        "SELECT block_meta FROM documents "
                        "WHERE id = CAST(:document_id AS uuid) AND deleted_at IS NULL"
                    ),
                    {"document_id": document_id},
                ).mappings().first()
                if document_row is None:
                    raise DocumentRebindError(
                        "DOCUMENT_NOT_FOUND",
                        "document was not found",
                        {"document_id": document_id},
                    )
                if project is not None:
                    applied_project_id = _upsert_project(
                        connection,
                        project_id=project_id,
                        name=project,
                        description=project_description,
                    )
                    project_updates = {
                        "project": project,
                        "project_id": applied_project_id,
                        "project_description": project_description,
                    }
                    document_updates.update(project_updates)
                    child_updates = {**project_updates, **child_updates}

                current_document_meta = _meta(document_row["block_meta"])
                updated_document_meta = {**current_document_meta, **document_updates}
                connection.execute(
                    text(
                        "UPDATE documents "
                        "SET block_meta = CAST(:block_meta AS jsonb), updated_at = now() "
                        "WHERE id = CAST(:document_id AS uuid)"
                    ),
                    {
                        "document_id": document_id,
                        "block_meta": json.dumps(updated_document_meta),
                    },
                )

                counts = {
                    "documents": 1,
                    "chapters": _merge_child_meta(connection, "chapters", document_id, child_updates),
                    "paragraphs": _merge_child_meta(connection, "paragraphs", document_id, child_updates),
                    "semantic_chunks": _merge_child_meta(
                        connection, "semantic_chunks", document_id, child_updates
                    ),
                }
        finally:
            engine.dispose()

        return {
            "outcome": "rebound",
            "document_id": document_id,
            "project": project,
            "project_id": applied_project_id,
            "project_description": applied_project_description,
            "document_properties": document_updates,
            "chunk_properties": child_updates,
            "updated": counts,
        }


def _merge_child_meta(
    connection: Any,
    table_name: str,
    document_id: str,
    updates: Mapping[str, Any],
) -> int:
    rows = connection.execute(
        text(
            f"SELECT id::text AS id, block_meta FROM {table_name} "
            "WHERE document_id = CAST(:document_id AS uuid) AND deleted_at IS NULL"
        ),
        {"document_id": document_id},
    ).mappings().all()
    for row in rows:
        merged = {**_meta(row["block_meta"]), **updates}
        connection.execute(
            text(
                f"UPDATE {table_name} "
                "SET block_meta = CAST(:block_meta AS jsonb) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": row["id"], "block_meta": json.dumps(merged)},
        )
    return len(rows)


def _upsert_project(
    connection: Any,
    *,
    project_id: str | None,
    name: str,
    description: str | None,
) -> str:
    if not project_id or not description:
        raise DocumentRebindError(
            "PROJECT_ID_DESCRIPTION_REQUIRED",
            "project_id and project_description are required when project is supplied",
            {"project": name},
        )

    existing = connection.execute(
        text(
            "UPDATE projects "
            "SET description = :description, is_deleted = FALSE, deleted_at = NULL, updated_at = now() "
            "WHERE name = :name "
            "RETURNING id::text AS id"
        ),
        {"name": name, "description": description},
    ).mappings().one_or_none()
    if existing is not None:
        return str(existing["id"])

    inserted = connection.execute(
        text(
            "INSERT INTO projects (id, name, description) "
            "VALUES (CAST(:project_id AS uuid), :name, :description) "
            "ON CONFLICT (id) DO UPDATE "
            "SET name = EXCLUDED.name, description = EXCLUDED.description, "
            "is_deleted = FALSE, deleted_at = NULL, updated_at = now() "
            "RETURNING id::text AS id"
        ),
        {"project_id": project_id, "name": name, "description": description},
    ).mappings().one()
    return str(inserted["id"])


def _meta(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


__all__ = ["DocumentRebindError", "DocumentRebindService"]
