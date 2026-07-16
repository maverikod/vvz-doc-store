"""Runtime CRUD and lifecycle service for addressable entity rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from doc_store_server.db.health import database_url_from_config


ENTITY_TABLES: dict[str, str] = {
    "block_type": "block_types",
    "block_types": "block_types",
    "category": "categories",
    "categories": "categories",
    "chunk_role": "chunk_roles",
    "chunk_roles": "chunk_roles",
    "chunk_status": "chunk_statuses",
    "chunk_statuses": "chunk_statuses",
    "chunk_type": "chunk_types",
    "chunk_types": "chunk_types",
    "document": "documents",
    "documents": "documents",
    "file": "files",
    "files": "files",
    "language": "languages",
    "languages": "languages",
    "chapter": "chapters",
    "chapters": "chapters",
    "paragraph": "paragraphs",
    "paragraphs": "paragraphs",
    "chunk": "semantic_chunks",
    "chunks": "semantic_chunks",
    "semantic_chunk": "semantic_chunks",
    "semantic_chunks": "semantic_chunks",
    "project": "projects",
    "projects": "projects",
}

DICTIONARY_TABLES = frozenset(
    {
        "chunk_types",
        "chunk_roles",
        "chunk_statuses",
        "block_types",
        "languages",
        "categories",
    }
)

DICTIONARY_REFERENCE_COLUMNS: dict[str, str] = {
    "chunk_types": "chunk_type_id",
    "chunk_roles": "role_id",
    "chunk_statuses": "status_id",
    "block_types": "block_type_id",
    "languages": "language_id",
    "categories": "category_id",
}

OWNER_TABLES = frozenset(ENTITY_TABLES.values())
UPDATED_AT_TABLES = frozenset(
    {
        "chunk_types",
        "chunk_roles",
        "chunk_statuses",
        "block_types",
        "languages",
        "categories",
        "projects",
        "files",
        "documents",
    }
)

DEFAULT_FIELDS: dict[str, tuple[str, ...]] = {
    "chunk_types": ("id", "owner_id", "descr", "is_deleted", "created_at", "updated_at"),
    "chunk_roles": ("id", "owner_id", "descr", "is_deleted", "created_at", "updated_at"),
    "chunk_statuses": ("id", "owner_id", "descr", "is_deleted", "created_at", "updated_at"),
    "block_types": ("id", "owner_id", "descr", "is_deleted", "created_at", "updated_at"),
    "languages": ("id", "owner_id", "descr", "is_deleted", "created_at", "updated_at"),
    "categories": ("id", "owner_id", "descr", "is_deleted", "created_at", "updated_at"),
    "projects": ("id", "owner_id", "name", "description", "is_deleted", "created_at", "updated_at"),
    "files": (
        "id",
        "owner_id",
        "path",
        "name",
        "body_sha256",
        "content_sha256",
        "needs_rechunk",
        "needs_revectorize",
        "is_deleted",
        "updated_at",
        "block_meta",
    ),
    "documents": (
        "id",
        "owner_id",
        "title",
        "source_name",
        "processing_status",
        "body_sha256",
        "content_sha256",
        "needs_revectorize",
        "is_deleted",
        "updated_at",
        "block_meta",
    ),
    "chapters": ("id", "owner_id", "document_id", "order_index", "heading", "is_deleted", "block_meta"),
    "paragraphs": ("id", "owner_id", "document_id", "chapter_id", "order_index", "text", "is_deleted", "block_meta"),
    "semantic_chunks": ("id", "owner_id", "document_id", "paragraph_id", "chapter_id", "order_index", "text", "is_deleted", "block_meta"),
}

LIST_TABLES = frozenset(DEFAULT_FIELDS)

ORDER_BY: dict[str, str] = {
    "chunk_types": "descr ASC, id ASC",
    "chunk_roles": "descr ASC, id ASC",
    "chunk_statuses": "descr ASC, id ASC",
    "block_types": "descr ASC, id ASC",
    "languages": "descr ASC, id ASC",
    "categories": "descr ASC, id ASC",
    "projects": "name ASC, id ASC",
    "files": "updated_at DESC NULLS LAST, id ASC",
    "documents": "updated_at DESC NULLS LAST, id ASC",
    "chapters": "order_index ASC NULLS LAST, id ASC",
    "paragraphs": "order_index ASC NULLS LAST, id ASC",
    "semantic_chunks": "order_index ASC NULLS LAST, id ASC",
}


class DeletionSafetyError(RuntimeError):
    """Raised when hard delete would leave references outside the delete set."""


@dataclass(frozen=True, slots=True)
class EntityRef:
    table: str
    id: UUID

    def as_key(self) -> tuple[str, UUID]:
        return self.table, self.id


class EntityLifecycleService:
    """DB-backed implementation of common CRUD/lifecycle operations."""

    def __init__(self, database_url: str | None) -> None:
        self._database_url = database_url

    def list_entities(
        self,
        *,
        entity_type: str,
        fields: Sequence[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0,
        show_deleted: bool = False,
    ) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table not in LIST_TABLES:
            raise ValueError(f"unsupported list entity_type: {entity_type}")
        selected = _validated_fields(table, fields)
        where, params = _filters_sql(table, filters or {})
        if not show_deleted:
            where.append("is_deleted IS FALSE")
        sql = (
            f"SELECT {', '.join(selected)} FROM {table} "
            f"{'WHERE ' + ' AND '.join(where) if where else ''} "
            f"ORDER BY {ORDER_BY[table]} LIMIT :limit OFFSET :offset"
        )
        count_sql = f"SELECT count(*) FROM {table} {'WHERE ' + ' AND '.join(where) if where else ''}"
        params.update({"limit": _limit(limit), "offset": _offset(offset)})
        with self._connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
            total = int(connection.execute(text(count_sql), params).scalar_one())
        return {
            "entity_type": table,
            "items": [_json_row(row) for row in rows],
            "limit": params["limit"],
            "offset": params["offset"],
            "total": total,
            "show_deleted": show_deleted,
        }

    def get_entity(
        self,
        *,
        entity_type: str,
        entity_id: str,
        fields: Sequence[str] | None = None,
        show_deleted: bool = False,
    ) -> dict[str, Any]:
        table = _entity_table(entity_type)
        selected = _validated_fields(table, fields)
        where = ["id = CAST(:entity_id AS uuid)"]
        if not show_deleted:
            where.append("is_deleted IS FALSE")
        with self._connect() as connection:
            row = connection.execute(
                text(f"SELECT {', '.join(selected)} FROM {table} WHERE {' AND '.join(where)}"),
                {"entity_id": str(UUID(entity_id))},
            ).mappings().one_or_none()
        if row is None:
            raise LookupError(entity_id)
        return {"entity_type": table, "id": entity_id, "value": _json_row(row)}

    def create_entity(self, *, entity_type: str, values: Mapping[str, Any]) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table not in _crud_tables():
            raise ValueError(f"create is unsupported for {table}")
        payload = _validated_values(table, values, require_id=True)
        with self._transaction() as connection:
            _validate_owner(connection, payload.get("owner_id"))
            columns = tuple(payload)
            value_exprs = tuple(_insert_value_expr(column) for column in columns)
            row = connection.execute(
                text(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join(value_exprs)}) "
                    f"RETURNING {', '.join(DEFAULT_FIELDS[table])}"
                ),
                payload,
            ).mappings().one()
        return {"entity_type": table, "outcome": "created", "value": _json_row(row)}

    def update_entity(
        self,
        *,
        entity_type: str,
        entity_id: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table not in _crud_tables():
            raise ValueError(f"update is unsupported for {table}")
        payload = _validated_values(table, values, require_id=False)
        if not payload:
            raise ValueError("update values must not be empty")
        entity_uuid = UUID(entity_id)
        with self._transaction() as connection:
            _validate_owner(connection, payload.get("owner_id"))
            assignments = ", ".join(_assignment_expr(column) for column in payload)
            row = connection.execute(
                text(
                    f"UPDATE {table} SET {assignments}, updated_at = now() "
                    "WHERE id = :entity_id "
                    f"RETURNING {', '.join(DEFAULT_FIELDS[table])}"
                ),
                {**payload, "entity_id": entity_uuid},
            ).mappings().one_or_none()
        if row is None:
            raise LookupError(entity_id)
        return {"entity_type": table, "outcome": "updated", "value": _json_row(row)}

    def rebind_owner(
        self,
        *,
        entity_type: str,
        ids: Sequence[str],
        owner_id: str | None,
    ) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table not in OWNER_TABLES:
            raise ValueError(f"owner rebind is unsupported for {table}")
        parsed_ids = [UUID(item) for item in ids]
        if not parsed_ids:
            raise ValueError("ids must not be empty")
        parsed_owner = UUID(owner_id) if owner_id is not None else None
        selected = DEFAULT_FIELDS[table]
        updated_at = ", updated_at = now()" if table in UPDATED_AT_TABLES else ""
        with self._transaction() as connection:
            _validate_owner(connection, parsed_owner)
            rows = connection.execute(
                text(
                    f"UPDATE {table} SET owner_id = :owner_id{updated_at} "
                    "WHERE id = ANY(CAST(:ids AS uuid[])) "
                    f"RETURNING {', '.join(selected)}"
                ),
                {"owner_id": parsed_owner, "ids": [str(item) for item in parsed_ids]},
            ).mappings().all()
        return {
            "entity_type": table,
            "owner_id": str(parsed_owner) if parsed_owner is not None else None,
            "requested": len(parsed_ids),
            "updated": len(rows),
            "items": [_json_row(row) for row in rows],
        }

    def soft_delete(self, *, entity_type: str, ids: Sequence[str]) -> dict[str, Any]:
        return self._mark_deleted(entity_type=entity_type, ids=ids, deleted=True)

    def undelete(self, *, entity_type: str, ids: Sequence[str]) -> dict[str, Any]:
        return self._mark_deleted(entity_type=entity_type, ids=ids, deleted=False)

    def hard_delete(self, *, entity_type: str, ids: Sequence[str]) -> dict[str, Any]:
        table = _entity_table(entity_type)
        roots = self._roots_for(table, ids)
        if not roots:
            return {"outcome": "deleted", "deleted": {}, "blocked": []}
        with self._transaction() as connection:
            closure = self._closure(connection, roots)
            blockers = self.references_to(connection, closure)
            if blockers:
                raise DeletionSafetyError("unsafe hard delete: external references exist")
            deleted = self._delete_closure(connection, closure)
        return {"outcome": "deleted", "deleted": deleted, "blocked": []}

    def references_for(self, *, entity_type: str, entity_id: str) -> dict[str, Any]:
        table = _entity_table(entity_type)
        if table == "projects":
            roots = self._roots_for(table, [entity_id])
            refs = {root.as_key() for root in roots}
        else:
            refs = {EntityRef(table, UUID(entity_id)).as_key()}
        with self._connect() as connection:
            references = self.references_to(connection, refs)
        return {"entity_type": table, "id": entity_id, "references": references}

    def references_to(
        self,
        connection: Connection,
        refs: set[tuple[str, UUID]],
    ) -> list[dict[str, Any]]:
        """Return references from rows outside refs to rows inside refs."""

        result: list[dict[str, Any]] = []
        refs_by_table = _group_refs(refs)
        for project_id in refs_by_table.get("projects", ()):
            result.extend(_external_owner_rows(connection, project_id, refs))
            project = connection.execute(
                text("SELECT name FROM projects WHERE id = :project_id"),
                {"project_id": project_id},
            ).mappings().one_or_none()
            if project is not None:
                result.extend(
                    _external_project_rows(
                        connection,
                        project_id,
                        str(project["name"]),
                        refs,
                    )
                )
        for document_id in refs_by_table.get("documents", ()):
            result.extend(_external_owner_rows(connection, document_id, refs))
            result.extend(
                _external_fk_rows(
                    connection,
                    "chapters",
                    "document_id",
                    document_id,
                    refs,
                )
            )
            result.extend(_external_fk_rows(connection, "paragraphs", "document_id", document_id, refs))
            result.extend(_external_fk_rows(connection, "semantic_chunks", "document_id", document_id, refs))
        for file_id in refs_by_table.get("files", ()):
            result.extend(_external_owner_rows(connection, file_id, refs))
        for chapter_id in refs_by_table.get("chapters", ()):
            result.extend(_external_fk_rows(connection, "paragraphs", "chapter_id", chapter_id, refs))
            result.extend(_external_fk_rows(connection, "semantic_chunks", "chapter_id", chapter_id, refs))
        for paragraph_id in refs_by_table.get("paragraphs", ()):
            result.extend(_external_fk_rows(connection, "semantic_chunks", "paragraph_id", paragraph_id, refs))
        for chunk_id in refs_by_table.get("semantic_chunks", ()):
            rows = connection.execute(
                text(
                    "SELECT source_chunk_uuid::text AS id, relation_type "
                    "FROM semantic_chunk_links "
                    "WHERE target_chunk_uuid = :target"
                ),
                {"target": chunk_id},
            ).mappings().all()
            for row in rows:
                source = UUID(str(row["id"]))
                if ("semantic_chunks", source) not in refs:
                    result.append(
                        {
                            "from_table": "semantic_chunk_links",
                            "from_id": str(source),
                            "from_column": "target_chunk_uuid",
                            "to_table": "semantic_chunks",
                            "to_id": str(chunk_id),
                        }
                    )
        for table, column in DICTIONARY_REFERENCE_COLUMNS.items():
            for dictionary_id in refs_by_table.get(table, ()):
                result.extend(
                    _external_fk_rows(
                        connection,
                        "semantic_chunks",
                        column,
                        dictionary_id,
                        refs,
                        to_table=table,
                    )
                )
        return result

    def _mark_deleted(self, *, entity_type: str, ids: Sequence[str], deleted: bool) -> dict[str, Any]:
        table = _entity_table(entity_type)
        roots = self._roots_for(table, ids)
        if not roots:
            return {"outcome": "updated", "updated": {}, "is_deleted": deleted}
        with self._transaction() as connection:
            closure = self._closure(connection, roots)
            updated = self._set_deleted(connection, closure, deleted)
        return {"outcome": "updated", "updated": updated, "is_deleted": deleted}

    def _closure(self, connection: Connection, roots: Sequence[EntityRef]) -> set[tuple[str, UUID]]:
        refs = {root.as_key() for root in roots}
        changed = True
        while changed:
            changed = False
            grouped = _group_refs(refs)
            for project_id in tuple(grouped.get("projects", ())):
                changed |= _add_owner_children(connection, refs, project_id)
                changed |= self._add_project_documents(connection, refs, project_id)
            for file_id in tuple(grouped.get("files", ())):
                changed |= _add_owner_children(connection, refs, file_id)
            for document_id in tuple(grouped.get("documents", ())):
                changed |= _add_owner_children(connection, refs, document_id)
                changed |= _add_children(connection, refs, "chapters", "document_id", document_id)
                changed |= _add_children(connection, refs, "paragraphs", "document_id", document_id)
                changed |= _add_children(connection, refs, "semantic_chunks", "document_id", document_id)
            for chapter_id in tuple(grouped.get("chapters", ())):
                changed |= _add_children(connection, refs, "paragraphs", "chapter_id", chapter_id)
                changed |= _add_children(connection, refs, "semantic_chunks", "chapter_id", chapter_id)
            for paragraph_id in tuple(grouped.get("paragraphs", ())):
                changed |= _add_children(connection, refs, "semantic_chunks", "paragraph_id", paragraph_id)
        return refs

    def _roots_for(self, table: str, ids: Sequence[str]) -> list[EntityRef]:
        if table != "projects":
            return [EntityRef(table, UUID(item)) for item in ids]
        with self._connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id FROM projects "
                    "WHERE id = ANY(CAST(:project_ids AS uuid[]))"
                ),
                {"project_ids": [str(UUID(item)) for item in ids]},
            ).scalars().all()
        return [EntityRef("projects", row) for row in rows]

    def _add_project_documents(
        self,
        connection: Connection,
        refs: set[tuple[str, UUID]],
        project_id: UUID,
    ) -> bool:
        row = connection.execute(
            text("SELECT name FROM projects WHERE id = :project_id"),
            {"project_id": project_id},
        ).mappings().one_or_none()
        if row is None:
            return False
        rows = connection.execute(
            text(
                "SELECT id FROM documents "
                "WHERE block_meta ->> 'project_id' = :project_id "
                "OR block_meta ->> 'project' = :project_name"
            ),
            {"project_id": str(project_id), "project_name": str(row["name"])},
        ).scalars().all()
        changed = False
        for document_id in rows:
            key = ("documents", document_id)
            if key not in refs:
                refs.add(key)
                changed = True
        return changed

    def _set_deleted(
        self,
        connection: Connection,
        refs: set[tuple[str, UUID]],
        deleted: bool,
    ) -> dict[str, int]:
        updated: dict[str, int] = {}
        for table, ids in _group_refs(refs).items():
            timestamp = "now()" if deleted else "NULL"
            count = connection.execute(
                text(
                    f"UPDATE {table} SET is_deleted = :deleted, deleted_at = {timestamp} "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"deleted": deleted, "ids": [str(item) for item in ids]},
            ).rowcount
            updated[table] = int(count or 0)
        return updated

    def _delete_closure(self, connection: Connection, refs: set[tuple[str, UUID]]) -> dict[str, int]:
        deleted: dict[str, int] = {}
        grouped = _group_refs(refs)
        chunk_ids = grouped.get("semantic_chunks")
        if chunk_ids:
            link_count = connection.execute(
                text(
                    "DELETE FROM semantic_chunk_links "
                    "WHERE source_chunk_uuid = ANY(CAST(:ids AS uuid[])) "
                    "OR target_chunk_uuid = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(item) for item in chunk_ids]},
            ).rowcount
            if link_count:
                deleted["semantic_chunk_links"] = int(link_count)
        for table in (
            "semantic_chunks",
            "paragraphs",
            "chapters",
            "documents",
            "files",
            "projects",
            "categories",
            "languages",
            "block_types",
            "chunk_statuses",
            "chunk_roles",
            "chunk_types",
        ):
            ids = grouped.get(table)
            if not ids:
                continue
            count = connection.execute(
                text(f"DELETE FROM {table} WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": [str(item) for item in ids]},
            ).rowcount
            deleted[table] = int(count or 0)
        return deleted

    def _engine(self) -> Any:
        if not self._database_url:
            raise RuntimeError("database URL is not configured")
        return create_engine(self._database_url, pool_pre_ping=True)

    @contextmanager
    def _connect(self) -> Iterator[Connection]:
        engine = self._engine()
        try:
            with engine.connect() as connection:
                yield connection
        finally:
            engine.dispose()

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        engine = self._engine()
        try:
            with engine.begin() as connection:
                yield connection
        finally:
            engine.dispose()


def installed_entity_lifecycle_service(config: Mapping[str, Any] | None = None) -> EntityLifecycleService | None:
    database_url = database_url_from_config(config or {})
    if not database_url:
        database_url = os.getenv("DOC_STORE_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return None
    return EntityLifecycleService(database_url)


def _entity_table(entity_type: str) -> str:
    key = entity_type.strip().lower()
    try:
        return ENTITY_TABLES[key]
    except KeyError as exc:
        raise ValueError(f"unsupported entity_type: {entity_type}") from exc


def _validated_fields(table: str, fields: Sequence[str] | None) -> tuple[str, ...]:
    allowed = _allowed_fields(table)
    selected = tuple(fields or DEFAULT_FIELDS[table])
    unknown = sorted(set(selected) - allowed)
    if unknown:
        raise ValueError(f"unsupported fields for {table}: {', '.join(unknown)}")
    return selected


def _allowed_fields(table: str) -> set[str]:
    return set(DEFAULT_FIELDS[table]) | {
        "created_at",
        "updated_at",
        "deleted_at",
        "owner_id",
        "path",
        "media_type",
        "byte_length",
        "content_sha256",
        "body_sha256",
        "checksum_algorithm",
        "needs_rechunk",
        "needs_revectorize",
        "source_version",
        "source_path",
        "source_name",
        "source_hash",
        "language",
        "quality_score",
        "search_weight",
        "char_count",
        "chunk_type",
        "score",
        "source_start",
        "source_end",
        "level",
        "name",
        "description",
        "descr",
        "chunk_type_id",
        "role_id",
        "status_id",
        "block_type_id",
        "language_id",
        "category_id",
    }


def _crud_tables() -> set[str]:
    return {"projects", "files", "documents"} | set(DICTIONARY_TABLES)


def _writable_fields(table: str) -> set[str]:
    if table in DICTIONARY_TABLES:
        return {"id", "owner_id", "descr", "is_deleted", "deleted_at"}
    fields: dict[str, set[str]] = {
        "projects": {"id", "owner_id", "name", "description", "is_deleted", "deleted_at"},
        "files": {
            "id",
            "owner_id",
            "path",
            "name",
            "media_type",
            "byte_length",
            "char_count",
            "checksum_algorithm",
            "content_sha256",
            "body_sha256",
            "needs_rechunk",
            "needs_revectorize",
            "is_deleted",
            "deleted_at",
            "block_meta",
        },
        "documents": {
            "id",
            "owner_id",
            "source_upload_id",
            "source_version",
            "source_path",
            "source_name",
            "source_hash",
            "checksum_algorithm",
            "content_sha256",
            "body_sha256",
            "title",
            "processing_status",
            "processing_attempt",
            "needs_revectorize",
            "processing_trace_id",
            "processing_started_at",
            "processing_completed_at",
            "is_deleted",
            "deleted_at",
            "block_meta",
        },
    }
    return fields[table]


def _required_create_fields(table: str) -> set[str]:
    if table in DICTIONARY_TABLES:
        return {"id", "descr"}
    return {
        "projects": {"id", "name", "description"},
        "files": {"id", "path", "name", "body_sha256"},
        "documents": {
            "id",
            "source_upload_id",
            "source_version",
            "title",
            "block_meta",
        },
    }[table]


def _validated_values(table: str, values: Mapping[str, Any], *, require_id: bool) -> dict[str, Any]:
    allowed = _writable_fields(table)
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"unsupported fields for {table}: {', '.join(unknown)}")
    payload = dict(values)
    if table == "documents":
        payload.setdefault("processing_status", "draft")
        payload.setdefault("processing_attempt", 0)
        payload.setdefault("block_meta", {})
        payload.setdefault("checksum_algorithm", "sha256")
        if "body_sha256" not in payload and payload.get("source_hash") is not None:
            payload["body_sha256"] = payload["source_hash"]
        if "content_sha256" not in payload and payload.get("source_hash") is not None:
            payload["content_sha256"] = payload["source_hash"]
        payload.setdefault("needs_revectorize", False)
    if table == "files":
        payload.setdefault("checksum_algorithm", "sha256")
        payload.setdefault("needs_rechunk", False)
        payload.setdefault("needs_revectorize", False)
    if require_id:
        missing = sorted(_required_create_fields(table) - set(payload))
        if missing:
            raise ValueError(f"missing required fields for {table}: {', '.join(missing)}")
    for key in (
        "id",
        "owner_id",
        "source_upload_id",
        "processing_trace_id",
        "chunk_type_id",
        "role_id",
        "status_id",
        "block_type_id",
        "language_id",
        "category_id",
    ):
        if key in payload and payload[key] is not None:
            payload[key] = UUID(str(payload[key]))
    if "descr" in payload and len(str(payload["descr"])) > 100:
        raise ValueError("descr must be at most 100 characters")
    if table == "files" and payload.get("checksum_algorithm") not in {None, "sha256"}:
        raise ValueError("files.checksum_algorithm must be sha256")
    if "block_meta" in payload and not isinstance(payload["block_meta"], Mapping):
        raise ValueError("block_meta must be an object")
    if "block_meta" in payload:
        payload["block_meta"] = json.dumps(payload["block_meta"], ensure_ascii=False)
    return payload


def _insert_value_expr(column: str) -> str:
    if column == "block_meta":
        return "CAST(:block_meta AS jsonb)"
    return f":{column}"


def _assignment_expr(column: str) -> str:
    if column == "block_meta":
        return "block_meta = CAST(:block_meta AS jsonb)"
    return f"{column} = :{column}"


def _filters_sql(table: str, filters: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    allowed = _allowed_fields(table) | {
        "id",
        "document_id",
        "chapter_id",
        "paragraph_id",
        "is_deleted",
    }
    where: list[str] = []
    params: dict[str, Any] = {}
    for index, (key, value) in enumerate(filters.items()):
        param = f"filter_{index}"
        if key.startswith("block_meta."):
            meta_key = key.split(".", 1)[1]
            where.append(f"block_meta ->> :{param}_key = :{param}")
            params[f"{param}_key"] = meta_key
            params[param] = str(value)
        elif key in allowed:
            where.append(f"{key} = :{param}")
            params[param] = value
        else:
            raise ValueError(f"unsupported filter for {table}: {key}")
    return where, params


def _limit(value: int) -> int:
    if value < 1 or value > 500:
        raise ValueError("limit must be between 1 and 500")
    return value


def _offset(value: int) -> int:
    if value < 0:
        raise ValueError("offset must be non-negative")
    return value


def _group_refs(refs: Iterable[tuple[str, UUID]]) -> dict[str, list[UUID]]:
    grouped: dict[str, list[UUID]] = {}
    for table, entity_id in refs:
        grouped.setdefault(table, []).append(entity_id)
    return grouped


def _add_children(
    connection: Connection,
    refs: set[tuple[str, UUID]],
    table: str,
    column: str,
    parent_id: UUID,
) -> bool:
    rows = connection.execute(
        text(f"SELECT id FROM {table} WHERE {column} = :parent_id"),
        {"parent_id": parent_id},
    ).scalars().all()
    changed = False
    for row in rows:
        key = (table, row)
        if key not in refs:
            refs.add(key)
            changed = True
    return changed


def _add_owner_children(
    connection: Connection,
    refs: set[tuple[str, UUID]],
    owner_id: UUID,
) -> bool:
    changed = False
    for table in OWNER_TABLES:
        rows = connection.execute(
            text(f"SELECT id FROM {table} WHERE owner_id = :owner_id"),
            {"owner_id": owner_id},
        ).scalars().all()
        for row in rows:
            key = (table, row)
            if key not in refs:
                refs.add(key)
                changed = True
    return changed


def _validate_owner(connection: Connection, owner_id: Any) -> None:
    if owner_id is None:
        return
    row = connection.execute(
        text("SELECT 1 FROM entity_uuid_registry WHERE entity_id = :owner_id"),
        {"owner_id": owner_id},
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"owner_id does not reference a registered entity: {owner_id}")


def _external_fk_rows(
    connection: Connection,
    table: str,
    column: str,
    target_id: UUID,
    deleting: set[tuple[str, UUID]],
    *,
    to_table: str | None = None,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(f"SELECT id FROM {table} WHERE {column} = :target_id"),
        {"target_id": target_id},
    ).scalars().all()
    result: list[dict[str, Any]] = []
    for row in rows:
        if (table, row) in deleting:
            continue
        item = {
            "from_table": table,
            "from_id": str(row),
            "from_column": column,
            "to_id": str(target_id),
        }
        if to_table is not None:
            item["to_table"] = to_table
        result.append(item)
    return result


def _external_owner_rows(
    connection: Connection,
    target_id: UUID,
    deleting: set[tuple[str, UUID]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for table in OWNER_TABLES:
        rows = connection.execute(
            text(f"SELECT id FROM {table} WHERE owner_id = :target_id"),
            {"target_id": target_id},
        ).scalars().all()
        for row in rows:
            if (table, row) not in deleting:
                result.append(
                    {
                        "from_table": table,
                        "from_id": str(row),
                        "from_column": "owner_id",
                        "to_id": str(target_id),
                    }
                )
    return result


def _external_project_rows(
    connection: Connection,
    project_id: UUID,
    project_name: str,
    deleting: set[tuple[str, UUID]],
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT id FROM documents "
            "WHERE block_meta ->> 'project_id' = :project_id "
            "OR block_meta ->> 'project' = :project_name"
        ),
        {"project_id": str(project_id), "project_name": project_name},
    ).scalars().all()
    return [
        {
            "from_table": "documents",
            "from_id": str(row),
            "from_column": "block_meta.project_id",
            "to_table": "projects",
            "to_id": str(project_id),
        }
        for row in rows
        if ("documents", row) not in deleting
    ]


def _json_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif isinstance(value, tuple):
            result[key] = list(value)
        else:
            result[key] = value
    return result


__all__ = [
    "DeletionSafetyError",
    "ENTITY_TABLES",
    "EntityLifecycleService",
    "installed_entity_lifecycle_service",
]
